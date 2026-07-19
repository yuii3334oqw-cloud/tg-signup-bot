# -*- coding: utf-8 -*-
"""
群活动报名统计机器人
====================
用途:在 Telegram 群里发起团建/爬山等活动报名,群成员点按钮报名,
机器人实时统计人数和名单。

指令:
  /new 活动名称 | 补充说明(可选)   发起一个新活动报名
  /stats                           查看本群所有进行中活动的报名情况
  /close                           (回复某条报名消息)截止该活动报名
  /help                            使用说明

环境变量:
  BOT_TOKEN     必填,@BotFather 给的 Token
  WEBHOOK_URL   选填,填了就用 webhook 模式(如 https://xxx.onrender.com),
                不填则用轮询(polling)模式
  PORT          webhook 模式监听端口(Render 等平台会自动注入)
  DB_PATH       选填,SQLite 数据库路径,默认 ./signup.db
"""

import html
import logging
import os
import sqlite3
import threading
from datetime import datetime

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("signup-bot")

DB_PATH = os.environ.get("DB_PATH", "signup.db")

# 报名状态
GOING, NOT_GOING, MAYBE = "going", "not_going", "maybe"
STATUS_LABEL = {GOING: "✅ 参加", NOT_GOING: "❌ 不参加", MAYBE: "🤔 待定"}

_db_lock = threading.Lock()


# ---------------------------------------------------------------- 数据库

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                title TEXT NOT NULL,
                note TEXT DEFAULT '',
                creator_id INTEGER,
                creator_name TEXT,
                closed INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS signup (
                activity_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                status TEXT NOT NULL,
                extra INTEGER DEFAULT 0,
                flakes INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                PRIMARY KEY (activity_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS vote (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                title TEXT NOT NULL,
                closed INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS vote_option (
                vote_id INTEGER NOT NULL,
                idx INTEGER NOT NULL,
                text TEXT NOT NULL,
                PRIMARY KEY (vote_id, idx)
            );
            CREATE TABLE IF NOT EXISTS vote_ballot (
                vote_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                option_idx INTEGER NOT NULL,
                PRIMARY KEY (vote_id, user_id)
            );
            """
        )
        try:  # 旧库补列
            conn.execute("ALTER TABLE signup ADD COLUMN flakes INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass


# 活动模板字段 → 图标
FIELD_EMOJI = {
    "活动时间": "🕐",
    "活动地点": "📍",
    "集合地点": "🚩",
    "活动内容": "🎯",
    "活动费用": "💰",
    "参与人数": "👥",
    "注意事项": "⚠️",
    "报名方式": "📝",
}


# ---------------------------------------------------------------- 渲染

def render_activity(conn, activity) -> str:
    rows = conn.execute(
        "SELECT * FROM signup WHERE activity_id=? ORDER BY updated_at",
        (activity["id"],),
    ).fetchall()
    going = [r for r in rows if r["status"] == GOING]
    not_going = [r for r in rows if r["status"] == NOT_GOING]
    maybe = [r for r in rows if r["status"] == MAYBE]

    total = sum(1 + r["extra"] for r in going)

    lines = [f"📋 <b>{html.escape(activity['title'])}</b>"]
    if activity["note"]:
        lines.append("")
        for note_line in activity["note"].split("\n"):
            note_line = note_line.strip()
            if not note_line:
                continue
            decorated = html.escape(note_line)
            for key, emoji in FIELD_EMOJI.items():
                if note_line.startswith(key):
                    rest = note_line[len(key):].lstrip(":: ")
                    decorated = f"{emoji} <b>{key}</b>:{html.escape(rest)}"
                    break
            lines.append(decorated)
    lines.append("")

    def block(title, people, show_extra=False):
        lines.append(title)
        if not people:
            lines.append("(暂无)")
        for i, r in enumerate(people, 1):
            name = html.escape(r["user_name"])
            extra = f" +{r['extra']}" if (show_extra and r["extra"]) else ""
            lines.append(f"{i}. {name}{extra}")
        lines.append("")

    block(f"✅ <b>参加({total} 人)</b>", going, show_extra=True)
    if maybe:
        block(f"🤔 <b>待定({len(maybe)} 人)</b>", maybe)
    if not_going:
        block(f"❌ <b>不参加({len(not_going)} 人)</b>", not_going)

    if activity["closed"]:
        lines.append("🔒 <b>报名已截止</b>")
    else:
        lines.append("👇 点下面按钮报名,可随时改")
    return "\n".join(lines).strip()


def keyboard(activity_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ 参加", callback_data=f"s:{activity_id}:{GOING}"),
                InlineKeyboardButton("❌ 不参加", callback_data=f"s:{activity_id}:{NOT_GOING}"),
                InlineKeyboardButton("🤔 待定", callback_data=f"s:{activity_id}:{MAYBE}"),
            ],
            [
                InlineKeyboardButton("➕ 带1人", callback_data=f"e:{activity_id}:+"),
                InlineKeyboardButton("➖ 减1人", callback_data=f"e:{activity_id}:-"),
            ],
        ]
    )


async def refresh_message(context, chat_id, message_id, activity_id):
    with _db_lock, db() as conn:
        activity = conn.execute(
            "SELECT * FROM activity WHERE id=?", (activity_id,)
        ).fetchone()
        if not activity:
            return
        text = render_activity(conn, activity)
        closed = activity["closed"]
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=None if closed else keyboard(activity_id),
        )
    except Exception as e:  # 内容没变化时 Telegram 会报错,忽略即可
        if "not modified" not in str(e).lower():
            log.warning("edit failed: %s", e)


# ---------------------------------------------------------------- 投票

MEDALS = ["🥇", "🥈", "🥉"]


def render_vote(conn, vote) -> str:
    options = conn.execute(
        "SELECT * FROM vote_option WHERE vote_id=? ORDER BY idx", (vote["id"],)
    ).fetchall()
    ballots = conn.execute(
        "SELECT * FROM vote_ballot WHERE vote_id=?", (vote["id"],)
    ).fetchall()
    by_opt = {}
    for b in ballots:
        by_opt.setdefault(b["option_idx"], []).append(b["user_name"])

    max_votes = max((len(v) for v in by_opt.values()), default=0)
    lines = [f"🗳 <b>{html.escape(vote['title'])}</b>", ""]
    for o in options:
        voters = by_opt.get(o["idx"], [])
        crown = " 👑" if voters and len(voters) == max_votes else ""
        bar = "▓" * len(voters) if voters else "░"
        lines.append(f"<b>{html.escape(o['text'])}</b> — {len(voters)} 票{crown}")
        lines.append(bar)
        if voters:
            lines.append("(" + "、".join(html.escape(v) for v in voters) + ")")
        lines.append("")
    lines.append(f"共 {len(ballots)} 人投票")
    if vote["closed"]:
        lines.append("🔒 <b>投票已截止</b>")
    else:
        lines.append("👇 点按钮投票,再点一次取消,可随时改")
    return "\n".join(lines).strip()


def vote_keyboard(conn, vote_id: int) -> InlineKeyboardMarkup:
    options = conn.execute(
        "SELECT * FROM vote_option WHERE vote_id=? ORDER BY idx", (vote_id,)
    ).fetchall()
    rows, row = [], []
    for o in options:
        row.append(
            InlineKeyboardButton(
                o["text"], callback_data=f"v:{vote_id}:{o['idx']}"
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def refresh_vote_message(context, vote_id):
    with _db_lock, db() as conn:
        vote = conn.execute("SELECT * FROM vote WHERE id=?", (vote_id,)).fetchone()
        if not vote:
            return
        text = render_vote(conn, vote)
        markup = None if vote["closed"] else vote_keyboard(conn, vote_id)
    try:
        await context.bot.edit_message_text(
            chat_id=vote["chat_id"],
            message_id=vote["message_id"],
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.warning("edit vote failed: %s", e)


async def cmd_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    raw = " ".join(context.args)
    if "|" in raw:  # 带竖线写法:选项可以含空格
        parts = [p.strip() for p in raw.split("|") if p.strip()]
    else:  # 空格写法:第一个词是主题,后面都是选项
        parts = raw.split()
    if len(parts) < 3:
        await msg.reply_text(
            "格式:/vote 主题 选项1 选项2 ...\n"
            "例:/vote 这周去哪 爬山 剧本杀 骑行\n"
            "(选项本身带空格就用 | 隔开)"
        )
        return
    title, options = parts[0], parts[1:][:10]  # 最多10个选项

    with _db_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO vote(chat_id, title) VALUES (?,?)", (msg.chat_id, title)
        )
        vote_id = cur.lastrowid
        for i, text in enumerate(options):
            conn.execute(
                "INSERT INTO vote_option(vote_id, idx, text) VALUES (?,?,?)",
                (vote_id, i, text[:30]),
            )
        vote = conn.execute("SELECT * FROM vote WHERE id=?", (vote_id,)).fetchone()
        body = render_vote(conn, vote)
        markup = vote_keyboard(conn, vote_id)

    sent = await msg.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)
    with _db_lock, db() as conn:
        conn.execute(
            "UPDATE vote SET message_id=? WHERE id=?", (sent.message_id, vote_id)
        )


async def on_vote_button(update: Update, context: ContextTypes.DEFAULT_TYPE, vote_id, idx):
    query = update.callback_query
    user = query.from_user
    with _db_lock, db() as conn:
        vote = conn.execute("SELECT * FROM vote WHERE id=?", (vote_id,)).fetchone()
        if not vote or vote["closed"]:
            await query.answer("投票已截止", show_alert=True)
            return
        row = conn.execute(
            "SELECT * FROM vote_ballot WHERE vote_id=? AND user_id=?",
            (vote_id, user.id),
        ).fetchone()
        if row and row["option_idx"] == idx:
            conn.execute(
                "DELETE FROM vote_ballot WHERE vote_id=? AND user_id=?",
                (vote_id, user.id),
            )
            feedback = "已取消投票"
        else:
            conn.execute(
                "INSERT INTO vote_ballot(vote_id, user_id, user_name, option_idx)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(vote_id, user_id) DO UPDATE SET"
                " option_idx=excluded.option_idx, user_name=excluded.user_name",
                (vote_id, user.id, user.full_name, idx),
            )
            opt = conn.execute(
                "SELECT text FROM vote_option WHERE vote_id=? AND idx=?",
                (vote_id, idx),
            ).fetchone()
            feedback = f"已投给:{opt['text']}"
    await query.answer(feedback)
    await refresh_vote_message(context, vote_id)


# ---------------------------------------------------------------- 排行榜

async def cmd_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_message.chat_id
    now = datetime.now()
    month_key = now.strftime("%Y-%m")
    month_label = f"{now.month}月"
    with _db_lock, db() as conn:
        rows = conn.execute(
            """
            SELECT s.user_name,
                   SUM(CASE WHEN s.status='going' THEN 1 ELSE 0 END) AS gone,
                   SUM(s.flakes) AS flakes
            FROM signup s JOIN activity a ON a.id = s.activity_id
            WHERE a.chat_id=? AND strftime('%Y-%m', a.created_at)=?
            GROUP BY s.user_id
            """,
            (chat_id, month_key),
        ).fetchall()
    if not rows or all(r["gone"] == 0 for r in rows):
        await update.effective_message.reply_text(
            f"{month_label}还没有报名记录,先 /new 搞几次活动吧!"
        )
        return

    sep = "➖➖➖➖➖➖➖➖"
    top = sorted(rows, key=lambda r: -r["gone"])[:10]
    lines = [f"🏆 <b>{month_label}运动达人排行榜</b>", sep]
    for i, r in enumerate(top):
        if r["gone"] == 0:
            continue
        medal = MEDALS[i] if i < 3 else f" {i + 1}. "
        crown = " 👑" if i == 0 else ""
        lines.append(f"{medal} {html.escape(r['user_name'])} · {r['gone']}次{crown}")

    flakers = sorted(
        [r for r in rows if r["flakes"] > 0], key=lambda r: -r["flakes"]
    )[:3]
    if flakers:
        lines += [sep, "🕊 <b>本月鸽子榜</b>"]
        for r in flakers:
            lines.append(f"🕊 {html.escape(r['user_name'])} · {r['flakes']}次")
        lines.append(f"本月鸽王:{html.escape(flakers[0]['user_name'])} 🎉")

    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


# ---------------------------------------------------------------- 指令

HELP_TEXT = (
    "🤖 <b>活动报名机器人</b>\n\n"
    "/new 活动名称 | 补充说明 — 发起报名\n"
    "例:<code>/new 周六爬山 | 早上8点西门集合,自带水</code>\n"
    "也支持多行活动模板,/new 后直接换行粘贴模板即可\n\n"
    "/vote 主题 选项1 选项2 — 发起投票\n"
    "例:<code>/vote 这周去哪 爬山 剧本杀 骑行</code>\n\n"
    "/stats — 查看本群进行中活动的报名统计\n"
    "/rank — 本月运动达人排行榜 + 鸽子榜 🕊\n"
    "/close — 回复某条报名/投票消息,截止它\n\n"
    "报名直接点消息下面的按钮:参加 / 不参加 / 待定,"
    "带家属朋友的点「➕带1人」。"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    raw = msg.text or ""
    split = raw.split(None, 1)  # 去掉 /new 指令本身
    args_text = split[1].strip() if len(split) > 1 else ""
    if not args_text:
        await msg.reply_text(
            "请带上活动名称,例如:\n"
            "/new 周六爬山 | 早上8点西门集合\n\n"
            "也支持多行模板(每行一项):\n"
            "/new 标题:城市徒步|XX公园(8月10日)\n"
            "活动时间:8月10日 09:00-12:00\n"
            "活动地点:XX公园\n"
            "集合地点:XX公园南门\n"
            "活动内容:徒步+拍照,约8公里\n"
            "活动费用:免费(餐饮AA)\n"
            "参与人数:5-20人\n"
            "注意事项:穿运动鞋,自备水",
        )
        return
    if "\n" in args_text:  # 多行模板:第一行是标题,其余原样保留
        template_lines = [l.strip() for l in args_text.split("\n") if l.strip()]
        title = template_lines[0]
        if title.startswith("标题"):
            title = title[2:].lstrip(":: ").strip()
        note = "\n".join(template_lines[1:])
    else:  # 单行写法:标题 | 说明
        title, _, note = args_text.partition("|")
        title, note = title.strip(), note.strip()

    user = update.effective_user
    with _db_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO activity(chat_id, title, note, creator_id, creator_name)"
            " VALUES (?,?,?,?,?)",
            (msg.chat_id, title, note, user.id, user.full_name),
        )
        activity_id = cur.lastrowid
        activity = conn.execute(
            "SELECT * FROM activity WHERE id=?", (activity_id,)
        ).fetchone()
        text = render_activity(conn, activity)

    sent = await msg.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard(activity_id)
    )
    with _db_lock, db() as conn:
        conn.execute(
            "UPDATE activity SET message_id=? WHERE id=?",
            (sent.message_id, activity_id),
        )
    # 尝试置顶(机器人需要是管理员,失败就算了)
    try:
        await context.bot.pin_chat_message(
            msg.chat_id, sent.message_id, disable_notification=True
        )
    except Exception:
        pass


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_message.chat_id
    with _db_lock, db() as conn:
        activities = conn.execute(
            "SELECT * FROM activity WHERE chat_id=? AND closed=0"
            " ORDER BY created_at DESC LIMIT 10",
            (chat_id,),
        ).fetchall()
        if not activities:
            await update.effective_message.reply_text(
                "本群当前没有进行中的活动。用 /new 活动名称 发起一个吧!"
            )
            return
        parts = [render_activity(conn, a) for a in activities]
    await update.effective_message.reply_text(
        "\n\n——————————\n\n".join(parts), parse_mode=ParseMode.HTML
    )


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    replied = msg.reply_to_message
    if not replied:
        await msg.reply_text("请「回复」要截止的那条报名消息再发 /close。")
        return
    with _db_lock, db() as conn:
        activity = conn.execute(
            "SELECT * FROM activity WHERE chat_id=? AND message_id=?",
            (msg.chat_id, replied.message_id),
        ).fetchone()
        vote = None
        if not activity:
            vote = conn.execute(
                "SELECT * FROM vote WHERE chat_id=? AND message_id=?",
                (msg.chat_id, replied.message_id),
            ).fetchone()
        if not activity and not vote:
            await msg.reply_text("这条消息不是我发的报名/投票消息哦。")
            return
        if activity:
            conn.execute(
                "UPDATE activity SET closed=1 WHERE id=?", (activity["id"],)
            )
        else:
            conn.execute("UPDATE vote SET closed=1 WHERE id=?", (vote["id"],))
    if activity:
        await refresh_message(
            context, msg.chat_id, replied.message_id, activity["id"]
        )
        await msg.reply_text(f"已截止「{activity['title']}」的报名 ✅")
    else:
        await refresh_vote_message(context, vote["id"])
        await msg.reply_text(f"已截止「{vote['title']}」的投票 ✅")


# ---------------------------------------------------------------- 按钮回调

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    kind, activity_id, arg = query.data.split(":")
    if kind == "v":
        await on_vote_button(update, context, int(activity_id), int(arg))
        return
    activity_id = int(activity_id)
    user = query.from_user

    with _db_lock, db() as conn:
        activity = conn.execute(
            "SELECT * FROM activity WHERE id=?", (activity_id,)
        ).fetchone()
        if not activity or activity["closed"]:
            await query.answer("报名已截止", show_alert=True)
            return

        row = conn.execute(
            "SELECT * FROM signup WHERE activity_id=? AND user_id=?",
            (activity_id, user.id),
        ).fetchone()

        if kind == "s":  # 改状态
            if row and row["status"] == GOING and arg == NOT_GOING:
                conn.execute(  # 点了参加又跑路,记一笔鸽子账
                    "UPDATE signup SET flakes=flakes+1"
                    " WHERE activity_id=? AND user_id=?",
                    (activity_id, user.id),
                )
            conn.execute(
                "INSERT INTO signup(activity_id, user_id, user_name, status, extra,"
                " updated_at) VALUES (?,?,?,?,?, datetime('now','localtime'))"
                " ON CONFLICT(activity_id, user_id) DO UPDATE SET"
                " status=excluded.status, user_name=excluded.user_name,"
                " extra=CASE WHEN excluded.status='going' THEN extra ELSE 0 END,"
                " updated_at=excluded.updated_at",
                (activity_id, user.id, user.full_name, arg,
                 (row["extra"] if row and arg == GOING else 0)),
            )
            feedback = f"已登记:{STATUS_LABEL[arg]}"
        else:  # kind == "e",加减携带人数
            if not row or row["status"] != GOING:
                await query.answer("先点「✅ 参加」才能带人哦", show_alert=True)
                return
            new_extra = max(0, row["extra"] + (1 if arg == "+" else -1))
            conn.execute(
                "UPDATE signup SET extra=? WHERE activity_id=? AND user_id=?",
                (new_extra, activity_id, user.id),
            )
            feedback = f"你共带 {new_extra} 人"

    await query.answer(feedback)
    await refresh_message(
        context, activity["chat_id"], activity["message_id"], activity_id
    )


# ---------------------------------------------------------------- 启动

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("请设置环境变量 BOT_TOKEN(@BotFather 给的 Token)")

    init_db()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("vote", cmd_vote))
    app.add_handler(CommandHandler("rank", cmd_rank))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CallbackQueryHandler(on_button))

    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    if webhook_url:
        port = int(os.environ.get("PORT", "10000"))
        log.info("webhook mode on port %s -> %s", port, webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=token,
            webhook_url=f"{webhook_url.rstrip('/')}/{token}",
        )
    else:
        log.info("polling mode")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

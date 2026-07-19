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
                updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                PRIMARY KEY (activity_id, user_id)
            );
            """
        )


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
        lines.append(html.escape(activity["note"]))
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


# ---------------------------------------------------------------- 指令

HELP_TEXT = (
    "🤖 <b>活动报名机器人</b>\n\n"
    "/new 活动名称 | 补充说明 — 发起报名\n"
    "例:<code>/new 周六爬山 | 早上8点西门集合,自带水</code>\n\n"
    "/stats — 查看本群进行中活动的报名统计\n"
    "/close — 回复某条报名消息,截止该活动\n\n"
    "报名直接点消息下面的按钮:参加 / 不参加 / 待定,"
    "带家属朋友的点「➕带1人」。"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    args_text = " ".join(context.args).strip()
    if not args_text:
        await msg.reply_text(
            "请带上活动名称,例如:\n/new 周六爬山 | 早上8点西门集合",
        )
        return
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
        if not activity:
            await msg.reply_text("这条消息不是我发的报名消息哦。")
            return
        conn.execute("UPDATE activity SET closed=1 WHERE id=?", (activity["id"],))
    await refresh_message(context, msg.chat_id, replied.message_id, activity["id"])
    await msg.reply_text(f"已截止「{activity['title']}」的报名 ✅")


# ---------------------------------------------------------------- 按钮回调

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    kind, activity_id, arg = query.data.split(":")
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

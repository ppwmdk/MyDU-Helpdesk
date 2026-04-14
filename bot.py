# =========================
# IMPORTS
# =========================
import os
from datetime import datetime
from io import BytesIO

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from openpyxl import Workbook

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x}
DEVELOPER_IDS = {int(x) for x in os.getenv("DEVELOPER_IDS", "").split(",") if x}

# =========================
# DATABASE
# =========================
def get_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMP,
                    user_id BIGINT,
                    username TEXT,
                    name TEXT,
                    group_name TEXT,
                    module TEXT,
                    description TEXT,
                    screenshot_file_id TEXT,
                    status TEXT DEFAULT 'Новая'
                )
            """)
        conn.commit()

# =========================
# ROLES
# =========================
def is_staff(uid):
    return uid in ADMIN_IDS or uid in DEVELOPER_IDS

# =========================
# STATES
# =========================
NAME, GROUP, MODULE, DESC, SCREEN = range(5)
REPLY_TEXT = 10

# =========================
# MODULE BUTTONS
# =========================
async def get_group(update, context):
    context.user_data["group"] = update.message.text

    keyboard = [
        ["Регистрация на дисциплины", "Общежитие"],
        ["Платежи", "Другое"]
    ]

    await update.message.reply_text(
        "Выберите модуль:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )
    return MODULE

# =========================
# START
# =========================
async def start(update, context):
    await update.message.reply_text(
        "✨ Добро пожаловать!\n\n"
        "• /report — отправить заявку\n\n"
        "⚠️ Пишите @ppwmdk ТОЛЬКО если бот не отвечает на /start или /report"
    )

# =========================
# REPORT FLOW
# =========================
async def report_start(update, context):
    await update.message.reply_text("ФИО:")
    return NAME

async def get_name(update, context):
    context.user_data["name"] = update.message.text
    await update.message.reply_text("Группа:")
    return GROUP

async def get_module(update, context):
    context.user_data["module"] = update.message.text
    await update.message.reply_text("Описание:", reply_markup=ReplyKeyboardRemove())
    return DESC

async def get_desc(update, context):
    context.user_data["desc"] = update.message.text
    await update.message.reply_text("Скриншот или 'пропустить':")
    return SCREEN

async def get_screen(update, context):
    photo = None
    if update.message.photo:
        photo = update.message.photo[-1].file_id

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO reports (created_at,user_id,username,name,group_name,module,description,screenshot_file_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                datetime.now(),
                update.effective_user.id,
                update.effective_user.username,
                context.user_data["name"],
                context.user_data["group"],
                context.user_data["module"],
                context.user_data["desc"],
                photo
            ))
            rid = cur.fetchone()["id"]
        conn.commit()

    text = f"""
📌 Заявка #{rid}

👤 {context.user_data["name"]}
🎓 {context.user_data["group"]}
🧩 {context.user_data["module"]}
📝 {context.user_data["desc"]}
"""

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛠 В работу", callback_data=f"take_{rid}"),
            InlineKeyboardButton("✅ Решено", callback_data=f"done_{rid}")
        ],
        [
            InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_{rid}")
        ]
    ])

    for uid in ADMIN_IDS:
        await context.bot.send_message(uid, text, reply_markup=keyboard)

    await update.message.reply_text(
        "✅ Заявка отправлена\n\n"
        "Если НЕ пришло это сообщение после /report — пишите @ppwmdk"
    )

    return ConversationHandler.END

# =========================
# BUTTON HANDLER
# =========================
async def handle_buttons(update, context):
    query = update.callback_query
    await query.answer()

    data = query.data
    uid = query.from_user.id

    if not is_staff(uid):
        return

    rid = int(data.split("_")[1])

    if data.startswith("take_"):
        status = "В работе"

    elif data.startswith("done_"):
        status = "Решено"

    elif data.startswith("reply_"):
        context.user_data["reply_id"] = rid
        await query.message.reply_text("Введите сообщение студенту:")
        return REPLY_TEXT

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE reports SET status=%s WHERE id=%s", (status, rid))
        conn.commit()

    await query.message.reply_text(f"#{rid} → {status}")

# =========================
# REPLY FLOW
# =========================
async def send_reply(update, context):
    text = update.message.text
    rid = context.user_data.get("reply_id")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM reports WHERE id=%s", (rid,))
            row = cur.fetchone()

    if row:
        await context.bot.send_message(
            chat_id=row["user_id"],
            text=f"📩 Ответ по заявке #{rid}\n\n{text}"
        )

    await update.message.reply_text("Сообщение отправлено ✅")
    return ConversationHandler.END

# =========================
# APP
# =========================
telegram_app = Application.builder().token(TOKEN).build()

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CallbackQueryHandler(handle_buttons))

report_conv = ConversationHandler(
    entry_points=[CommandHandler("report", report_start)],
    states={
        NAME: [MessageHandler(filters.TEXT, get_name)],
        GROUP: [MessageHandler(filters.TEXT, get_group)],
        MODULE: [MessageHandler(filters.TEXT, get_module)],
        DESC: [MessageHandler(filters.TEXT, get_desc)],
        SCREEN: [MessageHandler(filters.ALL, get_screen)],
    },
    fallbacks=[]
)

reply_conv = ConversationHandler(
    entry_points=[MessageHandler(filters.ALL, send_reply)],
    states={
        REPLY_TEXT: [MessageHandler(filters.TEXT, send_reply)]
    },
    fallbacks=[]
)

telegram_app.add_handler(report_conv)
telegram_app.add_handler(reply_conv)

# =========================
# WEBHOOK
# =========================
app = FastAPI()

@app.on_event("startup")
async def startup():
    init_db()
    await telegram_app.initialize()
    await telegram_app.start()

@app.get("/")
def home():
    return {"ok": True}

@app.post(f"/{TOKEN}")
async def webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}
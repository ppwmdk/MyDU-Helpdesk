# ==========================
# IMPORTS
# ==========================
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
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
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

# ==========================
# ENV
# ==========================
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.environ.get("PORT", 10000))

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x}
DEVELOPER_IDS = {int(x) for x in os.getenv("DEVELOPER_IDS", "").split(",") if x}

# ==========================
# DB
# ==========================
def get_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
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

# ==========================
# ROLES
# ==========================
def is_admin(user_id):
    return user_id in ADMIN_IDS

def is_staff(user_id):
    return user_id in ADMIN_IDS or user_id in DEVELOPER_IDS

# ==========================
# COMMANDS
# ==========================
async def set_commands(app):
    student = [
        BotCommand("start", "Старт"),
        BotCommand("report", "Отправить ошибку"),
    ]
    admin = student + [
        BotCommand("list_reports", "Заявки"),
        BotCommand("export_excel", "Excel"),
    ]

    await app.bot.set_my_commands(student, scope=BotCommandScopeDefault())

    for admin_id in ADMIN_IDS:
        await app.bot.set_my_commands(admin, scope=BotCommandScopeChat(admin_id))

# ==========================
# START
# ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот поддержки запущен 🚀")

# ==========================
# REPORT FLOW
# ==========================
NAME, GROUP, MODULE, DESC, SCREEN = range(5)

async def report_start(update: Update, context):
    await update.message.reply_text("ФИО:")
    return NAME

async def get_name(update, context):
    context.user_data["name"] = update.message.text
    await update.message.reply_text("Группа:")
    return GROUP

async def get_group(update, context):
    context.user_data["group"] = update.message.text
    await update.message.reply_text("Модуль:")
    return MODULE

async def get_module(update, context):
    context.user_data["module"] = update.message.text
    await update.message.reply_text("Описание:")
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
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO reports (created_at, user_id, username, name, group_name, module, description, screenshot_file_id)
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
            report_id = cursor.fetchone()["id"]
        conn.commit()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛠 В работу", callback_data=f"take_{report_id}"),
            InlineKeyboardButton("✅ Решено", callback_data=f"done_{report_id}")
        ]
    ])

    for admin_id in ADMIN_IDS:
        await context.bot.send_message(
            admin_id,
            f"Новая заявка #{report_id}",
            reply_markup=keyboard
        )

    await update.message.reply_text("Заявка отправлена ✅")
    return ConversationHandler.END

# ==========================
# BUTTONS
# ==========================
async def handle_buttons(update: Update, context):
    query = update.callback_query
    await query.answer()

    data = query.data
    user = query.from_user

    if not is_staff(user.id):
        return

    report_id = int(data.split("_")[1])

    if data.startswith("take_"):
        status = "В работе"
    else:
        status = "Решено"

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE reports SET status=%s WHERE id=%s",
                (status, report_id)
            )
        conn.commit()

    await query.edit_message_reply_markup(None)
    await query.message.reply_text(f"#{report_id} → {status}")

# ==========================
# LIST
# ==========================
async def list_reports(update, context):
    if not is_staff(update.effective_user.id):
        return

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, status FROM reports ORDER BY id DESC LIMIT 10")
            rows = cursor.fetchall()

    text = "\n".join([f"#{r['id']} - {r['status']}" for r in rows])
    await update.message.reply_text(text)

# ==========================
# EXCEL
# ==========================
async def export_excel(update, context):
    if not is_admin(update.effective_user.id):
        return

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM reports")
            rows = cursor.fetchall()

    wb = Workbook()
    ws = wb.active

    ws.append(list(rows[0].keys()))

    for r in rows:
        ws.append(list(r.values()))

    stream = BytesIO()
    wb.save(stream)
    stream.seek(0)

    await update.message.reply_document(stream, filename="reports.xlsx")

# ==========================
# APP
# ==========================
telegram_app = Application.builder().token(TOKEN).build()

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("list_reports", list_reports))
telegram_app.add_handler(CommandHandler("export_excel", export_excel))
telegram_app.add_handler(CallbackQueryHandler(handle_buttons))

conv = ConversationHandler(
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

telegram_app.add_handler(conv)

app = FastAPI()

@app.on_event("startup")
async def startup():
    init_db()
    await telegram_app.initialize()
    await telegram_app.start()
    await set_commands(telegram_app)

@app.get("/")
def home():
    return {"ok": True}

@app.post(f"/{TOKEN}")
async def webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}
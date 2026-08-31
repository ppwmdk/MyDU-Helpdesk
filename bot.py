import os
import re
import hmac
import time
import math
import hashlib
import logging
import asyncio
import subprocess
from datetime import datetime
from io import BytesIO

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_PANEL_USERNAME = os.getenv("ADMIN_PANEL_USERNAME", "admin")
ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD")
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET") or TOKEN or "change-me"
ADMIN_COOKIE_SECURE = os.getenv("ADMIN_COOKIE_SECURE", "false").lower() == "true"
ADMIN_SESSION_COOKIE = "admin_session"
ADMIN_SESSION_MAX_AGE = 60 * 60 * 12

if not TOKEN:
    raise ValueError("BOT_TOKEN не найден")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не найден")

app = FastAPI()

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DEVELOPER_IDS = {int(x.strip()) for x in os.getenv("DEVELOPER_IDS", "").split(",") if x.strip()}
SUPER_ADMIN_IDS = {548200976}

NAME, GROUP, MODULE, ACADEMIC_STATUS, DESCRIPTION, SMART_FAQ_WAIT, SCREENSHOT = range(7)
STAFF_REPORT_ID = 100
STAFF_FILTER_MODULE = 101
STAFF_SET_STATUS_ID = 102
STAFF_SET_STATUS_VALUE = 103
STAFF_TAKE_REPORT_ID = 104
STAFF_RESOLVE_REPORT_ID = 105

STATUSES = ["Новая", "В работе", "Решено"]

# =========================
# DATABASE
# =========================
async def get_conn_async():
    return await psycopg.AsyncConnection.connect(DATABASE_URL, row_factory=dict_row)

def get_sync_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

async def init_db():
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMP NOT NULL,
                    user_id BIGINT,
                    username TEXT,
                    name TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    module TEXT NOT NULL,
                    description TEXT NOT NULL,
                    screenshot_file_id TEXT,
                    status TEXT NOT NULL DEFAULT 'Новая'
                )
            """)
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS report_messages (
                    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                    chat_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    PRIMARY KEY (report_id, chat_id, message_id)
                )
            """)
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS student_report_messages (
                    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                    chat_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    PRIMARY KEY (report_id, chat_id, message_id)
                )
            """)
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS ticket_messages (
                    id SERIAL PRIMARY KEY,
                    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                    sender_type TEXT NOT NULL,
                    sender_name TEXT,
                    message_text TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_modules (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS quick_templates (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS smart_faq_rules (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    keywords TEXT NOT NULL,
                    reply_text TEXT NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            
            # Заполняем базовые модули, если таблица пуста
            await cursor.execute("SELECT COUNT(*) FROM system_modules")
            if (await cursor.fetchone())["count"] == 0:
                default_modules = ["Регистрация на дисциплины", "Общежитие", "Платежи", "Приемная комиссия", "Другое"]
                for m in default_modules:
                    await cursor.execute("INSERT INTO system_modules (name) VALUES (%s) ON CONFLICT DO NOTHING", (m,))

        await conn.commit()

async def get_active_modules() -> list[str]:
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT name FROM system_modules WHERE is_active = TRUE ORDER BY id ASC")
            rows = await cursor.fetchall()
            return [r["name"] for r in rows] if rows else ["Другое"]

async def check_smart_faq(text: str) -> dict | None:
    lower_text = text.lower()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT id, title, keywords, reply_text FROM smart_faq_rules WHERE is_active = TRUE")
            rules = await cursor.fetchall()
            for rule in rules:
                keywords = [k.strip().lower() for k in rule["keywords"].split(",") if k.strip()]
                for kw in keywords:
                    if kw in lower_text:
                        return rule
    return None

# =========================
# ROLES & UI
# =========================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def is_developer(user_id: int) -> bool:
    return user_id in DEVELOPER_IDS

def is_staff(user_id: int) -> bool:
    return is_admin(user_id) or is_developer(user_id)

def get_role_name(user_id: int) -> str:
    if is_admin(user_id):
        return "Админ"
    if is_developer(user_id):
        return "Разработчик"
    return "Студент"

def get_staff_keyboard(user_id: int):
    keyboard = [
        ["Новые заявки", "Последние заявки"],
        ["Поиск по ID"],
        ["Фильтр по модулю", "Изменить статус"],
        ["Взять в работу", "Отметить решённой"],
    ]
    if is_admin(user_id):
        keyboard.append(["Выгрузить Excel"])
    keyboard.append(["Скрыть меню"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_skip_screenshot_keyboard():
    return ReplyKeyboardMarkup([["Пропустить"]], resize_keyboard=True, one_time_keyboard=True)

def build_inline_keyboard(report_id: int, status: str = "Новая", module: str = "") -> InlineKeyboardMarkup:
    rows = []
    if module == "Приемная комиссия" and status == "Новая":
        rows.append([
            InlineKeyboardButton("📨 Шаблонный ответ", callback_data=f"template_{report_id}"),
            InlineKeyboardButton("✉️ Ответить вручную", callback_data=f"reply_{report_id}")
        ])
        rows.append([
            InlineKeyboardButton("🛠 В работу", callback_data=f"take_{report_id}"),
            InlineKeyboardButton("✅ Решено", callback_data=f"done_{report_id}")
        ])
    else:
        if status == "Новая":
            rows.append([
                InlineKeyboardButton("🛠 В работу", callback_data=f"take_{report_id}"),
                InlineKeyboardButton("✅ Решено", callback_data=f"done_{report_id}")
            ])
        elif status == "В работе":
            rows.append([
                InlineKeyboardButton("✅ Решено", callback_data=f"done_{report_id}")
            ])
        rows.append([InlineKeyboardButton("✉️ Ответить студенту", callback_data=f"reply_{report_id}")])
    return InlineKeyboardMarkup(rows)

async def set_commands(application: Application):
    student_commands = [
        BotCommand("start", "Главное сообщение"),
        BotCommand("faq", "Частые вопросы"),
        BotCommand("report", "Отправить заявку"),
        BotCommand("my_role", "Показать мою роль"),
        BotCommand("my_reports", "Мои заявки"),
    ]
    staff_commands = student_commands + [
        BotCommand("staff_menu", "Открыть меню сотрудника"),
        BotCommand("new_reports", "Новые заявки"),
        BotCommand("list_reports", "Последние заявки"),
        BotCommand("report_by_id", "Полная заявка по ID"),
        BotCommand("filter_module", "Фильтр по модулю"),
        BotCommand("set_status", "Изменить статус заявки"),
        BotCommand("take_report", "Взять заявку в работу"),
        BotCommand("resolve_report", "Отметить заявку решённой"),
        BotCommand("cancel", "Отменить действие"),
    ]
    admin_commands = staff_commands + [
        BotCommand("export_excel", "Выгрузить Excel"),
        BotCommand("restart_bot", "🔄 Перезапустить бота"),
    ]

    await application.bot.set_my_commands(student_commands, scope=BotCommandScopeDefault())
    for dev_id in DEVELOPER_IDS:
        try:
            await application.bot.set_my_commands(staff_commands, scope=BotCommandScopeChat(chat_id=dev_id))
        except Exception:
            pass
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            pass

# =========================
# HELPERS
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка в боте: {context.error}")

def build_report_text(report_id: int, created_at: datetime, name: str, group_name: str, module: str, description: str, status: str, user_id: int | None, username: str | None) -> str:
    username_text = f"@{username}" if username else "-"
    return (
        f"📌 Новая заявка #{report_id}\n\n"
        f"🕒 Дата: {created_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"👤 ФИО: {name}\n"
        f"🎓 Группа: {group_name}\n"
        f"🧩 Модуль: {module}\n"
        f"📝 Описание: {description}\n"
        f"📊 Статус: {status}\n"
        f"🆔 Telegram ID: {user_id if user_id else '-'}\n"
        f"🔗 Username: {username_text}"
    )

async def save_ticket_message(report_id: int, sender_type: str, sender_name: str, message_text: str):
    created_at = datetime.now()
    try:
        async with await get_conn_async() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    INSERT INTO ticket_messages (report_id, sender_type, sender_name, message_text, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                """, (report_id, sender_type, sender_name, message_text, created_at))
            await conn.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения сообщения тикета: {e}")

async def get_ticket_messages(report_id: int) -> list[dict]:
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                SELECT id, sender_type, sender_name, message_text, created_at
                FROM ticket_messages
                WHERE report_id = %s
                ORDER BY id ASC
            """, (report_id,))
            return await cursor.fetchall()

async def save_report_message(report_id: int, chat_id: int, message_id: int):
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                INSERT INTO report_messages (report_id, chat_id, message_id)
                VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
            """, (report_id, chat_id, message_id))
        await conn.commit()

async def save_student_report_message(report_id: int, chat_id: int, message_id: int):
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                INSERT INTO student_report_messages (report_id, chat_id, message_id)
                VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
            """, (report_id, chat_id, message_id))
        await conn.commit()

async def get_report_id_from_student_reply(message) -> int | None:
    if not message or not message.reply_to_message:
        return None
    reply_message_id = message.reply_to_message.message_id
    chat_id = message.chat_id
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                SELECT report_id FROM student_report_messages
                WHERE chat_id = %s AND message_id = %s
            """, (chat_id, reply_message_id))
            row = await cursor.fetchone()
    if row:
        return row["report_id"]
    text = message.reply_to_message.text or message.reply_to_message.caption or ""
    match = re.search(r"#(\d+)", text)
    return int(match.group(1)) if match else None

async def get_last_active_report_for_user(user_id: int) -> dict | None:
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                SELECT id, module, name, group_name, status FROM reports
                WHERE user_id = %s AND status IN ('Новая', 'В работе')
                ORDER BY id DESC LIMIT 1
            """, (user_id,))
            return await cursor.fetchone()

async def sync_report_keyboards(context: ContextTypes.DEFAULT_TYPE, report_id: int, status: str, skip_chat_id: int | None = None, skip_message_id: int | None = None):
    report = await get_report_async(report_id)
    module = report["module"] if report else ""
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT chat_id, message_id FROM report_messages WHERE report_id = %s", (report_id,))
            rows = await cursor.fetchall()
    keyboard = build_inline_keyboard(report_id, status, module)
    for row in rows:
        if row["chat_id"] == skip_chat_id and row["message_id"] == skip_message_id:
            continue
        try:
            await context.bot.edit_message_reply_markup(chat_id=row["chat_id"], message_id=row["message_id"], reply_markup=keyboard)
        except Exception:
            pass

async def notify_admins_status_change(context: ContextTypes.DEFAULT_TYPE, report_id: int, module: str | None, new_status: str, actor):
    if new_status == "В работе":
        title = "🛠 Заявку взяли в работу"
    elif new_status == "Решено":
        title = "✅ Заявку завершили"
    else:
        return

    actor_username = getattr(actor, "username", None)
    actor_full_name = getattr(actor, "full_name", None)
    actor_id = getattr(actor, "id", None)
    actor_role = getattr(actor, "role_name", None)
    username_text = f"@{actor_username}" if actor_username else "-"
    actor_name = actor_full_name if actor_full_name else "-"
    role = actor_role if actor_role else (get_role_name(actor_id) if actor_id else "-")

    text = (
        f"{title}\n\n"
        f"📌 Заявка: #{report_id}\n"
        f"🧩 Модуль: {module if module else '-'}\n"
        f"📊 Статус: {new_status}\n\n"
        f"👤 Кто изменил: {actor_name}\n"
        f"🔐 Роль: {role}\n"
        f"🆔 Telegram ID: {actor_id if actor_id else '-'}\n"
        f"🔗 Username: {username_text}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception:
            pass

class WebActor:
    def __init__(self, username: str):
        self.id = None
        self.username = None
        self.full_name = f"Веб-панель ({username})"
        self.role_name = "Веб-панель"

def create_admin_session(username: str) -> str:
    timestamp = str(int(time.time()))
    payload = f"{username}|{timestamp}"
    signature = hmac.new(ADMIN_SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}|{signature}"

def verify_admin_session(session_value: str | None) -> str | None:
    if not session_value:
        return None
    parts = session_value.split("|")
    if len(parts) != 3:
        return None
    username, timestamp, signature = parts
    payload = f"{username}|{timestamp}"
    expected_signature = hmac.new(ADMIN_SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return None
    try:
        session_age = int(time.time()) - int(timestamp)
    except ValueError:
        return None
    if session_age > ADMIN_SESSION_MAX_AGE or username != ADMIN_PANEL_USERNAME:
        return None
    return username

def get_admin_username(request: Request) -> str | None:
    return verify_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE))

def admin_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/login", status_code=303)

async def get_dashboard_counts_async() -> dict:
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'Новая') AS new_count,
                    COUNT(*) FILTER (WHERE status = 'В работе') AS in_progress_count,
                    COUNT(*) FILTER (WHERE status = 'Решено') AS resolved_count
                FROM reports
            """)
            return await cursor.fetchone()

async def get_reports_async(status_filter: str | None = None, module_filter: str | None = None, search: str | None = None, page: int = 1, per_page: int = 20) -> tuple[list[dict], int]:
    conditions = []
    params = []
    if status_filter and status_filter in STATUSES:
        conditions.append("status = %s")
        params.append(status_filter)
    if module_filter:
        conditions.append("module = %s")
        params.append(module_filter)
    if search:
        search_value = search.strip()
        if search_value:
            like_value = f"%{search_value}%"
            conditions.append("""
                (CAST(id AS TEXT) ILIKE %s OR name ILIKE %s OR group_name ILIKE %s OR COALESCE(username, '') ILIKE %s OR module ILIKE %s OR description ILIKE %s)
            """)
            params.extend([like_value] * 6)

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(f"SELECT COUNT(*) FROM reports {where_sql}", params)
            total_records = (await cursor.fetchone())["count"]

    offset = (page - 1) * per_page
    main_params = params.copy()
    main_params.extend([per_page, offset])

    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(f"""
                SELECT id, created_at, user_id, username, name, group_name, module, description, screenshot_file_id, status
                FROM reports {where_sql}
                ORDER BY id DESC LIMIT %s OFFSET %s
            """, main_params)
            reports = await cursor.fetchall()
    return reports, total_records

def get_reports(status_filter: str | None = None, module_filter: str | None = None, search: str | None = None, limit: int | None = 100) -> list[dict]:
    conditions = []
    params = []
    if status_filter and status_filter in STATUSES:
        conditions.append("status = %s")
        params.append(status_filter)
    if module_filter:
        conditions.append("module = %s")
        params.append(module_filter)
    if search:
        search_value = search.strip()
        if search_value:
            like_value = f"%{search_value}%"
            conditions.append("""
                (CAST(id AS TEXT) ILIKE %s OR name ILIKE %s OR group_name ILIKE %s OR COALESCE(username, '') ILIKE %s OR module ILIKE %s OR description ILIKE %s)
            """)
            params.extend([like_value] * 6)

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    limit_sql = "LIMIT %s" if limit else ""
    if limit:
        params.append(limit)

    with get_sync_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"""
                SELECT id, created_at, user_id, username, name, group_name, module, description, screenshot_file_id, status
                FROM reports {where_sql}
                ORDER BY id DESC {limit_sql}
            """, params)
            return cursor.fetchall()

async def get_report_async(report_id: int) -> dict | None:
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM reports WHERE id = %s", (report_id,))
            return await cursor.fetchone()

async def update_report_status_in_db_async(report_id: int, new_status: str) -> dict | None:
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT id, user_id, module, name, group_name, status FROM reports WHERE id = %s", (report_id,))
            report = await cursor.fetchone()
            if not report:
                return None
            await cursor.execute("UPDATE reports SET status = %s WHERE id = %s", (new_status, report_id))
        await conn.commit()
    return report

async def notify_student_status(report: dict, report_id: int, new_status: str):
    if not report["user_id"]:
        return
    if new_status == "В работе":
        text = (
            f"🛠 Обновление по вашей заявке #{report_id}\n\n"
            f"Модуль: {report['module']}\n"
            "Статус: В работе\n\n"
            "Ваше обращение принято сотрудниками и уже находится в обработке.\n\n"
            "Если хотите что-то уточнить, ответьте на это сообщение."
        )
    elif new_status == "Решено":
        text = (
            f"✅ Обновление по вашей заявке #{report_id}\n\n"
            f"Модуль: {report['module']}\n"
            "Статус: Решено\n\n"
            "Здравствуйте! Ваша проблема была обработана и отмечена как решённая.\n\n"
            "Если хотите написать уточнение, ответьте на это сообщение."
        )
    else:
        return

    try:
        sent = await telegram_app.bot.send_message(chat_id=report["user_id"], text=text)
        await save_student_report_message(report_id, report["user_id"], sent.message_id)
    except Exception as e:
        logger.error(f"Не удалось уведомить студента: {e}")

async def apply_report_status_change(report_id: int, new_status: str, actor, silent: bool = False) -> bool:
    report = await update_report_status_in_db_async(report_id, new_status)
    if not report:
        return False
    await sync_report_keyboards(telegram_app, report_id, new_status)
    await notify_admins_status_change(context=telegram_app, report_id=report_id, module=report["module"], new_status=new_status, actor=actor)
    if not silent:
        await notify_student_status(report, report_id, new_status)
    return True

async def send_reply_to_student_from_admin(report_id: int, message_text: str, actor) -> bool:
    report = await get_report_async(report_id)
    if not report or not report["user_id"]:
        return False
    try:
        sent = await telegram_app.bot.send_message(
            chat_id=report["user_id"],
            text=f"📩 Сообщение по вашей заявке #{report_id}\n\n{message_text}\n\nОтветьте на это сообщение, чтобы продолжить диалог."
        )
        await save_student_report_message(report_id, report["user_id"], sent.message_id)
        await save_ticket_message(report_id, "staff", actor.full_name, message_text)
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение студенту: {e}")
        return False

    log_text = f"📩 Сотрудник ответил студенту\n\n👤 Отправитель: {actor.full_name}\n📌 Заявка: #{report_id}\n\n💬 Сообщение:\n{message_text}"
    for admin_id in ADMIN_IDS.union(SUPER_ADMIN_IDS):
        try:
            await telegram_app.bot.send_message(chat_id=admin_id, text=log_text)
        except Exception:
            pass
    return True

def build_reports_excel(rows: list[dict]) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Заявки"
    ws.append(["ID", "Дата", "Telegram ID", "Username", "ФИО", "Группа", "Модуль", "Описание", "Статус", "Есть скриншот"])
    for row in rows:
        ws.append([
            row["id"], row["created_at"].strftime("%Y-%m-%d %H:%M:%S"), row["user_id"], row["username"],
            row["name"], row["group_name"], row["module"], row["description"], row["status"],
            "Да" if row["screenshot_file_id"] else "Нет",
        ])
    file_stream = BytesIO()
    wb.save(file_stream)
    file_stream.seek(0)
    return file_stream

# =========================
# BOT FLOW & CONVERSATION
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    role = get_role_name(user.id)
    text = (
        "✨ Добро пожаловать в бот поддержки Astana IT University!\n\n"
        "Здесь можно отправить обращение или задать вопрос.\n\n"
        "📌 Доступные команды:\n"
        "• /report — отправить заявку\n"
        "• /faq — частые вопросы\n"
        "• /my_reports — мои заявки\n"
        "• /my_role — моя роль\n\n"
        f"👤 Ваша роль: {role}"
    )
    if is_staff(user.id):
        text += "\n\n🛠 Для сотрудников:\n/staff_menu — открыть меню сотрудника"
    await update.message.reply_text(text)

async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 Частые вопросы\n\n"
        "1. Как отправить заявку?\n"
        "— Введите команду /report и следуйте подсказкам бота.\n\n"
        "2. Как быстро придет ответ?\n"
        "— Заявки обрабатываются сотрудниками в порядке очереди.\n\n"
        "3. Можно ли отправить без скриншота?\n"
        "— Да, нажмите кнопку: Пропустить."
    )
    await update.message.reply_text(text)

async def my_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        await update.message.reply_text(f"Ваша роль: {get_role_name(user.id)}")

async def my_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT id, created_at, module, status FROM reports WHERE user_id = %s ORDER BY id DESC LIMIT 20", (user.id,))
            rows = await cursor.fetchall()
    if not rows:
        await update.message.reply_text("У вас пока нет отправленных заявок.")
        return
    lines = ["📋 Ваши заявки:\n"]
    for row in rows:
        lines.append(f"#{row['id']} | {row['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\nМодуль: {row['module']}\nСтатус: {row['status']}\n")
    await update.message.reply_text("\n".join(lines))

async def restart_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id not in SUPER_ADMIN_IDS:
        await update.message.reply_text("⛔️ У вас нет прав на эту команду.")
        return

    msg = await update.message.reply_text("🔄 Перезапускаю службу бота... Сообщение исчезнет через 10 секунд.")
    chat_id = update.effective_chat.id
    user_msg_id = update.message.message_id
    bot_msg_id = msg.message_id

    cleanup_and_restart_script = (
        f"sleep 10 && "
        f"curl -s -X POST 'https://api.telegram.org/bot{TOKEN}/deleteMessage' -d 'chat_id={chat_id}&message_id={user_msg_id}' > /dev/null && "
        f"curl -s -X POST 'https://api.telegram.org/bot{TOKEN}/deleteMessage' -d 'chat_id={chat_id}&message_id={bot_msg_id}' > /dev/null && "
        f"/usr/bin/sudo /usr/bin/systemctl restart mydu-bot"
    )
    try:
        subprocess.Popen(cleanup_and_restart_script, shell=True)
    except Exception as e:
        logger.error(f"Ошибка перезапуска: {e}")

async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["report_in_progress"] = True
    await update.message.reply_text("Введите ФИО:", reply_markup=ReplyKeyboardRemove())
    return NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("Введите группу:")
    return GROUP

async def get_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["group"] = update.message.text.strip()
    modules_list = await get_active_modules()
    # Строим клавиатуру парами кнопок
    keyboard = []
    for i in range(0, len(modules_list), 2):
        keyboard.append(modules_list[i:i+2])

    await update.message.reply_text(
        "Выберите модуль обращения:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    )
    return MODULE

async def get_module(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chosen_module = update.message.text.strip()
    context.user_data["module"] = chosen_module
    if chosen_module == "Приемная комиссия":
        keyboard = [["🎓 Бакалавриат"], ["📚 Магистратура"], ["🔬 Докторантура (PhD)"]]
        await update.message.reply_text(
            "Пожалуйста, выберите ваш академический статус:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        )
        return ACADEMIC_STATUS
    else:
        await update.message.reply_text("Опишите возникшую проблему:", reply_markup=ReplyKeyboardRemove())
        return DESCRIPTION

async def get_academic_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["academic_status"] = update.message.text.strip()
    await update.message.reply_text("Опишите ваш вопрос к приемной комиссии:", reply_markup=ReplyKeyboardRemove())
    return DESCRIPTION

async def get_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    context.user_data["description"] = desc

    # Проверяем Smart FAQ автоответ
    faq_match = await check_smart_faq(desc)
    if faq_match:
        faq_keyboard = [
            ["✅ Спасибо, проблема решена"],
            ["✉️ Все равно создать заявку"]
        ]
        text = (
            f"💡 **Возможно, это вам поможет:**\n\n"
            f"{faq_match['reply_text']}\n\n"
            f"Помог ли данный ответ?"
        )
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(faq_keyboard, resize_keyboard=True, one_time_keyboard=True)
        )
        return SMART_FAQ_WAIT

    await update.message.reply_text("Теперь отправьте скриншот ошибки или нажмите кнопку: Пропустить", reply_markup=get_skip_screenshot_keyboard())
    return SCREENSHOT

async def handle_smart_faq_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_choice = update.message.text.strip()
    if user_choice == "✅ Спасибо, проблема решена":
        context.user_data.clear()
        await update.message.reply_text("Рады были помочь! Обращение отменено.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    else:
        await update.message.reply_text("Хорошо, продолжим. Отправьте скриншот или нажмите кнопку: Пропустить", reply_markup=get_skip_screenshot_keyboard())
        return SCREENSHOT

async def get_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    screenshot_file_id = None
    text_value = update.message.text.strip().lower() if update.message.text else ""
    if update.message.photo:
        screenshot_file_id = update.message.photo[-1].file_id
    elif text_value == "пропустить":
        screenshot_file_id = None
    else:
        await update.message.reply_text("Пожалуйста, отправьте скриншот или нажмите кнопку: Пропустить", reply_markup=get_skip_screenshot_keyboard())
        return SCREENSHOT

    user = update.effective_user
    created_at = datetime.now()
    student_name = context.user_data.get("name", "Не указано")
    student_group = context.user_data.get("group", "Не указано")
    student_module = context.user_data.get("module", "Другое")
    base_desc = context.user_data.get("description", "")
    
    final_description = base_desc
    if context.user_data.get("academic_status"):
        final_description = f"[{context.user_data['academic_status']}] {base_desc}"

    try:
        async with await get_conn_async() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    INSERT INTO reports (created_at, user_id, username, name, group_name, module, description, screenshot_file_id, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                """, (
                    created_at, user.id if user else None, user.username if user and user.username else None,
                    student_name, student_group, student_module, final_description, screenshot_file_id, "Новая"
                ))
                report_id = (await cursor.fetchone())["id"]
            await conn.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения заявки в БД: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте снова через /report")
        return ConversationHandler.END

    await update.message.reply_text(
        f"✅ Ваша заявка #{report_id} успешно отправлена!\n\nСпасибо, мы уже приняли обращение в обработку.",
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data.clear()
    context.user_data["report_in_progress"] = False

    report_text = build_report_text(report_id, created_at, student_name, student_group, student_module, final_description, "Новая", user.id if user else None, user.username if user else None)
    keyboard = build_inline_keyboard(report_id, "Новая", student_module)
    recipients = ADMIN_IDS.union(DEVELOPER_IDS)

    for staff_id in recipients:
        try:
            if screenshot_file_id:
                staff_message = await context.bot.send_photo(chat_id=staff_id, photo=screenshot_file_id, caption=report_text, reply_markup=keyboard)
            else:
                staff_message = await context.bot.send_message(chat_id=staff_id, text=report_text, reply_markup=keyboard)
            await save_report_message(report_id, staff_id, staff_message.message_id)
        except Exception:
            pass

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Действие отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# =========================
# STAFF COMMANDS & INLINE
# =========================
async def staff_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        await update.message.reply_text("У вас нет доступа к меню сотрудника.")
        return
    await update.message.reply_text("Меню сотрудника открыто.", reply_markup=get_staff_keyboard(user.id))

async def hide_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Меню скрыто.", reply_markup=ReplyKeyboardRemove())

async def list_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        return
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT id, created_at, name, group_name, module, description, status FROM reports ORDER BY id DESC LIMIT 10")
            rows = await cursor.fetchall()
    if not rows:
        await update.message.reply_text("Заявок пока нет.")
        return
    await update.message.reply_text("Последние заявки:")
    for row in rows:
        text = f"#{row['id']} | {row['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n{row['name']} | {row['group_name']}\nМодуль: {row['module']}\nСтатус: {row['status']}\nОписание: {row['description']}"
        report_message = await update.message.reply_text(text, reply_markup=build_inline_keyboard(row["id"], row["status"], row["module"]))
        await save_report_message(row["id"], report_message.chat_id, report_message.message_id)

async def new_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        return
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT id, created_at, name, group_name, module, description FROM reports WHERE status = 'Новая' ORDER BY id DESC")
            rows = await cursor.fetchall()
    if not rows:
        await update.message.reply_text("Новых заявок нет.")
        return
    await update.message.reply_text("Новые заявки:")
    for row in rows:
        text = f"#{row['id']} | {row['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n{row['name']} | {row['group_name']}\nМодуль: {row['module']}\nОписание: {row['description']}"
        report_message = await update.message.reply_text(text, reply_markup=build_inline_keyboard(row["id"], "Новая", row["module"]))
        await save_report_message(row["id"], report_message.chat_id, report_message.message_id)

async def send_full_report(update: Update, context: ContextTypes.DEFAULT_TYPE, report_id: int):
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM reports WHERE id = %s", (report_id,))
            row = await cursor.fetchone()
    if not row:
        await update.message.reply_text("Заявка с таким ID не найдена.")
        return
    username_text = f"@{row['username']}" if row["username"] else "-"
    text = (
        f"📄 Полная заявка #{row['id']}\n\n"
        f"🕒 Дата: {row['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"👤 ФИО: {row['name']}\n"
        f"🎓 Группа: {row['group_name']}\n"
        f"🧩 Модуль: {row['module']}\n"
        f"📝 Описание:\n{row['description']}\n\n"
        f"📊 Статус: {row['status']}\n"
        f"🆔 Telegram ID: {row['user_id']}\n"
        f"🔗 Username: {username_text}"
    )
    if row["screenshot_file_id"]:
        await update.message.reply_photo(photo=row["screenshot_file_id"], caption=text)
    else:
        await update.message.reply_text(text)

async def report_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id) or not context.args:
        return
    try:
        report_id = int(context.args[0])
        await send_full_report(update, context, report_id)
    except ValueError:
        await update.message.reply_text("ID заявки должен быть числом.")

async def filter_module(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        return
    modules_list = await get_active_modules()
    if not context.args:
        modules_text = "\n".join(f"- {m}" for m in modules_list)
        await update.message.reply_text(f"Использование: /filter_module Платежи\n\nМодули:\n{modules_text}")
        return
    module_name = " ".join(context.args).strip()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT id, created_at, name, group_name, status FROM reports WHERE module = %s ORDER BY id DESC LIMIT 20", (module_name,))
            rows = await cursor.fetchall()
    if not rows:
        await update.message.reply_text(f"По модулю '{module_name}' заявок нет.")
        return
    lines = [f"Заявки по модулю: {module_name}\n"]
    for row in rows:
        lines.append(f"#{row['id']} | {row['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n{row['name']} | {row['group_name']}\nСтатус: {row['status']}\n")
    await update.message.reply_text("\n".join(lines))

async def set_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id) or len(context.args) < 2:
        return
    try:
        report_id = int(context.args[0])
    except ValueError:
        return
    new_status = " ".join(context.args[1:]).strip()
    if new_status in STATUSES:
        await apply_report_status_change(report_id, new_status, user)
        await update.message.reply_text(f"Статус заявки #{report_id} изменён на: {new_status}")

async def take_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id) or not context.args:
        return
    try:
        report_id = int(context.args[0])
        await apply_report_status_change(report_id, "В работе", user)
        await update.message.reply_text(f"🛠 Заявка #{report_id} взята в работу")
    except ValueError:
        pass

async def resolve_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id) or not context.args:
        return
    try:
        report_id = int(context.args[0])
        await apply_report_status_change(report_id, "Решено", user)
        await update.message.reply_text(f"✅ Заявка #{report_id} переведена в статус: Решено")
    except ValueError:
        pass

async def export_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    reports_list = get_reports(limit=None)
    file_stream = build_reports_excel(reports_list)
    await update.message.reply_document(
        document=file_stream,
        filename=f"reports_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        caption="Готово. Вот Excel с заявками."
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if not is_staff(user.id):
        await query.answer("У вас нет доступа.", show_alert=True)
        return

    data = query.data
    if data.startswith("template_"):
        report_id = int(data.split("_")[1])
        await save_report_message(report_id, query.message.chat_id, query.message.message_id)

        async with await get_conn_async() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT id, user_id, module, description FROM reports WHERE id = %s", (report_id,))
                row = await cursor.fetchone()
                if not row:
                    return
                await cursor.execute("UPDATE reports SET status = 'Решено' WHERE id = %s", (report_id,))
            await conn.commit()

        desc = row["description"] or ""
        if "Магистратура" in desc:
            subject = "📚 Информация для поступающих в Магистратуру"
            url = "https://astanait.edu.kz/ru/master"
            template_text = f"Здравствуйте! Актуальную информацию по вопросам поступления в **Магистратуру** Astana IT University вы найдете по ссылке:\n\n👉 {url}"
        elif "Докторантура" in desc or "PhD" in desc:
            subject = "🔬 Информация для поступающих в Докторантуру (PhD)"
            url = "https://astanait.edu.kz/ru/phd"
            template_text = f"Здравствуйте! Подробные правила приема и список документов в **Докторантуру (PhD)** доступны на сайте:\n\n👉 {url}"
        else:
            subject = "🎓 Информация для поступающих на Бакалавриат"
            url = "https://astanait.edu.kz/ru/bachelor"
            template_text = f"Здравствуйте! Правила приема и гранты для поступающих на **Бакалавриат** доступны по ссылке:\n\n👉 {url}"

        if row["user_id"]:
            full_student_message = f"✉️ Ответ по вашей заявке #{report_id} (Приемная комиссия)\n\n{template_text}"
            try:
                sent = await context.bot.send_message(chat_id=row["user_id"], text=full_student_message)
                await save_student_report_message(report_id, row["user_id"], sent.message_id)
                await save_ticket_message(report_id, "staff", f"Шаблон ({user.full_name})", template_text)
            except Exception:
                pass

        await query.edit_message_reply_markup(reply_markup=build_inline_keyboard(report_id, "Решено", row["module"]))
        await sync_report_keyboards(context, report_id, "Решено", skip_chat_id=query.message.chat_id, skip_message_id=query.message.message_id)
        await query.message.reply_text(f"✅ Для заявки #{report_id} отправлен шаблон: {subject}")
        await notify_admins_status_change(context=context, report_id=report_id, module=row["module"], new_status="Решено", actor=user)

    elif data.startswith("take_"):
        report_id = int(data.split("_")[1])
        await apply_report_status_change(report_id, "В работе", user)
        await query.edit_message_reply_markup(reply_markup=build_inline_keyboard(report_id, "В работе"))
        await query.message.reply_text(f"🛠 Заявка #{report_id} взята в работу")

    elif data.startswith("done_"):
        report_id = int(data.split("_")[1])
        await apply_report_status_change(report_id, "Решено", user)
        await query.edit_message_reply_markup(reply_markup=build_inline_keyboard(report_id, "Решено"))
        await query.message.reply_text(f"✅ Заявка #{report_id} решена")

    elif data.startswith("reply_"):
        report_id = int(data.split("_")[1])
        context.user_data["reply_report_id"] = report_id
        await query.message.reply_text(f"Введите сообщение для студента по заявке #{report_id}:")

async def staff_reply_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        return
    report_id = context.user_data.get("reply_report_id")
    if not report_id or not update.message or not update.message.text or update.message.text.startswith("/"):
        return

    success = await send_reply_to_student_from_admin(report_id, update.message.text, user)
    if success:
        await update.message.reply_text("Сообщение отправлено студенту ✅")
    else:
        await update.message.reply_text("Не удалось отправить ❌")
    context.user_data.pop("reply_report_id", None)

async def student_reply_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message
    if not user or not message or not message.text or is_staff(user.id) or context.user_data.get("report_in_progress"):
        return

    ignored = {"Пропустить", "Новые заявки", "Последние заявки", "Поиск по ID", "Фильтр по модулю", "Изменить статус", "Взять в работу", "Отметить решённой", "Выгрузить Excel", "Скрыть меню", "✅ Спасибо, проблема решена", "✉️ Все равно создать заявку"}
    if message.text.strip() in ignored:
        return

    report_id = await get_report_id_from_student_reply(message)
    report = None
    if report_id:
        report = await get_report_async(report_id)
    else:
        report = await get_last_active_report_for_user(user.id)
        if report:
            report_id = report["id"]

    if not report_id or not report:
        return

    await save_ticket_message(report_id, "student", report["name"], message.text)

    username_text = f"@{user.username}" if user.username else "-"
    text = (
        f"💬 Ответ студента по заявке #{report_id}\n\n"
        f"👤 Студент: {report['name']}\n"
        f"🎓 Группа: {report['group_name']}\n"
        f"🧩 Модуль: {report['module']}\n"
        f"📊 Статус: {report['status']}\n\n"
        f"Сообщение:\n{message.text}"
    )
    for admin_id in ADMIN_IDS.union(DEVELOPER_IDS).union(SUPER_ADMIN_IDS):
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=build_inline_keyboard(report_id, report["status"], report["module"]))
        except Exception:
            pass

    await message.reply_text(f"Ваше сообщение по заявке #{report_id} передано сотрудникам ✅")

async def staff_button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        return
    text = update.message.text.strip()
    if text == "Новые заявки":
        await new_reports(update, context)
    elif text == "Последние заявки":
        await list_reports(update, context)
    elif text == "Поиск по ID":
        await update.message.reply_text("Введите номер заявки:")
        return STAFF_REPORT_ID
    elif text == "Фильтр по модулю":
        modules_list = await get_active_modules()
        await update.message.reply_text("Введите название модуля:\n\n" + "\n".join(modules_list))
        return STAFF_FILTER_MODULE
    elif text == "Изменить статус":
        await update.message.reply_text("Введите ID заявки:")
        return STAFF_SET_STATUS_ID
    elif text == "Взять в работу":
        await update.message.reply_text("Введите ID заявки:")
        return STAFF_TAKE_REPORT_ID
    elif text == "Отметить решённой":
        await update.message.reply_text("Введите ID заявки:")
        return STAFF_RESOLVE_REPORT_ID
    elif text == "Выгрузить Excel" and is_admin(user.id):
        await export_excel(update, context)
    elif text == "Скрыть меню":
        await hide_menu(update, context)

async def staff_get_report_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await send_full_report(update, context, int(update.message.text.strip()))
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
    return ConversationHandler.END

async def staff_get_filter_module(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.args = update.message.text.strip().split()
    await filter_module(update, context)
    return ConversationHandler.END

async def staff_get_status_report_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["status_report_id"] = int(update.message.text.strip())
        await update.message.reply_text("Введите новый статус:\nНовая\nВ работе\nРешено")
        return STAFF_SET_STATUS_VALUE
    except ValueError:
        return ConversationHandler.END

async def staff_get_status_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    report_id = context.user_data.pop("status_report_id", None)
    new_status = update.message.text.strip()
    if report_id and new_status in STATUSES:
        await apply_report_status_change(report_id, new_status, update.effective_user)
        await update.message.reply_text(f"Статус заявки #{report_id} изменён на: {new_status}")
    return ConversationHandler.END

async def staff_take_report_by_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await apply_report_status_change(int(update.message.text.strip()), "В работе", update.effective_user)
        await update.message.reply_text("Взято в работу ✅")
    except ValueError:
        pass
    return ConversationHandler.END

async def staff_resolve_report_by_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await apply_report_status_change(int(update.message.text.strip()), "Решено", update.effective_user)
        await update.message.reply_text("Отмечено решенным ✅")
    except ValueError:
        pass
    return ConversationHandler.END

# =========================
# APPLICATION SETUP
# =========================
request_pool = HTTPXRequest(
    connection_pool_size=20,
    read_timeout=30.0,
    write_timeout=30.0,
    connect_timeout=30.0,
    pool_timeout=30.0
)
telegram_app = Application.builder().token(TOKEN).request(request_pool).build()

report_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("report", report_start)],
    states={
        NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
        GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_group)],
        MODULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_module)],
        ACADEMIC_STATUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_academic_status)],
        DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_description)],
        SMART_FAQ_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_smart_faq_decision)],
        SCREENSHOT: [
            MessageHandler(filters.PHOTO, get_screenshot),
            MessageHandler(filters.TEXT & ~filters.COMMAND, get_screenshot),
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

staff_conv_handler = ConversationHandler(
    entry_points=[
        MessageHandler(filters.Regex("^Поиск по ID$"), staff_button_router),
        MessageHandler(filters.Regex("^Фильтр по модулю$"), staff_button_router),
        MessageHandler(filters.Regex("^Изменить статус$"), staff_button_router),
        MessageHandler(filters.Regex("^Взять в работу$"), staff_button_router),
        MessageHandler(filters.Regex("^Отметить решённой$"), staff_button_router),
    ],
    states={
        STAFF_REPORT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_get_report_id)],
        STAFF_FILTER_MODULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_get_filter_module)],
        STAFF_SET_STATUS_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_get_status_report_id)],
        STAFF_SET_STATUS_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_get_status_value)],
        STAFF_TAKE_REPORT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_take_report_by_button)],
        STAFF_RESOLVE_REPORT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_resolve_report_by_button)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

telegram_app.add_error_handler(error_handler)
telegram_app.add_handler(report_conv_handler)
telegram_app.add_handler(staff_conv_handler)
telegram_app.add_handler(CallbackQueryHandler(handle_buttons))

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("faq", faq))
telegram_app.add_handler(CommandHandler("my_role", my_role))
telegram_app.add_handler(CommandHandler("my_reports", my_reports))
telegram_app.add_handler(CommandHandler("restart_bot", restart_bot_cmd))
telegram_app.add_handler(CommandHandler("staff_menu", staff_menu))
telegram_app.add_handler(CommandHandler("new_reports", new_reports))
telegram_app.add_handler(CommandHandler("list_reports", list_reports))
telegram_app.add_handler(CommandHandler("report_by_id", report_by_id))
telegram_app.add_handler(CommandHandler("filter_module", filter_module))
telegram_app.add_handler(CommandHandler("set_status", set_status))
telegram_app.add_handler(CommandHandler("take_report", take_report))
telegram_app.add_handler(CommandHandler("resolve_report", resolve_report))
telegram_app.add_handler(CommandHandler("export_excel", export_excel))
telegram_app.add_handler(CommandHandler("cancel", cancel))

telegram_app.add_handler(MessageHandler(filters.Regex("^Новые заявки$"), staff_button_router))
telegram_app.add_handler(MessageHandler(filters.Regex("^Последние заявки$"), staff_button_router))
telegram_app.add_handler(MessageHandler(filters.Regex("^Выгрузить Excel$"), staff_button_router))
telegram_app.add_handler(MessageHandler(filters.Regex("^Скрыть меню$"), staff_button_router))

telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, staff_reply_router), group=10)
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, student_reply_router), group=20)

# =========================
# FASTAPI ADMIN ROUTES
# =========================
@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if get_admin_username(request):
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "request": request, "error": request.query_params.get("error"),
        "configured": bool(ADMIN_PANEL_PASSWORD), "username": ADMIN_PANEL_USERNAME,
    })

@app.post("/admin/login")
async def admin_login(username: str = Form(...), password: str = Form(...)):
    if not ADMIN_PANEL_PASSWORD:
        return RedirectResponse(url="/admin/login?error=config", status_code=303)
    if hmac.compare_digest(username, ADMIN_PANEL_USERNAME) and hmac.compare_digest(password, ADMIN_PANEL_PASSWORD):
        res = RedirectResponse(url="/admin", status_code=303)
        res.set_cookie(key=ADMIN_SESSION_COOKIE, value=create_admin_session(username), max_age=ADMIN_SESSION_MAX_AGE, httponly=True, secure=ADMIN_COOKIE_SECURE, samesite="lax")
        return res
    return RedirectResponse(url="/admin/login?error=1", status_code=303)

@app.post("/admin/logout")
async def admin_logout():
    res = RedirectResponse(url="/admin/login", status_code=303)
    res.delete_cookie(ADMIN_SESSION_COOKIE)
    return res

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, status: str | None = None, module: str | None = None, q: str | None = None, page: int = 1):
    admin_username = get_admin_username(request)
    if not admin_username:
        return admin_redirect()
    if status and status not in STATUSES:
        status = None
    if page < 1:
        page = 1

    all_modules = await get_active_modules()
    per_page = 20
    counts = await get_dashboard_counts_async()
    reports, total_filtered = await get_reports_async(status_filter=status, module_filter=module, search=q, page=page, per_page=per_page)
    total_pages = math.ceil(total_filtered / per_page) if total_filtered > 0 else 1

    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request, "admin_username": admin_username, "counts": counts, "reports": reports,
        "statuses": STATUSES, "modules": all_modules, "selected_status": status or "", "selected_module": module or "",
        "query": q or "", "active_page": "dashboard", "message": request.query_params.get("message"),
        "current_page": page, "total_pages": total_pages, "total_filtered": total_filtered
    })

@app.get("/admin/reports")
async def admin_reports_redirect():
    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/reports/{report_id}", response_class=HTMLResponse)
async def admin_report_detail(request: Request, report_id: int):
    admin_username = get_admin_username(request)
    if not admin_username:
        return admin_redirect()
    report = await get_report_async(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Заявка не найдена")

    chat_messages = []
    try:
        chat_messages = await get_ticket_messages(report_id)
    except Exception as e:
        logger.error(f"Ошибка получения сообщений тикета: {e}")

    # Загружаем шаблоны быстрых ответов
    templates_list = []
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT id, title, text FROM quick_templates ORDER BY id ASC")
            templates_list = await cursor.fetchall()

    return templates.TemplateResponse(request, "report_detail.html", {
        "request": request, "admin_username": admin_username, "report": report,
        "chat_messages": chat_messages, "statuses": STATUSES, "quick_templates": templates_list,
        "active_page": "reports", "message": request.query_params.get("message"), "error": request.query_params.get("error"),
    })

# API: Live-чат (JSON polling новых сообщений)
@app.get("/admin/reports/{report_id}/messages/poll")
async def admin_poll_messages(request: Request, report_id: int, last_id: int = 0):
    if not get_admin_username(request):
        return JSONResponse(status_code=403, content={"error": "unauthorized"})
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                SELECT id, sender_type, sender_name, message_text, created_at
                FROM ticket_messages
                WHERE report_id = %s AND id > %s
                ORDER BY id ASC
            """, (report_id, last_id))
            rows = await cursor.fetchall()
            messages = []
            for r in rows:
                dt_str = r["created_at"].strftime('%H:%M | %d.%m.%Y') if hasattr(r["created_at"], "strftime") else str(r["created_at"])
                messages.append({
                    "id": r["id"],
                    "sender_type": r["sender_type"],
                    "sender_name": r["sender_name"],
                    "message_text": r["message_text"],
                    "created_at": dt_str
                })
            return JSONResponse(content={"messages": messages})

@app.get("/admin/reports/{report_id}/screenshot")
async def admin_report_screenshot(request: Request, report_id: int):
    if not get_admin_username(request):
        return admin_redirect()
    report = await get_report_async(report_id)
    if not report or not report["screenshot_file_id"]:
        raise HTTPException(status_code=404, detail="Скриншот не найден")
    try:
        file = await telegram_app.bot.get_file(report["screenshot_file_id"])
        image_bytes = await file.download_as_bytearray()
    except Exception:
        raise HTTPException(status_code=502, detail="Не удалось загрузить скриншот")
    return Response(content=bytes(image_bytes), media_type="image/jpeg")

@app.post("/admin/reports/{report_id}/status")
async def admin_report_status(request: Request, report_id: int, status: str = Form(...)):
    admin_username = get_admin_username(request)
    if not admin_username:
        return admin_redirect()
    await apply_report_status_change(report_id, status, WebActor(admin_username))
    return RedirectResponse(url=f"/admin/reports/{report_id}?message=status", status_code=303)

@app.post("/admin/reports/{report_id}/reply")
async def admin_report_reply(request: Request, report_id: int, message: str = Form(...)):
    admin_username = get_admin_username(request)
    if not admin_username:
        return admin_redirect()
    message_text = message.strip()
    if not message_text:
        return RedirectResponse(url=f"/admin/reports/{report_id}?error=reply_empty", status_code=303)
    success = await send_reply_to_student_from_admin(report_id, message_text, WebActor(admin_username))
    if not success:
        return RedirectResponse(url=f"/admin/reports/{report_id}?error=reply", status_code=303)
    return RedirectResponse(url=f"/admin/reports/{report_id}?message=reply", status_code=303)

# -----------------
# УПРАВЛЕНИЕ МОДУЛЯМИ
# -----------------
@app.get("/admin/modules", response_class=HTMLResponse)
async def admin_modules_page(request: Request):
    admin_username = get_admin_username(request)
    if not admin_username:
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT id, name, is_active, created_at FROM system_modules ORDER BY id ASC")
            modules_list = await cursor.fetchall()
    return templates.TemplateResponse(request, "modules.html", {
        "request": request, "admin_username": admin_username, "modules": modules_list,
        "active_page": "modules", "message": request.query_params.get("message")
    })

@app.post("/admin/modules/add")
async def admin_modules_add(request: Request, name: str = Form(...)):
    if not get_admin_username(request):
        return admin_redirect()
    name = name.strip()
    if name:
        async with await get_conn_async() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("INSERT INTO system_modules (name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
            await conn.commit()
    return RedirectResponse(url="/admin/modules?message=added", status_code=303)

@app.post("/admin/modules/{module_id}/toggle")
async def admin_modules_toggle(request: Request, module_id: int):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("UPDATE system_modules SET is_active = NOT is_active WHERE id = %s", (module_id,))
        await conn.commit()
    return RedirectResponse(url="/admin/modules?message=updated", status_code=303)

@app.post("/admin/modules/{module_id}/delete")
async def admin_modules_delete(request: Request, module_id: int):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("DELETE FROM system_modules WHERE id = %s", (module_id,))
        await conn.commit()
    return RedirectResponse(url="/admin/modules?message=deleted", status_code=303)

# -----------------
# УПРАВЛЕНИЕ ШАБЛОНАМИ
# -----------------
@app.get("/admin/templates", response_class=HTMLResponse)
async def admin_templates_page(request: Request):
    admin_username = get_admin_username(request)
    if not admin_username:
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT id, title, text, created_at FROM quick_templates ORDER BY id ASC")
            templates_list = await cursor.fetchall()
    return templates.TemplateResponse(request, "templates.html", {
        "request": request, "admin_username": admin_username, "templates": templates_list,
        "active_page": "templates", "message": request.query_params.get("message")
    })

@app.post("/admin/templates/add")
async def admin_templates_add(request: Request, title: str = Form(...), text: str = Form(...)):
    if not get_admin_username(request):
        return admin_redirect()
    title, text = title.strip(), text.strip()
    if title and text:
        async with await get_conn_async() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("INSERT INTO quick_templates (title, text) VALUES (%s, %s)", (title, text))
            await conn.commit()
    return RedirectResponse(url="/admin/templates?message=added", status_code=303)

@app.post("/admin/templates/{tpl_id}/delete")
async def admin_templates_delete(request: Request, tpl_id: int):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("DELETE FROM quick_templates WHERE id = %s", (tpl_id,))
        await conn.commit()
    return RedirectResponse(url="/admin/templates?message=deleted", status_code=303)

# -----------------
# SMART FAQ (АВТООТВЕТЫ)
# -----------------
@app.get("/admin/smart-faq", response_class=HTMLResponse)
async def admin_smart_faq_page(request: Request):
    admin_username = get_admin_username(request)
    if not admin_username:
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT id, title, keywords, reply_text, is_active FROM smart_faq_rules ORDER BY id ASC")
            rules = await cursor.fetchall()
    return templates.TemplateResponse(request, "smart_faq.html", {
        "request": request, "admin_username": admin_username, "rules": rules,
        "active_page": "smart_faq", "message": request.query_params.get("message")
    })

@app.post("/admin/smart-faq/add")
async def admin_smart_faq_add(request: Request, title: str = Form(...), keywords: str = Form(...), reply_text: str = Form(...)):
    if not get_admin_username(request):
        return admin_redirect()
    title, keywords, reply_text = title.strip(), keywords.strip(), reply_text.strip()
    if title and keywords and reply_text:
        async with await get_conn_async() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("INSERT INTO smart_faq_rules (title, keywords, reply_text) VALUES (%s, %s, %s)", (title, keywords, reply_text))
            await conn.commit()
    return RedirectResponse(url="/admin/smart-faq?message=added", status_code=303)

@app.post("/admin/smart-faq/{rule_id}/toggle")
async def admin_smart_faq_toggle(request: Request, rule_id: int):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("UPDATE smart_faq_rules SET is_active = NOT is_active WHERE id = %s", (rule_id,))
        await conn.commit()
    return RedirectResponse(url="/admin/smart-faq?message=updated", status_code=303)

@app.post("/admin/smart-faq/{rule_id}/delete")
async def admin_smart_faq_delete(request: Request, rule_id: int):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("DELETE FROM smart_faq_rules WHERE id = %s", (rule_id,))
        await conn.commit()
    return RedirectResponse(url="/admin/smart-faq?message=deleted", status_code=303)

# -----------------
# SYSTEM & EXCEL
# -----------------
@app.get("/admin/reports/export.xlsx")
async def admin_export_reports(request: Request):
    if not get_admin_username(request):
        return admin_redirect()
    reports_list = get_reports(limit=None)
    loop = asyncio.get_running_loop()
    file_stream = await loop.run_in_executor(None, build_reports_excel, reports_list)
    filename = f"reports_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(file_stream, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.post("/admin/system/restart")
async def admin_system_restart(request: Request):
    admin_username = get_admin_username(request)
    if not admin_username:
        return admin_redirect()
    subprocess.Popen(["/usr/bin/sudo", "/usr/bin/systemctl", "restart", "mydu-bot"])
    return RedirectResponse(url="/admin?message=restarting", status_code=303)

@app.on_event("startup")
async def on_startup():
    await init_db()
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True, bootstrap_retries=-1)
    logger.info("Бот успешно запущен в режиме Polling!")

@app.on_event("shutdown")
async def on_shutdown():
    if telegram_app.updater.running:
        await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()

@app.get("/")
async def healthcheck():
    return {"status": "ok"}
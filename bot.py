import os
import hashlib
import hmac
import time
from datetime import datetime
from io import BytesIO
from fastapi.responses import HTMLResponse

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
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
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

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

ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
}
DEVELOPER_IDS = {
    int(x.strip()) for x in os.getenv("DEVELOPER_IDS", "").split(",") if x.strip()
}

# СЮДА ВСТАВЬ СВОЙ TELEGRAM ID, ЧТОБЫ ПОЛУЧАТЬ КОПИИ ВСЕХ СООБЩЕНИЙ СОТРУДНИКОВ
SUPER_ADMIN_IDS = {
    548200976
}

NAME, GROUP, MODULE, DESCRIPTION, SCREENSHOT = range(5)
STAFF_REPORT_ID = 100
STAFF_FILTER_MODULE = 101
STAFF_SET_STATUS_ID = 102
STAFF_SET_STATUS_VALUE = 103
STAFF_TAKE_REPORT_ID = 104
STAFF_RESOLVE_REPORT_ID = 105

MODULES = [
    "Регистрация на дисциплины",
    "Общежитие",
    "Платежи",
    "Другое",
]

STATUSES = ["Новая", "В работе", "Решено"]


# =========================
# DATABASE
# =========================
def get_conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
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
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS report_messages (
                    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                    chat_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    PRIMARY KEY (report_id, chat_id, message_id)
                )
            """)
        conn.commit()


# =========================
# ROLES
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


# =========================
# UI
# =========================
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
    return ReplyKeyboardMarkup(
        [["Пропустить"]],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def build_inline_keyboard(report_id: int, status: str = "Новая") -> InlineKeyboardMarkup:
    rows = []

    if status == "Новая":
        rows.append([
            InlineKeyboardButton("🛠 В работу", callback_data=f"take_{report_id}"),
            InlineKeyboardButton("✅ Решено", callback_data=f"done_{report_id}")
        ])
    elif status == "В работе":
        rows.append([
            InlineKeyboardButton("✅ Решено", callback_data=f"done_{report_id}")
        ])

    rows.append([
        InlineKeyboardButton("✉️ Ответить студенту", callback_data=f"reply_{report_id}")
    ])

    return InlineKeyboardMarkup(rows)


async def set_commands(application: Application):
    student_commands = [
        BotCommand("start", "Главное сообщение"),
        BotCommand("faq", "Частые вопросы"),
        BotCommand("support", "Связаться с поддержкой"),
        BotCommand("report", "Отправить ошибку"),
        BotCommand("my_role", "Показать мою роль"),
        BotCommand("my_reports", "Мои заявки"),
    ]

    staff_commands = [
        BotCommand("start", "Главное сообщение"),
        BotCommand("faq", "Частые вопросы"),
        BotCommand("support", "Связаться с поддержкой"),
        BotCommand("report", "Отправить ошибку"),
        BotCommand("my_role", "Показать мою роль"),
        BotCommand("my_reports", "Мои заявки"),
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
    ]

    await application.bot.set_my_commands(
        student_commands,
        scope=BotCommandScopeDefault()
    )

    for dev_id in DEVELOPER_IDS:
        await application.bot.set_my_commands(
            staff_commands,
            scope=BotCommandScopeChat(chat_id=dev_id)
        )

    for admin_id in ADMIN_IDS:
        await application.bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=admin_id)
        )


# =========================
# HELPERS
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"Ошибка: {context.error}")


def build_report_text(
    report_id: int,
    created_at: datetime,
    name: str,
    group_name: str,
    module: str,
    description: str,
    status: str,
    user_id: int | None,
    username: str | None,
) -> str:
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


def save_report_message(report_id: int, chat_id: int, message_id: int):
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO report_messages (report_id, chat_id, message_id)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (report_id, chat_id, message_id)
            )
        conn.commit()


async def sync_report_keyboards(
    context: ContextTypes.DEFAULT_TYPE,
    report_id: int,
    status: str,
    skip_chat_id: int | None = None,
    skip_message_id: int | None = None,
):
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT chat_id, message_id
                FROM report_messages
                WHERE report_id = %s
                """,
                (report_id,)
            )
            rows = cursor.fetchall()

    keyboard = build_inline_keyboard(report_id, status)
    for row in rows:
        if row["chat_id"] == skip_chat_id and row["message_id"] == skip_message_id:
            continue

        try:
            await context.bot.edit_message_reply_markup(
                chat_id=row["chat_id"],
                message_id=row["message_id"],
                reply_markup=keyboard,
            )
        except Exception as e:
            print(
                f"Не удалось обновить кнопки заявки #{report_id} "
                f"для чата {row['chat_id']}, сообщения {row['message_id']}: {e}"
            )


async def notify_admins_status_change(
    context: ContextTypes.DEFAULT_TYPE,
    report_id: int,
    module: str | None,
    new_status: str,
    actor,
):
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
        except Exception as e:
            print(f"Не удалось уведомить админа {admin_id}: {e}")


class WebActor:
    def __init__(self, username: str):
        self.id = None
        self.username = None
        self.full_name = f"Веб-панель: {username}"
        self.role_name = "Веб-панель"


def create_admin_session(username: str) -> str:
    timestamp = str(int(time.time()))
    payload = f"{username}|{timestamp}"
    signature = hmac.new(
        ADMIN_SESSION_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}|{signature}"


def verify_admin_session(session_value: str | None) -> str | None:
    if not session_value:
        return None

    parts = session_value.split("|")
    if len(parts) != 3:
        return None

    username, timestamp, signature = parts
    payload = f"{username}|{timestamp}"
    expected_signature = hmac.new(
        ADMIN_SESSION_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        return None

    try:
        session_age = int(time.time()) - int(timestamp)
    except ValueError:
        return None

    if session_age > ADMIN_SESSION_MAX_AGE:
        return None

    if username != ADMIN_PANEL_USERNAME:
        return None

    return username


def get_admin_username(request: Request) -> str | None:
    return verify_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE))


def admin_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/login", status_code=303)


def get_dashboard_counts() -> dict:
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'Новая') AS new_count,
                    COUNT(*) FILTER (WHERE status = 'В работе') AS in_progress_count,
                    COUNT(*) FILTER (WHERE status = 'Решено') AS resolved_count
                FROM reports
            """)
            return cursor.fetchone()


def get_reports(
    status_filter: str | None = None,
    module_filter: str | None = None,
    search: str | None = None,
    limit: int | None = 100,
) -> list[dict]:
    conditions = []
    params = []

    if status_filter and status_filter in STATUSES:
        conditions.append("status = %s")
        params.append(status_filter)

    if module_filter and module_filter in MODULES:
        conditions.append("module = %s")
        params.append(module_filter)

    if search:
        search_value = search.strip()
        if search_value:
            like_value = f"%{search_value}%"
            conditions.append("""
                (
                    CAST(id AS TEXT) ILIKE %s
                    OR name ILIKE %s
                    OR group_name ILIKE %s
                    OR COALESCE(username, '') ILIKE %s
                    OR module ILIKE %s
                    OR description ILIKE %s
                )
            """)
            params.extend([like_value] * 6)

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    limit_sql = "LIMIT %s" if limit else ""
    if limit:
        params.append(limit)

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, created_at, user_id, username, name, group_name, module,
                       description, screenshot_file_id, status
                FROM reports
                {where_sql}
                ORDER BY id DESC
                {limit_sql}
                """,
                params,
            )
            return cursor.fetchall()


def get_report(report_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, created_at, user_id, username, name, group_name, module,
                       description, screenshot_file_id, status
                FROM reports
                WHERE id = %s
            """, (report_id,))
            return cursor.fetchone()


def update_report_status_in_db(report_id: int, new_status: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, user_id, module
                FROM reports
                WHERE id = %s
            """, (report_id,))
            report = cursor.fetchone()

            if not report:
                return None

            cursor.execute(
                "UPDATE reports SET status = %s WHERE id = %s",
                (new_status, report_id),
            )
        conn.commit()

    return report


async def notify_student_status(report: dict, report_id: int, new_status: str):
    if not report["user_id"]:
        return

    if new_status == "В работе":
        text = (
            f"🛠 Обновление по вашей заявке #{report_id}\n\n"
            f"Модуль: {report['module']}\n"
            "Статус: В работе\n\n"
            "Ваше обращение принято сотрудниками и уже находится в обработке."
        )
    elif new_status == "Решено":
        text = (
            f"✅ Обновление по вашей заявке #{report_id}\n\n"
            f"Модуль: {report['module']}\n"
            "Статус: Решено\n\n"
            "Здравствуйте! Ваша проблема была обработана и отмечена как решённая.\n"
            "Пожалуйста, проверьте работу модуля снова.\n\n"
            "Если ошибка всё ещё сохраняется, отправьте новую заявку через /report."
        )
    else:
        return

    try:
        await telegram_app.bot.send_message(chat_id=report["user_id"], text=text)
    except Exception as e:
        print(f"Не удалось уведомить студента: {e}")


async def apply_report_status_change(report_id: int, new_status: str, actor) -> bool:
    if new_status not in STATUSES:
        return False

    report = update_report_status_in_db(report_id, new_status)
    if not report:
        return False

    await sync_report_keyboards(telegram_app, report_id, new_status)
    await notify_admins_status_change(
        context=telegram_app,
        report_id=report_id,
        module=report["module"],
        new_status=new_status,
        actor=actor,
    )
    await notify_student_status(report, report_id, new_status)
    return True


async def send_reply_to_student_from_admin(report_id: int, message_text: str, actor) -> bool:
    report = get_report(report_id)
    if not report or not report["user_id"]:
        return False

    try:
        await telegram_app.bot.send_message(
            chat_id=report["user_id"],
            text=(
                f"📩 Сообщение по вашей заявке #{report_id}\n\n"
                f"{message_text}"
            ),
        )
    except Exception as e:
        print(f"Не удалось отправить сообщение студенту: {e}")
        return False

    log_text = (
        "📩 Сотрудник ответил студенту\n\n"
        f"👤 Отправитель: {actor.full_name}\n"
        f"📌 Заявка: #{report_id}\n\n"
        f"💬 Сообщение:\n{message_text}"
    )

    for admin_id in ADMIN_IDS.union(SUPER_ADMIN_IDS):
        try:
            await telegram_app.bot.send_message(chat_id=admin_id, text=log_text)
        except Exception as e:
            print(f"Ошибка отправки лога: {e}")

    return True


def build_reports_excel(rows: list[dict]) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Заявки"

    ws.append([
        "ID",
        "Дата",
        "Telegram ID",
        "Username",
        "ФИО",
        "Группа",
        "Модуль",
        "Описание",
        "Статус",
        "Есть скриншот",
    ])

    for row in rows:
        ws.append([
            row["id"],
            row["created_at"].strftime("%Y-%m-%d %H:%M:%S"),
            row["user_id"],
            row["username"],
            row["name"],
            row["group_name"],
            row["module"],
            row["description"],
            row["status"],
            "Да" if row["screenshot_file_id"] else "Нет",
        ])

    for column in ws.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            value = str(cell.value) if cell.value is not None else ""
            if len(value) > max_length:
                max_length = len(value)
        ws.column_dimensions[column_letter].width = min(max_length + 2, 40)

    file_stream = BytesIO()
    wb.save(file_stream)
    file_stream.seek(0)
    return file_stream


# =========================
# STUDENT COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    role = get_role_name(user.id)

    text = (
        "✨ Добро пожаловать в бот поддержки студентов!\n\n"
        "Здесь можно быстро отправить сообщение об ошибке, сбое или проблеме в модуле.\n\n"
        "📌 Что можно сделать:\n"
        "• /report — отправить заявку\n"
        "• /faq — посмотреть частые вопросы\n"
        "• /support — резервная связь с поддержкой\n"
        "• /my_role — узнать свою роль\n"
        "• /my_reports — посмотреть свои заявки\n\n"
        "⚠️ Писать напрямую в поддержку нужно только если бот не отвечает на /start и(или) /report.\n\n"
        f"👤 Ваша роль: {role}"
    )

    if is_staff(user.id):
        text += "\n\n🛠 Для сотрудников доступно:\n/staff_menu — открыть меню сотрудника"

    await update.message.reply_text(text)


async def faq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 Частые вопросы\n\n"
        "1. Как отправить ошибку?\n"
        "— Используйте команду /report и заполните все шаги.\n\n"
        "2. Нужно ли прикладывать скриншот?\n"
        "— Желательно да. Так проблему проще понять.\n\n"
        "3. Что писать в описании?\n"
        "— Напишите, что именно не работает, в каком модуле и что вы делали до ошибки.\n\n"
        "4. Можно ли отправить без скриншота?\n"
        "— Да. На этапе скриншота нажмите кнопку: Пропустить.\n\n"
        "5. Что делать, если бот не ответил?\n"
        "— Используйте /support."
    )
    await update.message.reply_text(text)


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📞 Поддержка\n\n"
        "Если бот не прислал вам ответ на команду /start и(или) /report, "
        "либо вы не получили подтверждение об успешной отправке заявки, "
        "тогда вы можете написать в поддержку:\n\n"
        "👉 @ppwmdk\n\n"
        "При обращении желательно указать:\n"
        "• ФИО\n"
        "• группу\n"
        "• модуль\n"
        "• описание проблемы\n"
        "• скриншот\n\n"
        "По обычным обращениям, пожалуйста, используйте сам бот."
    )


async def my_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    await update.message.reply_text(f"Ваша роль: {get_role_name(user.id)}")


async def my_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, created_at, module, status
                FROM reports
                WHERE user_id = %s
                ORDER BY id DESC
                LIMIT 20
            """, (user.id,))
            rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("У вас пока нет отправленных заявок.")
        return

    lines = ["📋 Ваши заявки:\n"]
    for row in rows:
        lines.append(
            f"#{row['id']} | {row['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Модуль: {row['module']}\n"
            f"Статус: {row['status']}\n"
        )

    await update.message.reply_text("\n".join(lines))


# =========================
# REPORT FLOW
# =========================
async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Введите ФИО:")
    return NAME


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("Введите группу:")
    return GROUP


async def get_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["group"] = update.message.text.strip()

    keyboard = [
        ["Регистрация на дисциплины", "Общежитие"],
        ["Платежи", "Другое"]
    ]

    await update.message.reply_text(
        "Выберите модуль:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )
    return MODULE


async def get_module(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["module"] = update.message.text.strip()
    await update.message.reply_text(
        "Опишите проблему:",
        reply_markup=ReplyKeyboardRemove()
    )
    return DESCRIPTION


async def get_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["description"] = update.message.text.strip()

    await update.message.reply_text(
        "Теперь отправьте скриншот или нажмите кнопку ниже.",
        reply_markup=get_skip_screenshot_keyboard()
    )
    return SCREENSHOT


async def get_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    screenshot_file_id = None
    text_value = update.message.text.strip().lower() if update.message.text else ""

    if update.message.photo:
        screenshot_file_id = update.message.photo[-1].file_id
    elif text_value == "пропустить":
        screenshot_file_id = None
    else:
        await update.message.reply_text(
            "Пожалуйста, отправьте скриншот или нажмите кнопку: Пропустить",
            reply_markup=get_skip_screenshot_keyboard()
        )
        return SCREENSHOT

    user = update.effective_user
    created_at = datetime.now()

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO reports (
                    created_at, user_id, username, name, group_name, module, description, screenshot_file_id, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    created_at,
                    user.id if user else None,
                    user.username if user and user.username else None,
                    context.user_data["name"],
                    context.user_data["group"],
                    context.user_data["module"],
                    context.user_data["description"],
                    screenshot_file_id,
                    "Новая",
                )
            )
            report_id = cursor.fetchone()["id"]
        conn.commit()

    report_text = build_report_text(
        report_id=report_id,
        created_at=created_at,
        name=context.user_data["name"],
        group_name=context.user_data["group"],
        module=context.user_data["module"],
        description=context.user_data["description"],
        status="Новая",
        user_id=user.id if user else None,
        username=user.username if user else None,
    )

    keyboard = build_inline_keyboard(report_id, "Новая")
    recipients = ADMIN_IDS.union(DEVELOPER_IDS)

    for staff_id in recipients:
        try:
            if screenshot_file_id:
                staff_message = await context.bot.send_photo(
                    chat_id=staff_id,
                    photo=screenshot_file_id,
                    caption=report_text,
                    reply_markup=keyboard
                )
            else:
                staff_message = await context.bot.send_message(
                    chat_id=staff_id,
                    text=report_text,
                    reply_markup=keyboard
                )
            save_report_message(report_id, staff_id, staff_message.message_id)
        except Exception as e:
            print(f"Ошибка отправки сотруднику {staff_id}: {e}")

    await update.message.reply_text(
        "✅ Ваша заявка успешно отправлена!\n\n"
        "Спасибо, что сообщили о проблеме.\n\n"
        "Если бот НЕ прислал вам это подтверждение после команды /report, "
        "или не отвечает на /start, только в этом случае можно написать в поддержку:\n"
        "👉 @ppwmdk",
        reply_markup=ReplyKeyboardRemove()
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data.pop("reply_report_id", None)
    await update.message.reply_text(
        "Действие отменено.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# =========================
# STAFF MENU / COMMANDS
# =========================
async def staff_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        await update.message.reply_text("У вас нет доступа к меню сотрудника.")
        return

    await update.message.reply_text(
        "Меню сотрудника открыто.",
        reply_markup=get_staff_keyboard(user.id)
    )


async def hide_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Меню скрыто.",
        reply_markup=ReplyKeyboardRemove()
    )


async def list_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, created_at, name, group_name, module, description, status
                FROM reports
                ORDER BY id DESC
                LIMIT 10
            """)
            rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("Заявок пока нет.")
        return

    await update.message.reply_text("Последние заявки:")

    for row in rows:
        text = (
            f"#{row['id']} | {row['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{row['name']} | {row['group_name']}\n"
            f"Модуль: {row['module']}\n"
            f"Статус: {row['status']}\n"
            f"Описание: {row['description']}"
        )

        report_message = await update.message.reply_text(
            text,
            reply_markup=build_inline_keyboard(row["id"], row["status"])
        )
        save_report_message(row["id"], report_message.chat_id, report_message.message_id)


async def new_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, created_at, name, group_name, module, description
                FROM reports
                WHERE status = %s
                ORDER BY id DESC
            """, ("Новая",))
            rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("Новых заявок нет. Все заявки уже взяты в работу или закрыты.")
        return

    await update.message.reply_text("Новые заявки, ещё не взятые в работу:")

    for row in rows:
        text = (
            f"#{row['id']} | {row['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{row['name']} | {row['group_name']}\n"
            f"Модуль: {row['module']}\n"
            f"Описание: {row['description']}"
        )

        report_message = await update.message.reply_text(
            text,
            reply_markup=build_inline_keyboard(row["id"], "Новая")
        )
        save_report_message(row["id"], report_message.chat_id, report_message.message_id)


async def send_full_report(update: Update, context: ContextTypes.DEFAULT_TYPE, report_id: int):
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, created_at, user_id, username, name, group_name, module, description, screenshot_file_id, status
                FROM reports
                WHERE id = %s
            """, (report_id,))
            row = cursor.fetchone()

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
    if not user or not is_staff(user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    if not context.args:
        await update.message.reply_text("Использование:\n/report_by_id 5")
        return

    try:
        report_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID заявки должен быть числом.")
        return

    await send_full_report(update, context, report_id)


async def filter_module(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    if not context.args:
        modules_text = "\n".join(f"- {m}" for m in MODULES)
        await update.message.reply_text(
            "Напишите команду так:\n"
            "/filter_module Платежи\n\n"
            "Доступные модули:\n"
            f"{modules_text}"
        )
        return

    module_name = " ".join(context.args).strip()

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, created_at, name, group_name, status
                FROM reports
                WHERE module = %s
                ORDER BY id DESC
                LIMIT 20
            """, (module_name,))
            rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text(f"По модулю '{module_name}' заявок нет.")
        return

    lines = [f"Заявки по модулю: {module_name}\n"]
    for row in rows:
        lines.append(
            f"#{row['id']} | {row['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{row['name']} | {row['group_name']}\n"
            f"Статус: {row['status']}\n"
        )

    await update.message.reply_text("\n".join(lines))


async def set_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование:\n"
            "/set_status 5 В работе\n\n"
            "Статусы:\n"
            "- Новая\n"
            "- В работе\n"
            "- Решено"
        )
        return

    try:
        report_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID заявки должен быть числом.")
        return

    new_status = " ".join(context.args[1:]).strip()

    if new_status not in STATUSES:
        await update.message.reply_text("Недопустимый статус.")
        return

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, module FROM reports WHERE id = %s", (report_id,))
            report = cursor.fetchone()

            if not report:
                await update.message.reply_text("Заявка с таким ID не найдена.")
                return

            cursor.execute(
                "UPDATE reports SET status = %s WHERE id = %s",
                (new_status, report_id)
            )
        conn.commit()

    await update.message.reply_text(
        f"Статус заявки #{report_id} изменён на: {new_status}"
    )
    await sync_report_keyboards(context, report_id, new_status)
    await notify_admins_status_change(
        context=context,
        report_id=report_id,
        module=report["module"],
        new_status=new_status,
        actor=user,
    )


async def take_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    if not context.args:
        await update.message.reply_text("Использование:\n/take_report 5")
        return

    try:
        report_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID заявки должен быть числом.")
        return

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, user_id, module
                FROM reports
                WHERE id = %s
            """, (report_id,))
            row = cursor.fetchone()

            if not row:
                await update.message.reply_text("Заявка не найдена.")
                return

            cursor.execute(
                "UPDATE reports SET status = %s WHERE id = %s",
                ("В работе", report_id)
            )
        conn.commit()

    await update.message.reply_text(f"🛠 Заявка #{report_id} переведена в статус: В работе")
    await sync_report_keyboards(context, report_id, "В работе")
    await notify_admins_status_change(
        context=context,
        report_id=report_id,
        module=row["module"],
        new_status="В работе",
        actor=user,
    )

    if row["user_id"]:
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    f"🛠 Обновление по вашей заявке #{report_id}\n\n"
                    f"Модуль: {row['module']}\n"
                    "Статус: В работе\n\n"
                    "Ваше обращение принято сотрудниками и уже находится в обработке."
                )
            )
        except Exception as e:
            print(f"Не удалось уведомить студента: {e}")


async def resolve_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    if not context.args:
        await update.message.reply_text("Использование:\n/resolve_report 5")
        return

    try:
        report_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID заявки должен быть числом.")
        return

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, user_id, module
                FROM reports
                WHERE id = %s
            """, (report_id,))
            row = cursor.fetchone()

            if not row:
                await update.message.reply_text("Заявка не найдена.")
                return

            cursor.execute(
                "UPDATE reports SET status = %s WHERE id = %s",
                ("Решено", report_id)
            )
        conn.commit()

    await update.message.reply_text(f"✅ Заявка #{report_id} переведена в статус: Решено")
    await sync_report_keyboards(context, report_id, "Решено")
    await notify_admins_status_change(
        context=context,
        report_id=report_id,
        module=row["module"],
        new_status="Решено",
        actor=user,
    )

    if row["user_id"]:
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    f"✅ Обновление по вашей заявке #{report_id}\n\n"
                    f"Модуль: {row['module']}\n"
                    "Статус: Решено\n\n"
                    "Здравствуйте! Ваша проблема была обработана и отмечена как решённая.\n"
                    "Пожалуйста, проверьте работу модуля снова.\n\n"
                    "Если ошибка всё ещё сохраняется, отправьте новую заявку через /report."
                )
            )
        except Exception as e:
            print(f"Не удалось уведомить студента: {e}")


async def export_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("Только админ может выгружать Excel.")
        return

    file_stream = build_reports_excel(get_reports(limit=None))

    await update.message.reply_document(
        document=file_stream,
        filename=f"reports_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        caption="Готово. Вот Excel с заявками."
    )


# =========================
# INLINE BUTTONS
# =========================
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    if not is_staff(user.id):
        await query.answer("У вас нет доступа.", show_alert=True)
        return

    data = query.data

    if data.startswith("take_"):
        report_id = int(data.split("_")[1])
        save_report_message(report_id, query.message.chat_id, query.message.message_id)

        with get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT id, user_id, module
                    FROM reports
                    WHERE id = %s
                """, (report_id,))
                row = cursor.fetchone()

                if not row:
                    await query.message.reply_text("Заявка не найдена.")
                    return

                cursor.execute(
                    "UPDATE reports SET status = %s WHERE id = %s",
                    ("В работе", report_id)
                )
            conn.commit()

        await query.edit_message_reply_markup(
            reply_markup=build_inline_keyboard(report_id, "В работе")
        )
        await sync_report_keyboards(
            context,
            report_id,
            "В работе",
            skip_chat_id=query.message.chat_id,
            skip_message_id=query.message.message_id,
        )
        await query.message.reply_text(f"🛠 Заявка #{report_id} взята в работу")
        await notify_admins_status_change(
            context=context,
            report_id=report_id,
            module=row["module"],
            new_status="В работе",
            actor=user,
        )

        if row["user_id"]:
            try:
                await context.bot.send_message(
                    chat_id=row["user_id"],
                    text=(
                        f"🛠 Обновление по вашей заявке #{report_id}\n\n"
                        f"Модуль: {row['module']}\n"
                        "Статус: В работе\n\n"
                        "Ваше обращение принято сотрудниками и уже находится в обработке."
                    )
                )
            except Exception as e:
                print(f"Не удалось уведомить студента: {e}")

    elif data.startswith("done_"):
        report_id = int(data.split("_")[1])
        save_report_message(report_id, query.message.chat_id, query.message.message_id)

        with get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT id, user_id, module
                    FROM reports
                    WHERE id = %s
                """, (report_id,))
                row = cursor.fetchone()

                if not row:
                    await query.message.reply_text("Заявка не найдена.")
                    return

                cursor.execute(
                    "UPDATE reports SET status = %s WHERE id = %s",
                    ("Решено", report_id)
                )
            conn.commit()

        await query.edit_message_reply_markup(
            reply_markup=build_inline_keyboard(report_id, "Решено")
        )
        await sync_report_keyboards(
            context,
            report_id,
            "Решено",
            skip_chat_id=query.message.chat_id,
            skip_message_id=query.message.message_id,
        )
        await query.message.reply_text(f"✅ Заявка #{report_id} решена")
        await notify_admins_status_change(
            context=context,
            report_id=report_id,
            module=row["module"],
            new_status="Решено",
            actor=user,
        )

        if row["user_id"]:
            try:
                await context.bot.send_message(
                    chat_id=row["user_id"],
                    text=(
                        f"✅ Обновление по вашей заявке #{report_id}\n\n"
                        f"Модуль: {row['module']}\n"
                        "Статус: Решено\n\n"
                        "Здравствуйте! Ваша проблема была обработана и отмечена как решённая.\n"
                        "Пожалуйста, проверьте работу модуля снова.\n\n"
                        "Если ошибка всё ещё сохраняется, отправьте новую заявку через /report."
                    )
                )
            except Exception as e:
                print(f"Не удалось уведомить студента: {e}")

    elif data.startswith("reply_"):
        report_id = int(data.split("_")[1])
        context.user_data["reply_report_id"] = report_id
        await query.message.reply_text(
            f"Введите сообщение для студента по заявке #{report_id}:"
        )


# =========================
# REPLY TO STUDENT + LOG TO YOU
# =========================
async def staff_reply_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_staff(update.effective_user.id):
        return

    report_id = context.user_data.get("reply_report_id")
    if not report_id:
        return

    if update.message and update.message.text and not update.message.text.startswith("/"):
        with get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT user_id
                    FROM reports
                    WHERE id = %s
                """, (report_id,))
                row = cursor.fetchone()

        if not row or not row["user_id"]:
            await update.message.reply_text("Не удалось найти получателя для этой заявки.")
            context.user_data.pop("reply_report_id", None)
            return

        try:
            # Сообщение студенту
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    f"📩 Сообщение по вашей заявке #{report_id}\n\n"
                    f"{update.message.text}"
                )
            )

            # Копия тебе
            for admin_id in SUPER_ADMIN_IDS:
                try:
                    username_text = f"@{update.effective_user.username}" if update.effective_user.username else "-"
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"📩 Сотрудник ответил студенту\n\n"
                            f"👤 Отправитель: {username_text} "
                            f"(ID: {update.effective_user.id})\n"
                            f"📌 Заявка: #{report_id}\n\n"
                            f"💬 Сообщение:\n{update.message.text}"
                        )
                    )
                except Exception as e:
                    print(f"Ошибка отправки лога: {e}")

            await update.message.reply_text("Сообщение студенту отправлено ✅")

        except Exception as e:
            print(f"Не удалось отправить сообщение студенту: {e}")
            await update.message.reply_text("Не удалось отправить сообщение студенту.")

        context.user_data.pop("reply_report_id", None)


# =========================
# STAFF BUTTON ROUTER
# =========================
async def staff_button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        return

    text = update.message.text.strip()

    if text == "Новые заявки":
        await new_reports(update, context)
        return

    if text == "Последние заявки":
        await list_reports(update, context)
        return

    if text == "Поиск по ID":
        await update.message.reply_text("Введите номер заявки:")
        return STAFF_REPORT_ID

    if text == "Фильтр по модулю":
        modules_text = "\n".join(MODULES)
        await update.message.reply_text(
            "Введите название модуля точно так же, как ниже:\n\n"
            f"{modules_text}"
        )
        return STAFF_FILTER_MODULE

    if text == "Изменить статус":
        await update.message.reply_text("Введите ID заявки:")
        return STAFF_SET_STATUS_ID

    if text == "Взять в работу":
        await update.message.reply_text("Введите ID заявки:")
        return STAFF_TAKE_REPORT_ID

    if text == "Отметить решённой":
        await update.message.reply_text("Введите ID заявки:")
        return STAFF_RESOLVE_REPORT_ID

    if text == "Выгрузить Excel":
        if is_admin(user.id):
            await export_excel(update, context)
        else:
            await update.message.reply_text("Только админ может выгружать Excel.")
        return

    if text == "Скрыть меню":
        await hide_menu(update, context)
        return


async def staff_get_report_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        report_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return STAFF_REPORT_ID

    await send_full_report(update, context, report_id)
    return ConversationHandler.END


async def staff_get_filter_module(update: Update, context: ContextTypes.DEFAULT_TYPE):
    module_name = update.message.text.strip()

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, created_at, name, group_name, status
                FROM reports
                WHERE module = %s
                ORDER BY id DESC
                LIMIT 20
            """, (module_name,))
            rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text(f"По модулю '{module_name}' заявок нет.")
        return ConversationHandler.END

    lines = [f"Заявки по модулю: {module_name}\n"]
    for row in rows:
        lines.append(
            f"#{row['id']} | {row['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{row['name']} | {row['group_name']}\n"
            f"Статус: {row['status']}\n"
        )

    await update.message.reply_text("\n".join(lines))
    return ConversationHandler.END


async def staff_get_status_report_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        report_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return STAFF_SET_STATUS_ID

    context.user_data["status_report_id"] = report_id
    await update.message.reply_text("Введите новый статус:\nНовая\nВ работе\nРешено")
    return STAFF_SET_STATUS_VALUE


async def staff_get_status_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_status = update.message.text.strip()
    report_id = context.user_data.get("status_report_id")

    if new_status not in STATUSES:
        await update.message.reply_text("Недопустимый статус.")
        return STAFF_SET_STATUS_VALUE

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, module FROM reports WHERE id = %s", (report_id,))
            report = cursor.fetchone()

            if not report:
                await update.message.reply_text("Заявка с таким ID не найдена.")
                context.user_data.pop("status_report_id", None)
                return ConversationHandler.END

            cursor.execute(
                "UPDATE reports SET status = %s WHERE id = %s",
                (new_status, report_id)
            )
        conn.commit()

    context.user_data.pop("status_report_id", None)
    await update.message.reply_text(
        f"Статус заявки #{report_id} изменён на: {new_status}"
    )
    await sync_report_keyboards(context, report_id, new_status)
    await notify_admins_status_change(
        context=context,
        report_id=report_id,
        module=report["module"],
        new_status=new_status,
        actor=update.effective_user,
    )
    return ConversationHandler.END


async def staff_take_report_by_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        report_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return STAFF_TAKE_REPORT_ID

    context.args = [str(report_id)]
    await take_report(update, context)
    return ConversationHandler.END


async def staff_resolve_report_by_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        report_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return STAFF_RESOLVE_REPORT_ID

    context.args = [str(report_id)]
    await resolve_report(update, context)
    return ConversationHandler.END


# =========================
# TELEGRAM APP
# =========================
telegram_app = Application.builder().token(TOKEN).build()

report_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("report", report_start)],
    states={
        NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
        GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_group)],
        MODULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_module)],
        DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_description)],
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

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("faq", faq))
telegram_app.add_handler(CommandHandler("support", support))
telegram_app.add_handler(CommandHandler("my_role", my_role))
telegram_app.add_handler(CommandHandler("my_reports", my_reports))
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

telegram_app.add_handler(CallbackQueryHandler(handle_buttons))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, staff_reply_router), group=10)

telegram_app.add_handler(MessageHandler(filters.Regex("^Новые заявки$"), staff_button_router))
telegram_app.add_handler(MessageHandler(filters.Regex("^Последние заявки$"), staff_button_router))
telegram_app.add_handler(MessageHandler(filters.Regex("^Выгрузить Excel$"), staff_button_router))
telegram_app.add_handler(MessageHandler(filters.Regex("^Скрыть меню$"), staff_button_router))

telegram_app.add_handler(report_conv_handler)
telegram_app.add_handler(staff_conv_handler)


# =========================
# FASTAPI WEBHOOK
# =========================
app = FastAPI()

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if get_admin_username(request):
        return RedirectResponse(url="/admin", status_code=303)

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": None,
            "configured": bool(ADMIN_PANEL_PASSWORD),
            "username": ADMIN_PANEL_USERNAME,
        },
    )


@app.post("/admin/login")
async def admin_login(
    username: str = Form(...),
    password: str = Form(...),
):
    if not ADMIN_PANEL_PASSWORD:
        return RedirectResponse(url="/admin/login?error=config", status_code=303)

    if (
        hmac.compare_digest(username, ADMIN_PANEL_USERNAME)
        and hmac.compare_digest(password, ADMIN_PANEL_PASSWORD)
    ):
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie(
            key=ADMIN_SESSION_COOKIE,
            value=create_admin_session(username),
            max_age=ADMIN_SESSION_MAX_AGE,
            httponly=True,
            secure=ADMIN_COOKIE_SECURE,
            samesite="lax",
        )
        return response

    return RedirectResponse(url="/admin/login?error=1", status_code=303)


@app.post("/admin/logout")
async def admin_logout():
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(ADMIN_SESSION_COOKIE)
    return response


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    admin_username = get_admin_username(request)
    if not admin_username:
        return admin_redirect()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "admin_username": admin_username,
            "counts": get_dashboard_counts(),
            "recent_reports": get_reports(limit=8),
            "statuses": STATUSES,
            "modules": MODULES,
            "active_page": "dashboard",
        },
    )


@app.get("/admin/reports", response_class=HTMLResponse)
async def admin_reports(
    request: Request,
    status: str | None = None,
    module: str | None = None,
    q: str | None = None,
):
    admin_username = get_admin_username(request)
    if not admin_username:
        return admin_redirect()

    if status and status not in STATUSES:
        status = None
    if module and module not in MODULES:
        module = None

    reports = get_reports(limit=None, status=status, module=module, query=q)

    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "admin_username": admin_username,
            "reports": reports,
            "statuses": STATUSES,
            "modules": MODULES,
            "selected_status": status or "",
            "selected_module": module or "",
            "query": q or "",
            "active_page": "reports",
        },
    )


@app.get("/admin/reports/export.xlsx")
async def admin_export_reports(request: Request):
    if not get_admin_username(request):
        return admin_redirect()

    file_stream = build_reports_excel(get_reports(limit=None))
    filename = f"reports_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        file_stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/admin/reports/{report_id}", response_class=HTMLResponse)
async def admin_report_detail(request: Request, report_id: int):
    admin_username = get_admin_username(request)
    if not admin_username:
        return admin_redirect()

    report = get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Заявка не найдена")

    return templates.TemplateResponse(
        "report_detail.html",
        {
            "request": request,
            "admin_username": admin_username,
            "report": report,
            "statuses": STATUSES,
            "modules": MODULES,
            "active_page": "reports",
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/admin/reports/{report_id}/screenshot")
async def admin_report_screenshot(request: Request, report_id: int):
    if not get_admin_username(request):
        return admin_redirect()

    report = get_report(report_id)
    if not report or not report["screenshot_file_id"]:
        raise HTTPException(status_code=404, detail="Скриншот не найден")

    try:
        file = await telegram_app.bot.get_file(report["screenshot_file_id"])
        image_bytes = await file.download_as_bytearray()
    except Exception as e:
        print(f"Не удалось загрузить скриншот заявки #{report_id}: {e}")
        raise HTTPException(status_code=502, detail="Не удалось загрузить скриншот")

    return Response(content=bytes(image_bytes), media_type="image/jpeg")


@app.post("/admin/reports/{report_id}/status")
async def admin_report_status(
    request: Request,
    report_id: int,
    status: str = Form(...),
    next_url: str = Form(default=""),
):
    admin_username = get_admin_username(request)
    if not admin_username:
        return admin_redirect()

    success = await apply_report_status_change(report_id, status, WebActor(admin_username))
    if not success:
        return RedirectResponse(url=f"/admin/reports/{report_id}?error=status", status_code=303)

    if next_url.startswith("/admin"):
        return RedirectResponse(url=next_url, status_code=303)

    return RedirectResponse(url=f"/admin/reports/{report_id}?message=status", status_code=303)


@app.post("/admin/reports/{report_id}/reply")
async def admin_report_reply(
    request: Request,
    report_id: int,
    message: str = Form(...),
):
    admin_username = get_admin_username(request)
    if not admin_username:
        return admin_redirect()

    message_text = message.strip()
    if not message_text:
        return RedirectResponse(url=f"/admin/reports/{report_id}?error=reply_empty", status_code=303)

    success = await send_reply_to_student_from_admin(
        report_id=report_id,
        message_text=message_text,
        actor=WebActor(admin_username),
    )
    if not success:
        return RedirectResponse(url=f"/admin/reports/{report_id}?error=reply", status_code=303)

    return RedirectResponse(url=f"/admin/reports/{report_id}?message=reply", status_code=303)


@app.post("/admin/report/{report_id}/take")
async def admin_take_report(request: Request, report_id: int):
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, user_id, module
                FROM reports
                WHERE id = %s
            """, (report_id,))
            report = cursor.fetchone()

            if not report:
                return RedirectResponse(url="/admin", status_code=303)

            cursor.execute("""
                UPDATE reports
                SET status = %s
                WHERE id = %s
            """, ("В работе", report_id))
        conn.commit()

    if report["user_id"]:
        try:
            await telegram_app.bot.send_message(
                chat_id=report["user_id"],
                text=(
                    f"🛠 Обновление по вашей заявке #{report_id}\n\n"
                    f"Модуль: {report['module']}\n"
                    "Статус: В работе\n\n"
                    "Ваше обращение принято сотрудниками и уже находится в обработке."
                )
            )
        except Exception as e:
            print(f"Ошибка уведомления студента: {e}")

    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/report/{report_id}/resolve")
async def admin_resolve_report(request: Request, report_id: int):
    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, user_id, module
                FROM reports
                WHERE id = %s
            """, (report_id,))
            report = cursor.fetchone()

            if not report:
                return RedirectResponse(url="/admin", status_code=303)

            cursor.execute("""
                UPDATE reports
                SET status = %s
                WHERE id = %s
            """, ("Решено", report_id))
        conn.commit()

    if report["user_id"]:
        try:
            await telegram_app.bot.send_message(
                chat_id=report["user_id"],
                text=(
                    f"✅ Обновление по вашей заявке #{report_id}\n\n"
                    f"Модуль: {report['module']}\n"
                    "Статус: Решено\n\n"
                    "Здравствуйте! Ваша проблема была обработана и отмечена как решённая.\n"
                    "Пожалуйста, проверьте работу модуля снова.\n\n"
                    "Если ошибка всё ещё сохраняется, отправьте новую заявку через /report."
                )
            )
        except Exception as e:
            print(f"Ошибка уведомления студента: {e}")

    return RedirectResponse(url="/admin", status_code=303)


@app.on_event("startup")
async def on_startup():
    init_db()
    await telegram_app.initialize()
    await telegram_app.start()
    await set_commands(telegram_app)


@app.on_event("shutdown")
async def on_shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()


@app.get("/")
async def healthcheck():
    return {"status": "ok"}


@app.post(f"/{TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

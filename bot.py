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

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.environ.get("PORT", 10000))

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
        ["Последние заявки", "Поиск по ID"],
        ["Фильтр по модулю", "Изменить статус"],
        ["Взять в работу", "Отметить решённой"],
    ]

    if is_admin(user_id):
        keyboard.append(["Выгрузить Excel"])

    keyboard.append(["Скрыть меню"])

    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def set_commands(application: Application):
    student_commands = [
        BotCommand("start", "Главное сообщение"),
        BotCommand("faq", "Частые вопросы"),
        BotCommand("support", "Связаться с поддержкой"),
        BotCommand("report", "Отправить ошибку"),
        BotCommand("my_role", "Показать мою роль"),
    ]

    staff_commands = [
        BotCommand("start", "Главное сообщение"),
        BotCommand("faq", "Частые вопросы"),
        BotCommand("support", "Связаться с поддержкой"),
        BotCommand("report", "Отправить ошибку"),
        BotCommand("my_role", "Показать мою роль"),
        BotCommand("staff_menu", "Открыть меню сотрудника"),
        BotCommand("list_reports", "Последние заявки"),
        BotCommand("report_by_id", "Полная заявка по ID"),
        BotCommand("filter_module", "Фильтр по модулю"),
        BotCommand("set_status", "Изменить статус заявки"),
        BotCommand("take_report", "Взять заявку в работу"),
        BotCommand("resolve_report", "Отметить заявку решённой"),
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


def build_inline_keyboard(report_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛠 В работу", callback_data=f"take_{report_id}"),
            InlineKeyboardButton("✅ Решено", callback_data=f"done_{report_id}"),
        ]
    ])


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
        "• /support — связаться с поддержкой\n"
        "• /my_role — узнать свою роль\n\n"
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
        "— Да. На этапе скриншота просто напишите: Пропустить.\n\n"
        "5. Что делать, если бот не ответил?\n"
        "— Напишите в поддержку через /support."
    )
    await update.message.reply_text(text)


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📞 Связаться с поддержкой\n\n"
        "Если у вас срочный вопрос или возникли проблемы с ботом, напишите напрямую в поддержку:\n\n"
        "👉 @ppwmdk\n\n"
        "Пожалуйста, укажите:\n"
        "• ФИО\n"
        "• группу\n"
        "• модуль\n"
        "• описание проблемы\n"
        "• скриншот\n\n"
        "Также вы можете отправить заявку через /report."
    )


async def my_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    await update.message.reply_text(f"Ваша роль: {get_role_name(user.id)}")


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
        "Теперь отправьте скриншот.\nЕсли скриншота нет, напишите: Пропустить"
    )
    return SCREENSHOT


async def get_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    screenshot_file_id = None
    text_value = update.message.text.strip().lower() if update.message.text else ""

    if update.message.photo:
        screenshot_file_id = update.message.photo[-1].file_id
    elif text_value in ["пропустить", "skip", "-"]:
        screenshot_file_id = None
    else:
        await update.message.reply_text(
            "Пожалуйста, отправьте скриншот или напишите: Пропустить"
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

    keyboard = build_inline_keyboard(report_id)
    recipients = ADMIN_IDS.union(DEVELOPER_IDS)

    for staff_id in recipients:
        try:
            if screenshot_file_id:
                await context.bot.send_photo(
                    chat_id=staff_id,
                    photo=screenshot_file_id,
                    caption=report_text,
                    reply_markup=keyboard
                )
            else:
                await context.bot.send_message(
                    chat_id=staff_id,
                    text=report_text,
                    reply_markup=keyboard
                )
        except Exception as e:
            print(f"Ошибка отправки сотруднику {staff_id}: {e}")

    await update.message.reply_text(
        "✅ Ваша заявка успешно отправлена!\n\n"
        "Спасибо, что сообщили о проблеме.\n\n"
        "⚠️ Если вдруг вам не пришло это сообщение или вы не уверены, что заявка отправилась, "
        "напишите в поддержку:\n"
        "👉 @ppwmdk"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Действие отменено.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# =========================
# STAFF COMMANDS
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
                SELECT id, created_at, name, group_name, module, status
                FROM reports
                ORDER BY id DESC
                LIMIT 10
            """)
            rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("Заявок пока нет.")
        return

    lines = ["Последние заявки:\n"]
    for row in rows:
        lines.append(
            f"#{row['id']} | {row['created_at'].strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{row['name']} | {row['group_name']}\n"
            f"Модуль: {row['module']}\n"
            f"Статус: {row['status']}\n"
        )

    await update.message.reply_text("\n".join(lines))


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
            cursor.execute("SELECT id FROM reports WHERE id = %s", (report_id,))
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

    with get_conn() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, created_at, user_id, username, name, group_name, module, description, status, screenshot_file_id
                FROM reports
                ORDER BY id DESC
            """)
            rows = cursor.fetchall()

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
        "Есть скриншот"
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
            "Да" if row["screenshot_file_id"] else "Нет"
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

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"🛠 Заявка #{report_id} взята в работу")

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

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"✅ Заявка #{report_id} решена")

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


# =========================
# STAFF BUTTON ROUTER
# =========================
async def staff_button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        return

    text = update.message.text.strip()

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
            cursor.execute("SELECT id FROM reports WHERE id = %s", (report_id,))
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
telegram_app.add_handler(CommandHandler("staff_menu", staff_menu))
telegram_app.add_handler(CommandHandler("list_reports", list_reports))
telegram_app.add_handler(CommandHandler("report_by_id", report_by_id))
telegram_app.add_handler(CommandHandler("filter_module", filter_module))
telegram_app.add_handler(CommandHandler("set_status", set_status))
telegram_app.add_handler(CommandHandler("take_report", take_report))
telegram_app.add_handler(CommandHandler("resolve_report", resolve_report))
telegram_app.add_handler(CommandHandler("export_excel", export_excel))

telegram_app.add_handler(CallbackQueryHandler(handle_buttons))

telegram_app.add_handler(MessageHandler(filters.Regex("^Последние заявки$"), staff_button_router))
telegram_app.add_handler(MessageHandler(filters.Regex("^Выгрузить Excel$"), staff_button_router))
telegram_app.add_handler(MessageHandler(filters.Regex("^Скрыть меню$"), staff_button_router))

telegram_app.add_handler(report_conv_handler)
telegram_app.add_handler(staff_conv_handler)


# =========================
# FASTAPI WEBHOOK
# =========================
app = FastAPI()


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
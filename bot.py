import os
import sqlite3
import shutil
import time
from datetime import datetime

from dotenv import load_dotenv
from openpyxl import Workbook
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    BotCommand,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
}
DEVELOPER_IDS = {
    int(x.strip()) for x in os.getenv("DEVELOPER_IDS", "").split(",") if x.strip()
}

# Состояния для заявки
NAME, GROUP, MODULE, DESCRIPTION, SCREENSHOT = range(5)

# Состояния для меню сотрудников
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

DB_PATH = "reports.db"

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    user_id INTEGER,
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


def create_backup():
    if not os.path.exists(DB_PATH):
        return None

    if not os.path.exists("backups"):
        os.makedirs("backups")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_path = f"backups/reports_{timestamp}.db"
    shutil.copy(DB_PATH, backup_path)
    return backup_path


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
        ["Последние заявки", "Поиск по ID"],
        ["Фильтр по модулю", "Изменить статус"],
        ["Взять в работу", "Отметить решённой"],
    ]

    if is_admin(user_id):
        keyboard.append(["Выгрузить Excel", "Backup базы"])
    else:
        keyboard.append(["Backup базы"])

    keyboard.append(["Скрыть меню"])

    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def set_commands(application):
    commands = [
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
        BotCommand("export_excel", "Выгрузить Excel"),
        BotCommand("backup", "Сделать backup базы"),
        BotCommand("cancel", "Отменить действие"),
    ]
    await application.bot.set_my_commands(commands)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"Ошибка: {context.error}")


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
        "— Используйте команду /report и заполните шаги.\n\n"
        "2. Нужно ли прикладывать скриншот?\n"
        "— Желательно да. Так проблему проще понять.\n\n"
        "3. Что писать в описании?\n"
        "— Напишите, что именно не работает, в каком модуле и что вы делали до ошибки.\n\n"
        "4. Можно ли отправить без скриншота?\n"
        "— Да. На этапе скриншота просто напишите: Пропустить.\n\n"
        "5. Как понять, что заявка дошла?\n"
        "— После отправки бот покажет подтверждение.\n\n"
        "6. Что делать, если бот не ответил?\n"
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
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute(
        """
        INSERT INTO reports (
            created_at, user_id, username, name, group_name, module, description, screenshot_file_id, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    conn.commit()

    report_id = cursor.lastrowid
    username_text = f"@{user.username}" if user and user.username else "-"

    staff_text = (
        f"📌 Новая заявка #{report_id}\n\n"
        f"🕒 Дата: {created_at}\n"
        f"👤 ФИО: {context.user_data['name']}\n"
        f"🎓 Группа: {context.user_data['group']}\n"
        f"🧩 Модуль: {context.user_data['module']}\n"
        f"📝 Описание: {context.user_data['description']}\n"
        f"📊 Статус: Новая\n"
        f"🆔 Telegram ID: {user.id if user else '-'}\n"
        f"🔗 Username: {username_text}"
    )

    recipients = ADMIN_IDS.union(DEVELOPER_IDS)

    for staff_id in recipients:
        try:
            if screenshot_file_id:
                await context.bot.send_photo(
                    chat_id=staff_id,
                    photo=screenshot_file_id,
                    caption=staff_text
                )
            else:
                await context.bot.send_message(
                    chat_id=staff_id,
                    text=staff_text
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


async def list_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

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
            f"#{row[0]} | {row[1]}\n"
            f"{row[2]} | {row[3]}\n"
            f"Модуль: {row[4]}\n"
            f"Статус: {row[5]}\n"
        )

    await update.message.reply_text("\n".join(lines))


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


async def send_full_report(update: Update, context: ContextTypes.DEFAULT_TYPE, report_id: int):
    cursor.execute("""
        SELECT id, created_at, user_id, username, name, group_name, module, description, screenshot_file_id, status
        FROM reports
        WHERE id = ?
    """, (report_id,))
    row = cursor.fetchone()

    if not row:
        await update.message.reply_text("Заявка с таким ID не найдена.")
        return

    username_text = f"@{row[3]}" if row[3] else "-"
    text = (
        f"📄 Полная заявка #{row[0]}\n\n"
        f"🕒 Дата: {row[1]}\n"
        f"👤 ФИО: {row[4]}\n"
        f"🎓 Группа: {row[5]}\n"
        f"🧩 Модуль: {row[6]}\n"
        f"📝 Описание:\n{row[7]}\n\n"
        f"📊 Статус: {row[9]}\n"
        f"🆔 Telegram ID: {row[2]}\n"
        f"🔗 Username: {username_text}"
    )

    if row[8]:
        await update.message.reply_photo(photo=row[8], caption=text)
    else:
        await update.message.reply_text(text)


async def filter_module(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    args = context.args
    if not args:
        modules_text = "\n".join(f"- {m}" for m in MODULES)
        await update.message.reply_text(
            "Напишите команду так:\n"
            "/filter_module Платежи\n\n"
            "Доступные модули:\n"
            f"{modules_text}"
        )
        return

    module_name = " ".join(args).strip()

    cursor.execute("""
        SELECT id, created_at, name, group_name, status
        FROM reports
        WHERE module = ?
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
            f"#{row[0]} | {row[1]}\n"
            f"{row[2]} | {row[3]}\n"
            f"Статус: {row[4]}\n"
        )

    await update.message.reply_text("\n".join(lines))


async def set_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    args = context.args
    if len(args) < 2:
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
        report_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID заявки должен быть числом.")
        return

    new_status = " ".join(args[1:]).strip()

    if new_status not in STATUSES:
        await update.message.reply_text(
            "Недопустимый статус.\n"
            "Разрешены: Новая, В работе, Решено"
        )
        return

    cursor.execute("SELECT id FROM reports WHERE id = ?", (report_id,))
    report = cursor.fetchone()

    if not report:
        await update.message.reply_text("Заявка с таким ID не найдена.")
        return

    cursor.execute(
        "UPDATE reports SET status = ? WHERE id = ?",
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

    cursor.execute("""
        SELECT id, user_id, module
        FROM reports
        WHERE id = ?
    """, (report_id,))
    row = cursor.fetchone()

    if not row:
        await update.message.reply_text("Заявка не найдена.")
        return

    cursor.execute(
        "UPDATE reports SET status = ? WHERE id = ?",
        ("В работе", report_id)
    )
    conn.commit()

    await update.message.reply_text(f"🛠 Заявка #{report_id} переведена в статус: В работе")

    student_id = row[1]
    module_name = row[2]

    if student_id:
        try:
            await context.bot.send_message(
                chat_id=student_id,
                text=(
                    f"🛠 Обновление по вашей заявке #{report_id}\n\n"
                    f"Модуль: {module_name}\n"
                    "Статус: В работе\n\n"
                    "Ваше обращение принято сотрудниками и уже находится в обработке."
                )
            )
        except Exception as e:
            print(f"Не удалось уведомить студента по заявке #{report_id}: {e}")


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

    cursor.execute("""
        SELECT id, user_id, module
        FROM reports
        WHERE id = ?
    """, (report_id,))
    row = cursor.fetchone()

    if not row:
        await update.message.reply_text("Заявка не найдена.")
        return

    cursor.execute(
        "UPDATE reports SET status = ? WHERE id = ?",
        ("Решено", report_id)
    )
    conn.commit()

    await update.message.reply_text(f"✅ Заявка #{report_id} переведена в статус: Решено")

    student_id = row[1]
    module_name = row[2]

    if student_id:
        try:
            await context.bot.send_message(
                chat_id=student_id,
                text=(
                    f"✅ Обновление по вашей заявке #{report_id}\n\n"
                    f"Модуль: {module_name}\n"
                    "Статус: Решено\n\n"
                    "Здравствуйте! Ваша проблема была обработана и отмечена как решённая.\n"
                    "Пожалуйста, проверьте работу модуля снова.\n\n"
                    "Если ошибка всё ещё сохраняется, отправьте новую заявку через /report."
                )
            )
        except Exception as e:
            print(f"Не удалось уведомить студента по заявке #{report_id}: {e}")


async def export_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("Только админ может выгружать Excel.")
        return

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
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
            "Да" if row[9] else "Нет"
        ])

    for column in ws.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            value = str(cell.value) if cell.value is not None else ""
            if len(value) > max_length:
                max_length = len(value)
        ws.column_dimensions[column_letter].width = min(max_length + 2, 40)

    filename = f"reports_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    wb.save(filename)

    with open(filename, "rb") as file:
        await update.message.reply_document(
            document=file,
            filename=filename,
            caption="Готово. Вот Excel с заявками."
        )

    os.remove(filename)


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_staff(user.id):
        await update.message.reply_text("У вас нет доступа к этой команде.")
        return

    backup_file = create_backup()

    if not backup_file:
        await update.message.reply_text("База данных не найдена.")
        return

    with open(backup_file, "rb") as file:
        await update.message.reply_document(
            document=file,
            filename=os.path.basename(backup_file),
            caption="Backup базы данных"
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Действие отменено.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


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

    if text == "Backup базы":
        await backup_command(update, context)
        return

    if text == "Скрыть меню":
        await hide_menu(update, context)
        return


async def staff_get_report_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    try:
        report_id = int(text)
    except ValueError:
        await update.message.reply_text("ID должен быть числом. Введите номер заявки:")
        return STAFF_REPORT_ID

    await send_full_report(update, context, report_id)
    return ConversationHandler.END


async def staff_get_filter_module(update: Update, context: ContextTypes.DEFAULT_TYPE):
    module_name = update.message.text.strip()

    cursor.execute("""
        SELECT id, created_at, name, group_name, status
        FROM reports
        WHERE module = ?
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
            f"#{row[0]} | {row[1]}\n"
            f"{row[2]} | {row[3]}\n"
            f"Статус: {row[4]}\n"
        )

    await update.message.reply_text("\n".join(lines))
    return ConversationHandler.END


async def staff_get_status_report_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    try:
        report_id = int(text)
    except ValueError:
        await update.message.reply_text("ID должен быть числом. Введите ID заявки:")
        return STAFF_SET_STATUS_ID

    context.user_data["status_report_id"] = report_id
    await update.message.reply_text(
        "Введите новый статус:\nНовая\nВ работе\nРешено"
    )
    return STAFF_SET_STATUS_VALUE


async def staff_get_status_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_status = update.message.text.strip()
    report_id = context.user_data.get("status_report_id")

    if new_status not in STATUSES:
        await update.message.reply_text(
            "Недопустимый статус. Введите один из вариантов:\nНовая\nВ работе\nРешено"
        )
        return STAFF_SET_STATUS_VALUE

    cursor.execute("SELECT id FROM reports WHERE id = ?", (report_id,))
    report = cursor.fetchone()

    if not report:
        await update.message.reply_text("Заявка с таким ID не найдена.")
        context.user_data.pop("status_report_id", None)
        return ConversationHandler.END

    cursor.execute(
        "UPDATE reports SET status = ? WHERE id = ?",
        (new_status, report_id)
    )
    conn.commit()

    context.user_data.pop("status_report_id", None)
    await update.message.reply_text(
        f"Статус заявки #{report_id} изменён на: {new_status}"
    )
    return ConversationHandler.END


async def staff_take_report_by_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    try:
        report_id = int(text)
    except ValueError:
        await update.message.reply_text("ID должен быть числом. Введите ID заявки:")
        return STAFF_TAKE_REPORT_ID

    context.args = [str(report_id)]
    await take_report(update, context)
    return ConversationHandler.END


async def staff_resolve_report_by_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    try:
        report_id = int(text)
    except ValueError:
        await update.message.reply_text("ID должен быть числом. Введите ID заявки:")
        return STAFF_RESOLVE_REPORT_ID

    context.args = [str(report_id)]
    await resolve_report(update, context)
    return ConversationHandler.END


async def post_init(application):
    await set_commands(application)


def build_application():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

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

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("faq", faq))
    app.add_handler(CommandHandler("support", support))
    app.add_handler(CommandHandler("my_role", my_role))
    app.add_handler(CommandHandler("staff_menu", staff_menu))
    app.add_handler(CommandHandler("list_reports", list_reports))
    app.add_handler(CommandHandler("report_by_id", report_by_id))
    app.add_handler(CommandHandler("filter_module", filter_module))
    app.add_handler(CommandHandler("set_status", set_status))
    app.add_handler(CommandHandler("take_report", take_report))
    app.add_handler(CommandHandler("resolve_report", resolve_report))
    app.add_handler(CommandHandler("export_excel", export_excel))
    app.add_handler(CommandHandler("backup", backup_command))

    app.add_handler(MessageHandler(filters.Regex("^Последние заявки$"), staff_button_router))
    app.add_handler(MessageHandler(filters.Regex("^Выгрузить Excel$"), staff_button_router))
    app.add_handler(MessageHandler(filters.Regex("^Backup базы$"), staff_button_router))
    app.add_handler(MessageHandler(filters.Regex("^Скрыть меню$"), staff_button_router))

    app.add_handler(report_conv_handler)
    app.add_handler(staff_conv_handler)

    return app


def main():
    if not TOKEN:
        raise ValueError("BOT_TOKEN не найден в .env")

    while True:
        try:
            backup_file = create_backup()
            if backup_file:
                print(f"Backup создан: {backup_file}")

            app = build_application()

            print("Бот запущен...")
            app.run_polling(
                drop_pending_updates=True,
                timeout=60,
                read_timeout=60,
                write_timeout=60,
                connect_timeout=60,
                pool_timeout=60,
            )
        except Exception as e:
            print(f"Бот упал: {e}")
            print("Перезапуск через 5 секунд...")
            time.sleep(5)


if __name__ == "__main__":
    main()
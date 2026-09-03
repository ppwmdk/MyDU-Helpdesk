import os
import io
import asyncio
import logging
from datetime import datetime
from typing import List, Optional

import psycopg
from psycopg.rows import dict_row
import openpyxl
from dotenv import load_dotenv
from telegram.request import HTTPXRequest

from fastapi import FastAPI, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InputMediaPhoto
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters
)

from locales import t

# --- ЗАГРУЗКА ОКРУЖЕНИЯ ---
load_dotenv()

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("HelpdeskApp")

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_LOGIN = os.getenv("ADMIN_PANEL_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD", "admin123")

MEDIA_BASE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guides_media")

# --- СОСТОЯНИЯ ДЛЯ CONVERSATION HANDLER (ПОДАЧА ЗАЯВКИ) ---
FIO, GROUP, MODULE, DESC, SCREENSHOT = range(5)

# Глобальный объект Telegram-приложения
tg_app: Optional[Application] = None


# =====================================================================
#                          РАБОТА С БАЗОЙ ДАННЫХ
# =====================================================================

async def get_conn_async():
    if DATABASE_URL:
        return await psycopg.AsyncConnection.connect(
            DATABASE_URL,
            row_factory=dict_row
        )
    return await psycopg.AsyncConnection.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "mydu_helpdesk"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS", ""),
        row_factory=dict_row
    )

async def init_db():
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            # Пользователи бота
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS bot_users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(100),
                    first_name VARCHAR(150),
                    language VARCHAR(10) DEFAULT 'ru',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Заявки
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    username VARCHAR(100),
                    name VARCHAR(200),
                    group_name VARCHAR(100),
                    module VARCHAR(150),
                    description TEXT,
                    screenshot_file_id VARCHAR(255),
                    status VARCHAR(50) DEFAULT 'Новая',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Сообщения в чате тикета
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS report_messages (
                    id SERIAL PRIMARY KEY,
                    report_id INT REFERENCES reports(id) ON DELETE CASCADE,
                    sender_type VARCHAR(20),
                    sender_name VARCHAR(150),
                    message_text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Модули (категории)
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_modules (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(150) UNIQUE NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE
                );
            """)
            # Быстрые шаблоны ответов
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS quick_templates (
                    id SERIAL PRIMARY KEY,
                    title VARCHAR(150) NOT NULL,
                    text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Smart FAQ правила
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS smart_faq_rules (
                    id SERIAL PRIMARY KEY,
                    title VARCHAR(150) NOT NULL,
                    keywords TEXT NOT NULL,
                    reply_text TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE
                );
            """)
            # Инструкции
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS module_guides (
                    id SERIAL PRIMARY KEY,
                    module_name VARCHAR(150) NOT NULL,
                    title VARCHAR(200) NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Базовые модули, если таблица пустая
            await cursor.execute("SELECT COUNT(*) as cnt FROM system_modules;")
            res = await cursor.fetchone()
            if res["cnt"] == 0:
                defaults = [
                    "Регистрация на дисциплины",
                    "Общежитие",
                    "Корпоративная почта / Office 365",
                    "Kaspi / Оплата за обучение",
                    "Справки и документы",
                    "Другое"
                ]
                for d in defaults:
                    await cursor.execute("INSERT INTO system_modules (name) VALUES (%s) ON CONFLICT DO NOTHING;", (d,))


# =====================================================================
#                     ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ БОТА
# =====================================================================

async def get_user_lang(user_id: int) -> str:
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT language FROM bot_users WHERE user_id = %s;", (user_id,))
            row = await cursor.fetchone()
            if row and row.get("language"):
                return row["language"]
    return "ru"

async def save_user(user_id: int, username: str, first_name: str, language: str = "ru"):
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                INSERT INTO bot_users (user_id, username, first_name, language)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    language = EXCLUDED.language;
            """, (user_id, username, first_name, language))

def get_main_reply_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(t(lang, "btn_report"))],
            [KeyboardButton(t(lang, "btn_guides")), KeyboardButton(t(lang, "btn_my_reports"))],
            [KeyboardButton(t(lang, "btn_change_lang"))]
        ],
        resize_keyboard=True
    )

def get_lang_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇷🇺 Русский", callback_data="set_lang:ru"),
            InlineKeyboardButton("🇰🇿 Қазақша", callback_data="set_lang:kz"),
            InlineKeyboardButton("🇬🇧 English", callback_data="set_lang:en"),
        ]
    ])


# =====================================================================
#                     TELEGRAM BOT HANDLERS
# =====================================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    await update.message.reply_text(
        t("ru", "choose_lang"),
        reply_markup=get_lang_inline_keyboard()
    )

async def set_lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang = query.data.split(":")[1]
    user = query.from_user

    await save_user(user.id, user.username or "", user.first_name or "", lang)
    await query.edit_message_text(t(lang, "lang_saved"))
    await context.bot.send_message(
        chat_id=user.id,
        text=t(lang, "welcome"),
        parse_mode="HTML",
        reply_markup=get_main_reply_keyboard(lang)
    )

async def change_lang_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        t("ru", "choose_lang"),
        reply_markup=get_lang_inline_keyboard()
    )

# --- FSM ПОДАЧИ ЗАЯВКИ ---
async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = await get_user_lang(user.id)
    context.user_data["lang"] = lang

    cancel_kb = ReplyKeyboardMarkup([[KeyboardButton(t(lang, "btn_cancel"))]], resize_keyboard=True)
    await update.message.reply_text(t(lang, "enter_fio"), parse_mode="HTML", reply_markup=cancel_kb)
    return FIO

async def report_fio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang", "ru")
    text = update.message.text.strip()
    if text == t(lang, "btn_cancel"):
        await update.message.reply_text(t(lang, "action_cancelled"), reply_markup=get_main_reply_keyboard(lang))
        return ConversationHandler.END

    context.user_data["fio"] = text
    cancel_kb = ReplyKeyboardMarkup([[KeyboardButton(t(lang, "btn_cancel"))]], resize_keyboard=True)
    await update.message.reply_text(t(lang, "enter_group"), parse_mode="HTML", reply_markup=cancel_kb)
    return GROUP

async def report_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang", "ru")
    text = update.message.text.strip()
    if text == t(lang, "btn_cancel"):
        await update.message.reply_text(t(lang, "action_cancelled"), reply_markup=get_main_reply_keyboard(lang))
        return ConversationHandler.END

    context.user_data["group"] = text

    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT name FROM system_modules WHERE is_active = TRUE ORDER BY id ASC;")
            mods = await cursor.fetchall()

    kb = [[KeyboardButton(m["name"])] for m in mods]
    kb.append([KeyboardButton(t(lang, "btn_cancel"))])
    await update.message.reply_text(
        t(lang, "choose_module"),
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )
    return MODULE

async def report_module(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang", "ru")
    text = update.message.text.strip()
    if text == t(lang, "btn_cancel"):
        await update.message.reply_text(t(lang, "action_cancelled"), reply_markup=get_main_reply_keyboard(lang))
        return ConversationHandler.END

    context.user_data["module"] = text
    cancel_kb = ReplyKeyboardMarkup([[KeyboardButton(t(lang, "btn_cancel"))]], resize_keyboard=True)
    await update.message.reply_text(t(lang, "enter_desc"), parse_mode="HTML", reply_markup=cancel_kb)
    return DESC

async def report_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang", "ru")
    text = update.message.text.strip()
    if text == t(lang, "btn_cancel"):
        await update.message.reply_text(t(lang, "action_cancelled"), reply_markup=get_main_reply_keyboard(lang))
        return ConversationHandler.END

    context.user_data["desc"] = text

    # Проверка Smart FAQ
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT title, keywords, reply_text FROM smart_faq_rules WHERE is_active = TRUE;")
            rules = await cursor.fetchall()

    lowered_desc = text.lower()
    faq_match = None
    for r in rules:
        keywords = [k.strip().lower() for k in r["keywords"].split(",") if k.strip()]
        if any(kw in lowered_desc for kw in keywords):
            faq_match = r
            break

    if faq_match:
        await update.message.reply_text(
            f"💡 <b>Автоматическая подсказка: {faq_match['title']}</b>\n\n{faq_match['reply_text']}",
            parse_mode="HTML"
        )

    kb = [
        [KeyboardButton(t(lang, "btn_skip"))],
        [KeyboardButton(t(lang, "btn_cancel"))]
    ]
    await update.message.reply_text(
        t(lang, "send_screen"),
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )
    return SCREENSHOT

async def report_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang", "ru")
    user = update.effective_user

    file_id = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.text and update.message.text.strip() == t(lang, "btn_cancel"):
        await update.message.reply_text(t(lang, "action_cancelled"), reply_markup=get_main_reply_keyboard(lang))
        return ConversationHandler.END

    fio = context.user_data.get("fio")
    group_name = context.user_data.get("group")
    module_name = context.user_data.get("module")
    desc = context.user_data.get("desc")

    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                INSERT INTO reports (user_id, username, name, group_name, module, description, screenshot_file_id, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'Новая')
                RETURNING id;
            """, (user.id, user.username or "", fio, group_name, module_name, desc, file_id))
            new_row = await cursor.fetchone()
            ticket_id = new_row["id"]

    await update.message.reply_text(
        t(lang, "report_success", id=ticket_id, module=module_name),
        parse_mode="HTML",
        reply_markup=get_main_reply_keyboard(lang)
    )
    context.user_data.clear()
    return ConversationHandler.END

# --- БАЗА ЗНАНИЙ И ИНСТРУКЦИЯ ПО ОБЩЕЖИТИЮ (8 ФОТО ПО ПОРЯДКУ) ---
async def guides_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT DISTINCT module_name FROM module_guides ORDER BY module_name;")
            mods = await cursor.fetchall()

    buttons = [
        [InlineKeyboardButton(t(lang, "dorm_guide_btn"), callback_data="guide:dorm")]
    ]
    for m in mods:
        buttons.append([InlineKeyboardButton(f"📁 {m['module_name']}", callback_data=f"gmod:{m['module_name'][:30]}")])

    await update.message.reply_text(
        t(lang, "guides_title"),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML"
    )

async def guides_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = await get_user_lang(user_id)
    data = query.data

    # Отправка альбома общежития (1.jpg - 8.jpg)
    if data == "guide:dorm":
        folder = os.path.join(MEDIA_BASE_PATH, "dorm", lang)
        if not os.path.exists(folder) or not os.listdir(folder):
            folder = os.path.join(MEDIA_BASE_PATH, "dorm", "ru")

        if not os.path.exists(folder):
            await query.message.reply_text(t(lang, "guides_empty"))
            return

        files = [f for f in os.listdir(folder) if f.lower().endswith((".jpg", ".jpeg", ".png"))]
        files.sort(key=lambda x: int(os.path.splitext(x)[0]) if os.path.splitext(x)[0].isdigit() else 999)

        if not files:
            await query.message.reply_text(t(lang, "guides_empty"))
            return

        media_list = []
        for i, fname in enumerate(files[:8]):
            path = os.path.join(folder, fname)
            with open(path, "rb") as f:
                photo_bytes = f.read()
            caption = t(lang, "dorm_caption") if i == 0 else None
            media_list.append(InputMediaPhoto(media=photo_bytes, caption=caption, parse_mode="HTML"))

        await query.message.reply_text("⏳ Жүктелуде... / Отправляем страницы инструкции...")
        await context.bot.send_media_group(chat_id=user_id, media=media_list)
        return

    # Динамические гайды
    if data.startswith("gmod:"):
        mod_name = data.split(":", 1)[1]
        async with await get_conn_async() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT id, title FROM module_guides WHERE module_name = %s ORDER BY id ASC;", (mod_name,))
                items = await cursor.fetchall()

        if not items:
            await query.edit_message_text(t(lang, "guides_empty"))
            return

        kb = [[InlineKeyboardButton(f"📄 {it['title']}", callback_data=f"gview:{it['id']}")] for it in items]
        kb.append([InlineKeyboardButton(t(lang, "back_to_guides"), callback_data="gback:all")])
        await query.edit_message_text(f"📁 <b>{mod_name}</b>:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif data.startswith("gview:"):
        gid = int(data.split(":", 1)[1])
        async with await get_conn_async() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT module_name, title, content FROM module_guides WHERE id = %s;", (gid,))
                guide = await cursor.fetchone()

        if not guide:
            await query.edit_message_text(t(lang, "guides_empty"))
            return

        kb = [[InlineKeyboardButton(t(lang, "back_to_guides"), callback_data=f"gmod:{guide['module_name'][:30]}")]]
        msg = f"📖 <b>{guide['title']}</b>\n\n{guide['content']}"
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif data == "gback:all":
        async with await get_conn_async() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT DISTINCT module_name FROM module_guides ORDER BY module_name;")
                mods = await cursor.fetchall()

        buttons = [[InlineKeyboardButton(t(lang, "dorm_guide_btn"), callback_data="guide:dorm")]]
        for m in mods:
            buttons.append([InlineKeyboardButton(f"📁 {m['module_name']}", callback_data=f"gmod:{m['module_name'][:30]}")])

        await query.edit_message_text(t(lang, "guides_title"), reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

# --- МОИ ЗАЯВКИ ---
async def my_reports_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = await get_user_lang(user_id)

    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT id, module, status, created_at FROM reports WHERE user_id = %s ORDER BY id DESC LIMIT 5;",
                (user_id,)
            )
            rows = await cursor.fetchall()

    if not rows:
        await update.message.reply_text(t(lang, "my_reports_empty"))
        return

    text = t(lang, "my_reports_header")
    for r in rows:
        dt = r["created_at"].strftime("%d.%m.%Y %H:%M") if r["created_at"] else ""
        text += t(lang, "ticket_item", id=r["id"], status=r["status"], module=r["module"], date=dt)

    await update.message.reply_text(text, parse_mode="HTML")

# --- ОТВЕТ СТУДЕНТА В ЧАТЕ (REPLY) ---
async def student_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message
    if not msg.reply_to_message:
        return

    orig_text = msg.reply_to_message.text or msg.reply_to_message.caption or ""
    import re
    match = re.search(r"#(\d+)", orig_text)
    if not match:
        return

    report_id = int(match.group(1))

    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                INSERT INTO report_messages (report_id, sender_type, sender_name, message_text)
                VALUES (%s, 'student', %s, %s);
            """, (report_id, user.first_name or "Student", msg.text))

    await msg.reply_text("✅ Сообщение отправлено в техподдержку.")


# =====================================================================
#                     FASTAPI WEB PANEL
# =====================================================================

app = FastAPI(title="Astana IT Helpdesk")
templates = Jinja2Templates(directory="templates")

def get_admin_username(request: Request) -> Optional[str]:
    return request.cookies.get("admin_session")

def admin_redirect():
    return RedirectResponse(url="/admin/login", status_code=303)

@app.on_event("startup")
async def on_startup():
    await init_db()
    global tg_app

    # Настройка повышенных таймаутов для стабильной отправки тяжелых медиа и PDF
    request_config = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=30.0
    )

    tg_app = Application.builder().token(BOT_TOKEN).request(request_config).build()

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^(📝 Подать заявку|📝 Өтініш беру|📝 Submit Ticket)$"), report_start),
            CommandHandler("report", report_start)
        ],
        states={
            FIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_fio)],
            GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_group)],
            MODULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_module)],
            DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_desc)],
            SCREENSHOT: [
                MessageHandler(filters.PHOTO, report_screenshot),
                MessageHandler(filters.TEXT & ~filters.COMMAND, report_screenshot)
            ]
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )

    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CallbackQueryHandler(set_lang_callback, pattern=r"^set_lang:"))
    tg_app.add_handler(MessageHandler(filters.Regex(r"^🌐"), change_lang_btn))
    tg_app.add_handler(MessageHandler(filters.Regex(r"^(📚 Инструкции|📚 Нұсқаулықтар|📚 Guides)"), guides_menu))
    tg_app.add_handler(MessageHandler(filters.Regex(r"^(📂 Мои заявки|📂 Менің өтініштерім|📂 My Tickets)"), my_reports_cmd))
    tg_app.add_handler(CallbackQueryHandler(guides_callbacks, pattern=r"^(guide:|gmod:|gview:|gback:)"))
    tg_app.add_handler(conv)
    tg_app.add_handler(MessageHandler(filters.TEXT & filters.REPLY, student_reply_handler))

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)

@app.on_event("shutdown")
async def on_shutdown():
    global tg_app
    if tg_app:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()

# --- АВТОРИЗАЦИЯ ---
@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request})

@app.post("/admin/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_LOGIN and password == ADMIN_PASSWORD:
        resp = RedirectResponse(url="/admin", status_code=303)
        resp.set_cookie("admin_session", username, max_age=86400, httponly=True)
        return resp
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": "Неверный логин или пароль"})

@app.post("/admin/logout")
async def logout():
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie("admin_session")
    return resp

# --- ДАШБОРД ---
@app.get("/admin", response_class=HTMLResponse)
async def dashboard(request: Request, q: Optional[str] = None, status: Optional[str] = None, module: Optional[str] = None):
    admin = get_admin_username(request)
    if not admin:
        return admin_redirect()

    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                SELECT 
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE status = 'Новая') as new_count,
                    COUNT(*) FILTER (WHERE status = 'В работе') as in_progress_count,
                    COUNT(*) FILTER (WHERE status = 'Решено') as resolved_count
                FROM reports;
            """)
            counts = await cursor.fetchone()

            sql = "SELECT * FROM reports WHERE 1=1"
            params = []
            if q:
                sql += " AND (name ILIKE %s OR group_name ILIKE %s OR description ILIKE %s)"
                pattern = f"%{q}%"
                params.extend([pattern, pattern, pattern])
            if status:
                sql += " AND status = %s"
                params.append(status)
            if module:
                sql += " AND module = %s"
                params.append(module)

            sql += " ORDER BY id DESC LIMIT 150;"
            await cursor.execute(sql, tuple(params))
            reports = await cursor.fetchall()

            await cursor.execute("SELECT name FROM system_modules ORDER BY name;")
            mods = [m["name"] for m in await cursor.fetchall()]

    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "admin_username": admin,
        "counts": counts,
        "reports": reports,
        "statuses": ["Новая", "В работе", "Решено"],
        "modules": mods,
        "selected_status": status or "",
        "selected_module": module or "",
        "query": q or "",
        "active_page": "dashboard"
    })

# --- МАССОВЫЕ ДЕЙСТВИЯ (ЧЕКБОКСЫ) ---
@app.post("/admin/reports/bulk-action")
async def bulk_action(
    request: Request,
    report_ids: List[int] = Form(...),
    action_status: str = Form(...),
    notify_text: Optional[str] = Form(None)
):
    admin = get_admin_username(request)
    if not admin:
        return admin_redirect()

    if not report_ids:
        return RedirectResponse(url="/admin", status_code=303)

    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT id, user_id FROM reports WHERE id = ANY(%s);", (report_ids,))
            targets = await cursor.fetchall()

            await cursor.execute("UPDATE reports SET status = %s WHERE id = ANY(%s);", (action_status, report_ids))

            if notify_text and notify_text.strip():
                for t_rec in targets:
                    await cursor.execute("""
                        INSERT INTO report_messages (report_id, sender_type, sender_name, message_text)
                        VALUES (%s, 'staff', %s, %s);
                    """, (t_rec["id"], admin, notify_text.strip()))

    for t_rec in targets:
        uid = t_rec.get("user_id")
        if uid:
            u_lang = await get_user_lang(uid)
            msg = t(u_lang, "status_notification", id=t_rec["id"], status=action_status)
            if notify_text and notify_text.strip():
                msg += f"\n\n{notify_text.strip()}"
            try:
                await tg_app.bot.send_message(chat_id=uid, text=msg, parse_mode="HTML")
            except Exception:
                pass

    return RedirectResponse(url="/admin?message=bulk_success", status_code=303)

# --- КАРТОЧКА ТИКЕТА И LIVE ЧАТ ---
@app.get("/admin/reports/{report_id}", response_class=HTMLResponse)
async def report_detail(request: Request, report_id: int):
    admin = get_admin_username(request)
    if not admin:
        return admin_redirect()

    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM reports WHERE id = %s;", (report_id,))
            report = await cursor.fetchone()
            if not report:
                raise HTTPException(status_code=404, detail="Ticket not found")

            await cursor.execute("SELECT * FROM report_messages WHERE report_id = %s ORDER BY id ASC;", (report_id,))
            chat_messages = await cursor.fetchall()

            await cursor.execute("SELECT title, text FROM quick_templates ORDER BY id ASC;")
            quick_templates = await cursor.fetchall()

    return templates.TemplateResponse(request, "report_detail.html", {
        "request": request,
        "admin_username": admin,
        "report": report,
        "chat_messages": chat_messages,
        "quick_templates": quick_templates,
        "statuses": ["Новая", "В работе", "Решено"],
        "message": request.query_params.get("message")
    })

@app.post("/admin/reports/{report_id}/status")
async def update_status(request: Request, report_id: int, status: str = Form(...)):
    if not get_admin_username(request):
        return admin_redirect()

    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("UPDATE reports SET status = %s WHERE id = %s RETURNING user_id;", (status, report_id))
            row = await cursor.fetchone()

    if row and row.get("user_id"):
        u_lang = await get_user_lang(row["user_id"])
        try:
            await tg_app.bot.send_message(
                chat_id=row["user_id"],
                text=t(u_lang, "status_notification", id=report_id, status=status),
                parse_mode="HTML"
            )
        except Exception:
            pass

    return RedirectResponse(url=f"/admin/reports/{report_id}?message=status", status_code=303)

@app.post("/admin/reports/{report_id}/reply")
async def send_reply(request: Request, report_id: int, message: str = Form(...)):
    admin = get_admin_username(request)
    if not admin:
        return admin_redirect()

    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                INSERT INTO report_messages (report_id, sender_type, sender_name, message_text)
                VALUES (%s, 'staff', %s, %s);
            """, (report_id, admin, message.strip()))

            await cursor.execute("SELECT user_id FROM reports WHERE id = %s;", (report_id,))
            row = await cursor.fetchone()

    if row and row.get("user_id"):
        u_lang = await get_user_lang(row["user_id"])
        try:
            await tg_app.bot.send_message(
                chat_id=row["user_id"],
                text=t(u_lang, "reply_notification", id=report_id, text=message.strip()),
                parse_mode="HTML"
            )
        except Exception:
            pass

    return RedirectResponse(url=f"/admin/reports/{report_id}?message=reply", status_code=303)

@app.get("/admin/reports/{report_id}/messages/poll")
async def poll_messages(report_id: int, last_id: int = 0):
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                SELECT id, sender_type, sender_name, message_text, created_at
                FROM report_messages
                WHERE report_id = %s AND id > %s
                ORDER BY id ASC;
            """, (report_id, last_id))
            msgs = await cursor.fetchall()

    data = []
    for m in msgs:
        data.append({
            "id": m["id"],
            "sender_type": m["sender_type"],
            "sender_name": m["sender_name"],
            "message_text": m["message_text"],
            "created_at": m["created_at"].strftime("%H:%M | %d.%m.%Y") if m["created_at"] else ""
        })
    return JSONResponse({"messages": data})

@app.get("/admin/reports/{report_id}/screenshot")
async def get_screenshot(report_id: int):
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT screenshot_file_id FROM reports WHERE id = %s;", (report_id,))
            row = await cursor.fetchone()

    if not row or not row.get("screenshot_file_id"):
        raise HTTPException(status_code=404, detail="No screenshot")

    tg_file = await tg_app.bot.get_file(row["screenshot_file_id"])
    stream = io.BytesIO()
    await tg_file.download_to_memory(stream)
    stream.seek(0)
    return Response(content=stream.read(), media_type="image/jpeg")

# --- ЭКСПОРТ В EXCEL ---
@app.get("/admin/reports/export.xlsx")
async def export_excel(request: Request):
    if not get_admin_username(request):
        return admin_redirect()

    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM reports ORDER BY id ASC;")
            rows = await cursor.fetchall()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Reports"
    ws.append(["ID", "Дата", "ФИО", "Группа", "Модуль", "Статус", "Telegram ID", "Username", "Описание"])

    for r in rows:
        dt = r["created_at"].strftime("%Y-%m-%d %H:%M:%S") if r["created_at"] else ""
        ws.append([
            r["id"],
            dt,
            r["name"],
            r["group_name"],
            r["module"],
            r["status"],
            r["user_id"],
            r["username"],
            r["description"]
        ])

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return Response(
        content=stream.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=helpdesk_reports_{datetime.now().strftime('%Y%m%d')}.xlsx"}
    )

# --- МОДУЛИ ---
@app.get("/admin/modules", response_class=HTMLResponse)
async def admin_modules(request: Request):
    admin = get_admin_username(request)
    if not admin:
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM system_modules ORDER BY id ASC;")
            mods = await cursor.fetchall()
    return templates.TemplateResponse(request, "modules.html", {"request": request, "admin_username": admin, "modules": mods, "active_page": "modules"})

@app.post("/admin/modules/add")
async def add_module(request: Request, name: str = Form(...)):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("INSERT INTO system_modules (name) VALUES (%s) ON CONFLICT DO NOTHING;", (name.strip(),))
    return RedirectResponse(url="/admin/modules", status_code=303)

@app.post("/admin/modules/{mod_id}/toggle")
async def toggle_module(request: Request, mod_id: int):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("UPDATE system_modules SET is_active = NOT is_active WHERE id = %s;", (mod_id,))
    return RedirectResponse(url="/admin/modules", status_code=303)

@app.post("/admin/modules/{mod_id}/delete")
async def delete_module(request: Request, mod_id: int):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("DELETE FROM system_modules WHERE id = %s;", (mod_id,))
    return RedirectResponse(url="/admin/modules", status_code=303)

# --- ШАБЛОНЫ ОТВЕТОВ ---
@app.get("/admin/templates", response_class=HTMLResponse)
async def admin_templates(request: Request):
    admin = get_admin_username(request)
    if not admin:
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM quick_templates ORDER BY id ASC;")
            tpls = await cursor.fetchall()
    return templates.TemplateResponse(request, "templates.html", {
        "request": request,
        "admin_username": admin,
        "quick_templates_list": tpls,
        "active_page": "templates"
    })

@app.post("/admin/templates/add")
async def add_template(request: Request, title: str = Form(...), text: str = Form(...)):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("INSERT INTO quick_templates (title, text) VALUES (%s, %s);", (title.strip(), text.strip()))
    return RedirectResponse(url="/admin/templates", status_code=303)

@app.post("/admin/templates/{tpl_id}/delete")
async def delete_template(request: Request, tpl_id: int):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("DELETE FROM quick_templates WHERE id = %s;", (tpl_id,))
    return RedirectResponse(url="/admin/templates", status_code=303)

# --- SMART FAQ ---
@app.get("/admin/smart-faq", response_class=HTMLResponse)
async def smart_faq_page(request: Request):
    admin = get_admin_username(request)
    if not admin:
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM smart_faq_rules ORDER BY id ASC;")
            rules = await cursor.fetchall()
    return templates.TemplateResponse(request, "smart_faq.html", {"request": request, "admin_username": admin, "rules": rules, "active_page": "smart_faq"})

@app.post("/admin/smart-faq/add")
async def add_smart_faq(request: Request, title: str = Form(...), keywords: str = Form(...), reply_text: str = Form(...)):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO smart_faq_rules (title, keywords, reply_text) VALUES (%s, %s, %s);",
                (title.strip(), keywords.strip(), reply_text.strip())
            )
    return RedirectResponse(url="/admin/smart-faq", status_code=303)

@app.post("/admin/smart-faq/{rule_id}/toggle")
async def toggle_smart_faq(request: Request, rule_id: int):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("UPDATE smart_faq_rules SET is_active = NOT is_active WHERE id = %s;", (rule_id,))
    return RedirectResponse(url="/admin/smart-faq", status_code=303)

@app.post("/admin/smart-faq/{rule_id}/delete")
async def delete_smart_faq(request: Request, rule_id: int):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("DELETE FROM smart_faq_rules WHERE id = %s;", (rule_id,))
    return RedirectResponse(url="/admin/smart-faq", status_code=303)

# --- ИНСТРУКЦИИ (ГАЙДЫ) ---
@app.get("/admin/guides", response_class=HTMLResponse)
async def guides_page(request: Request):
    admin = get_admin_username(request)
    if not admin:
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT * FROM module_guides ORDER BY id DESC;")
            g_list = await cursor.fetchall()
            await cursor.execute("SELECT name FROM system_modules WHERE is_active = TRUE ORDER BY name;")
            mods = [m["name"] for m in await cursor.fetchall()]

    return templates.TemplateResponse(request, "guides.html", {
        "request": request,
        "admin_username": admin,
        "guides": g_list,
        "modules": mods,
        "active_page": "guides"
    })

@app.post("/admin/guides/add")
async def add_guide(request: Request, module_name: str = Form(...), title: str = Form(...), content: str = Form(...)):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO module_guides (module_name, title, content) VALUES (%s, %s, %s);",
                (module_name, title.strip(), content.strip())
            )
    return RedirectResponse(url="/admin/guides", status_code=303)

@app.post("/admin/guides/{gid}/delete")
async def delete_guide(request: Request, gid: int):
    if not get_admin_username(request):
        return admin_redirect()
    async with await get_conn_async() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("DELETE FROM module_guides WHERE id = %s;", (gid,))
    return RedirectResponse(url="/admin/guides", status_code=303)
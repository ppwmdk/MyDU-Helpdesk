import os
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.environ.get("PORT", 10000))

if not TOKEN:
    raise ValueError("BOT_TOKEN не найден")

app = FastAPI()
telegram_app = Application.builder().token(TOKEN).build()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот работает через webhook 🚀")


telegram_app.add_handler(CommandHandler("start", start))


@app.on_event("startup")
async def on_startup():
    await telegram_app.initialize()
    await telegram_app.start()


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
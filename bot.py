
import os
import asyncio
import tempfile
import threading
import logging
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse, quote
from http.server import HTTPServer, BaseHTTPRequestHandler

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DB_PATH = os.environ.get("DB_PATH", "stats.db")

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("VideoSaver")

limit = asyncio.Semaphore(2)
active_users = set()
last_alert = 0
MAX_SIZE = 45 * 1024 * 1024

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS users "
            "(id INTEGER PRIMARY KEY)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS downloads "
            "(id INTEGER PRIMARY KEY, platform TEXT, success INTEGER)"
        )

def register_user(user_id):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT OR IGNORE INTO users VALUES (?)",
            (user_id,)
        )

def record_download(platform, success):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO downloads (platform, success) VALUES (?, ?)",
            (platform, int(success))
        )

class HealthHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        pass

def health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

def share_keyboard():
    link = "https://t.me/VideoSaverUA24_bot"
    share = "https://t.me/share/url?url=" + quote(link, safe="")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📲 Поділитися ботом", url=share)]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user.id)

    await update.message.reply_text(
        "🎬 Вітаю у VideoSaver UA!\n\n"
        "📱 TikTok\n"
        "📸 Instagram та Reels\n"
        "▶️ YouTube та Shorts\n\n"
        "⚡ Просто надішли посилання на відео.\n"
        "💸 Безкоштовно\n"
        "🇺🇦 Українською мовою\n\n"
        "Завантажуй власні відео або контент, "
        "на збереження якого маєш дозвіл.",
        reply_markup=share_keyboard()
    )

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Твій Telegram ID: {update.effective_user.id}"
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    with sqlite3.connect(DB_PATH) as db:
        users = db.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        total = db.execute(
            "SELECT COUNT(*) FROM downloads"
        ).fetchone()[0]

        success = db.execute(
            "SELECT COUNT(*) FROM downloads WHERE success=1"
        ).fetchone()[0]

    await update.message.reply_text(
        "📊 Статистика VideoSaver UA\n\n"
        f"👥 Користувачів: {users}\n"
        f"📥 Спроб завантаження: {total}\n"
        f"✅ Успішних: {success}\n"
        f"❌ Невдалих: {total - success}"
    )

def download(url, folder):
    options = {
        "format": "best[ext=mp4][filesize<45M]/best[ext=mp4]",
        "outtmpl": folder + "/video.%(ext)s",
        "noplaylist": True,
        "max_filesize": MAX_SIZE,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        "quiet": True,
        "enable_file_urls": False,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

async def notify_admin(context, platform, error):
    global last_alert

    if not ADMIN_ID:
        return

    now = time.monotonic()

    if last_alert and now - last_alert < 300:
        return

    last_alert = now

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "⚠️ Помилка VideoSaver UA\n\n"
                f"Платформа: {platform}\n"
                f"Тип: {type(error).__name__}\n"
                "Перевір журнали Render."
            )
        )
    except Exception:
        logger.warning("Admin notification failed")

async def save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    register_user(user_id)

    if len(context.args) != 1:
        await update.message.reply_text(
            "Надішли посилання на відео."
        )
        return

    url = context.args[0]

    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except ValueError:
        await update.message.reply_text("Некоректне посилання.")
        return

    allowed = {
        "youtube.com": "YouTube",
        "youtu.be": "YouTube",
        "tiktok.com": "TikTok",
        "instagram.com": "Instagram",
    }

    platform = next(
        (
            name for domain, name in allowed.items()
            if host == domain or host.endswith("." + domain)
        ),
        None
    )

    if parsed.scheme != "https" or not platform:
        await update.message.reply_text(
            "Підтримую TikTok, Instagram та YouTube."
        )
        return

    if user_id in active_users or limit.locked():
        await update.message.reply_text(
            "⏳ Бот зайнятий. Спробуй трохи пізніше."
        )
        return

    active_users.add(user_id)

    try:
        async with limit:
            status = await update.message.reply_text(
                f"📥 Завантажую відео з {platform}..."
            )

            try:
                with tempfile.TemporaryDirectory() as folder:
                    await asyncio.to_thread(download, url, folder)

                    files = list(Path(folder).glob("*.mp4"))

                    if not files:
                        raise ValueError("Video unavailable")

                    if files[0].stat().st_size > MAX_SIZE:
                        raise ValueError("Video too large")

                    with files[0].open("rb") as video:
                        await update.message.reply_video(
                            video=video,
                            caption="🎬 @VideoSaverUA24_bot",
                            write_timeout=120,
                            reply_markup=share_keyboard()
                        )

            except Exception as error:
                record_download(platform, False)
                logger.error(
                    "Download failed: %s — %s",
                    platform,
                    type(error).__name__
                )

                await notify_admin(context, platform, error)

                await status.edit_text(
                    "❌ Не вдалося завантажити відео.\n\n"
                    "Можливо, воно недоступне, завелике "
                    "або виникла тимчасова помилка."
                )
                return

            record_download(platform, True)
            await status.edit_text("✅ Готово!")

    finally:
        active_users.discard(user_id)

async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if url.startswith("https://"):
        context.args = [url]
        await save(update, context)
    else:
        await update.message.reply_text(
            "🔗 Надішли посилання на відео з TikTok, "
            "Instagram або YouTube."
        )

init_db()

threading.Thread(
    target=health_server,
    daemon=True
).start()

app = (
    Application.builder()
    .token(TOKEN)
    .concurrent_updates(8)
    .build()
)

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("save", save))
app.add_handler(CommandHandler("stats", stats))
app.add_handler(CommandHandler("myid", myid))
app.add_handler(MessageHandler(
    filters.TEXT & ~filters.COMMAND,
    message
))

app.run_polling()

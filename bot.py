import os
import asyncio
import tempfile
import threading
import logging
import time
import secrets
import subprocess
import sys
import imageio_ffmpeg
from pathlib import Path
from urllib.parse import urlparse, quote
from http.server import HTTPServer, BaseHTTPRequestHandler
from contextlib import closing

import psycopg2
import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DATABASE_URL = os.environ["DATABASE_URL"]

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("VideoSaver")

limit = asyncio.Semaphore(2)
active_users = set()
last_alert = 0
MAX_SIZE = 45 * 1024 * 1024
pending = {}


def get_db():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        connect_timeout=10
    )


def init_db():
    with closing(get_db()) as db:
        with db:
            with db.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id BIGINT PRIMARY KEY
                    )
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS downloads (
                        id BIGSERIAL PRIMARY KEY,
                        platform TEXT,
                        success INTEGER
                    )
                """)

                cur.execute(
                    "ALTER TABLE users ENABLE ROW LEVEL SECURITY"
                )
                cur.execute(
                    "ALTER TABLE downloads ENABLE ROW LEVEL SECURITY"
                )

    logger.info("Supabase database connected successfully")


def register_user(user_id):
    with closing(get_db()) as db:
        with db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (id) VALUES (%s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (user_id,)
                )


def record_download(platform, success):
    with closing(get_db()) as db:
        with db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO downloads (platform, success) "
                    "VALUES (%s, %s)",
                    (platform, int(success))
                )


def read_stats():
    with closing(get_db()) as db:
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            users = cur.fetchone()[0]

            cur.execute("""
                SELECT COUNT(*),
                COUNT(*) FILTER (WHERE success = 1)
                FROM downloads
            """)
            total, success = cur.fetchone()

    return users, total, success


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



MENU = ReplyKeyboardMarkup([
    ["📥 Завантажити"],
    ["🌐 Платформи", "📖 Інструкція"],
    ["🆘 Повідомити про проблему", "📲 Поділитися ботом"]
], resize_keyboard=True, is_persistent=True)

async def menu(update, context):
    context.user_data.pop("feedback", None)
    await update.message.reply_text("🎬 Обери функцію або надішли посилання.", reply_markup=MENU)

async def help_user(update, context):
    await update.message.reply_text("1. Скопіюй посилання на власне або дозволене медіа.\n2. Надішли його боту.\n3. Обери відео, MP3 або фото.\nЛіміт файлу — 45 МіБ. Stories поки не підтримуються. Доступність залежить від публікації.", reply_markup=MENU)

async def feedback(update, context):
    if not ADMIN_ID:
        await update.message.reply_text("Зворотний зв'язок тимчасово недоступний.")
        return
    context.user_data["feedback"] = time.monotonic()
    await update.message.reply_text("Опиши проблему одним повідомленням. Текст та твій Telegram ID будуть передані адміністратору. Не надсилай паролі. Для скасування — /menu.")

async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Почати"), BotCommand("menu", "Головне меню"),
        BotCommand("help", "Інструкція"),
        BotCommand("feedback", "Повідомити про проблему")
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("feedback", None)
    await asyncio.to_thread(
        register_user, update.effective_user.id
    )

    await update.message.reply_text(
        "🎬 Вітаю у VideoSaver UA!\n\n"
        "📱 TikTok\n"
        "📸 Instagram та Reels\n"
        "▶️ YouTube та Shorts\n"
        "📘 Facebook\n🧵 Threads\n\n"
        "⚡ Надішли посилання та обери відео, MP3 або фото.\n"
        "💸 Безкоштовно\n"
        "🇺🇦 Українською мовою\n\n"
        "Завантажуй власні відео або контент, "
        "на збереження якого маєш дозвіл.",
        reply_markup=MENU
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Твій Telegram ID: {update.effective_user.id}"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    users, total, success = await asyncio.to_thread(read_stats)

    await update.message.reply_text(
        "📊 Статистика VideoSaver UA\n\n"
        f"👥 Користувачів: {users}\n"
        f"📥 Спроб завантаження: {total}\n"
        f"✅ Успішних: {success}\n"
        f"❌ Невдалих: {total - success}"
    )


def download(url, folder, mode):
    if mode == "photo":
        subprocess.run([sys.executable, "-m", "gallery_dl", "--ignore-config",
                        "--no-input", "--range", "1-10", "-D", folder, url],
                       check=True, timeout=90, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        return sorted(p for p in Path(folder).rglob("*")
                      if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    options = {
        "format": "bestaudio/best" if mode == "audio" else "best[ext=mp4][filesize<45M]/best[ext=mp4]",
        "outtmpl": folder + "/media.%(ext)s",
        "noplaylist": True,
        "max_filesize": MAX_SIZE,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        "quiet": True,
        "enable_file_urls": False,
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
    }
    if mode == "audio":
        options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}]
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])
    return list(Path(folder).glob("*.mp3" if mode == "audio" else "*.mp4"))


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
                f"Платформа або сервіс: {platform}\n"
                f"Тип: {type(error).__name__}\n"
                "Перевір журнали Render."
            )
        )
    except Exception:
        logger.warning("Admin notification failed")


async def safe_record(context, platform, success):
    try:
        await asyncio.to_thread(
            record_download, platform, success
        )
    except Exception as error:
        logger.error(
            "Database write failed: %s",
            type(error).__name__
        )
        await notify_admin(context, "Supabase", error)


async def save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    await asyncio.to_thread(register_user, user_id)

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
        await update.message.reply_text(
            "Некоректне посилання."
        )
        return

    allowed = {
        "youtube.com": "YouTube",
        "youtu.be": "YouTube",
        "tiktok.com": "TikTok",
        "instagram.com": "Instagram",
        "facebook.com": "Facebook",
        "fb.watch": "Facebook",
        "threads.net": "Threads",
        "threads.com": "Threads",
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
            "Підтримую TikTok, Instagram, YouTube, Facebook та Threads."
        )
        return

    now = time.monotonic()
    for key, item in list(pending.items()):
        if now - item[3] > 900:
            pending.pop(key, None)
    if len(pending) >= 1000:
        await update.message.reply_text("Бот зайнятий. Спробуй пізніше.")
        return
    key = secrets.token_hex(8)
    pending[key] = (user_id, url, platform, now)
    buttons = [[InlineKeyboardButton("🎥 Відео", callback_data=f"media:{key}:video"),
                InlineKeyboardButton("🎵 MP3", callback_data=f"media:{key}:audio")]]
    if platform != "YouTube":
        buttons.append([InlineKeyboardButton("🖼 Фото", callback_data=f"media:{key}:photo")])
    await update.message.reply_text(f"{platform}: обери формат (якщо доступний у публікації).",
                                    reply_markup=InlineKeyboardMarkup(buttons))


async def choose_media(update, context):
    query = update.callback_query
    _, key, mode = query.data.split(":")
    item = pending.get(key)
    user_id = update.effective_user.id
    if not item or item[0] != user_id or time.monotonic() - item[3] > 900:
        await query.answer("Надішли посилання повторно.", show_alert=True)
        return
    if user_id in active_users or limit.locked():
        await query.answer("Бот зайнятий. Спробуй пізніше.", show_alert=True)
        return
    _, url, platform, _ = pending.pop(key)
    active_users.add(user_id)
    try:
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        async with limit:
            status = await query.message.reply_text(f"📥 Завантажую з {platform}...")
            try:
                with tempfile.TemporaryDirectory() as folder:
                    files = await asyncio.to_thread(download, url, folder, mode)
                    if not files:
                        raise ValueError("Media unavailable")
                    if any(p.stat().st_size > MAX_SIZE for p in files):
                        raise ValueError("Media too large")
                    for path in files:
                        with path.open("rb") as media:
                            kwargs = dict(caption="🎬 @VideoSaverUA24_bot", write_timeout=120,
                                          reply_markup=share_keyboard())
                            if mode == "audio":
                                await query.message.reply_audio(audio=media, **kwargs)
                            elif mode == "photo":
                                await query.message.reply_document(document=media, **kwargs)
                            else:
                                await query.message.reply_video(video=media, **kwargs)
            except Exception as error:
                await safe_record(context, platform, False)
                logger.error("Download failed: %s / %s — %s", platform, mode, type(error).__name__)
                await notify_admin(context, platform, error)
                await status.edit_text("❌ Не вдалося завантажити. Формат може бути недоступним або файл завеликий.")
                return
            await safe_record(context, platform, True)
            await status.edit_text("✅ Готово!")
    finally:
        active_users.discard(user_id)


async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "🆘 Повідомити про проблему":
        await feedback(update, context)
        return
    if text in {"📥 Завантажити", "📊 Статистика бота", "🌐 Платформи", "📖 Інструкція", "📲 Поділитися ботом"}:
        context.user_data.pop("feedback", None)
        if text == "📊 Статистика бота":
            await menu(update, context)
        elif text == "📖 Інструкція":
            await help_user(update, context)
        elif text == "📲 Поділитися ботом":
            await update.message.reply_text("Запроси друзів!", reply_markup=share_keyboard())
        elif text == "🌐 Платформи":
            await update.message.reply_text("TikTok, Instagram, YouTube, Facebook, Threads. Формати залежать від публікації. Stories поки не підтримуються.", reply_markup=MENU)
        else:
            await update.message.reply_text("Надішли посилання на медіа.", reply_markup=MENU)
        return
    started = context.user_data.pop("feedback", None)
    if started is not None and time.monotonic() - started < 600:
        now = time.monotonic()
        if now - context.user_data.get("last_feedback", -1000) < 60:
            await update.message.reply_text("Зачекай хвилину перед наступним зверненням.")
            return
        context.user_data["last_feedback"] = now
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"🆘 Звернення від користувача {update.effective_user.id}:\n{text[:3000]}")
        await update.message.reply_text("✅ Звернення надіслано.", reply_markup=MENU)
        return
    if text.startswith("https://"):
        context.args = [text]
        await save(update, context)
    else:
        await menu(update, context)


async def error_handler(update, context):
    error = context.error

    logger.error(
        "Application error: %s",
        type(error).__name__
    )

    await notify_admin(context, "Bot / Database", error)

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Тимчасова помилка. Спробуй трохи пізніше."
            )
        except Exception:
            pass


init_db()

threading.Thread(
    target=health_server,
    daemon=True
).start()

app = (
    Application.builder()
    .token(TOKEN)
    .concurrent_updates(8)
    .post_init(post_init)
    .build()
)

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("menu", menu))
app.add_handler(CommandHandler("help", help_user))
app.add_handler(CommandHandler("feedback", feedback))
app.add_handler(CommandHandler("save", save))
app.add_handler(CommandHandler("stats", stats))
app.add_handler(CommandHandler("myid", myid))

app.add_handler(MessageHandler(
    filters.TEXT & ~filters.COMMAND,
    message
))

app.add_handler(CallbackQueryHandler(choose_media, pattern=r"^media:[0-9a-f]{16}:(video|audio|photo)$"))
app.add_error_handler(error_handler)

app.run_polling()

import os
import asyncio
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse
from http.server import HTTPServer, BaseHTTPRequestHandler

import yt_dlp
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ["BOT_TOKEN"]
limit = asyncio.Semaphore(2)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

def health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! Я VideoSaver UA!\n\n"
        "Підтримую YouTube, TikTok та Instagram.\n"
        "Для завантаження власного або дозволеного відео "
        "надішли /save та посилання."
    )

def download(url, folder):
    options = {
        "format": "best[ext=mp4][filesize<45M]/best[ext=mp4]",
        "outtmpl": folder + "/video.%(ext)s",
        "noplaylist": True,
        "max_filesize": 45 * 1024 * 1024,
        "socket_timeout": 20,
        "retries": 1,
        "quiet": True,
        "enable_file_urls": False,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

async def save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text(
            "Надішли /save та посилання на відео."
        )
        return

    url = context.args[0]
    parsed = urlparse(url)
    host = parsed.hostname or ""
    allowed = (
        "youtube.com", "youtu.be",
        "tiktok.com", "instagram.com"
    )

    if parsed.scheme != "https" or not any(
        host == d or host.endswith("." + d)
        for d in allowed
    ):
        await update.message.reply_text(
            "Надішли коректне посилання YouTube, TikTok або Instagram."
        )
        return

    if limit.locked():
        await update.message.reply_text(
            "Бот зайнятий. Спробуй трохи пізніше."
        )
        return

    async with limit:
        status = await update.message.reply_text(
            "Завантажую відео. Зачекай..."
        )

        try:
            with tempfile.TemporaryDirectory() as folder:
                await asyncio.to_thread(download, url, folder)
                files = list(Path(folder).glob("*.mp4"))

                if not files or files[0].stat().st_size > 45 * 1024 * 1024:
                    raise ValueError("Video unavailable")

                with files[0].open("rb") as video:
                    await update.message.reply_video(
                        video=video,
                        caption="VideoSaver UA",
                        write_timeout=120,
                    )

            await status.edit_text("Готово!")

        except Exception:
            await status.edit_text(
                "Не вдалося завантажити відео. "
                "Можливо, воно недоступне або завелике."
            )

async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Для дозволеного завантаження використовуй /save та посилання."
    )

threading.Thread(target=health_server, daemon=True).start()

app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("save", save))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message))
app.run_polling()

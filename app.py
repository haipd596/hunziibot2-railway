import os
from dotenv import load_dotenv
from logging_config import setup_logging

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from utils.downloader import (
    download_media,
    download_list,
    download_urls_from_text,
    download_urls_from_reply,
    handle_convert_to_audio,
)


def _load_env() -> None:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)


async def start(update, context: ContextTypes.DEFAULT_TYPE):
    if update and update.message:
        await update.message.reply_text("Bot tải file sẵn sàng. Gửi link hoặc dùng /download, /downloadlist.")


def main() -> None:
    _load_env()
    setup_logging()

    token = "8438716386:AAGjfQkwVvMCeFtUO9jE44_j5q48tYN0lV4"
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in environment")

    app = Application.builder().token(token).build()

    # Basic
    app.add_handler(CommandHandler("start", start))

    # Download features
    app.add_handler(CommandHandler(["download", "dl"], download_media))
    app.add_handler(CommandHandler(["downloadlist", "dllist", "dlall"], download_list))

    # Auto-detect links in text or captions
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), download_urls_from_text))
    app.add_handler(MessageHandler(filters.Caption(True) & filters.Entity("url"), download_urls_from_text))

    # Reply-based downloads
    app.add_handler(CommandHandler("dlreply", download_urls_from_reply))
    app.add_handler(MessageHandler(filters.REPLY, download_urls_from_reply))

    # Callback for audio conversion button
    app.add_handler(CallbackQueryHandler(handle_convert_to_audio, pattern=r"^convert_audio\|"))

    app.run_polling(allowed_updates=["message", "callback_query"]) 


if __name__ == "__main__":
    main()



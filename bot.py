import os
import re
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import instaloader

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Initialize Instaloader with custom filename pattern
L = instaloader.Instaloader(
    filename_pattern="{date_utc:%Y-%m-%d}_{profile}_{typename}_{shortcode}_{mediaid}",
    download_videos=True,
    download_video_thumbnails=True,
    save_metadata=False,
    compress_json=False,
)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message and basic usage instructions."""
    welcome_message = (
        "👋 *Welcome to the IG Downloader Bot!*\n\n"
        "Send or paste any Instagram post, reel, or IGTV link here, and I will "
        "send you the original, uncompressed files directly to this chat.\n\n"
        "*Example link:*\n`https://www.instagram.com/p/CXXXXXX/`"
    )
    if update.message:
        await update.message.reply_text(welcome_message, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends helpful information on how the bot operates."""
    help_message = (
        "ℹ️ *How to use:*\n"
        "1. Copy a post or reel link from Instagram.\n"
        "2. Paste it into this chat (or a group where I am added).\n"
        "3. Wait a few seconds while I fetch and deliver the raw files.\n\n"
        "💡 *Note:* Files are sent as documents so original quality and custom filenames are fully preserved."
    )
    if update.message:
        await update.message.reply_text(help_message, parse_mode="Markdown")

async def group_instagram_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Listens for Instagram links in DMs/Groups and uploads content as files."""
    message = update.message
    if not message or not message.text:
        return

    text = message.text
    if "instagram.com" not in text:
        return

    match = re.search(r"/(?:p|reel|tv)/([^/?#&]+)", text)
    if not match:
        return

    shortcode = match.group(1)
    chat_id = message.chat_id  # Works in DMs and group chats

    status_msg = await message.reply_text("Fetching and downloading file(s)...")

    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        target_dir = f"temp_{shortcode}"
        L.download_post(post, target=target_dir)

        # Upload files to Telegram
        for file in os.listdir(target_dir):
            if file.endswith((".mp4", ".jpg", ".png")):
                file_path = os.path.join(target_dir, file)

                with open(file_path, "rb") as f:
                    # send_document forces Telegram to deliver media as raw files
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=file,  # Keeps {Date}_{Profile}_{Type} filename
                    )
                os.remove(file_path)

        # Clean up temporary directory and metadata files
        for f in os.listdir(target_dir):
            os.remove(os.path.join(target_dir, f))
        os.rmdir(target_dir)

        await status_msg.delete()

    except Exception as e:
        await message.reply_text(f"Error processing link: {str(e)}")

if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set!")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Register frontend UI command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))

    # Register main Instagram listener handler
    app.add_handler(
        MessageHandler(filters.TEXT & (~filters.COMMAND), group_instagram_listener)
    )

    print("Group-ready bot is running...")
    app.run_polling()


import os
import re
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
import instaloader

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

L = instaloader.Instaloader(
    filename_pattern="{date_utc:%Y-%m-%d}_{profile}_{typename}_{shortcode}_{mediaid}",
    download_videos=True,
    download_video_thumbnails=True,
    save_metadata=False,
    compress_json=False
)

async def group_instagram_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    chat_id = message.chat_id  # Automatically targets whichever chat (Group or DM) sent the link

    status_msg = await message.reply_text("Fetching and renaming file...")

    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        target_dir = f"temp_{shortcode}"
        L.download_post(post, target=target_dir)

        for file in os.listdir(target_dir):
            if file.endswith((".mp4", ".jpg", ".png")):
                file_path = os.path.join(target_dir, file)
                
                with open(file_path, "rb") as f:
                    if file.endswith(".mp4"):
                        await context.bot.send_video(
                            chat_id=chat_id, 
                            video=f, 
                            filename=file # Applies your {Date}_{IGName}_{Type} name
                        )
                    else:
                        await context.bot.send_photo(
                            chat_id=chat_id, 
                            photo=f, 
                            filename=file
                        )
                os.remove(file_path)

        for f in os.listdir(target_dir):
            os.remove(os.path.join(target_dir, f))
        os.rmdir(target_dir)

        await status_msg.delete()

    except Exception as e:
        await message.reply_text(f"Error: {str(e)}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Listens to text everywhere (DMs and Groups) without needing a command
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), group_instagram_listener))
    
    print("Group-ready bot is running...")
    app.run_polling()

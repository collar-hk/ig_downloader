import os
import re
from telegram import Update, InputMediaDocument
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import instaloader

# --- Environment Variables ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
IG_USERNAME = os.environ.get("IG_USERNAME")  # Required for stories
IG_PASSWORD = os.environ.get("IG_PASSWORD")  # Required for stories

# --- Initialize Instaloader ---
L = instaloader.Instaloader(
    filename_pattern="{date_utc:%Y-%m-%d}_{profile}_{typename}_{shortcode}_{mediaid}",
    download_videos=True,
    download_video_thumbnails=True,
    save_metadata=False,
    compress_json=False,
)

# Login to Instagram (Required for downloading Stories)
if IG_USERNAME and IG_PASSWORD:
    try:
        L.login(IG_USERNAME, IG_PASSWORD)
        print(f"Logged in to Instagram as {IG_USERNAME}")
    except Exception as e:
        print(f"Instagram Login Failed: {e}")
else:
    print("WARNING: No IG_USERNAME or IG_PASSWORD provided. Story downloads will fail.")


# --- Command Handlers (Frontend UI) ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_message = (
        "👋 *Welcome to the IG Downloader Bot!*\n\n"
        "Send me an Instagram Post, Reel, IGTV, or Story link, and I will "
        "send you the original, uncompressed files directly to this chat.\n\n"
        "*Example link:*\n`https://www.instagram.com/p/DcnCZlmFNdi/`"
    )
    if update.message:
        await update.message.reply_text(welcome_message, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_message = (
        "ℹ️ *How to use:*\n"
        "Just paste a link into this chat and wait a few seconds!\n\n"
        "💡 *Note:* If you send a general story link (e.g., `/stories/username/`), "
        "I will download ALL of their current active stories. If you send a specific story link, I will download just that one."
    )
    if update.message:
        await update.message.reply_text(help_message, parse_mode="Markdown")


# --- Main Instagram Listener ---
async def group_instagram_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    text = message.text
    if "instagram.com" not in text:
        return

    # Check for Post/Reel/TV link OR Story link
    post_match = re.search(r"/(?:p|reel|tv)/([^/?#&]+)", text)
    story_match = re.search(r"/stories/([^/?#&]+)(?:/([^/?#&]+))?", text)

    if not post_match and not story_match:
        return

    chat_id = message.chat_id
    status_msg = await message.reply_text("Fetching and downloading file(s)...")

    try:
        # 1. Handle Posts, Reels, and IGTV
        if post_match:
            shortcode = post_match.group(1)
            target_dir = f"temp_{shortcode}"
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            L.download_post(post, target=target_dir)

        # 2. Handle Stories and Highlights
        elif story_match:
            username = story_match.group(1)
            media_id = story_match.group(2) 
            
            # TRAP: Handle the Highlight URL limitation
            if username == "highlights":
                await status_msg.edit_text(
                    "⚠️ **Highlight Detected:**\n"
                    "I cannot download highlights directly from this link because Instagram hides the username in highlight URLs.\n\n"
                    "**How to fix:** Send me the profile's main story link (e.g., `instagram.com/stories/username/`) and I will download their active stories!"
                )
                return

            target_dir = f"temp_story_{username}"
            profile = instaloader.Profile.from_username(L.context, username)
            
            # Download stories
            for story in L.get_stories(userids=[profile.userid]):
                for item in story.get_items():
                    # If specific story ID is provided in URL, download just that one
                    if media_id and str(item.mediaid) == media_id:
                        L.download_storyitem(item, target=target_dir)
                        break
                    # Otherwise, download all active stories for the user
                    elif not media_id:
                        L.download_storyitem(item, target=target_dir)

        # Check if folder exists (prevents crashes if nothing downloaded)
        if not os.path.exists(target_dir):
             raise Exception("No media could be downloaded (Account might be private or stories expired).")

        # Collect all valid files
        valid_files = sorted([f for f in os.listdir(target_dir) if f.endswith((".mp4", ".jpg", ".png"))])
        
        media_group = []
        file_handles = []

        try:
            for file in valid_files:
                file_path = os.path.join(target_dir, file)
                f = open(file_path, "rb")
                file_handles.append(f)
                
                # Replace the ugly GraphQL names with friendly ones for the final download
                friendly_name = file.replace("GraphImage", "Photo") \
                                    .replace("GraphVideo", "Video") \
                                    .replace("GraphSidecar", "Album") \
                                    .replace("GraphStoryImage", "Story_Photo") \
                                    .replace("GraphStoryVideo", "Story_Video")

                # Appending as InputMediaDocument ensures raw file delivery & album grouping
                media_group.append(InputMediaDocument(media=f, filename=friendly_name))

            # Send in chunks of 10 (Telegram's album limit)
            if media_group:
                for i in range(0, len(media_group), 10):
                    chunk = media_group[i:i + 10]
                    await context.bot.send_media_group(chat_id=chat_id, media=chunk)
                    
        finally:
            # Safely close files before deletion
            for f in file_handles:
                f.close()

        # Clean up temporary directory
        for f in os.listdir(target_dir):
            os.remove(os.path.join(target_dir, f))
        os.rmdir(target_dir)

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"Error processing link: {str(e)}\n\n*(Note: Story links require the bot to be logged in via environment variables).*")


# --- Main Execution ---
if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set!")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Register frontend UI command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    
    # Register main Instagram listener handler
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), group_instagram_listener))

    print("Bot is running! Press Ctrl+C to stop.")
    app.run_polling()

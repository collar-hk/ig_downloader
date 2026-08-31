import os
import re
import uuid
import asyncio
import logging
from collections import defaultdict
from telegram import Update, InputMediaDocument
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import instaloader

# --- 1. Setup Production Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Variables ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
IG_USERNAME = os.environ.get("IG_USERNAME")
IG_PASSWORD = os.environ.get("IG_PASSWORD")

# --- Concurrency & Timeout Settings ---
MAX_TASKS_PER_USER = 2
DOWNLOAD_TIMEOUT_SECONDS = 120
user_active_tasks = defaultdict(int)

# --- 2. Initial Login & Session Creation (Runs once on startup) ---
# We do this once at startup to create/validate the session file.
if IG_USERNAME:
    SESSION_FILE = f"ig_session_{IG_USERNAME}"
    L_init = instaloader.Instaloader()
    try:
        L_init.load_session_from_file(IG_USERNAME, SESSION_FILE)
        logger.info(f"Loaded existing IG session for {IG_USERNAME}")
    except FileNotFoundError:
        if IG_PASSWORD:
            try:
                logger.info(f"No session found. Logging into Instagram as {IG_USERNAME}...")
                L_init.login(IG_USERNAME, IG_PASSWORD)
                L_init.save_session_to_file(SESSION_FILE)
                logger.info("Successfully logged in and saved session cookie.")
            except Exception as e:
                logger.error(f"Instagram Login Failed: {e}. Check if IG flagged the account.")
        else:
            logger.warning("IG_USERNAME provided but no IG_PASSWORD. Cannot login.")
else:
    logger.warning("No IG_USERNAME provided. Story downloads will fail.")


# --- Background Download Functions (Thread-Safe) ---
def get_thread_safe_loader():
    """Creates a fresh, isolated Instaloader instance for the current background thread."""
    local_L = instaloader.Instaloader(
        filename_pattern="{date_utc:%Y-%m-%d}_{profile}_{typename}_{shortcode}_{mediaid}",
        download_videos=True,
        download_video_thumbnails=True,
        save_metadata=False,
        compress_json=False,
    )
    if IG_USERNAME:
        session_file = f"ig_session_{IG_USERNAME}"
        if os.path.exists(session_file):
            local_L.load_session_from_file(IG_USERNAME, session_file)
    return local_L

def download_post_sync(shortcode, target_dir):
    try:
        local_L = get_thread_safe_loader()
        post = instaloader.Post.from_shortcode(local_L.context, shortcode)
        local_L.download_post(post, target=target_dir)
    except instaloader.exceptions.ConnectionException:
        raise Exception("Instagram blocked the connection. The account may require verification.")

def download_story_sync(username, media_id, target_dir):
    try:
        local_L = get_thread_safe_loader()
        profile = instaloader.Profile.from_username(local_L.context, username)
        
        for story in local_L.get_stories(userids=[profile.userid]):
            for item in story.get_items():
                if media_id:
                    # Target exactly one story
                    if str(item.mediaid).startswith(media_id):
                        local_L.download_storyitem(item, target=target_dir)
                        return 
                else:
                    # Download all active stories for the user
                    local_L.download_storyitem(item, target=target_dir)
    except instaloader.exceptions.LoginRequiredException:
        raise Exception("Story downloads require a valid login session, but the session expired or failed.")
    except instaloader.exceptions.ConnectionException:
        raise Exception("Instagram blocked the connection. Log into the account manually to clear the warning.")


# --- Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_message = "👋 *Welcome to the IG Downloader Bot!*\n\nSend me any Instagram Post, Reel, IGTV, or Story link."
    if update.message:
        await update.message.reply_text(welcome_message, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("ℹ️ Paste a link. Max 2 concurrent downloads per user allowed.")


# --- Main Listener ---
async def group_instagram_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    text = message.text
    if "instagram.com" not in text:
        return

    # Extract shortcode/username cleanly, ignoring ?utm tracking garbage
    post_match = re.search(r"/(?:p|reel|tv)/([^/?#&]+)", text)
    story_match = re.search(r"/stories/([^/?#&]+)(?:/([^/?#&]+))?", text)

    if not post_match and not story_match:
        return

    user_id = message.from_user.id
    chat_id = message.chat_id

    if user_active_tasks[user_id] >= MAX_TASKS_PER_USER:
        await message.reply_text("⏳ You have too many active downloads. Please wait for them to finish.")
        return

    user_active_tasks[user_id] += 1
    unique_id = uuid.uuid4().hex[:8]
    target_dir = f"temp_{unique_id}"
    
    status_msg = await message.reply_text("⏳ Fetching... (This may take up to 2 minutes)")
    logger.info(f"User {user_id} requested download into {target_dir}")

    try:
        if post_match:
            shortcode = post_match.group(1)
            await asyncio.wait_for(
                asyncio.to_thread(download_post_sync, shortcode, target_dir),
                timeout=DOWNLOAD_TIMEOUT_SECONDS
            )
            
        elif story_match:
            username = story_match.group(1)
            media_id = story_match.group(2) 

            if username == "highlights":
                await status_msg.edit_text("⚠️ Cannot download highlights directly. Please send the user's main story link instead.")
                return

            await status_msg.edit_text(f"⏳ Downloading stories for @{username}...")
            
            await asyncio.wait_for(
                asyncio.to_thread(download_story_sync, username, media_id, target_dir),
                timeout=DOWNLOAD_TIMEOUT_SECONDS
            )

        # Make sure the folder was created and isn't empty
        if not os.path.exists(target_dir) or not os.listdir(target_dir):
             raise Exception("No media was downloaded. The account might be private, stories expired, or no new content.")

        valid_files = sorted([f for f in os.listdir(target_dir) if f.endswith((".mp4", ".jpg", ".png"))])
        media_group = []
        file_handles = []

        await status_msg.edit_text(f"📤 Uploading {len(valid_files)} file(s) to Telegram...")

        try:
            for file in valid_files:
                file_path = os.path.join(target_dir, file)
                f = open(file_path, "rb")
                file_handles.append(f)
                
                # Make the filenames human-readable
                friendly_name = file.replace("GraphImage", "Photo").replace("GraphVideo", "Video").replace("GraphSidecar", "Album").replace("GraphStoryImage", "Story_Photo").replace("GraphStoryVideo", "Story_Video")
                media_group.append(InputMediaDocument(media=f, filename=friendly_name))

            # Send files in albums of 10 maximum (Telegram limit)
            if media_group:
                for i in range(0, len(media_group), 10):
                    await context.bot.send_media_group(chat_id=chat_id, media=media_group[i:i + 10])
                    
        finally:
            # Files must be closed before the OS will let us delete them
            for f in file_handles:
                f.close()

        await status_msg.delete()
        logger.info(f"Successfully processed request for User {user_id}")

    except asyncio.TimeoutError:
        logger.warning(f"Timeout for User {user_id} in {target_dir}")
        await status_msg.edit_text("❌ Download timed out! Instagram took too long to respond. Try again later.")
    except Exception as e:
        logger.error(f"Error for User {user_id}: {str(e)}")
        await status_msg.edit_text(f"❌ Error: {str(e)}")
    finally:
        # --- 3. Memory & File Cleanup ---
        user_active_tasks[user_id] -= 1
        if user_active_tasks[user_id] <= 0:
            del user_active_tasks[user_id] 
        
        # Safely wipe the temporary folder
        if os.path.exists(target_dir):
            for f in os.listdir(target_dir):
                try:
                    os.remove(os.path.join(target_dir, f))
                except OSError as e:
                    logger.error(f"Failed to delete file {f}: {e}")
            try:
                os.rmdir(target_dir)
            except OSError as e:
                logger.error(f"Failed to delete directory {target_dir}: {e}")


# --- Main Execution ---
if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN is missing! Bot cannot start.")
        exit(1)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), group_instagram_listener))
    
    logger.info("Bot background service is starting...")
    app.run_polling()

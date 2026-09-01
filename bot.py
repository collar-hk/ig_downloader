#!/usr/bin/env python3
"""Instagram & YouTube downloader Telegram bot with multi-session rotation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import instaloader
import yt_dlp
from telegram import InputMediaDocument, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_RAW = os.environ.get("ADMIN_TELEGRAM_IDS", "")
ADMIN_TELEGRAM_IDS = {int(x.strip()) for x in ADMIN_RAW.split(",") if x.strip().isdigit()}

MAX_TASKS_PER_USER = 2
MAX_GLOBAL_DOWNLOADS = 5
DOWNLOAD_TIMEOUT_SECONDS = 300  # Increased timeout for larger YouTube video processing
TELEGRAM_MEDIA_GROUP_LIMIT = 10
MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".jpg", ".jpeg", ".png", ".webp"}

CACHE_PATH = Path(os.environ.get("USER_ID_CACHE_PATH", "user_id_cache.json"))

# Match Instagram URLs
INSTAGRAM_HOST_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:instagram\.com|instagr\.am)/", re.I)
POST_RE = re.compile(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", re.I)
STORY_RE = re.compile(r"/stories/([^/?#&]+)(?:/([^/?#&]+))?", re.I)

# Match YouTube URLs
YOUTUBE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})",
    re.I,
)

_cache_lock = threading.Lock()
_user_id_cache: dict[str, int] = {}
_tls = threading.local()
_global_download_sema: asyncio.Semaphore | None = None
_slot_lock: asyncio.Lock | None = None

# Session Rotation State
_session_lock = threading.Lock()
_session_index = 0


class DownloadError(Exception):
    """User-facing download failure."""


def _normalize_username(username: str) -> str:
    return username.strip().lstrip("@").lower()


def get_available_session_files() -> list[Path]:
    """Find all files starting with 'ig_session_' in the working directory."""
    return sorted(list(Path(".").glob("ig_session_*")))


def get_next_session_file() -> tuple[str, Path] | tuple[None, None]:
    """Get the next session file using round-robin rotation."""
    global _session_index
    session_files = get_available_session_files()
    if not session_files:
        return None, None

    with _session_lock:
        selected_file = session_files[_session_index % len(session_files)]
        _session_index = (_session_index + 1) % len(session_files)

    # File format is expected to be `ig_session_username`
    username = selected_file.name.replace("ig_session_", "")
    return username, selected_file


def load_user_id_cache() -> None:
    global _user_id_cache
    if not CACHE_PATH.exists():
        return
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _user_id_cache = {
                _normalize_username(str(k)): int(v)
                for k, v in data.items()
                if str(v).isdigit() or isinstance(v, int)
            }
            logger.info("Loaded %s cached Instagram user IDs", len(_user_id_cache))
    except (OSError, ValueError) as exc:
        logger.warning("Could not load user ID cache: %s", exc)


def save_user_id_cache() -> None:
    with _cache_lock:
        snapshot = dict(_user_id_cache)
    try:
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except OSError as exc:
        logger.warning("Could not persist user ID cache: %s", exc)


def cache_get(username: str) -> int | None:
    key = _normalize_username(username)
    with _cache_lock:
        return _user_id_cache.get(key)


def cache_set(username: str, user_id: int) -> None:
    key = _normalize_username(username)
    with _cache_lock:
        _user_id_cache[key] = user_id
    save_user_id_cache()


def get_user_id_from_public_html(username: str) -> int | None:
    """Scrape profile_id from public HTML, optionally attaching session cookies."""
    url = f"https://www.instagram.com/{username}/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Mode": "navigate",
    }

    loader, _ = get_thread_safe_loader()
    if loader and loader.context._session.cookies:
        try:
            session_cookies = loader.context._session.cookies
            cookie_str = "; ".join([f"{c.name}={c.value}" for c in session_cookies])
            headers["Cookie"] = cookie_str
        except Exception as err:
            logger.debug("Could not attach session cookies to HTML request: %s", err)

    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=12) as response:
            html = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        logger.warning("HTML fallback request failed for @%s: %s", username, exc)
        return None

    patterns = [
        r'\\"profile_id\\":\\"(\d+)\\"',
        r'"profile_id":"(\d+)"',
        r'"container_id":"(\d+)"',
        r'"user_id":"(\d+)"',
        r'profilePage_(\d+)',
        r'instagram://user\?username=[^"]+?&id=(\d+)',
        r'"owner":\{"id":"(\d+)"',
        r'"id":"(\d+)","username":"' + re.escape(username) + r'"',
    ]

    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            extracted_id = int(match.group(1))
            logger.info("Successfully scraped user ID %s for @%s from HTML", extracted_id, username)
            return extracted_id

    logger.warning("HTML fallback fetched page for @%s, but no valid profile ID pattern matched.", username)
    return None


def get_thread_safe_loader(rotate_session: bool = False) -> tuple[instaloader.Instaloader, str | None]:
    """Gets thread-local Instaloader instance, cycling to next session if requested."""
    loader = getattr(_tls, "loader", None)
    current_user = getattr(_tls, "current_username", None)

    if loader is not None and not rotate_session:
        return loader, current_user

    loader = instaloader.Instaloader(
        filename_pattern="{date_utc:%Y-%m-%d}_{profile}_{typename}_{mediaid}",
        download_videos=True,
        download_video_thumbnails=True,
        save_metadata=False,
        compress_json=False,
        max_connection_attempts=2,
        request_timeout=60.0,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    )

    ig_user, session_path = get_next_session_file()
    if ig_user and session_path and session_path.exists():
        try:
            loader.load_session_from_file(ig_user, str(session_path))
            logger.info("Thread %s using session file: %s", threading.get_ident(), session_path.name)
        except (OSError, instaloader.exceptions.InstaloaderException) as exc:
            logger.error("Failed to load session file %s: %s", session_path, exc)
            ig_user = None

    _tls.loader = loader
    _tls.current_username = ig_user
    return loader, ig_user


def _friendly_filename(name: str) -> str:
    replacements = (
        ("GraphImage", "Photo"),
        ("GraphVideo", "Video"),
        ("GraphSidecar", "Album"),
        ("GraphStoryImage", "Story_Photo"),
        ("GraphStoryVideo", "Story_Video"),
    )
    for old, new in replacements:
        name = name.replace(old, new)
    return name


def resolve_instagram_user_id(loader: instaloader.Instaloader, username: str) -> int:
    username = _normalize_username(username)

    cached = cache_get(username)
    if cached is not None:
        logger.info("Cache hit for @%s (ID %s)", username, cached)
        return cached

    logger.info("Cache miss for @%s; attempting resolution", username)

    user_id = get_user_id_from_public_html(username)
    if user_id:
        cache_set(username, user_id)
        return user_id

    try:
        logger.info("HTML lookup failed for @%s; trying Instaloader API", username)
        profile = instaloader.Profile.from_username(loader.context, username)
        cache_set(username, profile.userid)
        return profile.userid
    except Exception as exc:
        logger.error("Failed to resolve user ID for @%s: %s", username, exc)
        raise DownloadError(
            f"Could not automatically resolve ID for @{username}. "
            f"An admin can set it using: /setid {username} <numeric_id>"
        ) from exc


def download_post_sync(shortcode: str, target_dir: str) -> None:
    session_files = get_available_session_files()
    attempts = max(1, len(session_files))

    for attempt in range(attempts):
        try:
            loader, current_user = get_thread_safe_loader(rotate_session=(attempt > 0))
            post = instaloader.Post.from_shortcode(loader.context, shortcode)
            loader.download_post(post, target=Path(target_dir))
            return
        except DownloadError:
            raise
        except Exception as exc:
            logger.warning("Attempt %s failed with session '%s': %s", attempt + 1, current_user, exc)
            if attempt == attempts - 1:
                raise DownloadError(f"Failed to fetch that post or reel: {exc}") from exc


def _story_item_matches(item: Any, media_id: str | None) -> bool:
    if not media_id:
        return True
    item_id = str(item.mediaid)
    return item_id == media_id or item_id.startswith(media_id)


def download_story_sync(username: str, media_id: str | None, target_dir: str) -> None:
    username = _normalize_username(username)
    session_files = get_available_session_files()
    attempts = max(1, len(session_files))

    for attempt in range(attempts):
        try:
            loader, current_user = get_thread_safe_loader(rotate_session=(attempt > 0))
            user_id = resolve_instagram_user_id(loader, username)
            found = False
            for story in loader.get_stories(userids=[user_id]):
                for item in story.get_items():
                    if not _story_item_matches(item, media_id):
                        continue
                    loader.download_storyitem(item, target=Path(target_dir))
                    found = True
                    if media_id:
                        break
                if found and media_id:
                    break

            if not found and media_id:
                raise DownloadError("That story was not found or has expired.")
            if not found:
                raise DownloadError("No active stories found for that account.")
            return
        except DownloadError:
            raise
        except Exception as exc:
            logger.warning(
                "Attempt %s failed for story @%s with session '%s': %s",
                attempt + 1,
                username,
                current_user,
                exc,
            )
            if attempt == attempts - 1:
                raise DownloadError(f"Failed to fetch that story: {exc}") from exc


def download_youtube_sync(url: str, target_dir: str) -> None:
    """Download best video and audio using yt-dlp and auto-merge via ffmpeg."""
    ydl_opts = {
        # Downloads highest quality video + highest quality audio and merges them into mp4
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(target_dir, "%(title)s [%(id)s].%(ext)s"),
        "cookiefile": "cookies-youtube-com.txt",
        "quiet": True,
        "no_warnings": True,
        # Restrict filenames to avoid weird special characters in pathing
        "restrictfilenames": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        logger.error("yt-dlp download failed for %s: %s", url, exc)
        raise DownloadError(f"Failed to download YouTube video: {exc}") from exc


def _flatten_target_directory(target_dir: str) -> list[Path]:
    target_path = Path(target_dir)
    media_files: list[Path] = []

    for path in list(target_path.rglob("*")):
        if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES:
            if path.parent != target_path:
                dest = target_path / path.name
                if dest.exists():
                    dest.unlink()
                shutil.move(str(path), str(dest))
                media_files.append(dest)
            else:
                media_files.append(path)

    return sorted(list(set(media_files)), key=lambda p: p.name)


def _task_counts(context: ContextTypes.DEFAULT_TYPE) -> dict[int, int]:
    return context.application.bot_data.setdefault("user_active_tasks", {})


async def _acquire_user_slot(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    assert _slot_lock is not None
    async with _slot_lock:
        counts = _task_counts(context)
        current = counts.get(user_id, 0)
        if current >= MAX_TASKS_PER_USER:
            return False
        counts[user_id] = current + 1
        return True


async def _release_user_slot(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    assert _slot_lock is not None
    async with _slot_lock:
        counts = _task_counts(context)
        current = counts.get(user_id, 0) - 1
        if current <= 0:
            counts.pop(user_id, None)
        else:
            counts[user_id] = current


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Welcome to the Media Downloader Bot.\n\n"
        "Send an Instagram link (post, reel, story, IGTV) or YouTube link.",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        f"Paste an Instagram or YouTube link. Max {MAX_TASKS_PER_USER} concurrent downloads per user."
    )


async def setid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not user:
        return

    if not ADMIN_TELEGRAM_IDS or user.id not in ADMIN_TELEGRAM_IDS:
        await message.reply_text("You are not authorized to use this command.")
        return

    if not context.args or len(context.args) != 2:
        await message.reply_text(
            "Usage: /setid username numeric_id\nExample: /setid ivyysooo 654976877"
        )
        return

    username = _normalize_username(context.args[0])
    try:
        target_id = int(context.args[1].strip())
    except ValueError:
        await message.reply_text("The ID must be numbers only.")
        return

    cache_set(username, target_id)
    await message.reply_text(f"Saved. ID {target_id} is linked to @{username}.")


def _safe_edit(status_msg: Any, text: str):
    return status_msg.edit_text(text)


async def media_listener(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    text = message.text

    # Route request based on matchers
    is_instagram = INSTAGRAM_HOST_RE.search(text)
    is_youtube = YOUTUBE_RE.search(text)

    if not is_instagram and not is_youtube:
        return

    post_match = POST_RE.search(text) if is_instagram else None
    story_match = STORY_RE.search(text) if is_instagram else None

    # If it matched instagram host but not a supported post/story structure, ignore
    if is_instagram and not post_match and not story_match:
        return

    user = message.from_user
    if not user:
        return
    user_id = user.id
    chat_id = message.chat_id

    if not await _acquire_user_slot(context, user_id):
        await message.reply_text("You have too many active downloads. Please wait for them to finish.")
        return

    target_dir = tempfile.mkdtemp(prefix=f"dl_{uuid.uuid4().hex[:8]}_")
    status_msg = await message.reply_text("Fetching… this may take a moment.")
    logger.info("User %s requested download into %s", user_id, target_dir)

    sema = _global_download_sema
    assert sema is not None

    try:
        async with sema:
            if is_youtube:
                yt_url = is_youtube.group(0)
                await _safe_edit(status_msg, "Downloading YouTube video in highest quality...")
                await asyncio.wait_for(
                    asyncio.to_thread(download_youtube_sync, yt_url, target_dir),
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )
            elif post_match:
                shortcode = post_match.group(1)
                await asyncio.wait_for(
                    asyncio.to_thread(download_post_sync, shortcode, target_dir),
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )
            elif story_match:
                username = _normalize_username(story_match.group(1))
                media_id = story_match.group(2)
                if username == "highlights":
                    await _safe_edit(
                        status_msg,
                        "Cannot download highlights from that URL. Send the user's main story link instead.",
                    )
                    return
                await _safe_edit(status_msg, f"Downloading stories for @{username}…")
                await asyncio.wait_for(
                    asyncio.to_thread(download_story_sync, username, media_id, target_dir),
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )

        await asyncio.sleep(0.5)

        entries = _flatten_target_directory(target_dir)

        if not entries:
            raise DownloadError(
                "No media was downloaded. The resource might be unavailable or private."
            )

        await _safe_edit(status_msg, f"Uploading {len(entries)} file(s) to Telegram…")

        with ExitStack() as stack:
            media_group: list[InputMediaDocument] = []
            for path in entries:
                handle = stack.enter_context(path.open("rb"))
                media_group.append(
                    InputMediaDocument(media=handle, filename=_friendly_filename(path.name))
                )
            for i in range(0, len(media_group), TELEGRAM_MEDIA_GROUP_LIMIT):
                await context.bot.send_media_group(
                    chat_id=chat_id,
                    media=media_group[i : i + TELEGRAM_MEDIA_GROUP_LIMIT],
                )

        await status_msg.delete()
        logger.info("Successfully processed request for user %s", user_id)

    except asyncio.TimeoutError:
        logger.warning("Timeout for user %s in %s", user_id, target_dir)
        await _safe_edit(status_msg, "Download timed out. The request took too long to complete.")
    except DownloadError as exc:
        logger.error("Download error for user %s: %s", user_id, exc)
        await _safe_edit(status_msg, f"Error: {exc}")
    except Exception:
        logger.exception("Unexpected error for user %s", user_id)
        await _safe_edit(status_msg, "Error: something went wrong while processing that link.")
    finally:
        await _release_user_slot(context, user_id)
        shutil.rmtree(target_dir, ignore_errors=True)


async def _post_init(application: Application) -> None:
    global _global_download_sema, _slot_lock
    _global_download_sema = asyncio.Semaphore(MAX_GLOBAL_DOWNLOADS)
    _slot_lock = asyncio.Lock()
    load_user_id_cache()

    sessions = get_available_session_files()
    logger.info("Detected %d session file(s): %s", len(sessions), [s.name for s in sessions])

    if not ADMIN_TELEGRAM_IDS:
        logger.warning("ADMIN_TELEGRAM_IDS is empty; /setid is disabled for everyone.")
    logger.info("Bot is ready.")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN is missing. Bot cannot start.")
        sys.exit(1)

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(8)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("setid", setid_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), media_listener))

    logger.info("Bot background service is starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

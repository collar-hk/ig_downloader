#!/usr/bin/env python3
"""Instagram & YouTube downloader Telegram bot."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import instaloader
import yt_dlp
import time
from telegram import InputMediaDocument, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from story_cache import StoryUnavailable, parse_story_target

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
DOWNLOAD_TIMEOUT_SECONDS = 600  # Extended timeout for 4K video downloads and re-encoding
YOUTUBE_DOWNLOAD_TIMEOUT_SECONDS = 1800  # 4K fetch + H.264/H.265 re-encode can be slow
TELEGRAM_MEDIA_GROUP_LIMIT = 10
MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".jpg", ".jpeg", ".png", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}

# Netscape cookies files
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "cookies.txt")
IG_COOKIES_FILE = os.environ.get("IG_COOKIES_FILE", "instagram_cookies.txt")

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_PATH = Path(os.environ.get("USER_ID_CACHE_PATH", str(SCRIPT_DIR / "user_id_cache.json")))

# Match Instagram URLs
INSTAGRAM_HOST_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:instagram\.com|instagr\.am)/", re.I)
POST_RE = re.compile(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", re.I)
STORY_RE = re.compile(r"/stories/([^/?#&]+)(?:/([^/?#&]+))?", re.I)

# Match YouTube URLs
YOUTUBE_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})",
    re.I,
)

_cache_lock = threading.Lock()
_user_id_cache: dict[str, int] = {}
_tls = threading.local()
_global_download_sema: asyncio.Semaphore | None = None
_slot_lock: asyncio.Lock | None = None


class DownloadError(Exception):
    """User-facing download failure."""

    def __init__(self, message: str, *, retry_session: bool = False, kind: str = "generic"):
        super().__init__(message)
        self.retry_session = retry_session
        self.kind = kind


_IG_USER_MESSAGES = {
    "login": (
        "Instagram rejected the session. "
        "Please update instagram_cookies.txt with fresh Netscape cookies from your browser."
    ),
    "not_found": "That Instagram link was not found. It may have been deleted.",
    "private": "This account is private, and the current Instagram session cannot access it.",
    "no_stories": "No active stories found for that account right now.",
    "expired_story": "That story was not found or has already expired.",
    "generic": "Could not download that Instagram media. Please try again later.",
}

_YT_USER_MESSAGES = {
    "bot_check": (
        "YouTube is asking to confirm this is not a bot. "
        "Export a Netscape cookies.txt from a logged-in browser, place it next to the bot, then retry."
    ),
    "rate_limit": "YouTube is temporarily rate-limiting this IP. Please wait a few minutes and try again.",
    "unavailable": "That YouTube video is unavailable (private, removed, or region-blocked).",
    "age": "That YouTube video is age-restricted and needs a logged-in cookies.txt to download.",
    "generic": "Could not download that YouTube video. Please try again later.",
}


def _exc_blob(exc: BaseException | str | None) -> str:
    if exc is None:
        return ""
    if isinstance(exc, str):
        return exc
    parts = [type(exc).__name__, str(exc)]
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        parts.append(type(cause).__name__)
        parts.append(str(cause))
    return " ".join(parts)


def log_failed_account(account_name: str, error_msg: str):
    """Appends failed target accounts or sessions to a dedicated log file."""
    try:
        with open("failed_accounts.log", "a", encoding="utf-8") as f:
            f.write(f"Failed Account: {account_name} | Error: {error_msg}\n")
    except IOError as e:
        logger.error("Could not write to failed_accounts.log: %s", e)


def _normalize_username(username: str) -> str:
    return username.strip().lstrip("@").lower()


def get_available_session_files() -> list[Path]:
    """Find all ig_session_* files next to bot.py."""
    return sorted(SCRIPT_DIR.glob("ig_session_*"))


def get_session_file() -> tuple[str, Path] | tuple[None, None]:
    """Use the first ig_session_* file."""
    session_files = get_available_session_files()
    if not session_files:
        return None, None

    selected_file = session_files[0]
    username = selected_file.name.replace("ig_session_", "", 1)
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


def cache_set(username: str, user_id: int) -> None:
    key = _normalize_username(username)
    with _cache_lock:
        _user_id_cache[key] = user_id
    save_user_id_cache()


def get_thread_safe_loader() -> tuple[instaloader.Instaloader, str | None]:
    """Return Instaloader instance bound to session file (used for posts/reels)."""
    ig_user, session_path = get_session_file()
    session_mtime: float | None = None
    if session_path is not None and session_path.exists():
        try:
            session_mtime = session_path.stat().st_mtime
        except OSError:
            session_mtime = None

    loader = getattr(_tls, "loader", None)
    cached_user = getattr(_tls, "current_username", None)
    cached_path = getattr(_tls, "session_path", None)
    cached_mtime = getattr(_tls, "session_mtime", None)

    if (
        loader is not None
        and cached_path == session_path
        and cached_mtime == session_mtime
        and cached_user == ig_user
    ):
        return loader, cached_user

    loader = instaloader.Instaloader(
        filename_pattern="{date_utc:%Y-%m-%d}_{profile}_{typename}_{mediaid}",
        download_videos=True,
        download_video_thumbnails=True,
        save_metadata=False,
        compress_json=False,
        max_connection_attempts=2,
        request_timeout=60.0,
    )

    if ig_user and session_path and session_path.exists():
        try:
            loader.load_session_from_file(ig_user, str(session_path))
        except (OSError, instaloader.exceptions.InstaloaderException) as exc:
            logger.error("Failed to load session file %s: %s", session_path, exc)
            ig_user = None

    _tls.loader = loader
    _tls.current_username = ig_user
    _tls.session_path = session_path
    _tls.session_mtime = session_mtime
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


def download_post_sync(shortcode: str, target_dir: str) -> None:
    loader, current_user = get_thread_safe_loader()
    try:
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        loader.download_post(post, target=Path(target_dir))
    except Exception as exc:
        logger.warning("Post %s failed with session '%s': %s", shortcode, current_user, exc)
        log_failed_account(f"post_{shortcode}", str(exc))
        raise DownloadError(_IG_USER_MESSAGES["generic"], kind="generic") from exc


def download_story_yt_dlp_sync(username: str, media_id: str | None, target_dir: str) -> None:
    """Download Instagram stories using yt-dlp and instagram_cookies.txt."""
    cookies_path = Path(IG_COOKIES_FILE)
    if not cookies_path.is_file():
        raise DownloadError(
            f"Missing {IG_COOKIES_FILE}. Please export your Instagram browser cookies "
            "in Netscape format and place them in the bot directory.",
            kind="login",
        )

    username = _normalize_username(username)
    if media_id:
        url = f"https://www.instagram.com/stories/{username}/{media_id}/"
    else:
        url = f"https://www.instagram.com/stories/{username}/"

    outtmpl = os.path.join(target_dir, "%(upload_date)s_%(uploader)s_GraphStory_%(id)s.%(ext)s")

    opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "cookiefile": str(cookies_path),
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "retries": 3,
    }

    logger.info("Downloading Instagram story via yt-dlp for @%s (url=%s)", username, url)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        logger.error("yt-dlp story download failed for @%s: %s", username, exc)
        blob = _exc_blob(exc).lower()
        if any(token in blob for token in ("login", "cookie", "redirect", "403", "401")):
            raise DownloadError(
                "Instagram rejected the cookies. Please export a fresh instagram_cookies.txt "
                "file from a logged-in browser.",
                kind="login",
            ) from exc
        log_failed_account(username, str(exc))
        raise DownloadError(
            f"No active stories found or could not download stories for @{username}.",
            kind="no_stories",
        ) from exc

    downloaded_files = [
        p
        for p in Path(target_dir).iterdir()
        if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES and p.stat().st_size > 0
    ]
    if not downloaded_files:
        raise DownloadError(_IG_USER_MESSAGES["no_stories"], kind="no_stories")


def clean_youtube_url(url: str) -> str:
    """Normalize any YouTube URL / partial match to a canonical watch URL."""
    match = YOUTUBE_RE.search(url)
    if match:
        return f"https://www.youtube.com/watch?v={match.group(1)}"

    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.netloc or "").lower()
    if "youtube.com" in host or host == "youtu.be":
        query = parse_qs(parsed.query)
        video_id = query.get("v")
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id[0]}"
        parts = [p for p in parsed.path.split("/") if p]
        if host == "youtu.be" and parts:
            return f"https://www.youtube.com/watch?v={parts[0][:11]}"
        if parts and parts[0] in {"shorts", "embed", "live"} and len(parts) > 1:
            return f"https://www.youtube.com/watch?v={parts[1][:11]}"
    return url


def _run_cmd(cmd: list[str], timeout: int = 1200) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _ffprobe_streams(path: Path) -> list[dict[str, Any]]:
    result = _run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        timeout=60,
    )
    if result.returncode != 0:
        logger.warning("ffprobe failed for %s: %s", path.name, result.stderr.strip())
        return []
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return []
    streams = payload.get("streams") or []
    return [s for s in streams if isinstance(s, dict)]


def _yt_base_opts(target_dir: str) -> dict[str, Any]:
    outtmpl = os.path.join(target_dir, "%(title).180B [%(id)s].%(ext)s")
    opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
    }
    cookies_path = Path(YTDLP_COOKIES_FILE)
    if cookies_path.is_file():
        opts["cookiefile"] = str(cookies_path)
        logger.info("Using YouTube cookies file: %s", cookies_path)
    return opts


def download_youtube_sync(url: str, target_dir: str) -> None:
    cleaned_url = clean_youtube_url(url)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise DownloadError("ffmpeg/ffprobe is required for YouTube downloads. Please install ffmpeg.")

    base_opts = _yt_base_opts(target_dir)
    download_opts = dict(base_opts)
    download_opts["format"] = "bestvideo[height<=2160]+bestaudio/best[height<=2160]/best"
    download_opts["merge_output_format"] = "mkv"

    try:
        with yt_dlp.YoutubeDL(download_opts) as ydl:
            ydl.download([cleaned_url])
    except Exception as exc:
        logger.error("yt-dlp download failed for %s: %s", cleaned_url, exc)
        raise DownloadError(_YT_USER_MESSAGES["generic"], kind="generic") from exc


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
        "Send an Instagram link (post, reel, story) or YouTube link.\n"
        "Story links require an instagram_cookies.txt file placed next to the bot.",
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

    is_instagram = INSTAGRAM_HOST_RE.search(text)
    is_youtube = YOUTUBE_RE.search(text)

    if not is_instagram and not is_youtube:
        return

    post_match = POST_RE.search(text) if is_instagram else None
    story_target: tuple[str, str | None] | None = None
    if is_instagram and not post_match:
        try:
            story_target = parse_story_target(text)
        except StoryUnavailable as exc:
            await message.reply_text(str(exc))
            return
        if story_target is None and STORY_RE.search(text):
            await message.reply_text(
                "Cannot download highlights from that URL. "
                "Send the user's main story link instead "
                "(for example https://www.instagram.com/stories/username/)."
            )
            return

    if is_instagram and not post_match and story_target is None:
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
    story_match = story_target is not None

    try:
        async with sema:
            if is_youtube:
                yt_url = clean_youtube_url(is_youtube.group(0))
                await _safe_edit(status_msg, "Downloading YouTube video…")
                await asyncio.wait_for(
                    asyncio.to_thread(download_youtube_sync, yt_url, target_dir),
                    timeout=YOUTUBE_DOWNLOAD_TIMEOUT_SECONDS,
                )
            elif post_match:
                shortcode = post_match.group(1)
                await asyncio.wait_for(
                    asyncio.to_thread(download_post_sync, shortcode, target_dir),
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )
            elif story_target:
                username, media_id = story_target
                if media_id:
                    await _safe_edit(status_msg, f"Downloading story {media_id} from @{username}…")
                else:
                    await _safe_edit(status_msg, f"Downloading active stories for @{username}…")
                
                # Using yt-dlp with instagram_cookies.txt
                await asyncio.wait_for(
                    asyncio.to_thread(download_story_yt_dlp_sync, username, media_id, target_dir),
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )

        await asyncio.sleep(0.5)

        entries = _flatten_target_directory(target_dir)

        if not entries:
            raise DownloadError("No media was downloaded. It may be private, deleted, or no longer available.")

        await _safe_edit(status_msg, f"Uploading {len(entries)} file(s) to Telegram…")

        with ExitStack() as stack:
            media_group: list[InputMediaDocument] = []
            for i, path in enumerate(entries):
                handle = stack.enter_context(path.open("rb"))

                caption = None
                if i == len(entries) - 1 and is_instagram:
                    parts = path.name.split("_")
                    if len(parts) >= 4 and parts[0].count("-") == 2:
                        date_str = parts[0].replace("-", "")
                        ig_user = "_".join(parts[1:-2])

                        if story_match:
                            ig_type = "IGS"
                        elif post_match and "/reel/" in text.lower():
                            ig_type = "IGReels"
                        else:
                            ig_type = "IG"

                        caption = f"#{date_str} #{ig_user}{ig_type}"

                media_group.append(
                    InputMediaDocument(
                        media=handle,
                        filename=_friendly_filename(path.name),
                        caption=caption,
                    )
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
        await _safe_edit(status_msg, "Download timed out. Please try again.")
    except DownloadError as exc:
        logger.error("Download error for user %s [%s]: %s", user_id, getattr(exc, "kind", "generic"), exc)
        await _safe_edit(status_msg, str(exc))
    except Exception:
        logger.exception("Unexpected error for user %s", user_id)
        await _safe_edit(status_msg, "Something went wrong while processing that link. Please try again later.")
    finally:
        await _release_user_slot(context, user_id)
        shutil.rmtree(target_dir, ignore_errors=True)


async def _post_init(application: Application) -> None:
    global _global_download_sema, _slot_lock
    _global_download_sema = asyncio.Semaphore(MAX_GLOBAL_DOWNLOADS)
    _slot_lock = asyncio.Lock()
    load_user_id_cache()

    if not Path(IG_COOKIES_FILE).is_file():
        logger.warning("Notice: %s not found. Story downloads will fail until added.", IG_COOKIES_FILE)

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
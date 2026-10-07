#!/usr/bin/env python3
"""Instagram & YouTube downloader Telegram bot with multi-session rotation."""

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
import time
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
from story_cache import StoryRequestCache, StoryUnavailable, parse_story_target
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
DOWNLOAD_TIMEOUT_SECONDS = 600  # Extended timeout for 4K video downloads and re-encoding
YOUTUBE_DOWNLOAD_TIMEOUT_SECONDS = 1800  # 4K fetch + H.264/H.265 re-encode can be slow
TELEGRAM_MEDIA_GROUP_LIMIT = 10
MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".jpg", ".jpeg", ".png", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
# Optional Netscape cookies file for YouTube (needed when guest IP is restricted).
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "cookies.txt")

# Standard desktop Chrome User-Agent to avoid generic python-requests/Instaloader blocks
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Instagram web app identifiers. Required for /api/v1/feed/reels_media/
IG_WEB_APP_ID = "936619743392459"
IG_WEB_ASBD_ID = "359341"
IG_WEB_ASBD_ID_FALLBACK = "129477"

CACHE_PATH = Path(os.environ.get("USER_ID_CACHE_PATH", "user_id_cache.json"))

# Match Instagram URLs
INSTAGRAM_HOST_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:instagram\.com|instagr\.am)/", re.I)
POST_RE = re.compile(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", re.I)
STORY_RE = re.compile(r"/stories/([^/?#&]+)(?:/([^/?#&]+))?", re.I)
# One profile story fetch is shared by every user. See load_cached_profile_stories.
STORY_CACHE = StoryRequestCache()

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

# Session Rotation State
_session_lock = threading.Lock()
_session_index = 0


class DownloadError(Exception):
    """User-facing download failure."""

    def __init__(self, message: str, *, retry_session: bool = False, kind: str = "generic"):
        super().__init__(message)
        self.retry_session = retry_session
        self.kind = kind


# Instagram / YouTube error kinds that should skip extra endpoint hammering.
_SESSION_BLOCK_KINDS = frozenset({"checkpoint", "rate_limit", "login", "restricted"})
_RETRY_SESSION_KINDS = _SESSION_BLOCK_KINDS | {"private", "generic", "no_stories"}

_IG_USER_MESSAGES = {
    "checkpoint": (
        "Instagram blocked this account (security checkpoint / bot check). "
        "Open Instagram in a browser, complete the verification, then try again later."
    ),
    "rate_limit": (
        "Instagram is temporarily rate-limiting this account. "
        "Please wait a few minutes and try again."
    ),
    "login": (
        "The Instagram session expired or was logged out. "
        "Please refresh the session file and try again."
    ),
    "restricted": (
        "Instagram refused this request. The content may be private, "
        "or this session is restricted."
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


def _classify_instagram_error(blob: str, status_code: int | None = None) -> str | None:
    text = (blob or "").lower()
    if status_code == 429 or any(
        token in text
        for token in (
            "too many requests",
            "rate limit",
            "rate_limit",
            "please wait a few minutes",
            "spam",
            "action blocked",
        )
    ):
        return "rate_limit"
    if any(
        token in text
        for token in (
            "checkpoint_required",
            "challenge_required",
            "feedback_required",
            "consent_required",
            "we suspect automated",
            "suspicious activity",
        )
    ):
        return "checkpoint"
    if any(
        token in text
        for token in (
            "login_required",
            "not logged in",
            "loginrequired",
            "session expired",
            "user is not logged in",
        )
    ):
        return "login"
    if "privateprofilenotfollowed" in text or "is private" in text:
        return "private"
    if any(
        token in text
        for token in (
            "profilenotexists",
            "queryreturnednotfound",
            "http 404",
            "status_code=404",
            "does not exist",
        )
    ) or ("not found" in text and "tray" not in text):
        return "not_found"
    if status_code in {401, 403} or any(
        token in text
        for token in (
            "queryreturnedforbidden",
            "http 403",
            "status_code=403",
            "forbidden",
            "not authorized",
        )
    ):
        return "restricted"
    if status_code == 400 and ("fail" in text or "bad request" in text or "queryreturnedbadrequest" in text):
        return "restricted"
    return None


def _payload_error_blob(payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(payload or "")
    parts = [
        str(payload.get("message") or ""),
        str(payload.get("error_type") or ""),
        str(payload.get("error_title") or ""),
        str(payload.get("status") or ""),
    ]
    if payload.get("require_login"):
        parts.append("login_required")
    spam = payload.get("spam")
    if spam:
        parts.append("spam")
    return " ".join(parts)


def _instagram_user_message(kind: str, resource: str = "media") -> str:
    if kind == "not_found":
        if resource == "story":
            return _IG_USER_MESSAGES["expired_story"]
        if resource == "post":
            return "That post or reel was not found. It may have been deleted."
    if kind == "no_stories":
        return _IG_USER_MESSAGES["no_stories"]
    return _IG_USER_MESSAGES.get(kind, _IG_USER_MESSAGES["generic"])


def _raise_instagram_block(
    blob: str,
    status_code: int | None = None,
    resource: str = "media",
    *,
    fatal_only: bool = False,
) -> None:
    kind = _classify_instagram_error(blob, status_code)
    if not kind:
        return
    if fatal_only and kind not in {"checkpoint", "rate_limit", "login"}:
        return
    raise DownloadError(
        _instagram_user_message(kind, resource),
        retry_session=kind in _RETRY_SESSION_KINDS,
        kind=kind,
    )


def _map_instagram_exception(exc: BaseException, resource: str = "media") -> DownloadError:
    if isinstance(exc, DownloadError):
        if exc.kind != "generic" or exc.retry_session:
            return exc
        kind = _classify_instagram_error(_exc_blob(exc)) or exc.kind
        if kind != exc.kind:
            return DownloadError(
                _instagram_user_message(kind, resource),
                retry_session=kind in _RETRY_SESSION_KINDS,
                kind=kind,
            )
        return exc

    blob = _exc_blob(exc)
    kind = _classify_instagram_error(blob)
    if kind:
        return DownloadError(
            _instagram_user_message(kind, resource),
            retry_session=kind in _RETRY_SESSION_KINDS,
            kind=kind,
        )

    fallback = {
        "post": "Could not download that post or reel. Please try again later.",
        "story": "Could not download that story. Please try again later.",
        "profile": "Could not look up that Instagram account. Please try again later.",
    }.get(resource, _IG_USER_MESSAGES["generic"])
    return DownloadError(fallback, retry_session=True, kind="generic")


def _map_youtube_exception(exc: BaseException) -> DownloadError:
    if isinstance(exc, DownloadError):
        return exc
    text = _exc_blob(exc).lower()
    if any(
        token in text
        for token in (
            "sign in to confirm",
            "not a bot",
            "confirm you’re not a bot",
            "confirm you're not a bot",
        )
    ):
        kind = "bot_check"
    elif "429" in text or "too many requests" in text:
        kind = "rate_limit"
    elif "age-restricted" in text or "age restricted" in text:
        kind = "age"
    elif any(
        token in text
        for token in (
            "private video",
            "video unavailable",
            "this video is not available",
            "removed by the uploader",
            "copyright",
        )
    ):
        kind = "unavailable"
    else:
        kind = "generic"
    return DownloadError(_YT_USER_MESSAGES[kind], kind=kind)


async def load_story_link(url: str, fetch_tray: Any, download_item: Any) -> Any | None:
    """Serve a profile story URL from the shared tray cache.

    Returns None for posts, reels, and highlights so those keep their own path.
    """
    parsed = parse_story_target(url)
    if parsed is None:
        return None
    username, story_id = parsed
    return await load_cached_profile_stories(username, story_id, fetch_tray, download_item)


async def load_cached_profile_stories(
    username: str,
    story_id: str | None,
    fetch_tray: Any,
    download_item: Any,
) -> Any:
    """Fetch a profile's whole story tray once, then reuse it across users.

    fetch_tray(username) performs the Instagram request, including session
    rotation, and returns every current story. Raise DownloadError on
    checkpoint, rate limit, and login. Return [] only when that account has
    no active stories.

    Each item is a dict: story_id (the number from the story URL), media_url,
    is_video, taken_at, expiring_at. Optional user_id may be returned as
    {"user_id": ..., "items": [...]}.

    download_item(story) saves that one story and returns its file path.
    The path inside the returned items is the shared cache file; leave it in
    place so the next user can send it without calling Instagram.
    """
    try:
        return await STORY_CACHE.get(
            username,
            story_id,
            fetch_tray=fetch_tray,
            download_item=download_item,
        )
    except StoryUnavailable as exc:
        kind = "not_found" if exc.kind in {"expired_story", "not_found"} else exc.kind
        raise DownloadError(
            _instagram_user_message(kind, "story"),
            retry_session=False,
            kind=kind,
        ) from exc


def log_failed_account(account_name: str, error_msg: str):
    """Appends failed target accounts or sessions to a dedicated log file."""
    try:
        with open("
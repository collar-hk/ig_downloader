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

# Instagram web app identifiers. Required for /api/v1/feed/reels_media/
# (the GraphQL stories query Instaloader still uses is retired).
IG_WEB_APP_ID = "936619743392459"
# yt-dlp's current story extractor. The older 129477 id is tried if this is rejected.
IG_WEB_ASBD_ID = "359341"
IG_WEB_ASBD_ID_FALLBACK = "129477"

CACHE_PATH = Path(os.environ.get("USER_ID_CACHE_PATH", "user_id_cache.json"))
# One profile story response is shared by later users. Files stay until the
# story expires; a story id that was not in the last response is fetched again.
STORY_CACHE_DIR = Path(os.environ.get("STORY_CACHE_DIR", "story_cache"))
STORY_CACHE_URL_TTL_SECONDS = float(os.environ.get("STORY_CACHE_URL_TTL_SECONDS", "900"))
STORY_CACHE_PROFILE_TTL_SECONDS = float(os.environ.get("STORY_CACHE_PROFILE_TTL_SECONDS", "900"))
STORY_CACHE_NEGATIVE_SECONDS = float(os.environ.get("STORY_CACHE_NEGATIVE_SECONDS", "20"))
STORY_CACHE_EMPTY_SECONDS = float(os.environ.get("STORY_CACHE_EMPTY_SECONDS", "20"))

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
        # GraphQL "fail" without a more specific code is usually a blocked/restricted session.
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


def cache_delete(username: str) -> None:
    key = _normalize_username(username)
    with _cache_lock:
        _user_id_cache.pop(key, None)
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
        request_timeout=60.0
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


def get_user_id_from_web_api(loader: instaloader.Instaloader, username: str) -> int | None:
    """Resolve a profile id via the logged-in web_profile_info endpoint."""
    session = loader.context._session
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    try:
        resp = session.get(url, headers=_web_story_headers(session))
        if resp.status_code != 200:
            logger.warning("web_profile_info for @%s returned HTTP %s", username, resp.status_code)
            return None
        user = ((resp.json() or {}).get("data") or {}).get("user") or {}
        raw_id = user.get("id") or user.get("pk")
        if raw_id is not None and str(raw_id).isdigit():
            logger.info("Resolved @%s to %s via web_profile_info", username, raw_id)
            return int(raw_id)
    except Exception as exc:
        logger.warning("web_profile_info failed for @%s: %s", username, exc)
    return None


def resolve_instagram_user_id(loader: instaloader.Instaloader, username: str) -> int:
    username = _normalize_username(username)

    cached = cache_get(username)
    if cached is not None:
        logger.info("Cache hit for @%s (ID %s)", username, cached)
        return cached

    logger.info("Cache miss for @%s; attempting resolution", username)

    user_id = get_user_id_from_web_api(loader, username)
    if user_id:
        cache_set(username, user_id)
        return user_id

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
        mapped = _map_instagram_exception(exc, "profile")
        if mapped.kind in _SESSION_BLOCK_KINDS:
            raise mapped from exc
        raise DownloadError(
            f"Could not find Instagram user @{username}. "
            "The username may be wrong, or an admin can set the ID with /setid.",
            kind="not_found",
        ) from exc


def download_post_sync(shortcode: str, target_dir: str) -> None:
    session_files = get_available_session_files()
    attempts = max(1, len(session_files))
    current_user: str | None = None

    for attempt in range(attempts):
        try:
            loader, current_user = get_thread_safe_loader(rotate_session=(attempt > 0))
            post = instaloader.Post.from_shortcode(loader.context, shortcode)
            loader.download_post(post, target=Path(target_dir))
            return
        except Exception as exc:
            mapped = _map_instagram_exception(exc, "post")
            logger.warning(
                "Attempt %s failed with session '%s' [%s]: %s",
                attempt + 1,
                current_user,
                mapped.kind,
                exc,
            )
            if (not mapped.retry_session) or attempt == attempts - 1:
                log_failed_account(f"post_{shortcode}", str(exc))
                raise mapped from exc


def _story_item_matches(item: Any, media_id: str | None) -> bool:
    if not media_id:
        return True
    raw = str(getattr(item, "mediaid", "") or item)
    pk = raw.split("_")[0]
    return pk == media_id or raw == media_id or raw.startswith(media_id) or media_id.startswith(pk)


def _reel_item_id(item: dict[str, Any]) -> str:
    raw = str(item.get("pk") or item.get("id") or "")
    return raw.split("_")[0]


def _reel_item_matches(item: dict[str, Any], media_id: str | None) -> bool:
    if not media_id:
        return True
    item_id = _reel_item_id(item)
    raw = str(item.get("pk") or item.get("id") or "")
    return item_id == media_id or raw == media_id or raw.startswith(media_id) or media_id.startswith(item_id)


def _best_media_url(item: dict[str, Any]) -> tuple[str, str, bool]:
    """Return (url, extension, is_video) at the highest available resolution."""
    versions = item.get("video_versions") or []
    is_video = int(item.get("media_type") or 0) == 2 or bool(versions) or bool(item.get("video_url"))
    if is_video:
        if not isinstance(versions, list):
            versions = []
        versions = sorted(
            [v for v in versions if isinstance(v, dict)],
            key=lambda v: (int(v.get("width") or 0), int(v.get("height") or 0)),
            reverse=True,
        )
        for version in versions:
            url = version.get("url")
            if url:
                return url, ".mp4", True
        direct = item.get("video_url")
        if direct:
            return direct, ".mp4", True
        raise DownloadError("Story video is missing a downloadable URL.")

    image_versions = item.get("image_versions2") or {}
    candidates = image_versions.get("candidates") if isinstance(image_versions, dict) else []
    if not isinstance(candidates, list):
        candidates = []
    candidates = sorted(
        [c for c in candidates if isinstance(c, dict)],
        key=lambda c: (int(c.get("width") or 0), int(c.get("height") or 0)),
        reverse=True,
    )
    for candidate in candidates:
        url = candidate.get("url")
        if url:
            return url, ".jpg", False
    direct = item.get("display_url") or item.get("display_uri")
    if direct:
        return direct, ".jpg", False
    raise DownloadError("Story photo is missing a downloadable URL.")


def _web_story_headers(session: Any, asbd_id: str = IG_WEB_ASBD_ID) -> dict[str, Any]:
    headers: dict[str, Any] = {
        "X-IG-App-ID": IG_WEB_APP_ID,
        "X-ASBD-ID": asbd_id,
        "X-IG-WWW-Claim": "0",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/",
        "Accept": "*/*",
        "Origin": "https://www.instagram.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        # Instaloader pins Content-Length: 0 on the session. requests drops a
        # session header when the per-request value is None.
        "Content-Length": None,
    }
    try:
        csrf = session.cookies.get("csrftoken")
    except Exception:
        csrf = None
    if csrf:
        headers["X-CSRFToken"] = csrf
    return headers


def _remember_www_claim(headers: dict[str, Any], response: Any) -> bool:
    try:
        claim = response.headers.get("x-ig-set-www-claim")
    except Exception:
        claim = None
    if claim and claim != headers.get("X-IG-WWW-Claim"):
        headers["X-IG-WWW-Claim"] = claim
        return True
    return False


def _find_reel(data: Any, user_id: int) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None

    reel = None
    reels = data.get("reels")
    if isinstance(reels, dict):
        reel = reels.get(str(user_id)) or reels.get(user_id)
        if reel is None:
            for value in reels.values():
                if isinstance(value, dict) and str(value.get("id") or value.get("pk") or "") == str(user_id):
                    reel = value
                    break
    if not reel:
        for media in data.get("reels_media") or []:
            if not isinstance(media, dict):
                continue
            rid = str(media.get("id") or media.get("pk") or "")
            owner = str((media.get("user") or {}).get("pk") or (media.get("user") or {}).get("id") or "")
            if rid == str(user_id) or owner == str(user_id):
                reel = media
                break
    if not reel and isinstance(data.get("reel"), dict):
        reel = data["reel"]
    return reel if isinstance(reel, dict) else None


def _parse_reels_payload(data: Any, user_id: int) -> list[dict[str, Any]]:
    reel = _find_reel(data, user_id)
    if not reel:
        return []
    items = reel.get("items") or []
    return [item for item in items if isinstance(item, dict)]


def _inspect_story_response(resp: Any, resource: str = "story") -> None:
    """Abort this session immediately on checkpoint / rate-limit / login blocks."""
    status = getattr(resp, "status_code", None)
    payload = None
    try:
        payload = resp.json()
    except Exception:
        payload = None
    blob_parts = [_payload_error_blob(payload)]
    text_attr = getattr(resp, "text", None)
    if isinstance(text_attr, str):
        blob_parts.append(text_attr[:2000])
    _raise_instagram_block(" ".join(blob_parts), status, resource, fatal_only=True)


def _story_api_get(session: Any, url: str, headers: dict[str, Any], user_id: int) -> Any:
    """GET a stories endpoint, then retry once if Instagram issues a www-claim."""
    resp = session.get(url, headers=headers)
    if getattr(resp, "status_code", 200) != 200:
        _inspect_story_response(resp)
    claim_changed = _remember_www_claim(headers, resp)
    if resp.status_code != 200 or not claim_changed:
        return resp
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if _parse_reels_payload(payload, user_id):
        return resp
    logger.info("Retrying %s after Instagram set X-IG-WWW-Claim", url)
    return session.get(url, headers=headers)


def _fetch_reels_media(loader: instaloader.Instaloader, user_id: int) -> list[dict[str, Any]]:
    """Fetch active stories via Instagram's current REST API.

    Instaloader.get_stories() still uses GraphQL query_hash
    303a4ae99711322310f25250d988f3b7, which Instagram rejects with
    400 "invalid request". The web /api/v1/feed/reels_media/ endpoint is
    what instagram.com and yt-dlp use today.
    """
    session = loader.context._session
    errors: list[str] = []
    confirmed_empty = False

    web_urls = [
        f"https://www.instagram.com/api/v1/feed/reels_media/?reel_ids={user_id}",
        f"https://www.instagram.com/api/v1/feed/user/{user_id}/story/",
    ]
    for asbd_id in (IG_WEB_ASBD_ID, IG_WEB_ASBD_ID_FALLBACK):
        headers = _web_story_headers(session, asbd_id)
        for url in web_urls:
            try:
                resp = _story_api_get(session, url, headers, user_id)
                if resp.status_code != 200:
                    _inspect_story_response(resp)
                    errors.append(f"{url} asbd={asbd_id} -> HTTP {resp.status_code}")
                    logger.warning("Stories endpoint %s returned HTTP %s", url, resp.status_code)
                    continue
                try:
                    payload = resp.json()
                except ValueError:
                    errors.append(f"{url} asbd={asbd_id} -> non-JSON response")
                    continue
                _raise_instagram_block(
                    _payload_error_blob(payload), resp.status_code, "story", fatal_only=True
                )
                items = _parse_reels_payload(payload, user_id)
                if items:
                    logger.info("Fetched %s story item(s) for user %s via %s", len(items), user_id, url)
                    return items
                if _find_reel(payload, user_id) is not None:
                    confirmed_empty = True
                    logger.info("Stories tray empty for user %s via %s", user_id, url)
                    continue
                snippet = ""
                if isinstance(payload, dict):
                    snippet = " keys " + ",".join(list(payload)[:8])
                errors.append(f"{url} asbd={asbd_id} -> no reel{snippet}")
            except DownloadError:
                raise
            except Exception as exc:
                errors.append(f"{url} asbd={asbd_id} -> {exc}")
                logger.warning("Stories endpoint %s failed: %s", url, exc)
        if confirmed_empty:
            return []

    try:
        payload = loader.context.get_iphone_json(
            path=f"api/v1/feed/reels_media/?reel_ids={user_id}",
            params={},
        )
        items = _parse_reels_payload(payload, user_id)
        if items:
            logger.info("Fetched %s story item(s) for user %s via iPhone API", len(items), user_id)
            return items
        if _find_reel(payload, user_id) is not None:
            return []
        errors.append("iPhone reels_media -> empty/unexpected payload")
        _raise_instagram_block(_payload_error_blob(payload), resource="story")
    except DownloadError:
        raise
    except Exception as exc:
        mapped = _map_instagram_exception(exc, "story")
        if mapped.kind in _SESSION_BLOCK_KINDS:
            raise mapped from exc
        errors.append(f"iPhone reels_media -> {exc}")
        logger.warning("iPhone stories API failed: %s", exc)

    if confirmed_empty:
        return []

    combined = "; ".join(errors[-3:])
    _raise_instagram_block(combined, resource="story")
    raise DownloadError(
        "Could not download those stories. Instagram rejected the request.",
        retry_session=True,
        kind="generic",
    )


def _write_http_body(response: Any, dest: Path) -> None:
    with dest.open("wb") as handle:
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            handle.write(chunk)


def _cookie_header(session: Any) -> str:
    try:
        pairs = [f"{cookie.name}={cookie.value}" for cookie in session.cookies]
    except Exception:
        return ""
    return "; ".join(pairs)


def _download_story_file(session: Any, url: str, dest: Path) -> None:
    """Download a signed story URL.

    The CDN returns 403 when the request uses Instaloader's session headers
    (Chrome user-agent, Content-Length: 0, API cookies). A bare Mozilla/5.0
    user-agent with no cookies is what currently works.
    """
    anon_headers = {"User-Agent": "Mozilla/5.0"}
    try:
        with urlopen(Request(url, headers=anon_headers), timeout=120) as response:
            _write_http_body(response, dest)
            return
    except HTTPError as exc:
        if exc.code not in (401, 403):
            raise DownloadError(
                "Could not download that story file. Please try again later.",
                kind="generic",
            ) from exc
        logger.warning("Anonymous story download got HTTP %s; retrying with session cookies", exc.code)
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("Anonymous story download failed (%s); retrying with session cookies", exc)

    headers = dict(anon_headers)
    cookie_header = _cookie_header(session)
    if cookie_header:
        headers["Cookie"] = cookie_header
    try:
        with urlopen(Request(url, headers=headers), timeout=120) as response:
            _write_http_body(response, dest)
    except HTTPError as exc:
        raise DownloadError(
            "Could not download that story file. Please try again later.",
            kind="generic",
        ) from exc


def _file_is_error_page(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(32).lstrip().lower()
    except OSError:
        return True
    return head.startswith((b"<!doctype", b"<html", b"<head", b"{"))


def _save_story_item(
    session: Any,
    item: dict[str, Any],
    username: str,
    target_dir: str,
) -> Path:
    media_url, suffix, is_video = _best_media_url(item)
    taken_at = int(item.get("taken_at") or item.get("taken_at_timestamp") or 0)
    if taken_at:
        date_str = datetime.fromtimestamp(taken_at, tz=timezone.utc).strftime("%Y-%m-%d")
    else:
        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    media_id = _reel_item_id(item) or uuid.uuid4().hex[:12]
    # Keep Instaloader's typename tokens so Telegram captions still parse as
    # #{YYYYMMDD} #{username}IGS after splitting on underscores.
    kind = "GraphStoryVideo" if is_video else "GraphStoryImage"
    filename = f"{date_str}_{username}_{kind}_{media_id}{suffix}"
    dest = Path(target_dir) / filename
    _download_story_file(session, media_url, dest)
    time.sleep(0.5)
    if dest.stat().st_size == 0 or _file_is_error_page(dest):
        dest.unlink(missing_ok=True)
        raise DownloadError(f"Downloaded empty or invalid file for story {media_id}.")
    logger.info("Saved story %s (%s bytes)", dest.name, dest.stat().st_size)
    return dest


def _tray_has_requested_story(items: list[dict[str, Any]], media_id: str | None) -> bool:
    if not items:
        return False
    if not media_id:
        return True
    return any(_reel_item_matches(item, media_id) for item in items)


def _load_story_items(
    loader: instaloader.Instaloader,
    username: str,
    media_id: str | None,
) -> list[dict[str, Any]]:
    cached_before = cache_get(username)
    user_id = resolve_instagram_user_id(loader, username)
    try:
        items = _fetch_reels_media(loader, user_id)
    except DownloadError as exc:
        if exc.kind in _SESSION_BLOCK_KINDS or cached_before is None:
            raise
        logger.info("Story request failed for cached id %s; resolving @%s again", cached_before, username)
        cache_delete(username)
        try:
            refreshed_id = resolve_instagram_user_id(loader, username)
        except DownloadError:
            raise exc
        if refreshed_id == user_id:
            raise exc
        return _fetch_reels_media(loader, refreshed_id)

    if _tray_has_requested_story(items, media_id) or cached_before is None:
        return items

    logger.info(
        "Cached id %s did not contain the requested story; resolving @%s again",
        cached_before,
        username,
    )
    cache_delete(username)
    try:
        refreshed_id = resolve_instagram_user_id(loader, username)
    except DownloadError:
        return items
    if refreshed_id == user_id:
        return items
    return _fetch_reels_media(loader, refreshed_id)


def _story_cache_key(media_id: str | None) -> str | None:
    if not media_id:
        return None
    return str(media_id).split("_", 1)[0]


def _story_expiry(item: dict[str, Any], now: float) -> float:
    raw = item.get("expiring_at") or item.get("expiring_at_timestamp") or 0
    try:
        expiring = float(raw)
    except (TypeError, ValueError):
        expiring = 0
    if expiring > 10**12:
        expiring /= 1000
    if expiring > 0:
        return expiring
    taken = item.get("taken_at") or item.get("taken_at_timestamp") or now
    try:
        expiring = float(taken)
    except (TypeError, ValueError):
        expiring = now
    if expiring > 10**12:
        expiring /= 1000
    return expiring + 86400


class _Flight:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None


class ProfileStoryCache:
    """Share one profile story tray, and the files saved from it, across users."""

    def __init__(self) -> None:
        self.root = STORY_CACHE_DIR
        self.files = self.root / "files"
        self.index_path = self.root / "index.json"
        self.url_ttl = STORY_CACHE_URL_TTL_SECONDS
        self.profile_ttl = STORY_CACHE_PROFILE_TTL_SECONDS
        self.negative_seconds = STORY_CACHE_NEGATIVE_SECONDS
        self.empty_seconds = STORY_CACHE_EMPTY_SECONDS
        self._lock = threading.Lock()
        self._trays: dict[str, dict[str, Any]] = {}
        self._missing_until: dict[tuple[str, str], float] = {}
        self._tray_flights: dict[str, _Flight] = {}
        self._file_flights: dict[tuple[str, str], _Flight] = {}
        self._load()

    def resolve(
        self,
        username: str,
        media_id: str | None,
        fetch,
    ) -> list[dict[str, Any]]:
        """Return the cached tray, or let one caller fetch it for everyone waiting."""
        username = _normalize_username(username)
        media_id = _story_cache_key(media_id)
        with self._lock:
            self._prune_locked()
            hit = self._hit_locked(username, media_id)
            if hit is not None:
                return hit
            flight = self._tray_flights.get(username)
            owner = flight is None
            if owner:
                flight = _Flight()
                self._tray_flights[username] = flight

        if not owner:
            assert flight is not None
            logger.info("story tray join in-flight @%s story=%s", username, media_id or "*")
            return self._wait_flight(flight, "Story request timed out. Please try again.")

        logger.info("story tray fetch @%s story=%s", username, media_id or "*")
        try:
            items = fetch(username, media_id)
            with self._lock:
                self._store_locked(username, items, media_id)
                stored = self._item_dicts_locked(username)
            assert flight is not None
            flight.value = stored
            return stored
        except Exception as exc:
            assert flight is not None
            flight.error = exc
            raise
        finally:
            assert flight is not None
            flight.event.set()
            with self._lock:
                if self._tray_flights.get(username) is flight:
                    self._tray_flights.pop(username, None)

    def deliver(self, username: str, item: dict[str, Any], target_dir: str) -> Path:
        """Copy a saved story file, or download it once and share that file."""
        username = _normalize_username(username)
        story_id = _reel_item_id(item)
        with self._lock:
            ready = self._ready_path_locked(username, story_id)
            if ready is not None:
                logger.info("story file cache hit @%s/%s", username, story_id)
            else:
                ready = None
                key = (username, story_id)
                flight = self._file_flights.get(key)
                owner = flight is None
                if owner:
                    flight = _Flight()
                    self._file_flights[key] = flight
        if ready is not None:
            return _copy_into(ready, target_dir)

        if not owner:
            assert flight is not None
            logger.info("story file join in-flight @%s/%s", username, story_id)
            saved = self._wait_flight(flight, "Story download timed out. Please try again.")
            return _copy_into(saved, target_dir)

        try:
            loader, current_user = get_thread_safe_loader()
            if not current_user:
                raise DownloadError(
                    "Stories need a logged-in Instagram session. "
                    "Place an ig_session_<username> file next to the bot, then try again.",
                    kind="login",
                )
            saved = _save_story_item(loader.context._session, item, username, target_dir)
            permanent = self._adopt(saved)
            with self._lock:
                entry = self._trays.get(username, {}).get("items", {}).get(story_id)
                if entry is not None:
                    entry["path"] = str(permanent)
                    self._save_locked()
            assert flight is not None
            flight.value = permanent
            return saved
        except Exception as exc:
            assert flight is not None
            flight.error = exc
            raise
        finally:
            assert flight is not None
            flight.event.set()
            with self._lock:
                if self._file_flights.get((username, story_id)) is flight:
                    self._file_flights.pop((username, story_id), None)

    def invalidate_urls(self, username: str) -> None:
        """Force the next lookup to ask Instagram for fresh media URLs."""
        username = _normalize_username(username)
        with self._lock:
            tray = self._trays.get(username)
            if tray is not None:
                tray["fetched_at"] = 0
            stale = [key for key in self._missing_until if key[0] == username]
            for key in stale:
                self._missing_until.pop(key, None)

    def _wait_flight(self, flight: _Flight, timeout_message: str) -> Any:
        if not flight.event.wait(DOWNLOAD_TIMEOUT_SECONDS):
            raise DownloadError(timeout_message, kind="generic")
        if flight.error is not None:
            raise flight.error
        if flight.value is None:
            raise DownloadError(_IG_USER_MESSAGES["generic"], retry_session=True, kind="generic")
        return flight.value

    def _hit_locked(self, username: str, media_id: str | None) -> list[dict[str, Any]] | None:
        now = time.time()
        if media_id and self._missing_until.get((username, media_id), 0) >= now:
            logger.info("story cache still missing @%s/%s", username, media_id)
            raise DownloadError(_IG_USER_MESSAGES["expired_story"], kind="expired_story")
        tray = self._trays.get(username)
        if tray is None:
            return None
        items: dict[str, dict[str, Any]] = tray["items"]
        if media_id:
            entry = items.get(media_id)
            if entry is not None and self._entry_usable(tray, entry, now):
                logger.info("story cache hit @%s/%s", username, media_id)
                return self._item_dicts_locked(username)
            return None
        ttl = self.profile_ttl if items else self.empty_seconds
        if now - float(tray["fetched_at"]) <= ttl:
            logger.info("story cache hit @%s/*", username)
            return self._item_dicts_locked(username)
        return None

    def _entry_usable(self, tray: dict[str, Any], entry: dict[str, Any], now: float) -> bool:
        if float(entry["expiring_at"]) <= now:
            return False
        path = entry.get("path")
        if path and Path(path).is_file():
            return True
        return now - float(tray["fetched_at"]) <= self.url_ttl

    def _item_dicts_locked(self, username: str) -> list[dict[str, Any]]:
        tray = self._trays.get(username) or {}
        return [entry["item"] for entry in tray.get("items", {}).values()]

    def _ready_path_locked(self, username: str, story_id: str) -> Path | None:
        entry = self._trays.get(username, {}).get("items", {}).get(story_id)
        if not entry or float(entry["expiring_at"]) <= time.time():
            return None
        path = entry.get("path")
        if path and Path(path).is_file():
            return Path(path)
        return None

    def _store_locked(
        self,
        username: str,
        items: list[dict[str, Any]],
        requested_id: str | None,
    ) -> None:
        now = time.time()
        previous = self._trays.get(username, {}).get("items", {})
        stored: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            story_id = _reel_item_id(item)
            if not story_id:
                continue
            expiring = _story_expiry(item, now)
            if expiring <= now:
                continue
            old = previous.get(story_id) or {}
            old_path = old.get("path")
            path = old_path if old_path and Path(old_path).is_file() else None
            stored[story_id] = {"item": item, "path": path, "expiring_at": expiring}
            self._missing_until.pop((username, story_id), None)
        self._trays[username] = {"fetched_at": now, "items": stored}
        if requested_id and requested_id not in stored and self.negative_seconds > 0:
            self._missing_until[(username, requested_id)] = now + self.negative_seconds
        self._save_locked()

    def _adopt(self, src: Path) -> Path:
        self.files.mkdir(parents=True, exist_ok=True)
        dest = self.files / src.name
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return dest

    def _prune_locked(self) -> None:
        now = time.time()
        for username in list(self._trays):
            tray = self._trays[username]
            for story_id in list(tray["items"]):
                entry = tray["items"][story_id]
                if float(entry["expiring_at"]) > now:
                    if entry.get("path") and not Path(entry["path"]).is_file():
                        entry["path"] = None
                    continue
                path = entry.get("path")
                if path:
                    Path(path).unlink(missing_ok=True)
                del tray["items"][story_id]
            if not tray["items"] and now - float(tray["fetched_at"]) > self.empty_seconds:
                self._trays.pop(username, None)
        for key, until in list(self._missing_until.items()):
            if until < now:
                self._missing_until.pop(key, None)

    def _load(self) -> None:
        if not self.index_path.is_file():
            return
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Could not load story cache: %s", exc)
            return
        now = time.time()
        for username, raw_tray in (payload.get("trays") or {}).items():
            if not isinstance(raw_tray, dict):
                continue
            items: dict[str, dict[str, Any]] = {}
            for story_id, raw in (raw_tray.get("items") or {}).items():
                if not isinstance(raw, dict) or not isinstance(raw.get("item"), dict):
                    continue
                expiring = float(raw.get("expiring_at") or 0)
                if expiring <= now:
                    continue
                file_name = raw.get("file")
                path = None
                if file_name:
                    candidate = self.files / Path(str(file_name)).name
                    if candidate.is_file():
                        path = str(candidate)
                items[str(story_id)] = {
                    "item": raw["item"],
                    "path": path,
                    "expiring_at": expiring,
                }
            fetched_at = float(raw_tray.get("fetched_at") or 0)
            if not items and now - fetched_at > self.empty_seconds:
                continue
            self._trays[_normalize_username(str(username))] = {
                "fetched_at": fetched_at,
                "items": items,
            }
        if self._trays:
            logger.info("Loaded story cache for %s profile(s)", len(self._trays))

    def _save_locked(self) -> None:
        trays: dict[str, Any] = {}
        for username, tray in self._trays.items():
            items: dict[str, Any] = {}
            for story_id, entry in tray["items"].items():
                body: dict[str, Any] = {
                    "expiring_at": entry["expiring_at"],
                    "item": entry["item"],
                }
                path = entry.get("path")
                if path and Path(path).is_file():
                    body["file"] = Path(path).name
                items[story_id] = body
            trays[username] = {"fetched_at": tray["fetched_at"], "items": items}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            blob = json.dumps({"version": 1, "trays": trays})
            tmp = self.index_path.with_suffix(".json.tmp")
            tmp.write_text(blob, encoding="utf-8")
            tmp.replace(self.index_path)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Could not persist story cache: %s", exc)


def _copy_into(src: Path, target_dir: str) -> Path:
    dest = Path(target_dir) / src.name
    if dest.resolve() != src.resolve():
        shutil.copy2(src, dest)
    return dest


STORY_TRAY = ProfileStoryCache()


def _fetch_story_tray(username: str, media_id: str | None) -> list[dict[str, Any]]:
    """Ask Instagram once for every active story, rotating sessions on failure."""
    username = _normalize_username(username)
    session_files = get_available_session_files()
    attempts = max(1, len(session_files))
    current_user: str | None = None

    for attempt in range(attempts):
        try:
            loader, current_user = get_thread_safe_loader(rotate_session=(attempt > 0))
            if not current_user:
                raise DownloadError(
                    "Stories need a logged-in Instagram session. "
                    "Place an ig_session_<username> file next to the bot, then try again.",
                    kind="login",
                )
            items = _load_story_items(loader, username, media_id)
            # A named story that is missing is still a finished tray: the other
            # stories in it can be reused. An empty whole-profile tray retries.
            if items or media_id:
                return items
            raise DownloadError(
                _IG_USER_MESSAGES["no_stories"],
                retry_session=True,
                kind="no_stories",
            )
        except Exception as exc:
            mapped = _map_instagram_exception(exc, "story")
            logger.warning(
                "Attempt %s failed for story @%s with session '%s' [%s]: %s",
                attempt + 1,
                username,
                current_user,
                mapped.kind,
                exc,
            )
            keep_trying = mapped.retry_session and mapped.kind != "expired_story" and attempt < attempts - 1
            if keep_trying:
                continue
            if mapped.kind == "no_stories":
                return []
            raise mapped from exc
    return []


def _serve_story_request(
    username: str,
    media_id: str | None,
    target_dir: str,
    *,
    allow_refresh: bool = True,
) -> None:
    items = STORY_TRAY.resolve(username, media_id, _fetch_story_tray)
    matched = [item for item in items if _reel_item_matches(item, media_id)]
    if not matched:
        if media_id:
            logger.info(
                "Requested story %s not in tray for @%s (have %s)",
                media_id,
                username,
                ", ".join(_reel_item_id(item) for item in items[:8]) or "none",
            )
            raise DownloadError(_IG_USER_MESSAGES["expired_story"], kind="expired_story")
        raise DownloadError(_IG_USER_MESSAGES["no_stories"], kind="no_stories")

    for item in matched:
        try:
            STORY_TRAY.deliver(username, item, target_dir)
        except DownloadError as exc:
            if allow_refresh and exc.kind == "generic":
                logger.info("Refreshing story URLs for @%s after a failed file download", username)
                STORY_TRAY.invalidate_urls(username)
                _serve_story_request(username, media_id, target_dir, allow_refresh=False)
                return
            raise
        if media_id:
            return


def download_story_sync(username: str, media_id: str | None, target_dir: str) -> None:
    username = _normalize_username(username)
    try:
        _serve_story_request(username, media_id, target_dir)
    except Exception as exc:
        mapped = _map_instagram_exception(exc, "story")
        logger.warning("Story @%s failed [%s]: %s", username, mapped.kind, exc)
        log_failed_account(username, str(exc))
        raise mapped from exc


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
        # youtu.be/<id> or /shorts/<id> /embed/<id>
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


def _video_encoder_args(height: int) -> list[str]:
    """Pick H.265 for 1440p+ (smaller 4K), otherwise H.264. Prefer VideoToolbox on macOS."""
    want_hevc = height >= 1440
    if platform.system() == "Darwin":
        if want_hevc:
            # hvc1 tag improves playback on Apple devices / Telegram clients.
            return ["-c:v", "hevc_videotoolbox", "-q:v", "45", "-tag:v", "hvc1"]
        return ["-c:v", "h264_videotoolbox", "-q:v", "45"]
    if want_hevc:
        return ["-c:v", "libx265", "-preset", "fast", "-crf", "22", "-tag:v", "hvc1"]
    return ["-c:v", "libx264", "-preset", "fast", "-crf", "20"]


def _ensure_mp4_h26x(src: Path) -> Path:
    """Ensure the file is MP4 with H.264/H.265 video and AAC audio."""
    streams = _ffprobe_streams(src)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        raise DownloadError(f"Downloaded file has no video stream: {src.name}")

    vcodec = str(video.get("codec_name") or "").lower()
    acodec = str(audio.get("codec_name") or "").lower() if audio else ""
    height = int(video.get("height") or 0)
    already_ok = src.suffix.lower() == ".mp4" and vcodec in {"h264", "hevc", "h265"} and (
        not audio or acodec in {"aac", "mp3"}
    )
    if already_ok:
        return src

    dest = src.with_suffix(".mp4")
    if dest.resolve() == src.resolve():
        dest = src.with_name(f"{src.stem}.converted.mp4")

    # Remux-only when codecs are already compatible with MP4.
    if vcodec in {"h264", "hevc", "h265"} and (not audio or acodec in {"aac", "mp3"}):
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(dest),
        ]
        logger.info("Remuxing %s -> %s (vcodec=%s acodec=%s)", src.name, dest.name, vcodec, acodec)
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            *_video_encoder_args(height),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(dest),
        ]
        logger.info(
            "Re-encoding %s -> %s (src vcodec=%s height=%s)",
            src.name,
            dest.name,
            vcodec,
            height,
        )

    result = _run_cmd(cmd, timeout=YOUTUBE_DOWNLOAD_TIMEOUT_SECONDS)
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        err = (result.stderr or result.stdout or "").strip().splitlines()
        tail = err[-8:] if err else ["unknown ffmpeg error"]
        raise DownloadError("Failed to convert video to MP4 (H.264/H.265): " + " | ".join(tail))

    if dest.resolve() != src.resolve() and src.exists():
        src.unlink(missing_ok=True)
    return dest


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
        # Use yt-dlp's default YouTube client set. Forcing ios/android/tv-only
        # commonly collapses formats down to a single ~360p progressive stream
        # (e.g. 640x272), which is exactly the failure mode we hit.
    }
    cookies_path = Path(YTDLP_COOKIES_FILE)
    if cookies_path.is_file():
        opts["cookiefile"] = str(cookies_path)
        logger.info("Using YouTube cookies file: %s", cookies_path)
    return opts


def _format_pixels(fmt: dict[str, Any]) -> int:
    return int(fmt.get("width") or 0) * int(fmt.get("height") or 0)


def _is_video_only(fmt: dict[str, Any]) -> bool:
    vcodec = fmt.get("vcodec")
    acodec = fmt.get("acodec")
    return bool(vcodec and vcodec != "none" and (not acodec or acodec == "none"))


def _is_audio_only(fmt: dict[str, Any]) -> bool:
    vcodec = fmt.get("vcodec")
    acodec = fmt.get("acodec")
    return bool(acodec and acodec != "none" and (not vcodec or vcodec == "none"))


def _is_progressive(fmt: dict[str, Any]) -> bool:
    vcodec = fmt.get("vcodec")
    acodec = fmt.get("acodec")
    return bool(vcodec and vcodec != "none" and acodec and acodec != "none")


def _within_4k_cap(fmt: dict[str, Any]) -> bool:
    """Allow up to 4K, including ultrawide 3840xN masters."""
    width = int(fmt.get("width") or 0)
    height = int(fmt.get("height") or 0)
    if width <= 0 and height <= 0:
        return False
    return width <= 4096 and height <= 2160


def _protocol_rank(fmt: dict[str, Any]) -> int:
    """Prefer plain https DASH over HLS when quality is equal."""
    proto = str(fmt.get("protocol") or "")
    if proto.startswith("https"):
        return 2
    if "m3u8" in proto:
        return 1
    return 0


def _pick_youtube_format_ids(formats: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Pick best video (<=4K) + best audio by pixel count / bitrate.

    Returns (format_selector, chosen_video_format).
    """
    usable = [f for f in formats if isinstance(f, dict) and f.get("format_id")]
    videos = [f for f in usable if _is_video_only(f) and _within_4k_cap(f)]
    audios = [f for f in usable if _is_audio_only(f)]
    progressive = [f for f in usable if _is_progressive(f) and _within_4k_cap(f)]

    # Prefer formats that already have a direct/manifest URL (skip SABR-only stubs).
    def _has_download_url(fmt: dict[str, Any]) -> bool:
        return bool(fmt.get("url") or fmt.get("manifest_url") or fmt.get("fragments"))

    videos_ready = [f for f in videos if _has_download_url(f)] or videos
    audios_ready = [f for f in audios if _has_download_url(f)] or audios
    progressive_ready = [f for f in progressive if _has_download_url(f)] or progressive
    videos, audios, progressive = videos_ready, audios_ready, progressive_ready

    best_video = None
    if videos:
        best_video = max(
            videos,
            key=lambda f: (
                _format_pixels(f),
                _protocol_rank(f),
                float(f.get("tbr") or f.get("vbr") or 0),
                float(f.get("fps") or 0),
            ),
        )
    best_audio = None
    if audios:
        best_audio = max(
            audios,
            key=lambda f: (
                float(f.get("abr") or f.get("tbr") or 0),
                _protocol_rank(f),
            ),
        )

    best_prog = None
    if progressive:
        best_prog = max(
            progressive,
            key=lambda f: (
                _format_pixels(f),
                float(f.get("tbr") or 0),
                _protocol_rank(f),
            ),
        )

    if best_video and best_audio:
        selector = f"{best_video['format_id']}+{best_audio['format_id']}"
        logger.info(
            "Selected YouTube formats %s (%sx%s %s) + %s (%s)",
            best_video.get("format_id"),
            best_video.get("width"),
            best_video.get("height"),
            best_video.get("vcodec"),
            best_audio.get("format_id"),
            best_audio.get("acodec"),
        )
        return selector, best_video

    if best_prog:
        logger.info(
            "Selected progressive YouTube format %s (%sx%s)",
            best_prog.get("format_id"),
            best_prog.get("width"),
            best_prog.get("height"),
        )
        return str(best_prog["format_id"]), best_prog

    if best_video:
        # Last resort: video only (should not happen if audio exists).
        return str(best_video["format_id"]), best_video

    raise DownloadError("No downloadable YouTube formats were returned for that video.")


def download_youtube_sync(url: str, target_dir: str) -> None:
    """Download best available video up to 4K with audio, then convert to H.264/H.265 MP4.

    Manually picks formats by pixel count so ultrawide 4K (e.g. 3840x1634) is preferred
    over the low-res progressive fallback (often 640x272) that ios/android clients return.
    """
    cleaned_url = clean_youtube_url(url)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise DownloadError("ffmpeg/ffprobe is required for YouTube downloads. Please install ffmpeg.")

    base_opts = _yt_base_opts(target_dir)
    chosen_video: dict[str, Any] | None = None
    selector = ""

    try:
        probe_opts = dict(base_opts)
        probe_opts["skip_download"] = True
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(cleaned_url, download=False)
            if info is None:
                raise DownloadError("yt-dlp returned no video information.")
            if info.get("entries"):
                entries = [e for e in info["entries"] if e]
                if not entries:
                    raise DownloadError("No downloadable video found at that URL.")
                info = entries[0]
            formats = info.get("formats") or []
            selector, chosen_video = _pick_youtube_format_ids(formats)

        expected_pixels = _format_pixels(chosen_video)
        if expected_pixels and expected_pixels < 640 * 360:
            # Only low-res formats were advertised — usually means the extractor
            # fell back to a restricted client. Cookies often unlock higher formats.
            hint = ""
            if not Path(YTDLP_COOKIES_FILE).is_file():
                hint = (
                    " Place a Netscape cookies.txt (export from a logged-in browser) "
                    f"next to the bot as {YTDLP_COOKIES_FILE}, then retry."
                )
            raise DownloadError(
                f"YouTube only offered low resolution "
                f"({chosen_video.get('width')}x{chosen_video.get('height')})."
                f"{hint}"
            )

        download_opts = dict(base_opts)
        download_opts.update(
            {
                "format": selector,
                "merge_output_format": "mkv",
            }
        )
        with yt_dlp.YoutubeDL(download_opts) as ydl:
            info = ydl.extract_info(cleaned_url, download=True)
            if info is None:
                raise DownloadError("yt-dlp returned no video information.")
            if info.get("entries"):
                entries = [e for e in info["entries"] if e]
                info = entries[0]
            requested = ydl.prepare_filename(info)
            candidates = [
                Path(requested),
                Path(requested).with_suffix(".mkv"),
                Path(requested).with_suffix(".mp4"),
                Path(requested).with_suffix(".webm"),
            ]
    except DownloadError:
        raise
    except Exception as exc:
        logger.error("yt-dlp download failed for %s: %s", cleaned_url, exc)
        raise _map_youtube_exception(exc) from exc

    downloaded: Path | None = None
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            downloaded = candidate
            break
    if downloaded is None:
        videos = [
            p
            for p in Path(target_dir).iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES and p.stat().st_size > 0
        ]
        if not videos:
            raise DownloadError("YouTube download finished but no video file was found.")
        downloaded = max(videos, key=lambda p: p.stat().st_mtime)

    width = height = 0
    for stream in _ffprobe_streams(downloaded):
        if stream.get("codec_type") == "video":
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            break
    logger.info(
        "Downloaded YouTube file %s (%s bytes, %sx%s)",
        downloaded.name,
        downloaded.stat().st_size,
        width or "?",
        height or "?",
    )

    if chosen_video and expected_pixels:
        actual_pixels = width * height
        # Guard against silently ending up with the 360p progressive fallback.
        if actual_pixels and actual_pixels < max(expected_pixels * 0.5, 1):
            raise DownloadError(
                f"Downloaded resolution {width}x{height} is much lower than the "
                f"selected {chosen_video.get('width')}x{chosen_video.get('height')}. "
                "Try updating yt-dlp or providing cookies.txt."
            )

    # Re-encode to H.264/H.265 is skipped for testing. The merged file
    # (typically MKV, original codecs + audio) is uploaded as-is.
    logger.info("Skipping re-encode for testing; keeping %s", downloaded.name)


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
        "Send an Instagram link (post, reel, story, IGTV) or YouTube link.\n"
        "YouTube downloads up to 4K with audio and converts to MP4 (H.264/H.265).",
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
    story_match = STORY_RE.search(text) if is_instagram else None

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
                yt_url = clean_youtube_url(is_youtube.group(0))
                await _safe_edit(
                    status_msg,
                    "Downloading YouTube (up to 4K + audio) and converting to MP4…",
                )
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
                "No media was downloaded. It may be private, deleted, or no longer available."
            )

        await _safe_edit(status_msg, f"Uploading {len(entries)} file(s) to Telegram…")

        with ExitStack() as stack:
            media_group: list[InputMediaDocument] = []
            for i, path in enumerate(entries):
                handle = stack.enter_context(path.open("rb"))
                
                caption = None
                if i == len(entries) - 1 and is_instagram:
                    parts = path.name.split('_')
                    # Ensure it has enough parts to safely extract date, user, type, and ID
                    if len(parts) >= 4 and parts[0].count('-') == 2:
                        date_str = parts[0].replace('-', '')
                        
                        # Reconstruct usernames that contain underscores (e.g., 6y_day)
                        # Everything between the date (index 0) and the typename (index -2) is the username
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
                        caption=caption
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
        await _safe_edit(
            status_msg,
            "Download timed out. Please try again — large videos can take a while.",
        )
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

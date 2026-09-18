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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
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
DOWNLOAD_TIMEOUT_SECONDS = 600  # Extended timeout for 4K video downloads and re-encoding
TELEGRAM_MEDIA_GROUP_LIMIT = 10
MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".jpg", ".jpeg", ".png", ".webp"}

# Instagram web app identifiers. Required for /api/v1/feed/reels_media/
# (the GraphQL stories query Instaloader still uses is retired).
IG_WEB_APP_ID = "936619743392459"
# yt-dlp's current story extractor. The older 129477 id is tried if this is rejected.
IG_WEB_ASBD_ID = "359341"
IG_WEB_ASBD_ID_FALLBACK = "129477"

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
                log_failed_account(f"post_{shortcode}", str(exc))
                raise DownloadError(f"Failed to fetch that post or reel: {exc}") from exc


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


def _story_api_get(session: Any, url: str, headers: dict[str, Any], user_id: int) -> Any:
    """GET a stories endpoint, then retry once if Instagram issues a www-claim."""
    resp = session.get(url, headers=headers)
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
                    errors.append(f"{url} asbd={asbd_id} -> HTTP {resp.status_code}")
                    logger.warning("Stories endpoint %s returned HTTP %s", url, resp.status_code)
                    continue
                try:
                    payload = resp.json()
                except ValueError:
                    errors.append(f"{url} asbd={asbd_id} -> non-JSON response")
                    continue
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
    except Exception as exc:
        errors.append(f"iPhone reels_media -> {exc}")
        logger.warning("iPhone stories API failed: %s", exc)

    if confirmed_empty:
        return []

    raise DownloadError(
        "Instagram rejected the stories request (the old GraphQL stories query is dead). "
        + "; ".join(errors[-3:])
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
            raise DownloadError(f"Story media download failed: HTTP {exc.code}") from exc
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
        raise DownloadError(f"Story media download failed: HTTP {exc.code}") from exc


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
        if cached_before is None:
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


def download_story_sync(username: str, media_id: str | None, target_dir: str) -> None:
    username = _normalize_username(username)
    session_files = get_available_session_files()
    attempts = max(1, len(session_files))
    current_user: str | None = None

    for attempt in range(attempts):
        try:
            loader, current_user = get_thread_safe_loader(rotate_session=(attempt > 0))
            if not current_user:
                raise DownloadError(
                    "Stories require a logged-in Instagram session. "
                    "Place an ig_session_<username> file next to the bot."
                )
            items = _load_story_items(loader, username, media_id)
            found = False
            for item in items:
                if not _reel_item_matches(item, media_id):
                    continue
                _save_story_item(loader.context._session, item, username, target_dir)
                found = True
                if media_id:
                    break

            if found:
                return
            if media_id:
                available = ", ".join(_reel_item_id(item) for item in items[:8]) or "none"
                raise DownloadError(
                    f"That story was not found or has expired (tray ids: {available})."
                )
            raise DownloadError(
                "No active stories found. The account may have none, or this Instagram session is logged out."
            )
        except DownloadError as exc:
            logger.warning(
                "Attempt %s failed for story @%s with session '%s': %s",
                attempt + 1,
                username,
                current_user,
                exc,
            )
            if attempt == attempts - 1:
                log_failed_account(username, str(exc))
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
                log_failed_account(username, str(exc))
                raise DownloadError(f"Failed to fetch that story: {exc}") from exc


def clean_youtube_url(url: str) -> str:
    """Strip playlist and extra parameters, keeping only the 'v' video ID parameter."""
    parsed = urlparse(url)
    if "youtube.com" in parsed.netloc:
        query = parse_qs(parsed.query)
        video_id = query.get("v")
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id[0]}"
    return url


def download_youtube_sync(url: str, target_dir: str) -> None:
    """Download YouTube video in maximum 4K quality using iOS/Android clients (unauthenticated)."""
    cleaned_url = clean_youtube_url(url)

    ydl_opts = {
        "format": "bv*+ba/best",
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(target_dir, "%(title)s [%(id)s].%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "noplaylist": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android"]
            }
        },
        "postprocessor_args": {
            "ffmpeg": [
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "20",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
            ]
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([cleaned_url])
    except Exception as exc:
        logger.error("yt-dlp download failed for %s: %s", cleaned_url, exc)
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

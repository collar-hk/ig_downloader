#!/usr/bin/env python3
"""Shared Instagram story tray cache for the Telegram downloader.

One profile fetch returns every active story. Later requests reuse that
result when the story id is already present, including when the second
request arrives while the first fetch is still running.

fetch_tray(username) must:
- rotate sessions the way the bot already does
- return every story from that profile, not only the one in the URL
- raise on checkpoint, rate limit, login, and other session failures
- return an empty list only when the account really has no active stories
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_STORY_URL_RE = re.compile(r"/stories/([^/?#&]+)(?:/([A-Za-z0-9_-]+))?", re.I)
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".jpg", ".jpeg", ".png", ".webp"}


class StoryUnavailable(Exception):
    """The tray was checked and this story cannot be served from it."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class StoryMedia:
    story_id: str
    username: str
    media_url: str
    is_video: bool = False
    taken_at: float = 0.0
    expiring_at: float = 0.0
    path: Path | None = None
    raw: Any = None


@dataclass
class CachedStories:
    username: str
    user_id: int | None
    items: list[StoryMedia]


@dataclass
class _Tray:
    username: str
    fetched_at: float
    items: dict[str, StoryMedia]
    user_id: int | None = None
    raw_by_id: dict[str, Any] = field(default_factory=dict)


def parse_story_target(url: str) -> tuple[str, str | None] | None:
    """Return (username, story_id) for a profile story URL.

    story_id is None when the URL asks for the whole profile tray.
    Highlight URLs are not profile trays and return None.
    """
    match = _STORY_URL_RE.search(url or "")
    if not match:
        return None
    username = _norm_username(match.group(1))
    if username == "highlights":
        return None
    story_id = _canonical_id(match.group(2)) if match.group(2) else None
    return username, story_id


class StoryRequestCache:
    """In-flight and on-disk cache of profile story trays."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        url_ttl_seconds: float | None = None,
        profile_ttl_seconds: float | None = None,
        negative_seconds: float | None = None,
        empty_seconds: float | None = None,
    ):
        self.root = Path(root or os.environ.get("STORY_CACHE_DIR", "story_cache"))
        self.files = self.root / "files"
        self.index_path = self.root / "index.json"
        self.url_ttl = _env_float("STORY_CACHE_URL_TTL_SECONDS", url_ttl_seconds, 900)
        self.profile_ttl = _env_float("STORY_CACHE_PROFILE_TTL_SECONDS", profile_ttl_seconds, 900)
        # Same missing id inside this window reuses the last full-tray answer.
        # A different id that was not in the tray still fetches immediately.
        self.negative_seconds = _env_float("STORY_CACHE_NEGATIVE_SECONDS", negative_seconds, 20)
        self.empty_seconds = _env_float("STORY_CACHE_EMPTY_SECONDS", empty_seconds, 20)
        self._lock: asyncio.Lock | None = None
        self._tray_inflight: dict[str, asyncio.Future[_Tray]] = {}
        self._file_inflight: dict[tuple[str, str], asyncio.Future[StoryMedia]] = {}
        self._trays: dict[str, _Tray] = {}
        # Exact story ids the last tray fetch did not contain, and when that
        # answer stops being reusable. Other ids still fetch immediately.
        self._missing_until: dict[tuple[str, str], float] = {}
        self._load()

    async def get(
        self,
        username: str,
        story_id: str | None,
        *,
        fetch_tray: Callable[[str], Any],
        download_item: Callable[[StoryMedia], Any] | None = None,
    ) -> CachedStories:
        """Return cached stories, fetching the whole profile tray at most once.

        story_id None returns every story from the tray. A concrete story id
        returns that item when the tray already contains it.
        """
        username = _norm_username(username)
        story_id = _canonical_id(story_id) if story_id else None
        tray = await self._tray_for(username, story_id, fetch_tray)
        await self._note_miss(username, story_id, tray)
        now = time.time()
        targets = self._select(tray, story_id, now)
        if download_item is not None:
            targets = list(
                await asyncio.gather(
                    *(self._materialize(item, download_item) for item in targets)
                )
            )
        return CachedStories(username=username, user_id=tray.user_id, items=targets)

    async def _tray_for(
        self,
        username: str,
        story_id: str | None,
        fetch_tray: Callable[[str], Any],
    ) -> _Tray:
        async with self._mutex():
            now = time.time()
            self._prune(now)
            tray = self._trays.get(username)
            if story_id and self._missing_until.get((username, story_id), 0) >= now:
                raise StoryUnavailable(
                    "That story was not found or has already expired.",
                    "expired_story",
                )
            if self._satisfies(tray, story_id, now):
                assert tray is not None
                logger.info("story tray cache hit user=%s story=%s", username, story_id or "*")
                return tray
            future = self._tray_inflight.get(username)
            owner = future is None
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._tray_inflight[username] = future

        if not owner:
            logger.info("story tray join in-flight user=%s story=%s", username, story_id or "*")
            return await _wait(future)

        logger.info("story tray fetch user=%s story=%s", username, story_id or "*")
        try:
            fetched = await _invoke(fetch_tray, username)
            stored = self._coerce_tray(username, fetched)
            async with self._mutex():
                stored = self._store(stored)
                if not future.done():
                    future.set_result(stored)
            return stored
        except asyncio.CancelledError:
            _fail(future, StoryUnavailable("Story request was interrupted. Please try again.", "generic"))
            raise
        except Exception as exc:
            _fail(future, exc)
            raise
        finally:
            async with self._mutex():
                if self._tray_inflight.get(username) is future:
                    self._tray_inflight.pop(username, None)

    def _select(self, tray: _Tray, story_id: str | None, now: float) -> list[StoryMedia]:
        if story_id is None:
            items = [item for item in tray.items.values() if item.expiring_at > now]
            items.sort(key=lambda item: (item.taken_at, item.story_id))
            if not items:
                raise StoryUnavailable(
                    "No active stories found for that account right now.",
                    "no_stories",
                )
            return items

        item = tray.items.get(story_id)
        if item is None or item.expiring_at <= now:
            raise StoryUnavailable(
                "That story was not found or has already expired.",
                "expired_story",
            )
        if item.path and item.path.is_file():
            logger.info("story file cache hit user=%s story=%s", item.username, item.story_id)
        return [item]

    def _satisfies(self, tray: _Tray | None, story_id: str | None, now: float) -> bool:
        if tray is None:
            return False
        if story_id is None:
            ttl = self.profile_ttl if tray.items else self.empty_seconds
            return now - tray.fetched_at <= ttl
        item = tray.items.get(story_id)
        if item is None or item.expiring_at <= now:
            return False
        if item.path and item.path.is_file():
            return True
        return bool(item.media_url) and now - tray.fetched_at <= self.url_ttl

    async def _note_miss(self, username: str, story_id: str | None, tray: _Tray) -> None:
        if not story_id or story_id in tray.items or self.negative_seconds <= 0:
            return
        async with self._mutex():
            self._missing_until[(username, story_id)] = time.time() + self.negative_seconds

    def _store(self, incoming: _Tray) -> _Tray:
        previous = self._trays.get(incoming.username)
        if previous:
            for story_id, item in incoming.items.items():
                old = previous.items.get(story_id)
                if old and old.path and old.path.is_file() and item.path is None:
                    item.path = old.path
                if item.raw is None and story_id in previous.raw_by_id:
                    item.raw = previous.raw_by_id[story_id]
        incoming.raw_by_id = {
            story_id: item.raw for story_id, item in incoming.items.items() if item.raw is not None
        }
        self._trays[incoming.username] = incoming
        for story_id in incoming.items:
            self._missing_until.pop((incoming.username, story_id), None)
        self._save()
        return incoming

    def _coerce_tray(self, username: str, fetched: Any) -> _Tray:
        user_id = None
        rows: Any
        if isinstance(fetched, _Tray):
            return fetched
        if isinstance(fetched, dict) and "items" in fetched:
            user_id = fetched.get("user_id")
            rows = fetched.get("items") or []
        else:
            rows = fetched or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        items: dict[str, StoryMedia] = {}
        now = time.time()
        for row in rows:
            item = _as_media(username, row)
            if item.expiring_at <= now:
                continue
            items[item.story_id] = item
        return _Tray(
            username=username,
            fetched_at=now,
            items=items,
            user_id=int(user_id) if user_id else None,
        )

    async def _materialize(
        self,
        item: StoryMedia,
        download_item: Callable[[StoryMedia], Any],
    ) -> StoryMedia:
        key = (item.username, item.story_id)
        async with self._mutex():
            if item.path and item.path.is_file() and item.expiring_at > time.time():
                return item
            item.path = None
            future = self._file_inflight.get(key)
            owner = future is None
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._file_inflight[key] = future

        if not owner:
            return await _wait(future)

        try:
            downloaded = await _invoke(download_item, item)
            path = self._adopt(item, _as_path(downloaded))
            async with self._mutex():
                item.path = path
                tray = self._trays.get(item.username)
                if tray and item.story_id in tray.items:
                    tray.items[item.story_id].path = path
                    self._save()
                if not future.done():
                    future.set_result(item)
            return item
        except asyncio.CancelledError:
            _fail(future, StoryUnavailable("Story download was interrupted. Please try again.", "generic"))
            raise
        except Exception as exc:
            _fail(future, exc)
            raise
        finally:
            async with self._mutex():
                if self._file_inflight.get(key) is future:
                    self._file_inflight.pop(key, None)

    def _adopt(self, item: StoryMedia, src: Path) -> Path:
        if not src.is_file():
            raise StoryUnavailable("Downloaded story file is missing.", "generic")
        suffix = src.suffix.lower() if src.suffix.lower() in _MEDIA_SUFFIXES else (".mp4" if item.is_video else ".jpg")
        dest = self.files / f"{item.username}_{item.story_id}{suffix}"
        self.files.mkdir(parents=True, exist_ok=True)
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return dest

    def _prune(self, now: float) -> None:
        for username in list(self._trays):
            tray = self._trays[username]
            for story_id in list(tray.items):
                item = tray.items[story_id]
                if item.expiring_at > now:
                    if item.path and not item.path.is_file():
                        item.path = None
                    continue
                if item.path:
                    item.path.unlink(missing_ok=True)
                tray.raw_by_id.pop(story_id, None)
                del tray.items[story_id]
            if not tray.items and now - tray.fetched_at > self.empty_seconds:
                self._trays.pop(username, None)

    def _mutex(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load(self) -> None:
        if not self.index_path.is_file():
            return
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("story cache index unreadable, starting empty")
            return
        now = time.time()
        for username, raw_tray in (payload.get("trays") or {}).items():
            try:
                username = _norm_username(username)
            except StoryUnavailable:
                continue
            items: dict[str, StoryMedia] = {}
            for story_id, raw in (raw_tray.get("items") or {}).items():
                item = _as_media(username, {**raw, "story_id": story_id})
                file_name = raw.get("file")
                if file_name:
                    path = self.files / Path(file_name).name
                    if path.is_file():
                        item.path = path
                if item.expiring_at > now:
                    items[item.story_id] = item
            if not items and now - float(raw_tray.get("fetched_at") or 0) > self.empty_seconds:
                continue
            user_id = raw_tray.get("user_id")
            self._trays[username] = _Tray(
                username=username,
                fetched_at=float(raw_tray.get("fetched_at") or 0),
                items=items,
                user_id=int(user_id) if user_id else None,
            )

    def _save(self) -> None:
        trays: dict[str, Any] = {}
        for username, tray in self._trays.items():
            items: dict[str, Any] = {}
            for story_id, item in tray.items.items():
                body: dict[str, Any] = {
                    "media_url": item.media_url,
                    "is_video": item.is_video,
                    "taken_at": item.taken_at,
                    "expiring_at": item.expiring_at,
                }
                if item.path and item.path.is_file():
                    body["file"] = item.path.name
                items[story_id] = body
            trays[username] = {
                "fetched_at": tray.fetched_at,
                "user_id": tray.user_id,
                "items": items,
            }
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": 1, "trays": trays}), encoding="utf-8")
        tmp.replace(self.index_path)


def _env_float(name: str, explicit: float | None, default: float) -> float:
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _norm_username(username: str) -> str:
    cleaned = (username or "").strip().lstrip("@").lower()
    if not _USERNAME_RE.fullmatch(cleaned):
        raise StoryUnavailable("That Instagram account name is not valid.", "not_found")
    return cleaned


def _canonical_id(story_id: Any) -> str:
    text = str(story_id or "").strip()
    if "_" in text:
        head, _tail = text.split("_", 1)
        if head.isdigit():
            text = head
    if not _ID_RE.fullmatch(text):
        raise StoryUnavailable("That story link is not valid.", "not_found")
    return text


def _as_media(username: str, raw: Any) -> StoryMedia:
    if isinstance(raw, StoryMedia):
        raw.username = raw.username or username
        raw.story_id = _canonical_id(raw.story_id)
        if raw.expiring_at <= 0:
            raw.expiring_at = time.time() + 86400
        elif raw.expiring_at > 10**12:
            raw.expiring_at /= 1000
        return raw
    if not isinstance(raw, dict):
        raise TypeError("story item must be a dict or StoryMedia")
    story_id = raw.get("story_id", raw.get("pk", raw.get("id")))
    media_url = str(raw.get("media_url") or raw.get("url") or "")
    if media_url:
        parsed = urlparse(media_url)
        if parsed.scheme not in {"http", "https"}:
            raise StoryUnavailable("Story media URL is not valid.", "generic")
    is_video = raw.get("is_video")
    if is_video is None:
        is_video = raw.get("media_type") == 2
    return StoryMedia(
        story_id=_canonical_id(story_id),
        username=username,
        media_url=media_url,
        is_video=bool(is_video),
        taken_at=_unix(raw.get("taken_at")),
        expiring_at=_unix(raw.get("expiring_at")) or (time.time() + 86400),
        raw=raw.get("raw"),
    )


def _unix(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    number = float(value)
    if number > 10**12:
        number /= 1000
    return number


def _as_path(downloaded: Any) -> Path:
    if isinstance(downloaded, StoryMedia):
        if downloaded.path is None:
            raise StoryUnavailable("Downloaded story file is missing.", "generic")
        return downloaded.path
    return Path(downloaded)


async def _invoke(fn: Callable[..., Any], arg: Any) -> Any:
    result = fn(arg)
    if inspect.isawaitable(result):
        return await result
    return result


async def _wait(future: asyncio.Future[Any]) -> Any:
    return await asyncio.shield(future)


def _fail(future: asyncio.Future[Any], exc: BaseException) -> None:
    if future.done():
        return
    future.set_exception(exc)
    future.add_done_callback(_consume_future)


def _consume_future(future: asyncio.Future[Any]) -> None:
    if future.cancelled():
        return
    future.exception()

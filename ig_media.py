"""Download every photo and video embedded in an Instagram page.

Stories and carousel posts are read from the JSON Instagram already puts in
the logged-in page. The mobile ``reels_media`` endpoint currently returns an
empty reel or HTTP 500, and yt-dlp only keeps items that have a video file,
so photo stories and later carousel slides never used to be saved.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any, Iterable

import requests

POST_RE = re.compile(r"(?<!/share)/(?:p|reels?|tv)/([A-Za-z0-9_-]+)", re.I)
STORY_RE = re.compile(r"/stories/([^/?#&\s]+)(?:/(\d+))?", re.I)
PROFILE_RE = re.compile(
    r"(?:instagram\.com|instagr\.am)/([A-Za-z0-9._]{1,30})(?:[/?#]|$)",
    re.I,
)
_URL_RE = re.compile(r"https?://[^\s<>()]+", re.I)
_RESERVED_PROFILES = {
    "p",
    "reel",
    "reels",
    "tv",
    "stories",
    "share",
    "explore",
    "accounts",
    "direct",
    "about",
    "legal",
    "developer",
    "nametag",
    "directory",
    "emails",
    "challenge",
    "privacy",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_SCRIPT_JSON_RE = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_MARKERS = (
    "xdt_api__v1__feed__reels_media",
    "xig_polaris_media",
    "shortcode_media",
)
_MEDIA_MAGIC = (
    b"\xff\xd8",  # jpeg
    b"\x89PNG",
    b"GIF8",
    b"RIFF",  # webp
)


class LoginRequired(Exception):
    """The Instagram response is a login wall rather than the media page."""


@dataclass(frozen=True)
class MediaAsset:
    media_id: str
    url: str
    suffix: str
    taken_at: int | None
    username: str | None
    kind: str


def iter_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        urls.append(match.group(0).rstrip(".,);]>\"'"))
    return urls


def classify_instagram(text: str) -> tuple[str, str | None, str | None]:
    """Return ``(kind, shortcode_or_username, media_id)``.

    ``kind`` is ``post``, ``story``, ``highlight``, or ``unknown``.
    A profile URL is a story tray with no media id.
    """
    post = POST_RE.search(text or "")
    if post:
        return "post", post.group(1), None

    story = STORY_RE.search(text or "")
    if story:
        username = story.group(1).strip().lstrip("@")
        if username.lower() == "highlights":
            return "highlight", None, None
        return "story", username, story.group(2)

    profile = PROFILE_RE.search(text or "")
    if profile:
        username = profile.group(1)
        if username.lower() not in _RESERVED_PROFILES:
            return "story", username, None
    return "unknown", None, None


def expand_instagram_text(text: str) -> str:
    """Follow Instagram share links to the post, reel, or story they open."""
    updated = text
    for url in iter_urls(text):
        lowered = url.lower()
        if "instagram.com/share/" not in lowered and "instagr.am/share/" not in lowered:
            continue
        if classify_instagram(url)[0] not in {"unknown", "highlight"}:
            continue
        try:
            response = requests.get(
                url,
                allow_redirects=True,
                timeout=20,
                stream=True,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            )
            final_url = response.url or url
            response.close()
        except requests.RequestException:
            continue
        if final_url and final_url != url:
            updated = updated.replace(url, final_url, 1)
    return updated


def load_netscape_cookies(path: Path) -> MozillaCookieJar:
    """Load a Netscape cookie export, adding the magic header if it is missing."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    first_line = raw.split("\n", 1)[0] if raw else ""
    if "Netscape HTTP Cookie File" not in first_line:
        raw = "# Netscape HTTP Cookie File\n" + raw

    jar = MozillaCookieJar()
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(raw)
            tmp_name = handle.name
        jar.load(tmp_name, ignore_discard=True, ignore_expires=True)
    finally:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)
    return jar


def build_instagram_session(
    cookie_file: Path | None = None,
    extra_cookies: Iterable[Any] | None = None,
) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/",
        }
    )
    if cookie_file is not None and cookie_file.is_file():
        for cookie in load_netscape_cookies(cookie_file):
            session.cookies.set_cookie(cookie)
    if extra_cookies is not None:
        for cookie in extra_cookies:
            session.cookies.set_cookie(cookie)
    return session


def session_has_login(session: requests.Session) -> bool:
    for cookie in session.cookies:
        if cookie.name == "sessionid" and cookie.value:
            return True
    return False


def fetch_html(session: requests.Session, url: str) -> tuple[str, str]:
    response = session.get(url, timeout=60, allow_redirects=True)
    final_url = response.url or url
    if response.status_code in {401, 403} or "accounts/login" in final_url:
        raise LoginRequired(f"Instagram sent the request to {final_url}")
    response.raise_for_status()
    text = response.text
    if _page_requires_login(text, final_url):
        raise LoginRequired("Instagram returned a login page")
    return text, final_url


def parse_html_payloads(page_html: str) -> list[Any]:
    payloads: list[Any] = []
    for raw in _SCRIPT_JSON_RE.findall(page_html):
        text = html.unescape(raw).strip()
        if not text or text[0] not in "{[":
            continue
        try:
            payloads.append(json.loads(text))
        except json.JSONDecodeError:
            continue

    decoder = json.JSONDecoder()
    for marker in _MARKERS:
        start = 0
        while True:
            index = page_html.find(marker, start)
            if index < 0:
                break
            colon = page_html.find(":", index)
            if colon < 0:
                break
            value_at = colon + 1
            while value_at < len(page_html) and page_html[value_at] in " \n\r\t":
                value_at += 1
            if value_at >= len(page_html) or page_html[value_at] not in "{[":
                start = index + len(marker)
                continue
            try:
                obj, offset = decoder.raw_decode(page_html[value_at:])
            except json.JSONDecodeError:
                start = index + len(marker)
                continue
            payloads.append(obj)
            start = value_at + offset
    return payloads


def story_assets_from_payloads(
    payloads: list[Any],
    username: str,
    media_id: str | None = None,
) -> list[MediaAsset]:
    """Active story items for ``username``. Highlight reels are left out."""
    want = username.strip().lstrip("@").lower()
    matched: list[dict[str, Any]] = []
    unscoped: list[dict[str, Any]] = []

    for node in _walk(payloads):
        raw_items = node.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            continue
        if _is_highlight_reel(node):
            continue
        story_items = [item for item in raw_items if isinstance(item, dict) and _is_media_node(item)]
        if not story_items:
            continue
        owner = _username_of(node)
        if owner == want:
            matched.extend(story_items)
        elif owner is None:
            unscoped.extend(story_items)

    chosen = matched or unscoped
    assets: list[MediaAsset] = []
    for item in chosen:
        for piece in _explode(item):
            asset = _asset_from_node(piece, default_username=want, story=True)
            if asset is None:
                continue
            if media_id and asset.media_id != str(media_id) and not asset.media_id.endswith(f"-{media_id}"):
                # Carousel pieces keep the parent id as a prefix; a direct hit
                # on the story id still has to match the parent.
                parent_id = str(item.get("pk") or item.get("id") or "")
                if parent_id != str(media_id):
                    continue
            assets.append(asset)
    return _dedupe_assets(assets)


def post_assets_from_payloads(
    payloads: list[Any],
    shortcode: str,
) -> tuple[list[MediaAsset], int]:
    """Every slide of a post. The second value is how many slides the page claims."""
    matched: list[dict[str, Any]] = []
    for node in _walk(payloads):
        code = node.get("code") or node.get("shortcode")
        if code == shortcode:
            matched.append(node)
    if not matched:
        return [], 0

    best = max(matched, key=lambda node: (len(_explode(node)), _declared_count(node)))
    pieces = _explode(best)
    assets = [
        asset
        for piece in pieces
        if (asset := _asset_from_node(piece, default_username=_username_of(best), story=False)) is not None
    ]
    expected = max(_declared_count(best), len(assets))
    return _dedupe_assets(assets), expected


def save_assets(
    session: requests.Session,
    assets: list[MediaAsset],
    target_dir: str | Path,
    *,
    referer: str,
) -> tuple[int, list[str]]:
    """Save each asset. One failure does not remove files that already succeeded."""
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    saved = 0
    failures: list[str] = []
    for asset in assets:
        destination = target / _filename(asset)
        if destination.exists() and destination.stat().st_size > 0 and _looks_like_media(destination):
            saved += 1
            continue
        try:
            _download(session, asset.url, destination, referer=referer)
        except Exception as exc:
            destination.unlink(missing_ok=True)
            failures.append(f"{asset.media_id}: {exc}")
            continue
        saved += 1
    return saved, failures


def dedupe_media_files(paths: list[Path]) -> list[Path]:
    """Drop later files that are the same bytes as an earlier file."""
    seen: dict[str, Path] = {}
    kept: list[Path] = []
    for path in paths:
        if not path.is_file():
            continue
        digest = _fingerprint(path)
        previous = seen.get(digest)
        if previous is not None:
            if path != previous:
                path.unlink(missing_ok=True)
            continue
        seen[digest] = path
        kept.append(path)
    return kept


def _download(session: requests.Session, url: str, destination: Path, *, referer: str) -> None:
    with session.get(url, stream=True, timeout=120, headers={"Referer": referer}) as response:
        response.raise_for_status()
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type.startswith("text/") or content_type in {"application/json", "text/html"}:
            raise RuntimeError(f"media URL returned {content_type or 'an empty type'}")
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if chunk:
                    handle.write(chunk)
    if destination.stat().st_size < 32 or not _looks_like_media(destination):
        raise RuntimeError("downloaded file is not an image or video")


def _filename(asset: MediaAsset) -> str:
    if asset.taken_at:
        date_text = datetime.fromtimestamp(asset.taken_at, timezone.utc).strftime("%Y-%m-%d")
    else:
        date_text = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    username = _safe_part(asset.username or "instagram")
    return f"{date_text}_{username}_{asset.kind}_{_safe_part(asset.media_id)}{asset.suffix}"


def _asset_from_node(node: dict[str, Any], *, default_username: str | None, story: bool) -> MediaAsset | None:
    chosen = _choose_url(node)
    if chosen is None:
        return None
    url, suffix, visual_kind = chosen
    media_id = _media_id(node)
    if not media_id:
        return None
    if story:
        kind = "GraphStoryVideo" if visual_kind == "video" else "GraphStoryImage"
    else:
        kind = "GraphVideo" if visual_kind == "video" else "GraphImage"
    taken_at = node.get("taken_at") or node.get("taken_at_timestamp")
    taken = int(taken_at) if isinstance(taken_at, (int, float)) else None
    username = _username_of(node) or default_username
    return MediaAsset(
        media_id=media_id,
        url=url,
        suffix=suffix,
        taken_at=taken,
        username=username,
        kind=kind,
    )


def _choose_url(node: dict[str, Any]) -> tuple[str, str, str] | None:
    videos = [item for item in (node.get("video_versions") or []) if isinstance(item, dict) and item.get("url")]
    if videos:
        best = max(videos, key=lambda item: (item.get("width") or 0) * (item.get("height") or 0))
        return str(best["url"]), ".mp4", "video"
    if isinstance(node.get("video_url"), str) and node["video_url"]:
        return node["video_url"], ".mp4", "video"
    resources = node.get("video_resources") or []
    if isinstance(resources, list) and resources and isinstance(resources[-1], dict) and resources[-1].get("src"):
        return str(resources[-1]["src"]), ".mp4", "video"

    candidates = ((node.get("image_versions2") or {}).get("candidates") or [])
    pictures = [item for item in candidates if isinstance(item, dict) and item.get("url")]
    if pictures:
        best = max(pictures, key=lambda item: (item.get("width") or 0) * (item.get("height") or 0))
        return str(best["url"]), ".jpg", "image"
    display_resources = node.get("display_resources") or []
    if (
        isinstance(display_resources, list)
        and display_resources
        and isinstance(display_resources[-1], dict)
        and display_resources[-1].get("src")
    ):
        return str(display_resources[-1]["src"]), ".jpg", "image"
    if isinstance(node.get("display_url"), str) and node["display_url"]:
        return node["display_url"], ".jpg", "image"
    return None


def _explode(node: dict[str, Any]) -> list[dict[str, Any]]:
    carousel = node.get("carousel_media")
    if isinstance(carousel, list) and carousel:
        children = [item for item in carousel if isinstance(item, dict)]
        if children:
            return children
    edges = (node.get("edge_sidecar_to_children") or {}).get("edges") if isinstance(node.get("edge_sidecar_to_children"), dict) else None
    if isinstance(edges, list) and edges:
        children = []
        for edge in edges:
            if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
                children.append(edge["node"])
            elif isinstance(edge, dict):
                children.append(edge)
        if children:
            return children
    return [node]


def _declared_count(node: dict[str, Any]) -> int:
    counts = [len(_explode(node))]
    carousel_count = node.get("carousel_media_count")
    if isinstance(carousel_count, int) and carousel_count > 0:
        counts.append(carousel_count)
    sidecar = node.get("edge_sidecar_to_children")
    if isinstance(sidecar, dict):
        if isinstance(sidecar.get("count"), int) and sidecar["count"] > 0:
            counts.append(sidecar["count"])
        edges = sidecar.get("edges")
        if isinstance(edges, list) and edges:
            counts.append(len(edges))
    return max(counts)


def _is_media_node(node: dict[str, Any]) -> bool:
    if _choose_url(node) is None:
        return False
    if node.get("media_type") in {1, 2, 8}:
        return True
    if "taken_at" in node or "taken_at_timestamp" in node or "is_video" in node:
        return True
    typename = str(node.get("__typename") or "")
    return typename.startswith("Graph") or typename.startswith("XDT")


def _is_highlight_reel(node: dict[str, Any]) -> bool:
    reel_id = str(node.get("id") or "")
    if reel_id.startswith("highlight:"):
        return True
    reel_type = str(node.get("reel_type") or node.get("__typename") or "").lower()
    if "highlight" in reel_type:
        return True
    title = node.get("title")
    return isinstance(title, str) and bool(title.strip()) and "items" in node


def _username_of(node: dict[str, Any]) -> str | None:
    for key in ("user", "owner"):
        value = node.get(key)
        if isinstance(value, dict):
            username = value.get("username")
            if isinstance(username, str) and username.strip():
                return username.strip().lstrip("@").lower()
    username = node.get("username")
    if isinstance(username, str) and username.strip():
        return username.strip().lstrip("@").lower()
    return None


def _media_id(node: dict[str, Any]) -> str:
    for key in ("pk", "id", "code", "shortcode"):
        value = node.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _dedupe_assets(assets: list[MediaAsset]) -> list[MediaAsset]:
    seen: set[str] = set()
    unique: list[MediaAsset] = []
    for asset in assets:
        if asset.media_id in seen:
            continue
        seen.add(asset.media_id)
        unique.append(asset)
    return unique


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _page_requires_login(page_html: str, final_url: str) -> bool:
    if "accounts/login" in final_url:
        return True
    if any(marker in page_html for marker in ("image_versions2", "reels_media", "shortcode_media", "video_versions")):
        return False
    head = page_html[:8000].lower()
    return "password" in head and "login" in head


def _looks_like_media(path: Path) -> bool:
    with path.open("rb") as handle:
        head = handle.read(16)
    if head.startswith(_MEDIA_MAGIC):
        return True
    return b"ftyp" in head or b"WEBP" in head


def _fingerprint(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(size.to_bytes(8, "big"))
    with path.open("rb") as handle:
        digest.update(handle.read(65536))
        if size > 65536:
            handle.seek(max(size - 65536, 0))
            digest.update(handle.read(65536))
    return digest.hexdigest()


def _safe_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "media"

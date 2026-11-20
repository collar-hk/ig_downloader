"""Turn instagram_cookies.txt into an Instaloader session file.

Instaloader's login check calls https://www.instagram.com/graphql/query.
Instagram now redirects that URL to the homepage, and the body is not JSON, so
the check retries until it is interrupted. The session file is only the browser
cookies, so this script saves those without calling graphql/query.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote

import instaloader
import requests

from ig_media import build_instagram_session, load_netscape_cookies

USERNAME = os.environ.get("IG_USERNAME", "ericpoon2026")
SCRIPT_DIR = Path(__file__).resolve().parent
COOKIE_PATH = SCRIPT_DIR / "instagram_cookies.txt"
_USERNAME_RE = re.compile(r"[A-Za-z0-9._]{1,30}")
_WEB_FORM_URL = "https://www.instagram.com/api/v1/accounts/edit/web_form_data/"


def _clean_username(value: str | None) -> str | None:
    if not value:
        return None
    name = unquote(value).strip().lstrip("@")
    if _USERNAME_RE.fullmatch(name):
        return name
    return None


def username_from_cookies(cookie_map: dict[str, str]) -> str | None:
    return _clean_username(cookie_map.get("ds_user"))


def username_from_web_form(session: requests.Session) -> str | None:
    """Ask Instagram for the logged-in username once. Never calls graphql/query."""
    csrf = session.cookies.get("csrftoken")
    headers = {
        "X-IG-App-ID": "936619743392459",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/accounts/edit/",
        "Accept": "application/json",
    }
    if csrf:
        headers["X-CSRFToken"] = csrf
    try:
        response = session.get(_WEB_FORM_URL, headers=headers, timeout=20, allow_redirects=False)
    except requests.RequestException as exc:
        print(f"Could not confirm the username with Instagram: {exc}")
        return None
    if response.is_redirect or response.status_code != 200:
        location = response.headers.get("Location", "")
        print(
            "Instagram did not confirm the cookies "
            f"(HTTP {response.status_code}{f' -> {location}' if location else ''}). "
            "The session file will still be written from the cookie file."
        )
        return None
    try:
        payload = response.json()
    except ValueError:
        print("Instagram did not return a username. The session file will still be written from the cookie file.")
        return None
    form = payload.get("form_data") if isinstance(payload, dict) else None
    if isinstance(form, dict):
        return _clean_username(form.get("username") if isinstance(form.get("username"), str) else None)
    return None


def write_session(cookie_path: Path, dest_dir: Path, *, fallback_username: str = USERNAME) -> Path:
    if not cookie_path.is_file():
        raise SystemExit(f"Cookie file not found: {cookie_path}")

    browser = build_instagram_session(cookie_path)
    cookie_map = {cookie.name: cookie.value for cookie in browser.cookies}
    if not cookie_map.get("sessionid"):
        raise SystemExit(f"{cookie_path.name} has no sessionid cookie. Export cookies again while logged in.")
    if not cookie_map.get("csrftoken"):
        raise SystemExit(f"{cookie_path.name} has no csrftoken cookie. Export cookies again while logged in.")

    username = username_from_cookies(cookie_map) or username_from_web_form(browser) or fallback_username
    loader = instaloader.Instaloader(max_connection_attempts=1, quiet=True)
    loader.context._session.cookies.clear()
    for cookie in load_netscape_cookies(cookie_path):
        loader.context._session.cookies.set_cookie(cookie)
    loader.context.username = username

    session_path = dest_dir / f"ig_session_{username}"
    loader.save_session_to_file(str(session_path))
    return session_path


def main() -> None:
    print(f"Reading cookies from: {COOKIE_PATH}")
    session_path = write_session(COOKIE_PATH, SCRIPT_DIR)
    print(f"Session saved to {session_path}")
    print("Skipped Instaloader's graphql/query login check. Instagram redirects that call to the homepage.")


if __name__ == "__main__":
    main()

import http.cookiejar
from pathlib import Path

import instaloader
import requests.utils

USERNAME = "ericpoon2026"
SCRIPT_DIR = Path(__file__).resolve().parent
COOKIE_PATH = SCRIPT_DIR / "instagram_cookies.txt"
L = instaloader.Instaloader()

if not COOKIE_PATH.exists():
    raise SystemExit(f"Cookie file not found: {COOKIE_PATH}")

# Load Netscape cookies from the same folder as this script.
cookie_jar = http.cookiejar.MozillaCookieJar(str(COOKIE_PATH))
cookie_jar.load(ignore_discard=True, ignore_expires=True)

# Copy into a Requests cookie jar (what Instaloader expects to save/load).
L.context._session.cookies.clear()
for cookie in cookie_jar:
    L.context._session.cookies.set_cookie(cookie)

# Prefer username from cookies when present; fall back to USERNAME.
cookie_map = requests.utils.dict_from_cookiejar(L.context._session.cookies)
ds_user_id = cookie_map.get("ds_user_id")
L.context.username = USERNAME

csrf_token = cookie_map.get("csrftoken")
if csrf_token:
    L.context._session.headers.update({
        "X-CSRFToken": csrf_token,
        "Referer": "https://www.instagram.com/",
    })

print(f"Reading cookies from: {COOKIE_PATH}")
if ds_user_id:
    print(f"Cookie ds_user_id: {ds_user_id}")
print(f"Cookie sessionid present: {'sessionid' in cookie_map}")

try:
    logged_in_user = L.test_login()
    print(f"Successfully logged in as: {logged_in_user}")
    ig_name = logged_in_user or USERNAME
    session_path = SCRIPT_DIR / f"ig_session_{ig_name}"
    L.save_session_to_file(str(session_path))
    print(f"Session saved to {session_path}")
except Exception as e:
    print(f"Error creating session: {e}")

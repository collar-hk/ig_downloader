import http.cookiejar
import instaloader

USERNAME = "ericpoon2026"
L = instaloader.Instaloader()

# Load cookies
cookie_jar = http.cookiejar.MozillaCookieJar("instagram_cookies.txt")
cookie_jar.load(ignore_discard=True, ignore_expires=True)

# Inject cookies into Instaloader session
L.context._session.cookies = cookie_jar
L.context.username = USERNAME

# Extract CSRF token from cookies and attach it to session headers
csrf_token = None
for cookie in cookie_jar:
    if cookie.name == "csrftoken":
        csrf_token = cookie.value
        break

if csrf_token:
    L.context._session.headers.update({
        "X-CSRFToken": csrf_token,
        "Referer": "https://www.instagram.com/",
    })

try:
    logged_in_user = L.test_login()
    print(f"Successfully logged in as: {logged_in_user}")
    L.save_session_to_file()
    print("Session saved!")
except Exception as e:
    print(f"Error creating session: {e}")
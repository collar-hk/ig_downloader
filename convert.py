import http.cookiejar
import instaloader

# Your Instagram username associated with ID 63660869608
USERNAME = "ericpoon2026"

L = instaloader.Instaloader()

# Load Netscape cookie file
cookie_jar = http.cookiejar.MozillaCookieJar("instagram_cookies.txt")
cookie_jar.load(ignore_discard=True, ignore_expires=True)

# Inject cookies into Instaloader context
L.context._session.cookies = cookie_jar
L.context.username = USERNAME

# Verify the session works and save to Instaloader's default session location
try:
    logged_in_user = L.test_login()
    print(f"Successfully logged in as: {logged_in_user}")
    
    # Saves the session file (e.g., session-YOUR_INSTAGRAM_USERNAME)
    L.save_session_to_file()
    print(f"Session saved! You can now use Instaloader via CLI or Python.")
except Exception as e:
    print(f"Error creating session: {e}")
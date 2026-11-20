# Media downloader bot

Telegram bot that saves an Instagram post, reel, or story, or a YouTube video, when you send the link.

A story link saves every active story, photos and videos. A post link saves every slide of a carousel. Highlight links are not downloaded.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=your-token
python bot.py
```

The process has to stay running. A host that only installs dependencies and does not run `python bot.py` will leave Telegram with no reply. `Procfile` and `nixpacks.toml` both start that command.

Sending a post, reel, `/reels/`, story, profile, or Instagram share link gets an immediate "Fetching…" reply. Share links are followed to the real post or story. A link the bot cannot read is answered instead of ignored.

Instagram needs a logged-in browser session. Put either of these next to `bot.py`:

- `instagram_cookies.txt`, a Netscape cookie export that includes `sessionid`
- `ig_session_<username>`, an Instaloader session file

YouTube needs `ffmpeg` and `ffprobe`. A Netscape `cookies.txt` is optional and is used when YouTube asks for a logged-in browser.

`ADMIN_TELEGRAM_IDS` is a comma-separated list of Telegram user ids allowed to run `/setid`.

## Tests

```bash
python -m unittest tests.test_ig_media
```

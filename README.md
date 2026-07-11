# Media Grabber (local, personal use)

A tiny local web app that wraps `yt-dlp` and `gallery-dl` so you can paste a
link and download YouTube videos/playlists/audio, and Twitter/X or Instagram
videos, pictures, reels, and stories.

Runs only on `127.0.0.1` (your machine) — it is not exposed to your network
or the internet.

## 1. Requirements

- Python 3.9+
- [ffmpeg](https://ffmpeg.org/download.html) on your PATH (needed for
  merging video/audio and converting to mp3)
  - macOS: `brew install ffmpeg`
  - Windows: `winget install ffmpeg` (or download a build and add to PATH)
  - Linux: `sudo apt install ffmpeg`

## 2. Install

```bash
cd media-grabber
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Run

```bash
python app.py
```

Then open **http://127.0.0.1:5000** in your browser.

## 4. Using it

1. Paste a link (YouTube video/playlist, a Twitter/X post, or an Instagram
   post/reel/story).
2. Pick a mode: Video, Audio only, or Playlist (playlist mode only matters
   for YouTube — it tells yt-dlp to grab the whole playlist instead of just
   one video).
3. Click Download. The log panel shows live output from the underlying
   tool. Finished files show up as download links, and everything you've
   ever grabbed is listed under "Library" (they're all saved in the
   `downloads/` folder).

## 5. Private content / stories / logged-in-only posts

Public YouTube videos, public tweets, and public Instagram posts work with
no login. Instagram Stories and private accounts require your session
cookies. To enable that:

1. Install a browser extension like "Get cookies.txt LOCALLY" and export
   cookies for the relevant site while logged in, saving them as e.g.
   `cookies_instagram.txt` in the `media-grabber` folder.
2. Before running the app, set the matching environment variable:

```bash
export INSTAGRAM_COOKIES_FILE=cookies_instagram.txt   # macOS/Linux
export TWITTER_COOKIES_FILE=cookies_twitter.txt
export YT_COOKIES_FILE=cookies_youtube.txt
# Windows (PowerShell):
$env:INSTAGRAM_COOKIES_FILE="cookies_instagram.txt"
```

Keep these cookie files private — treat them like a password, since anyone
with them can act as your logged-in session.

## 6. Notes

- This tool is for personal use. Downloading content still runs up against
  the Terms of Service of YouTube, Twitter/X, and Instagram, and you're
  responsible for how you use anything you download (respect copyright,
  don't redistribute other people's content, etc).
- `yt-dlp` and `gallery-dl` are updated frequently because these platforms
  change their internals often. If downloads suddenly start failing, the
  first thing to try is upgrading both:

  ```bash
  pip install -U yt-dlp gallery-dl
  ```
- Everything downloads into the local `downloads/` folder, named
  `Uploader - Title.ext` for YouTube/Twitter, or however `gallery-dl` names
  Instagram content by default.

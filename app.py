import hashlib
import json
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path

import shutil
import sys

from flask import Flask, render_template, request, jsonify, send_from_directory

APP_DIR = Path(__file__).resolve().parent


VENV_BIN = str(Path(sys.executable).parent)
# make sure spawned tools (yt-dlp) can find their helpers (ffmpeg, ffprobe, deno)
if VENV_BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = VENV_BIN + os.pathsep + os.environ.get("PATH", "")

# no system ffmpeg? fall back to the static build bundled by the
# static-ffmpeg package (downloads binaries on first use)
if shutil.which("ffmpeg") is None:
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
    except ImportError:
        pass


def find_tool(name: str) -> str:
    # prefer the copy installed alongside the running interpreter (venv)
    candidate = Path(VENV_BIN) / name
    if candidate.exists():
        return str(candidate)
    return shutil.which(name) or name

DOWNLOAD_DIR = APP_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
THUMB_DIR = DOWNLOAD_DIR / ".thumbs"
THUMB_DIR.mkdir(exist_ok=True)

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Optional: point these at cookies.txt files (Netscape format) exported from
# your browser if you need to grab private/story content. Leave as None to
# download without login (works for public posts/videos).
YT_COOKIES = os.environ.get("YT_COOKIES_FILE")  # e.g. "cookies_youtube.txt"
TWITTER_COOKIES = os.environ.get("TWITTER_COOKIES_FILE")
INSTAGRAM_COOKIES = os.environ.get("INSTAGRAM_COOKIES_FILE")


def detect_cookies_browser():
    """Pick an installed browser whose cookies yt-dlp can borrow —
    gets past YouTube's 'confirm you're not a bot' check."""
    support = Path.home() / "Library" / "Application Support"
    candidates = [
        ("firefox", support / "Firefox"),
        ("chrome", support / "Google" / "Chrome"),
        ("brave", support / "BraveSoftware"),
        ("edge", support / "Microsoft Edge"),
    ]
    for name, path in candidates:
        if path.exists():
            return name
    return None


# set COOKIES_BROWSER=none to disable, or e.g. COOKIES_BROWSER=chrome to force
COOKIES_BROWSER = os.environ.get("COOKIES_BROWSER") or detect_cookies_browser()
if COOKIES_BROWSER == "none":
    COOKIES_BROWSER = None

app = Flask(__name__)

# in-memory job store: job_id -> {"status", "log", "files"}
JOBS = {}


def detect_platform(url: str) -> str:
    url = url.lower()
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "twitter.com" in url or "x.com" in url:
        return "twitter"
    if "instagram.com" in url:
        return "instagram"
    if "tiktok.com" in url:
        return "tiktok"
    return "unknown"


def run_cmd(cmd, job_id):
    JOBS[job_id]["log"].append("$ " + " ".join(cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1
    )
    for line in proc.stdout:
        JOBS[job_id]["log"].append(line.rstrip())
    proc.wait()
    return proc.returncode


def all_download_files():
    # gallery-dl nests output (downloads/instagram/<user>/...), so walk recursively
    return {
        p for p in DOWNLOAD_DIR.rglob("*")
        if p.is_file() and not p.name.endswith(".part")
        and not any(part.startswith(".") for part in p.relative_to(DOWNLOAD_DIR).parts)
    }


def list_new_files(before_snapshot):
    after = all_download_files()
    return sorted(str(p.relative_to(DOWNLOAD_DIR)) for p in (after - before_snapshot))


def platform_cookies(platform):
    return {"youtube": YT_COOKIES, "twitter": TWITTER_COOKIES,
            "instagram": INSTAGRAM_COOKIES}.get(platform)


def cookie_args(platform):
    cookies = platform_cookies(platform)
    if cookies:
        return ["--cookies", cookies]
    if COOKIES_BROWSER:
        return ["--cookies-from-browser", COOKIES_BROWSER]
    return []


def build_ytdlp_cmd(url, media_type, quality, platform):
    if media_type == "playlist":
        out_template = str(DOWNLOAD_DIR / "%(playlist_title)s" / "%(playlist_index)03d - %(title)s.%(ext)s")
    else:
        out_template = str(DOWNLOAD_DIR / "%(uploader)s - %(title)s.%(ext)s")

    cmd = [find_tool("yt-dlp"), "-o", out_template,
           "--yes-playlist" if media_type == "playlist" else "--no-playlist"]

    # tiktok: never pick the watermarked format (falls back only if nothing else exists)
    wm = "[format_note!*=watermark]" if platform == "tiktok" else ""
    if quality == "audio" or media_type == "audio":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    elif quality and quality != "best":
        h = int(quality)
        cmd += ["-f", f"bv*[height<={h}]{wm}+ba/b[height<={h}]{wm}/bv*[height<={h}]+ba/b[height<={h}]",
                "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", f"bv*{wm}+ba/b{wm}/bv*+ba/b", "--merge-output-format", "mp4"]

    cmd += cookie_args(platform)
    cmd.append(url)
    return cmd


USER_DOWNLOADS = Path.home() / "Downloads"
ALREADY_HAVE_RE = re.compile(r"\[download\]\s+(.+?) has already been downloaded")


def export_to_user_downloads(rel_paths, job_id):
    """Clone finished files into ~/Downloads — instant on APFS, no re-download."""
    saved = []
    for rel in rel_paths:
        src = DOWNLOAD_DIR / rel
        dest = USER_DOWNLOADS / Path(rel).name
        if not dest.exists():
            try:
                subprocess.run(["cp", "-c", str(src), str(dest)],
                               check=True, capture_output=True)
            except (subprocess.CalledProcessError, OSError):
                pass
        if not dest.exists():   # clone claimed success but verify for real
            try:
                shutil.copy2(src, dest)
            except OSError as e:
                JOBS[job_id]["log"].append(f"Could not save {dest.name} to Downloads: {e}")
        if dest.exists():
            saved.append(dest.name)
    JOBS[job_id]["saved"] = saved


def gallery_dl_cmd(url, platform):
    cmd = [find_tool("gallery-dl"), "-d", str(DOWNLOAD_DIR)]
    cmd += cookie_args(platform)
    cmd.append(url)
    return cmd


def do_download(job_id, url, media_type, quality):
    JOBS[job_id]["status"] = "running"
    before = all_download_files()
    platform = detect_platform(url)
    JOBS[job_id]["log"].append(f"Detected platform: {platform}")

    try:
        if platform == "instagram":
            rc = run_cmd(gallery_dl_cmd(url, platform), job_id)
            if rc != 0:
                # some reels work via yt-dlp when gallery-dl fails
                rc = run_cmd(build_ytdlp_cmd(url, media_type, quality, platform), job_id)

        else:
            # yt-dlp handles youtube, twitter, tiktok and most other sites
            rc = run_cmd(build_ytdlp_cmd(url, media_type, quality, platform), job_id)
            if rc != 0 and platform == "twitter":
                # fall back to gallery-dl for image-only tweets
                rc = run_cmd(gallery_dl_cmd(url, platform), job_id)

        new_files = list_new_files(before)
        JOBS[job_id]["files"] = new_files

        if rc == 0:
            # also export files that already existed in the library (re-downloads)
            affected = set(new_files)
            for line in JOBS[job_id]["log"]:
                m = ALREADY_HAVE_RE.search(line)
                if m:
                    p = Path(m.group(1))
                    try:
                        affected.add(str(p.resolve().relative_to(DOWNLOAD_DIR)))
                    except ValueError:
                        pass
            export_to_user_downloads(sorted(affected), job_id)
            JOBS[job_id]["status"] = "done"
            if not new_files:
                JOBS[job_id]["log"].append("No new files — already in the library.")
        else:
            JOBS[job_id]["status"] = "error"

    except FileNotFoundError as e:
        JOBS[job_id]["log"].append(f"Tool not found: {e}. Did you install yt-dlp / gallery-dl?")
        JOBS[job_id]["status"] = "error"
    except Exception as e:
        JOBS[job_id]["log"].append(f"Unexpected error: {e}")
        JOBS[job_id]["status"] = "error"


@app.route("/")
def index():
    return render_template("index.html")


def best_thumbnail(info):
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = info.get("thumbnails") or []
    return thumbs[-1].get("url") if thumbs else None


@app.route("/api/probe", methods=["POST"])
def api_probe():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    platform = detect_platform(url)
    cmd = [find_tool("yt-dlp"), "-J", "--flat-playlist", "--no-warnings"]
    cmd += cookie_args(platform)
    cmd.append(url)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        info = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        info = None

    if not info:
        # not something yt-dlp understands (e.g. instagram photo post) —
        # the frontend offers a plain download instead
        return jsonify({"type": "unknown", "platform": platform})

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        thumb = best_thumbnail(info) or (best_thumbnail(entries[0]) if entries else None)
        return jsonify({
            "type": "playlist",
            "platform": platform,
            "title": info.get("title"),
            "uploader": info.get("uploader") or info.get("channel"),
            "count": len(entries),
            "thumbnail": thumb,
            "entries": [{"title": e.get("title"), "duration": e.get("duration")}
                        for e in entries[:500]],
        })

    formats = info.get("formats") or []
    heights = sorted({f["height"] for f in formats
                      if f.get("vcodec") not in (None, "none") and f.get("height")},
                     reverse=True)
    return jsonify({
        "type": "video" if heights else "audio",
        "platform": platform,
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": best_thumbnail(info),
        "qualities": heights,
    })


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    media_type = data.get("type", "video")
    quality = str(data.get("quality") or "best")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"status": "queued", "log": [], "files": [], "saved": []}

    t = threading.Thread(target=do_download, args=(job_id, url, media_type, quality), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
ITEM_RE = re.compile(r"Downloading item (\d+) of (\d+)")


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404

    progress = None
    item = None
    error = None
    for line in reversed(job["log"]):
        if progress is None:
            m = PROGRESS_RE.search(line)
            if m:
                progress = float(m.group(1))
        if item is None:
            m = ITEM_RE.search(line)
            if m:
                item = {"n": int(m.group(1)), "of": int(m.group(2))}
        if error is None and line.startswith("ERROR"):
            error = line
        if progress is not None and item is not None and error is not None:
            break

    return jsonify({
        "status": job["status"],
        "files": job["files"],
        "saved": job.get("saved", []),
        "progress": progress,
        "item": item,
        "error": error if job["status"] == "error" else None,
    })


@app.route("/downloads/<path:filename>")
def get_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)


@app.route("/thumb/<path:filename>")
def get_thumb(filename):
    src = (DOWNLOAD_DIR / filename).resolve()
    if not src.is_file() or DOWNLOAD_DIR not in src.parents and src.parent != DOWNLOAD_DIR:
        return "", 404
    ext = src.suffix.lower()
    if ext in IMAGE_EXTS:
        return send_from_directory(DOWNLOAD_DIR, filename)
    if ext not in VIDEO_EXTS:
        return "", 404  # audio etc. — frontend shows an icon instead

    thumb_name = hashlib.md5(filename.encode()).hexdigest() + ".jpg"
    thumb_path = THUMB_DIR / thumb_name
    if not thumb_path.exists():
        subprocess.run(
            [find_tool("ffmpeg"), "-y", "-ss", "3", "-i", str(src),
             "-frames:v", "1", "-vf", "scale=320:-1", "-q:v", "4", str(thumb_path)],
            capture_output=True, timeout=60,
        )
    if not thumb_path.exists():
        return "", 404
    return send_from_directory(THUMB_DIR, thumb_name)


@app.route("/api/clear", methods=["POST"])
def api_clear():
    removed = 0
    for p in DOWNLOAD_DIR.iterdir():
        if p.name in (".gitkeep", ".thumbs"):
            continue
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        removed += 1
    for t in THUMB_DIR.iterdir():
        t.unlink()
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/library")
def api_library():
    files = sorted(all_download_files(), key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonify([
        {"name": str(f.relative_to(DOWNLOAD_DIR)), "size": f.stat().st_size}
        for f in files
    ])


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)

#!/usr/bin/env python3
"""
🎧 YouTube → Audio Bot v2
Скачивает аудио с YouTube, максимально сжимает (Opus 12kbps, моно, 16kHz)
и отправляет пользователю через Telegram.

Особенности:
  - CPU throttling: nice + ionice + single thread (минимальная нагрузка)
  - Прогресс конвертации с оценкой времени (таймер)
  - Авто-сплит файлов >45MB на части
  - Публичный, любой может отправить ссылку

Требования:
  pip install yt-dlp python-telegram-bot Pillow mutagen
  apt install ffmpeg
"""

import os
import re
import sys
import glob
import time
import math
import json
import html
import logging
import sqlite3
import asyncio
import threading
import subprocess
import traceback
import urllib.request
import concurrent.futures
from pathlib import Path
from datetime import datetime, time as dt_time
from logging.handlers import RotatingFileHandler
from collections import deque

import yt_dlp
from PIL import Image
import base64
from mutagen.oggopus import OggOpus
from mutagen.flac import Picture
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

# ═══════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════

BOT_TOKEN = os.environ.get("YT_AUDIO_BOT_TOKEN", "")
DOWNLOAD_DIR = "/tmp/yt-audio-downloads"
MAX_FILE_SIZE = 45 * 1024 * 1024  # 45 MB safety margin
CLEANUP_AGE = 3600  # 1 hour
MAX_QUEUE_SIZE = 5  # max waiting users (anti-DDoS)
ADMIN_ID = int(os.environ.get("YT_AUDIO_ADMIN_ID", "0") or "0")  # bot admin (privileged user)
COOKIES_REMIND_DAYS = int(os.environ.get("YT_AUDIO_COOKIES_REMIND_DAYS", "14"))  # remind to refresh cookies after N days
VIP_USERS = set(filter(None, os.environ.get("YT_AUDIO_VIP_USERS", "").split(",")))
if ADMIN_ID:
    VIP_USERS.add(str(ADMIN_ID))  # admin is always a VIP user
COOKIES_FILE = os.environ.get("YT_AUDIO_COOKIES", "/opt/yt-audio-bot/cookies.txt")
YTDLP_PATH = os.environ.get("YTDLP_PATH", "/opt/yt-audio-bot/venv/bin/yt-dlp")
DB_PATH = os.environ.get("YT_AUDIO_DB", "/opt/yt-audio-bot/bot.db")

# YouTube player clients to try, in order (PO-token friendly clients first).
YT_CLIENTS = tuple(
    c.strip() for c in os.environ.get(
        "YT_AUDIO_CLIENTS", "android,ios,tv,web,mweb"
    ).split(",") if c.strip()
)
# bgutil PO token provider (HTTP server). Disable with YT_AUDIO_POT_ENABLED=0.
POT_PROVIDER_ENABLED = os.environ.get("YT_AUDIO_POT_ENABLED", "1").lower() not in ("0", "false", "no")
POT_PROVIDER_URL = os.environ.get("YT_AUDIO_POT_URL", "http://127.0.0.1:4416")
# yt-dlp JavaScript runtime for n-sig/challenge solving (node:/abs/path or deno:/abs/path)
YTDLP_JS_RUNTIME = os.environ.get("YT_AUDIO_JS_RUNTIME", "node:/usr/local/bin/node")
# yt-dlp network robustness
YTDLP_RETRY_ARGS = [
    "--retries", "10",
    "--fragment-retries", "10",
    "--socket-timeout", "30",
    "--extractor-retries", "3",
    "--file-access-retries", "3",
]
DOWNLOAD_TIMEOUT = int(os.environ.get("YT_AUDIO_DOWNLOAD_TIMEOUT", "1800"))
EXTRACT_TIMEOUT = int(os.environ.get("YT_AUDIO_EXTRACT_TIMEOUT", "300"))
# Extra full rounds over the client list when YouTube returns an anti-bot block
YTDLP_ROUNDS = int(os.environ.get("YT_AUDIO_ROUNDS", "4"))
YTDLP_ROUND_DELAY = int(os.environ.get("YT_AUDIO_ROUND_DELAY", "15"))
LOG_DIR = "/var/log/yt-audio-bot"
LOG_MAX_SIZE = 1 * 1024 * 1024  # 1 MB per file
LOG_BACKUP_COUNT = 3  # 3 files max = ~3 MB total

# yt-dlp JS runtime path (deno)
os.environ["PATH"] = os.environ.get("PATH", "") + ":/root/.deno/bin"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# ── Logging setup: file (rotating) + console (stderr) ──
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log_file = os.path.join(LOG_DIR, "bot.log")
file_handler = RotatingFileHandler(log_file, maxBytes=LOG_MAX_SIZE, backupCount=LOG_BACKUP_COUNT)
file_handler.setFormatter(log_formatter)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])

# Suppress noisy httpx polling logs
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════
# SQLite database (cache + usage stats)
# ═══════════════════════════════════════════

def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = _db_connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cached_files (
                video_id     TEXT PRIMARY KEY,
                file_id      TEXT,
                parts_json   TEXT,              -- JSON array of file_ids when split into parts
                title        TEXT,
                uploader     TEXT,
                duration     INTEGER DEFAULT 0,
                description  TEXT DEFAULT '',
                webpage_url  TEXT DEFAULT '',
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                video_id   TEXT,
                title      TEXT,
                cached     INTEGER DEFAULT 0,   -- 1 = served from cache, 0 = downloaded fresh
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audio_ratings (
                user_id     INTEGER NOT NULL,
                video_id    TEXT NOT NULL,
                rating      INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                title       TEXT NOT NULL,
                uploader    TEXT DEFAULT '',
                webpage_url TEXT DEFAULT '',
                rated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, video_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audio_ratings_user_rating
            ON audio_ratings (user_id, rating DESC, rated_at DESC)
        """)
        conn.commit()
        logger.info("Database initialized")
    finally:
        conn.close()


def get_cached_file(video_id: str):
    """Return cached file row or None."""
    conn = _db_connect()
    try:
        row = conn.execute(
            "SELECT * FROM cached_files WHERE video_id = ?", (video_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE cached_files SET last_used_at = CURRENT_TIMESTAMP WHERE video_id = ?",
                (video_id,)
            )
            conn.commit()
        return dict(row) if row else None
    finally:
        conn.close()


def save_cached_file(video_id, file_id, parts, title, uploader, duration, description, webpage_url):
    """Save/update cached file. parts = list of file_ids (may be single-element)."""
    conn = _db_connect()
    try:
        conn.execute("""
            INSERT INTO cached_files
                (video_id, file_id, parts_json, title, uploader, duration, description, webpage_url, last_used_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(video_id) DO UPDATE SET
                file_id = excluded.file_id,
                parts_json = excluded.parts_json,
                title = excluded.title,
                uploader = excluded.uploader,
                duration = excluded.duration,
                description = excluded.description,
                webpage_url = excluded.webpage_url,
                last_used_at = CURRENT_TIMESTAMP
        """, (video_id, file_id, json.dumps(parts), title, uploader, duration, description, webpage_url))
        conn.commit()
        logger.info(f"Cache saved: video_id={video_id}, parts={len(parts)}")
    finally:
        conn.close()


def delete_cached_file(video_id: str):
    """Remove a cached file (e.g. when file_id is stale)."""
    conn = _db_connect()
    try:
        conn.execute("DELETE FROM cached_files WHERE video_id = ?", (video_id,))
        conn.commit()
        logger.info(f"Cache removed: video_id={video_id}")
    finally:
        conn.close()


def log_download(user_id: int, video_id: str, title: str, cached: bool):
    """Record a download for usage stats."""
    conn = _db_connect()
    try:
        conn.execute(
            "INSERT INTO downloads (user_id, video_id, title, cached) VALUES (?, ?, ?, ?)",
            (user_id, video_id, title, 1 if cached else 0)
        )
        conn.commit()
    finally:
        conn.close()


def save_rating(user_id: int, video_id: str, rating: int, title: str, uploader: str, webpage_url: str):
    """Save a user's rating for a video, replacing their previous rating."""
    conn = _db_connect()
    try:
        conn.execute("""
            INSERT INTO audio_ratings (user_id, video_id, rating, title, uploader, webpage_url, rated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, video_id) DO UPDATE SET
                rating = excluded.rating,
                title = excluded.title,
                uploader = excluded.uploader,
                webpage_url = excluded.webpage_url,
                rated_at = CURRENT_TIMESTAMP
        """, (user_id, video_id, rating, title, uploader, webpage_url))
        conn.commit()
    finally:
        conn.close()


def get_rating(user_id: int, video_id: str) -> int | None:
    """Return a user's rating for a video, if one exists."""
    conn = _db_connect()
    try:
        row = conn.execute(
            "SELECT rating FROM audio_ratings WHERE user_id = ? AND video_id = ?",
            (user_id, video_id),
        ).fetchone()
        return row["rating"] if row else None
    finally:
        conn.close()


def delete_rating(user_id: int, video_id: str):
    """Remove a user's rating for a video."""
    conn = _db_connect()
    try:
        conn.execute(
            "DELETE FROM audio_ratings WHERE user_id = ? AND video_id = ?",
            (user_id, video_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_rated_audio(user_id: int, limit: int = 30) -> list[dict]:
    """Return a user's rated audio ordered by rating and most recent update."""
    conn = _db_connect()
    try:
        rows = conn.execute("""
            SELECT video_id, rating, title, uploader, webpage_url, rated_at
            FROM audio_ratings
            WHERE user_id = ?
            ORDER BY rating DESC, rated_at DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_stats() -> dict:
    """Aggregate usage statistics."""
    conn = _db_connect()
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM downloads").fetchone()["c"]
        cached = conn.execute("SELECT COUNT(*) AS c FROM downloads WHERE cached = 1").fetchone()["c"]
        fresh = total - cached
        unique_users = conn.execute("SELECT COUNT(DISTINCT user_id) AS c FROM downloads").fetchone()["c"]
        unique_videos = conn.execute("SELECT COUNT(DISTINCT video_id) AS c FROM downloads").fetchone()["c"]
        top = conn.execute("""
            SELECT title, video_id, COUNT(*) AS cnt
            FROM downloads
            GROUP BY video_id
            ORDER BY cnt DESC
            LIMIT 10
        """).fetchall()
        return {
            "total": total,
            "cached": cached,
            "fresh": fresh,
            "unique_users": unique_users,
            "unique_videos": unique_videos,
            "top": [dict(r) for r in top],
        }
    finally:
        conn.close()

# ── Queue system ──
# _active_downloads[user_id] = {url, video_id, proc, files, stage, status_msg, cancel_event}
# stage: "downloading" | "converting" | "sending" | "done"
_active_downloads = {}
_active_urls = set()  # video_ids currently in active processing (not queued)
_queue = deque()  # items: {user_id, video_id, url, update, status_msg}

# ═══════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════

def sanitize(s: str, maxlen: int = 200) -> str:
    s = re.sub(r'[\\/*?:"<>|\s]+', '_', s)
    s = re.sub(r'_+', '_', s).strip('_')
    return s[:maxlen]


def format_duration(seconds: int) -> str:
    if not seconds:
        return "?"
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def parse_ffmpeg_time(time_str: str) -> float:
    """Parse ffmpeg time string (HH:MM:SS.ms) to seconds."""
    parts = time_str.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return 0


def format_size(bytes_: int) -> str:
    if bytes_ < 1024:
        return f"{bytes_} B"
    elif bytes_ < 1024 ** 2:
        return f"{bytes_ / 1024:.1f} KB"
    else:
        return f"{bytes_ / 1024 / 1024:.1f} MB"


def escape_html(text: str) -> str:
    return html.escape(text)


def cleanup_old_files():
    now = time.time()
    removed = 0
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
        if os.path.isfile(f):
            age = now - os.path.getmtime(f)
            # A current yt-dlp download writes a .part file. Only remove stale
            # residues so another incoming update cannot cancel it mid-download.
            if f.endswith(".part") and age > 15 * 60:
                try:
                    os.remove(f)
                    removed += 1
                    logger.info(f"Removed orphaned .part: {os.path.basename(f)}")
                except OSError:
                    pass
                continue
            # Remove old files past cleanup age
            if age > CLEANUP_AGE:
                os.remove(f)
                removed += 1
    if removed:
        logger.info(f"Cleaned up {removed} old file(s)")


def _client_extractor_args(client: str) -> list:
    """Build --extractor-args for a player client, including the PO token provider."""
    args = ["--extractor-args", f"youtube:player_client={client}"]
    if POT_PROVIDER_ENABLED:
        args += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={POT_PROVIDER_URL}"]
    return args


def _js_runtime_args() -> list:
    """Pass the JavaScript runtime to yt-dlp if it is available."""
    if not YTDLP_JS_RUNTIME:
        return []
    path = YTDLP_JS_RUNTIME.split(":", 1)[-1]
    if os.path.exists(path):
        return ["--js-runtimes", YTDLP_JS_RUNTIME]
    return []


# Errors that mean the video itself is unusable — no point retrying.
_NON_RETRYABLE_MARKERS = (
    "video unavailable",
    "this video is unavailable",
    "private video",
    "this video is private",
    "unsupported url",
    "video has been removed",
    "incomplete youtube id",
    "removed by the uploader",
    "does not exist",
    "is not available in your country",
)


def _is_retryable(errors: list) -> bool:
    joined = " ".join(errors).lower()
    return not any(marker in joined for marker in _NON_RETRYABLE_MARKERS)


def _do_download(ydl_opts: dict, url: str) -> dict:
    """Run yt-dlp download via CLI (more reliable than Python API)."""
    errors = []
    rounds = max(1, YTDLP_ROUNDS)
    for round_no in range(rounds):
        for client in YT_CLIENTS:
            cmd = [YTDLP_PATH, "--no-warnings", "--ignore-no-formats-error"]
            cmd += _client_extractor_args(client)
            cmd += _js_runtime_args()
            cmd += YTDLP_RETRY_ARGS
            fmt = ydl_opts.get("format", "worstaudio/worst")
            cmd += ["--format", fmt]
            outtmpl = ydl_opts.get("outtmpl", "%(title)s.%(ext)s")
            cmd += ["--output", outtmpl]
            if ydl_opts.get("cookiefile"):
                cmd += ["--cookies", ydl_opts["cookiefile"]]
            cmd += [url]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
            except subprocess.TimeoutExpired:
                errors.append(f"[{client}] timeout after {DOWNLOAD_TIMEOUT}s")
                logger.warning(f"yt-dlp download timeout: client={client}, url={url}")
                continue
            if r.returncode == 0:
                # Find the downloaded file via glob
                outdir = os.path.dirname(outtmpl)
                base = os.path.basename(outtmpl)
                glob_pattern = re.sub(r"%\([^)]+\)[a-zA-Z]*", "*", base)
                glob_path = os.path.join(outdir, glob_pattern)
                candidates = sorted(glob.glob(glob_path), key=os.path.getmtime, reverse=True)
                if candidates:
                    fp = candidates[0]
                    return {"filepath": fp, "ext": os.path.splitext(fp)[1].lstrip(".")}
                # Download succeeded but file not found — record and try next
                errors.append(f"[{client}] download OK but file not found: {glob_path}")
            else:
                err_txt = (r.stderr or "").strip()[:300]
                errors.append(f"[{client}] rc={r.returncode}. {err_txt}")
        if round_no < rounds - 1 and _is_retryable(errors):
            logger.warning(
                f"yt-dlp download: round {round_no + 1}/{rounds} failed, "
                f"retrying in {YTDLP_ROUND_DELAY}s"
            )
            time.sleep(YTDLP_ROUND_DELAY)
        else:
            break
    # All attempts failed — combine all error details for diagnostics
    error_msg = " | ".join(errors) if errors else "Unknown error"
    raise yt_dlp.utils.DownloadError(error_msg)


def _do_extract_info(ydl_opts: dict, url: str) -> dict:
    """Run yt-dlp info extraction via CLI (more reliable format detection)."""
    errors = []
    rounds = max(1, YTDLP_ROUNDS)
    for round_no in range(rounds):
        for client in YT_CLIENTS:
            cmd = [YTDLP_PATH, "--dump-json", "--no-warnings", "--ignore-no-formats-error"]
            cmd += _client_extractor_args(client)
            cmd += _js_runtime_args()
            cmd += YTDLP_RETRY_ARGS
            if ydl_opts.get("cookiefile"):
                cmd += ["--cookies", ydl_opts["cookiefile"]]
            cmd += [url]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=EXTRACT_TIMEOUT)
            except subprocess.TimeoutExpired:
                errors.append(f"[{client}] timeout after {EXTRACT_TIMEOUT}s")
                logger.warning(f"yt-dlp extract timeout: client={client}, url={url}")
                continue
            if r.returncode == 0:
                info = json.loads(r.stdout)
                formats = info.get("formats") or []
                has_media = any(
                    f.get("acodec", "none") not in ("none", None)
                    or f.get("vcodec", "none") not in ("none", None)
                    for f in formats
                )
                if has_media:
                    return info
                # No media formats — record and try next client
                err_txt = (r.stderr or "").strip()[:300]
                errors.append(f"[{client}] no media formats ({len(formats)} formats). {err_txt}")
            else:
                err_txt = (r.stderr or "").strip()[:300]
                errors.append(f"[{client}] rc={r.returncode}. {err_txt}")
        if round_no < rounds - 1 and _is_retryable(errors):
            logger.warning(
                f"yt-dlp extract: round {round_no + 1}/{rounds} failed, "
                f"retrying in {YTDLP_ROUND_DELAY}s"
            )
            time.sleep(YTDLP_ROUND_DELAY)
        else:
            break
    # All attempts failed — combine all error details for diagnostics
    error_msg = " | ".join(errors) if errors else "Unknown error"
    raise yt_dlp.utils.DownloadError(error_msg)


async def run_ffmpeg_with_progress(
    cmd: list,
    total_duration: float,
    status_msg,
    title: str,
    user_id: int = None,
    stage: str = "Конвертирую"
) -> int:
    """
    Run ffmpeg with CPU throttling (nice+ionice) using async subprocess.
    Progress estimated by elapsed time. Stores process ref for cancellation.
    Does NOT block event loop — other users get responses.
    """
    throttled_cmd = [
        "nice", "-n", "19",
        "ionice", "-c", "3",
    ] + cmd

    start_time = time.time()
    last_update = 0
    last_pct = -1

    # Async subprocess — non-blocking!
    proc = await asyncio.create_subprocess_exec(
        *throttled_cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    # Store process ref for cancellation
    if user_id and user_id in _active_downloads:
        _active_downloads[user_id]["proc"] = proc

    while True:
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            break
        except asyncio.TimeoutError:
            # Check if cancelled
            if user_id and user_id not in _active_downloads:
                proc.kill()
                return -1

            elapsed = time.time() - start_time
            now = time.time()
            if total_duration > 0 and elapsed > 8 and (now - last_update >= 5):
                last_update = now
                # Estimate: Opus encoding at ~10x realtime on this server
                # 50min video → ~5min encode → first update at 30s = 10%
                estimate = total_duration / 10  # 10x realtime
                pct = min(int((elapsed / estimate) * 100), 95)
                if pct != last_pct or True:  # always update to show progress
                    last_pct = pct
                    eta_sec = max(1, int(estimate - elapsed))
                    try:
                        await status_msg.edit_text(
                            f"📥 <b>{escape_html(title)}</b>\n\n"
                            f"🔄 {stage}: {pct}%\n"
                            f"⏱ Осталось ~{format_duration(eta_sec)}\n"
                            f"⚡ Приоритет: низкий (nice+ionice)",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

    return proc.returncode


def split_audio_file(file_path: str, max_size: int, base_name: str) -> list:
    """
    Split audio file into parts if it exceeds max_size.
    Uses ffmpeg with -ss/-to and stream copy.
    Returns list of (part_path, part_number, total_parts, part_duration_secs).
    """
    file_size = os.path.getsize(file_path)
    if file_size <= max_size:
        # Probe actual duration of single file
        probe_cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", file_path
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
        probe_data = json.loads(probe_result.stdout)
        total_duration = float(probe_data["format"]["duration"])
        return [(file_path, 1, 1, total_duration)]

    # Get duration via ffprobe
    probe_cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", file_path
    ]
    probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
    probe_data = json.loads(probe_result.stdout)
    total_duration = float(probe_data["format"]["duration"])

    # Calculate number of parts and duration per part
    num_parts = math.ceil(file_size / max_size)
    part_duration = total_duration / num_parts

    dir_name = os.path.dirname(file_path)
    ext = os.path.splitext(file_path)[1]
    parts = []

    for i in range(num_parts):
        part_num = i + 1
        part_path = os.path.join(
            dir_name, f"{base_name}_part{part_num}_of_{num_parts}{ext}"
        )
        start = i * part_duration

        if i < num_parts - 1:
            split_cmd = [
                "nice", "-n", "19",
                "ffmpeg",
                "-i", file_path,
                "-ss", str(start),
                "-to", str(start + part_duration),
                "-c", "copy",
                "-y",
                part_path
            ]
            actual_duration = part_duration
        else:
            split_cmd = [
                "nice", "-n", "19",
                "ffmpeg",
                "-i", file_path,
                "-ss", str(start),
                "-c", "copy",
                "-y",
                part_path
            ]
            actual_duration = total_duration - start

        subprocess.run(split_cmd, capture_output=True, check=True)
        parts.append((part_path, part_num, num_parts, actual_duration))

    # Remove original file
    os.remove(file_path)

    return parts


# ═══════════════════════════════════════════
# Telegram Handlers
# ═══════════════════════════════════════════

def rating_keyboard(video_id: str, selected: int | None = None) -> InlineKeyboardMarkup:
    buttons = []
    for rating in range(1, 6):
        label = f"✓ {rating}" if rating == selected else str(rating)
        buttons.append(InlineKeyboardButton(label, callback_data=f"rate:{video_id}:{rating}"))
    return InlineKeyboardMarkup([buttons])


async def handle_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    match = re.fullmatch(r"rate:([A-Za-z0-9_-]{11}):([1-5])", query.data or "")
    if not match:
        await query.answer("Некорректная оценка.", show_alert=True)
        return

    video_id, rating_text = match.groups()
    rating = int(rating_text)
    row = get_cached_file(video_id)
    audio = query.message.audio if query.message else None
    title = (row or {}).get("title") or (audio.title if audio else None) or "Без названия"
    uploader = (row or {}).get("uploader") or (audio.performer if audio else None) or ""
    webpage_url = (row or {}).get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"

    try:
        if get_rating(query.from_user.id, video_id) == rating:
            delete_rating(query.from_user.id, video_id)
            await query.answer("Оценка снята")
            await query.edit_message_reply_markup(reply_markup=rating_keyboard(video_id))
            logger.info(f"Rating removed: user={query.from_user.id}, video_id={video_id}")
            return
        save_rating(query.from_user.id, video_id, rating, title, uploader, webpage_url)
        await query.answer(f"Оценка: {rating}/5")
        await query.edit_message_reply_markup(reply_markup=rating_keyboard(video_id, rating))
        logger.info(f"Rating saved: user={query.from_user.id}, video_id={video_id}, rating={rating}")
    except Exception:
        logger.error(f"Rating save failed: {traceback.format_exc()}")
        await query.answer("Не удалось сохранить оценку. Попробуйте ещё раз.", show_alert=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎧 <b>YouTube → Audio Bot</b>\n\n"
        "Просто отправь ссылку на YouTube видео — "
        "получишь аудио в максимально сжатом виде.\n\n"
        "🔹 <b>Формат:</b> Opus 12kbps, моно, 16 кГц\n"
        "🔹 <b>Сплит:</b> если >50 МБ — разбивается на части\n"
        "🔹 <b>CPU:</b> низкий приоритет, не нагружает сервер\n"
        "🔹 <b>Поддерживаются:</b> youtube.com, youtu.be\n\n"
        "Пример:\n"
        "  <code>https://youtube.com/watch?v=dQw4w9WgXcQ</code>\n\n"
        "📋 <b>Команды:</b>\n"
        "  /start — показать это сообщение\n"
        "  /help — справка и команды\n"
        "  /cancel — отменить текущую загрузку\n"
        "  /rated — мои оценённые аудио",
        parse_mode="HTML",
        disable_web_page_preview=True
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎧 <b>YouTube → Audio Bot — Справка</b>\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Найди видео на YouTube\n"
        "2. Отправь ссылку боту\n"
        "3. Дождись конвертации\n"
        "4. Получи аудиофайл\n\n"
        "<b>Поддерживаемые ссылки:</b>\n"
        "  • <code>https://youtube.com/watch?v=...</code>\n"
        "  • <code>https://youtu.be/...</code>\n"
        "  • <code>https://m.youtube.com/watch?v=...</code>\n\n"
        "<b>Команды:</b>\n"
        "  /start — приветствие и информация\n"
        "  /help — эта справка\n"
        "  /cancel — отменить текущую загрузку\n"
        "  /rated — мои оценённые аудио\n\n"
        "<b>Формат на выходе:</b> Opus 12kbps, моно, 16 кГц\n"
        "<b>Ограничение:</b> до 50 МБ (с авто-сплитом)\n\n"
        "⚡ Процесс конвертации имеет низкий приоритет\n"
        "и не нагружает сервер.",
        parse_mode="HTML",
        disable_web_page_preview=True
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show usage statistics (admin only)."""
    user_id = update.effective_user.id
    if str(user_id) != str(ADMIN_ID):
        await update.message.reply_text(
            "⛔ Команда доступна только администратору.",
            reply_to_message_id=update.message.message_id
        )
        return
    try:
        stats = get_stats()
        lines = [
            "📊 <b>Статистика использования</b>\n",
            f"👥 Всего пользователей: <b>{stats['unique_users']}</b>",
            f"🎬 Уникальных видео: <b>{stats['unique_videos']}</b>",
            f"⬇️ Всего загрузок: <b>{stats['total']}</b>",
            f"⚡ Из кэша: <b>{stats['cached']}</b> ({round(stats['cached'] / stats['total'] * 100) if stats['total'] else 0}%)",
            f"🔄 Свежих: <b>{stats['fresh']}</b>",
        ]
        if stats["top"]:
            lines.append("\n🏆 <b>Топ видео:</b>")
            for i, v in enumerate(stats["top"][:10], 1):
                lines.append(
                    f"{i}. {escape_html((v['title'] or '?')[:40])} — <b>{v['cnt']}</b>"
                )
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML",
            reply_to_message_id=update.message.message_id
        )
    except Exception as e:
        logger.error(f"Stats error: {traceback.format_exc()}")
        await update.message.reply_text(
            f"❌ Ошибка получения статистики: {escape_html(str(e)[:100])}",
            reply_to_message_id=update.message.message_id
        )


async def cmd_rated(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the calling user's rated audio."""
    try:
        ratings = get_rated_audio(update.effective_user.id)
        if not ratings:
            text = "⭐ Пока нет оценённых аудио. Поставьте оценку кнопками под аудиофайлом."
        else:
            lines = ["⭐ <b>Мои оценённые аудио</b>\n"]
            for index, item in enumerate(ratings, 1):
                title = escape_html((item["title"] or "Без названия")[:120])
                uploader = escape_html((item["uploader"] or "")[:80])
                line = f"{index}. <b>{item['rating']}/5</b> {title}"
                if uploader:
                    line += f"\n   👤 {uploader}"
                if item["webpage_url"]:
                    line += f"\n   🔗 {escape_html(item['webpage_url'])}"
                lines.append(line)
            text = "\n".join(lines)
        await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.error(f"Rated list failed: {traceback.format_exc()}")
        await update.message.reply_text("❌ Не удалось получить оценки. Попробуйте позже.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel current download or remove from queue."""
    user_id = update.effective_user.id
    global _active_downloads, _active_urls, _queue

    # Check if user is in queue
    for i, item in enumerate(_queue):
        if item["user_id"] == user_id:
            del _queue[i]
            await update.message.reply_text(
                "✅ Вы удалены из очереди.",
                reply_to_message_id=update.message.message_id
            )
            logger.info(f"User {user_id} removed from queue")
            return

    if user_id not in _active_downloads:
        await update.message.reply_text(
            "❌ Нет активных задач для отмены.",
            reply_to_message_id=update.message.message_id
        )
        return

    info = _active_downloads[user_id]
    video_id = info["video_id"]

    # Set cancel event flag for yt-dlp executor
    cancel_event = info.get("cancel_event")
    if cancel_event:
        cancel_event.set()

    # Kill the running process (ffmpeg)
    proc = info.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass

    # Clean up files
    files = info.get("files", [])
    for f in files:
        try:
            if os.path.exists(f):
                os.remove(f)
                logger.info(f"Cancelled: removed {f}")
        except OSError:
            pass

    # Remove from tracking
    _active_downloads.pop(user_id, None)
    _active_urls.discard(video_id)

    await update.message.reply_text(
        "✅ Задача отменена. Временные файлы удалены.",
        reply_to_message_id=update.message.message_id
    )
    logger.info(f"User {user_id} cancelled download of {video_id}")

    # Start next in queue
    await _process_next_in_queue()


async def _try_send_cached(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, video_id: str, user_id: int) -> bool:
    """
    Try to serve the audio from the Telegram cloud cache (file_id).
    Returns True if served from cache, False if not cached / needs fresh download.
    """
    row = get_cached_file(video_id)
    if not row or not (row.get("file_id") or row.get("parts_json")):
        return False

    title = row.get("title") or "Unknown"
    uploader = row.get("uploader") or "Unknown"
    duration = int(row.get("duration") or 0)
    description = (row.get("description") or "")[:200]
    webpage_url = row.get("webpage_url") or text
    duration_str = format_duration(duration)
    short_desc = description[:200] + ("…" if len(description) > 200 else "")

    parts = []
    try:
        parts = json.loads(row["parts_json"]) if row.get("parts_json") else ([row["file_id"]] if row.get("file_id") else [])
    except Exception:
        parts = [row["file_id"]] if row.get("file_id") else []
    if not parts:
        return False

    status_msg = await update.message.reply_text("⚡ Нашёл в кэше, отправляю…")
    try:
        for i, fid in enumerate(parts):
            part_num = i + 1
            total = len(parts)
            caption_parts = [
                f"<b>{escape_html(title)}</b>",
                f"👤 {escape_html(uploader)}",
                f"⏱ {duration_str}",
            ]
            if total > 1:
                caption_parts.append(f"📦 Часть {part_num} из {total} (кэш)")
            else:
                caption_parts.append(f"📦 Из кэша | Opus 12kbps")
            if short_desc and part_num == 1:
                caption_parts.append(f"\n{escape_html(short_desc)}")
                caption_parts.append(f"\n🔗 {webpage_url}")
            caption = "\n".join(caption_parts)
            audio_title = f"{title[:240]} (ч.{part_num}/{total})" if total > 1 else title[:256]
            await update.message.reply_audio(
                audio=fid, title=audio_title, performer=uploader[:256],
                duration=duration, caption=caption, parse_mode="HTML",
                reply_markup=rating_keyboard(video_id) if part_num == total else None,
                reply_to_message_id=update.message.message_id
            )
        await status_msg.delete()
        log_download(user_id, video_id, title, cached=True)
        logger.info(f"Cache hit: user={user_id}, video_id={video_id}, parts={len(parts)}")
        return True
    except Exception as e:
        # file_id likely stale — drop from cache and let fresh download happen
        logger.warning(f"Cache miss (stale file_id): video_id={video_id}, error={e}")
        try:
            await status_msg.delete()
        except Exception:
            pass
        delete_cached_file(video_id)
        return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    youtube_pattern = re.compile(
        r'(https?://)?(www\.|m\.)?(youtube\.com|youtu\.be)(/\S*)?',
        re.IGNORECASE
    )
    if not youtube_pattern.search(text):
        await update.message.reply_text(
            "❌ Пожалуйста, отправь ссылку на YouTube видео.\n\n"
            "Пример: <code>https://youtube.com/watch?v=...</code>",
            parse_mode="HTML",
            reply_to_message_id=update.message.message_id
        )
        return

    cleanup_old_files()

    # Normalize URL to video ID for dedup
    vid_match = re.search(r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})', text)
    video_id = vid_match.group(1) if vid_match else text
    user_id = update.effective_user.id

    # Try serving from cache (fast path — no download/conversion needed)
    try:
        if await _try_send_cached(update, context, text, video_id, user_id):
            return
    except Exception as e:
        logger.warning(f"Cache lookup failed: user={user_id}, video_id={video_id}, error={e}")

    global _active_downloads, _active_urls, _queue

    # Check concurrent: same user already active
    if user_id in _active_downloads:
        await update.message.reply_text(
            "⏳ Уже обрабатываю ссылку. Дождись завершения или отправь /cancel.",
            reply_to_message_id=update.message.message_id
        )
        return

    # Check concurrent: same user in queue
    for item in _queue:
        if item["user_id"] == user_id:
            await update.message.reply_text(
                "⏳ Дождитесь своей очереди.",
                reply_to_message_id=update.message.message_id
            )
            return

    # Check concurrent: same video already being processed
    if video_id in _active_urls:
        await update.message.reply_text(
            "⏳ Это видео уже обрабатывается. Дождись завершения.",
            reply_to_message_id=update.message.message_id
        )
        return

    # If someone else is busy → queue this user (max 5)
    if _active_downloads:
        is_vip = str(user_id) in VIP_USERS
        if len(_queue) >= MAX_QUEUE_SIZE:
            if is_vip:
                # VIP: kick the last regular user from queue
                kicked = _queue.pop()
                try:
                    await kicked["status_msg"].edit_text(
                        "⚠️ Ваше место в очереди занял приоритетный пользователь. Попробуйте позже."
                    )
                except Exception:
                    pass
                logger.info(f"VIP {user_id} kicked {kicked['user_id']} from queue")
            else:
                await update.message.reply_text(
                    "⏳ Очередь переполнена. Попробуй позже.",
                    reply_to_message_id=update.message.message_id
                )
                logger.info(f"Queue full: user={user_id}, video_id={video_id} rejected")
                return
        busy_user = next(iter(_active_downloads))
        busy_stage = _active_downloads[busy_user].get("stage", "processing")
        status_msg = await update.message.reply_text(
            f"⏳ Бот занят ({busy_stage}). Вы в очереди — ожидайте.",
            reply_to_message_id=update.message.message_id
        )
        _queue.append({
            "user_id": user_id,
            "video_id": video_id,
            "url": text,
            "update": update,
            "context": context,
            "status_msg": status_msg,
        })
        logger.info(f"User {user_id} queued (busy: {busy_user}), video_id={video_id}")
        return

    # Free slot — start processing
    logger.info(f"New download: user={user_id}, video_id={video_id}, url={text[:80]}")
    await _start_processing(update, context, text, user_id, video_id)


async def _start_processing(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user_id: int, video_id: str):
    """Run the full download → convert → send pipeline."""
    global _active_downloads, _active_urls, _queue

    # Mark as active with stage tracking
    cancel_event = threading.Event()
    _active_downloads[user_id] = {"url": text, "video_id": video_id, "files": [], "proc": None, "stage": "starting", "cancel_event": cancel_event}
    _active_urls.add(video_id)

    # ── Step 0: Check disk space ──
    st = os.statvfs(DOWNLOAD_DIR)
    free_bytes = st.f_frsize * st.f_bavail
    free_mb = free_bytes / 1024 / 1024
    MIN_FREE_MB = 200
    if free_mb < MIN_FREE_MB:
        raise Exception(f"Недостаточно места на диске: {free_mb:.0f} MB свободно. Нужно минимум {MIN_FREE_MB} MB.")

    success = False
    status_msg = None
    try:
        status_msg = await update.message.reply_text("⏳ Получаю информацию о видео...")
        _active_downloads[user_id]["status_msg"] = status_msg

        # ── Step 1: Extract info (in executor to not block event loop) ──
        _active_downloads[user_id]["stage"] = "extracting info"
        logger.info(f"Extracting info: user={user_id}, video_id={video_id}")
        ydl_opts_info = {}
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _do_extract_info, ydl_opts_info, text)

        # Check if there are any real formats (not just storyboard/images)
        formats = info.get("formats") or []
        has_media = any(
            f.get("acodec", "none") not in ("none", None)
            or f.get("vcodec", "none") not in ("none", None)
            for f in formats
        )
        if not has_media:
            raise Exception(
                "Это видео не имеет доступных для скачивания аудио/видео дорожек. "
                "Возможно, видео ещё обрабатывается YouTube или недоступно."
            )

        title = info.get("title", "Unknown")
        uploader = info.get("uploader", "Unknown")
        duration = info.get("duration", 0)
        description = (info.get("description") or "")[:500]
        webpage_url = info.get("webpage_url", text)
        thumbnails = info.get("thumbnails") or []
        thumbnail_url = ""
        if thumbnails:
            thumbnail_url = sorted(thumbnails, key=lambda t: t.get("preference", 0) or t.get("height", 0) or 0, reverse=True)[0].get("url", "")
        if not thumbnail_url:
            thumbnail_url = info.get("thumbnail", "")

        duration_str = format_duration(duration)
        short_desc = description[:200] + ("…" if len(description) > 200 else "")
        safe_title = sanitize(title, 120)

        # ── Check disk space for the source file ──
        source_size_mb = (info.get("filesize") or 0) / 1024 / 1024
        if source_size_mb > 0 and source_size_mb > free_mb - 200:
            raise Exception(
                f"Недостаточно места: файл {source_size_mb:.0f} MB, "
                f"свободно {free_mb:.0f} MB. Нужно минимум {source_size_mb + 200:.0f} MB."
            )

        await status_msg.edit_text(
            f"📥 <b>{escape_html(title)}</b>\n"
            f"👤 {escape_html(uploader)} | ⏱ {duration_str}\n\n"
            f"⬇️ Скачиваю аудио…",
            parse_mode="HTML"
        )

        # ── Step 2: Download audio with yt-dlp (with progress) ──
        _active_downloads[user_id]["stage"] = "downloading"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        source_template = os.path.join(
            DOWNLOAD_DIR, f"{timestamp}_{safe_title}_source.%(ext)s"
        )

        ydl_opts = {
            "format": "worstaudio/worst",
            "outtmpl": source_template,
            "cookiefile": COOKIES_FILE,
        }
        if COOKIES_FILE and not os.path.exists(COOKIES_FILE):
            ydl_opts.pop("cookiefile", None)

        loop = asyncio.get_event_loop()
        download_future = loop.run_in_executor(None, _do_download, ydl_opts, text)

        # Wait for download with periodic status updates
        last_update = 0
        while not download_future.done():
            if cancel_event.is_set():
                raise Exception("Download cancelled by user")
            now_t = time.time()
            if now_t - last_update >= 5:
                last_update = now_t
                try:
                    await status_msg.edit_text(
                        f"📥 <b>{escape_html(title)}</b>\n"
                        f"👤 {escape_html(uploader)} | ⏱ {duration_str}\n\n"
                        f"⬇️ Скачиваю… (пожалуйста, подождите)\n"
                        f"⏳ Это может занять до 2-3 минут",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            await asyncio.sleep(2)

        info = await download_future
        source_path = info.get("filepath")
        if not source_path or not os.path.exists(source_path):
            # Fallback: search by timestamp pattern
            candidates = glob.glob(
                os.path.join(DOWNLOAD_DIR, f"{timestamp}_{safe_title}_source.*")
            )
            source_path = candidates[0] if candidates else None

        if not source_path or not os.path.exists(source_path):
            raise Exception("Source file not found after download")

        _active_downloads[user_id]["files"] = [source_path]
        logger.info(f"yt-dlp download complete: user={user_id}, video_id={video_id}, source={source_path}")

        # ── Step 3: Download thumbnail (in executor) ──
        thumb_path = None
        if thumbnail_url:
            try:
                thumb_path = os.path.join(DOWNLOAD_DIR, f"{timestamp}_{safe_title}_thumb.jpg")
                await loop.run_in_executor(None, urllib.request.urlretrieve, thumbnail_url, thumb_path)
                with Image.open(thumb_path) as img_check:
                    await loop.run_in_executor(None, img_check.verify)
                logger.info(f"Thumbnail downloaded: {thumb_path}")
            except Exception as e:
                logger.warning(f"Failed to download thumbnail: {e}")
                if thumb_path and os.path.exists(thumb_path):
                    try:
                        os.remove(thumb_path)
                    except OSError:
                        pass
                thumb_path = None

        # ── Step 4: Convert to Opus ──
        _active_downloads[user_id]["stage"] = "converting"
        output_filename = f"{timestamp}_{safe_title}.opus"
        output_path = os.path.join(DOWNLOAD_DIR, output_filename)

        files = _active_downloads[user_id].get("files", [])
        if thumb_path:
            files.append(thumb_path)
        files.append(output_path)
        _active_downloads[user_id]["files"] = files

        meta = {
            "title": title,
            "artist": uploader,
            "description": description,
            "comment": f"Source: {webpage_url}",
            "purl": webpage_url,
        }
        metadata_args = []
        for k, v in meta.items():
            metadata_args += ["-metadata", f"{k}={v}"]

        ffmpeg_cmd = ["ffmpeg", "-i", source_path] + [
            "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "12k",
            "-application", "voip", "-threads", "1", "-map_metadata", "-1",
            "-progress", "pipe:1",
        ] + metadata_args + ["-y", output_path]

        logger.info(f"Starting ffmpeg: user={user_id}, video_id={video_id}, duration={format_duration(duration)}, source_size={os.path.getsize(source_path)}\n    ffmpeg cmd: {' '.join(ffmpeg_cmd[:8])}...")

        result_code = await run_ffmpeg_with_progress(
            ffmpeg_cmd, duration, status_msg, title, user_id=user_id
        )

        if result_code != 0:
            if user_id not in _active_downloads:
                raise Exception("Download cancelled by user")
            fallback_cmd = ["nice", "-n", "19", "ionice", "-c", "3"] + [a for a in ffmpeg_cmd if a != "-progress" and a != "pipe:1"]
            fallback_result = await loop.run_in_executor(None, lambda: subprocess.run(fallback_cmd, capture_output=True, text=True))
            if fallback_result.returncode != 0:
                bare_cmd = [a for a in ffmpeg_cmd if a != "-progress" and a != "pipe:1"]
                bare_result = await loop.run_in_executor(None, lambda: subprocess.run(bare_cmd, capture_output=True, text=True))
                if bare_result.returncode != 0:
                    raise Exception(f"ffmpeg failed: {bare_result.stderr[:300]}")
            result_code = 0

        # Embed cover art
        if thumb_path and os.path.exists(output_path):
            try:
                audio = OggOpus(output_path)
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                with open(thumb_path, "rb") as f:
                    pic.data = f.read()
                pic.width = 1280
                pic.height = 720
                pic.depth = 8
                pic.colors = 0
                pic_data = pic.write()
                encoded = base64.b64encode(pic_data).decode("ascii")
                audio["metadata_block_picture"] = [encoded]
                audio.save()
                logger.info("Cover art embedded successfully")
            except Exception as e:
                logger.warning(f"Failed to embed cover art: {e}")

        try:
            os.remove(source_path)
        except OSError:
            pass
        if thumb_path:
            try:
                os.remove(thumb_path)
            except OSError:
                pass

        logger.info(f"ffmpeg done: user={user_id}, video_id={video_id}, output={output_path}, size={format_size(os.path.getsize(output_path))}")

        if not os.path.exists(output_path):
            raise Exception("Output file not found after conversion")

        # ── Step 5: Check size, split if needed ──
        _active_downloads[user_id]["stage"] = "splitting"
        file_size = os.path.getsize(output_path)

        if file_size > MAX_FILE_SIZE:
            await status_msg.edit_text(
                f"📥 <b>{escape_html(title)}</b>\n\n"
                f"✂️ Файл {format_size(file_size)} — разбиваю на части…",
                parse_mode="HTML"
            )
            parts = await loop.run_in_executor(None, split_audio_file, output_path, MAX_FILE_SIZE, f"{timestamp}_{safe_title}")
            logger.info(f"Split: {len(parts)} parts, file={format_size(file_size)}")
        else:
            probe_cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", output_path]
            probe_r = await loop.run_in_executor(None, lambda: subprocess.run(probe_cmd, capture_output=True, text=True))
            single_dur = float(json.loads(probe_r.stdout)["format"]["duration"])
            parts = [(output_path, 1, 1, single_dur)]
            logger.info(f"No split needed: {format_size(file_size)}, duration={format_duration(int(single_dur))}")

        # ── Step 6: Send file(s) ──
        _active_downloads[user_id]["stage"] = "sending"
        sent_file_ids = []
        send_failures = []
        for part_path, part_num, total, part_dur in parts:
            part_size = os.path.getsize(part_path)

            caption_parts = [
                f"<b>{escape_html(title)}</b>",
                f"👤 {escape_html(uploader)}",
                f"⏱ {format_duration(int(part_dur))}",
            ]
            if total > 1:
                caption_parts.append(f"📦 Часть {part_num} из {total} — {format_size(part_size)}")
            else:
                caption_parts.append(f"📦 {format_size(part_size)} | Opus 12kbps")

            if short_desc and part_num == 1:
                caption_parts.append(f"\n{escape_html(short_desc)}")
                caption_parts.append(f"\n🔗 {webpage_url}")

            caption = "\n".join(caption_parts)

            if total > 1:
                await status_msg.edit_text(
                    f"📤 <b>{escape_html(title)}</b>\n⬆️ Отправляю часть {part_num} из {total}…",
                    parse_mode="HTML"
                )
            else:
                await status_msg.edit_text(
                    f"📤 <b>{escape_html(title)}</b>\n📦 {format_size(part_size)} | ⏱ {format_duration(int(part_dur))}\n\n⬆️ Отправляю…",
                    parse_mode="HTML"
                )

            audio_title = f"{title[:240]} (ч.{part_num}/{total})" if total > 1 else title[:256]
            reply_markup = rating_keyboard(video_id) if part_num == total else None
            sent = None
            last_err = None
            for attempt in range(2):
                try:
                    with open(part_path, "rb") as f:
                        sent = await update.message.reply_audio(
                            audio=f, title=audio_title, performer=uploader[:256],
                            duration=int(part_dur), caption=caption, parse_mode="HTML",
                            reply_markup=reply_markup,
                            reply_to_message_id=update.message.message_id
                        )
                    break
                except Exception as e:
                    last_err = e
                    logger.error(f"Send part {part_num}/{total} attempt {attempt + 1}/2 failed: user={user_id}, error={type(e).__name__}: {e}")
                    await asyncio.sleep(2)

            if sent is None:
                if last_err is not None and "VOICE_MESSAGES_FORBIDDEN" in str(last_err):
                    try:
                        with open(part_path, "rb") as f:
                            sent = await update.message.reply_document(
                                document=f,
                                filename=f"{safe_title}_part{part_num}_of_{total}.opus",
                                caption=caption, parse_mode="HTML",
                                reply_markup=reply_markup,
                                reply_to_message_id=update.message.message_id
                            )
                    except Exception as e:
                        last_err = e
                        logger.error(f"Send part {part_num}/{total} document fallback failed: user={user_id}, error={type(e).__name__}: {e}")
                if sent is None:
                    send_failures.append(part_num)
                    logger.error(f"Send part {part_num}/{total} FAILED: user={user_id}, error={type(last_err).__name__ if last_err else 'unknown'}: {last_err}")
                    if status_msg:
                        try:
                            await status_msg.edit_text(
                                f"⚠️ <b>{escape_html(title)}</b>\n"
                                f"Не удалось отправить часть {part_num} из {total} (файл сохранён на сервере).\n"
                                f"Ошибка: <code>{escape_html(str(last_err)[:120])}</code>",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                    continue

            # Collect file_id for caching (Telegram stores the file in the cloud)
            fid = (sent.audio.file_id if sent and sent.audio else None)
            if fid:
                sent_file_ids.append(fid)

            logger.info(f"Sent part {part_num}/{total}: user={user_id}, size={format_size(part_size)}, dur={format_duration(int(part_dur))}, file_id={'yes' if (sent_file_ids and len(sent_file_ids) >= part_num) else 'no'}")
            try:
                os.remove(part_path)
            except OSError:
                pass

        if send_failures:
            success = False
            if status_msg:
                ok_parts = [p for p in range(1, total + 1) if p not in send_failures]
                try:
                    await status_msg.edit_text(
                        f"⚠️ <b>{escape_html(title)}</b>\n\n"
                        f"Отправлено частей: {', '.join(str(p) for p in ok_parts) if ok_parts else 'нет'}\n"
                        f"Не отправлено: {', '.join(str(p) for p in send_failures)}\n\n"
                        f"Неотправленные файлы остались на сервере. Отправь ссылку ещё раз — они будут отправлены повторно.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        # ── Step 7: Save to cache + log usage ──
        if sent_file_ids and not send_failures:
            try:
                save_cached_file(
                    video_id, sent_file_ids[0], sent_file_ids,
                    title, uploader, int(duration), (description or ""), webpage_url
                )
            except Exception as e:
                logger.warning(f"Failed to save cache: video_id={video_id}, error={e}")
        try:
            log_download(user_id, video_id, title, cached=False)
        except Exception as e:
            logger.warning(f"Failed to log download: {e}")

        success = True
        _active_downloads[user_id]["stage"] = "done"

    except yt_dlp.utils.DownloadError as e:
        err_text = str(e)
        logger.error(f"yt-dlp error: user={user_id}, video_id={video_id}, error={err_text}")
        if ("Sign in to confirm" in err_text or "not a bot" in err_text
                or "No video formats" in err_text or "no media formats" in err_text):
            hint = ("YouTube требует подтверждение (антибот). Попробуй позже — "
                    "если повторяется, администратору нужно обновить cookies.")
        else:
            hint = "Проверь ссылку или попробуй позже."
        if status_msg:
            await status_msg.edit_text(
                f"❌ Не удалось скачать видео.\n{hint}\n\n<code>{escape_html(err_text[:200])}</code>",
                parse_mode="HTML"
            )
        success = False
    except Exception as e:
        logger.error(f"Unexpected error: user={user_id}, video_id={video_id}, error={traceback.format_exc()}")
        if status_msg:
            await status_msg.edit_text(
                f"❌ Ошибка: <code>{escape_html(str(e)[:200])}</code>\n\nПопробуй другую ссылку или повтори позже.",
                parse_mode="HTML"
            )
        success = False
    finally:
        _active_downloads.pop(user_id, None)
        _active_urls.discard(video_id)
        if success and status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
            logger.info(f"Completed OK: user={user_id}, video_id={video_id}")
        else:
            logger.info(f"Completed ERROR: user={user_id}, video_id={video_id}")
        # Process next in queue
        await _process_next_in_queue()


async def _process_next_in_queue():
    """Start processing the next item in the queue if any."""
    global _queue
    if not _queue:
        return
    # Wait a tiny bit for cleanup
    await asyncio.sleep(0.5)
    if _active_downloads:
        return  # still busy (shouldn't happen, but safety check)
    item = _queue.popleft()
    logger.info(f"Processing queue: user={item['user_id']}, video_id={item['video_id']}")
    try:
        await item["status_msg"].edit_text("🎬 Ваша очередь подошла! Начинаю обработку…")
    except Exception:
        pass
    await _start_processing(
        item["update"], item["context"],
        item["url"], item["user_id"], item["video_id"]
    )


# ═══════════════════════════════════════════
# Main
# ═══════════════════════════════════════════

# Last date (YYYY-MM-DD) a cookie reminder was sent — to avoid daily spam
_last_cookie_reminder = None


def cookies_age_days() -> float:
    """Return how many days old the cookies file is (0 if missing)."""
    if not os.path.exists(COOKIES_FILE):
        return 0.0
    return (time.time() - os.path.getmtime(COOKIES_FILE)) / 86400.0


async def check_cookies_job(context: ContextTypes.DEFAULT_TYPE):
    """Daily job: notify admin if cookies file is older than COOKIES_REMIND_DAYS."""
    global _last_cookie_reminder
    if not ADMIN_ID:
        return
    age_days = cookies_age_days()
    if not os.path.exists(COOKIES_FILE):
        logger.warning("Cookie reminder check: cookies file not found")
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if age_days < COOKIES_REMIND_DAYS or _last_cookie_reminder == today:
        return
    _last_cookie_reminder = today
    msg = (
        "⚠️ <b>Пора обновить cookies для YouTube</b>\n\n"
        f"Файл cookies не обновлялся уже <b>{int(age_days)} дней</b> "
        f"(рекомендуется обновлять каждые {COOKIES_REMIND_DAYS} дней).\n\n"
        "Протухшие cookies приводят к ошибкам скачивания "
        "(«Sign in to confirm you're not a bot», «No video formats found»).\n\n"
        "Как обновить (с Mac):\n"
        "1. <code>tools/export_youtube_cookies_macos.sh</code> — экспорт и загрузка cookies\n"
        "2. На сервере: <code>systemctl restart yt-audio-bot</code>\n"
        "Подробнее: <code>COOKIES_MAC.md</code>"
    )
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode="HTML")
        logger.info(f"Cookie reminder sent to admin {ADMIN_ID} (age={int(age_days)} days)")
    except Exception as e:
        logger.error(f"Failed to send cookie reminder to {ADMIN_ID}: {e}")


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Log errors; treat transient Telegram network issues as warnings, not failures."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning(f"Telegram network error (transient): {type(err).__name__}: {err}")
    else:
        logger.error(f"Unhandled exception in handler: {err}", exc_info=err)


def main():
    if not BOT_TOKEN:
        logger.error("❌ YT_AUDIO_BOT_TOKEN environment variable not set!")
        sys.exit(1)

    cleanup_old_files()
    init_db()

    app = Application.builder() \
        .token(BOT_TOKEN) \
        .concurrent_updates(True) \
        .request(HTTPXRequest(read_timeout=300, write_timeout=300, connect_timeout=30, pool_timeout=30)) \
        .build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("rated", cmd_rated))
    app.add_handler(CallbackQueryHandler(handle_rating, pattern=r"^rate:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    # Daily job: remind admin to refresh cookies (once a day at 09:00)
    try:
        app.job_queue.run_daily(check_cookies_job, time=dt_time(hour=9, minute=0))
        logger.info("Scheduled daily cookie reminder job (09:00)")
    except Exception as e:
        logger.warning(f"Could not schedule cookie reminder job: {e}")

    # Also check once shortly after startup (in case bot was down for days)
    try:
        app.job_queue.run_once(check_cookies_job, when=60)
    except Exception as e:
        logger.warning(f"Could not schedule startup cookie check: {e}")

    logger.info("🤖 Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()

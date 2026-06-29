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
import asyncio
import subprocess
import traceback
import urllib.request
import concurrent.futures
from pathlib import Path
from datetime import datetime

import yt_dlp
from PIL import Image
import base64
from mutagen.oggopus import OggOpus
from mutagen.flac import Picture
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ═══════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════

BOT_TOKEN = os.environ.get("YT_AUDIO_BOT_TOKEN", "")
DOWNLOAD_DIR = "/tmp/yt-audio-downloads"
MAX_FILE_SIZE = 45 * 1024 * 1024  # 45 MB safety margin
CLEANUP_AGE = 3600  # 1 hour
# yt-dlp JS runtime path (deno)
os.environ["PATH"] = os.environ.get("PATH", "") + ":/root/.deno/bin"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Active download tracking: {user_id: {"url": str, "video_id": str, "proc": Popen|None, "files": [str], ...}}
_active_downloads = {}
_active_urls = set()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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
            if age > CLEANUP_AGE:
                os.remove(f)
                removed += 1
    if removed:
        logger.info(f"Cleaned up {removed} old file(s)")


async def run_ffmpeg_with_progress(
    cmd: list,
    total_duration: float,
    status_msg,
    title: str,
    user_id: int = None,
    stage: str = "Конвертирую"
) -> int:
    """
    Run ffmpeg with CPU throttling (nice+ionice) in a thread.
    Progress estimated by elapsed time. Stores process ref for cancellation.
    """
    throttled_cmd = [
        "nice", "-n", "19",
        "ionice", "-c", "3",
    ] + cmd

    start_time = time.time()
    last_update = 0
    last_pct = -1

    # Use Popen so we can kill it later via /cancel
    proc = subprocess.Popen(
        throttled_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Store process ref for cancellation
    if user_id and user_id in _active_downloads:
        _active_downloads[user_id]["proc"] = proc

    while True:
        try:
            proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
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
    Returns list of (part_path, part_number, total_parts).
    """
    file_size = os.path.getsize(file_path)
    if file_size <= max_size:
        return [(file_path, 1, 1)]

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

        subprocess.run(split_cmd, capture_output=True, check=True)
        parts.append((part_path, part_num, num_parts))

    # Remove original file
    os.remove(file_path)

    return parts


# ═══════════════════════════════════════════
# Telegram Handlers
# ═══════════════════════════════════════════

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
        "  /cancel — отменить текущую загрузку",
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
        "  /cancel — отменить текущую загрузку\n\n"
        "<b>Формат на выходе:</b> Opus 12kbps, моно, 16 кГц\n"
        "<b>Ограничение:</b> до 50 МБ (с авто-сплитом)\n\n"
        "⚡ Процесс конвертации имеет низкий приоритет\n"
        "и не нагружает сервер.",
        parse_mode="HTML",
        disable_web_page_preview=True
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel current download and clean up files."""
    user_id = update.effective_user.id
    global _active_downloads, _active_urls

    if user_id not in _active_downloads:
        await update.message.reply_text(
            "❌ Нет активных задач для отмены.",
            reply_to_message_id=update.message.message_id
        )
        return

    info = _active_downloads[user_id]
    video_id = info["video_id"]
    url = info["url"]

    # Kill the running process if any
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

    # Clean up tracking
    _active_downloads.pop(user_id, None)
    _active_urls.discard(video_id)

    await update.message.reply_text(
        f"✅ Задача отменена. Временные файлы удалены.",
        reply_to_message_id=update.message.message_id
    )
    logger.info(f"User {user_id} cancelled download of {video_id}")


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

    # Check for concurrent downloads
    global _active_downloads, _active_urls
    if user_id in _active_downloads:
        await update.message.reply_text(
            "⏳ Уже обрабатываю другое видео. Дождись завершения.",
            reply_to_message_id=update.message.message_id
        )
        return
    if video_id in _active_urls:
        await update.message.reply_text(
            "⏳ Это видео уже обрабатывается. Дождись завершения.",
            reply_to_message_id=update.message.message_id
        )
        return

    # Mark as active
    _active_downloads[user_id] = {"url": text, "video_id": video_id, "files": []}
    _active_urls.add(video_id)

    try:
        status_msg = await update.message.reply_text("⏳ Получаю информацию о видео...")

        # ── Step 1: Extract info ──
        ydl_opts_info = {"quiet": True, "no_warnings": True}
        ydl_opts_info["extractor_args"] = {"youtube": {"player_client": ["android"]}}
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            info = ydl.extract_info(text, download=False)
            title = info.get("title", "Unknown")
            uploader = info.get("uploader", "Unknown")
            duration = info.get("duration", 0)
            description = (info.get("description") or "")[:500]
            webpage_url = info.get("webpage_url", text)
            # Get best thumbnail URL
            thumbnails = info.get("thumbnails") or []
            thumbnail_url = ""
            if thumbnails:
                # Pick the highest resolution thumbnail
                thumbnail_url = sorted(thumbnails, key=lambda t: t.get("preference", 0) or t.get("height", 0) or 0, reverse=True)[0].get("url", "")
            if not thumbnail_url:
                thumbnail_url = info.get("thumbnail", "")

        duration_str = format_duration(duration)
        short_desc = description[:200] + ("…" if len(description) > 200 else "")
        safe_title = sanitize(title, 120)

        await status_msg.edit_text(
            f"📥 <b>{escape_html(title)}</b>\n"
            f"👤 {escape_html(uploader)} | ⏱ {duration_str}\n\n"
            f"⬇️ Скачиваю аудио…",
            parse_mode="HTML"
        )

        # ── Step 2: Download audio with yt-dlp ──
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        source_template = os.path.join(
            DOWNLOAD_DIR, f"{timestamp}_{safe_title}_source.%(ext)s"
        )

        ydl_opts = {
            "format": "worst",
            "outtmpl": source_template,
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {"youtube": {"player_client": ["android"]}},
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(text, download=True)
            source_ext = info.get("ext", "webm")
            source_path = os.path.join(
                DOWNLOAD_DIR, f"{timestamp}_{safe_title}_source.{source_ext}"
            )
            if not os.path.exists(source_path):
                candidates = glob.glob(
                    os.path.join(DOWNLOAD_DIR, f"{timestamp}_{safe_title}_source.*")
                )
                source_path = candidates[0] if candidates else None

        if not source_path or not os.path.exists(source_path):
            raise Exception("Source file not found after download")

        # Track files for /cancel cleanup
        _active_downloads[user_id]["files"] = [source_path]

        # ── Step 3: Download thumbnail for cover art ──
        thumb_path = None
        if thumbnail_url:
            try:
                thumb_path = os.path.join(DOWNLOAD_DIR, f"{timestamp}_{safe_title}_thumb.jpg")
                import urllib.request
                urllib.request.urlretrieve(thumbnail_url, thumb_path)
                # Verify it's a valid image
                with Image.open(thumb_path) as img_check:
                    img_check.verify()
                logger.info(f"Thumbnail downloaded: {thumb_path}")
            except Exception as e:
                logger.warning(f"Failed to download thumbnail: {e}")
                if thumb_path and os.path.exists(thumb_path):
                    try:
                        os.remove(thumb_path)
                    except OSError:
                        pass
                thumb_path = None

        # ── Step 4: Convert to ultra-compressed Opus (throttled) ──
        output_filename = f"{timestamp}_{safe_title}.opus"
        output_path = os.path.join(DOWNLOAD_DIR, output_filename)

        # Track files for /cancel
        files = _active_downloads[user_id].get("files", [])
        if thumb_path:
            files.append(thumb_path)
        files.append(output_path)
        _active_downloads[user_id]["files"] = files

        # Build metadata
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

        # Base ffmpeg command (throttling wrappers added by run_ffmpeg_with_progress)
        ffmpeg_cmd = [
            "ffmpeg",
            "-i", source_path,
        ]
        ffmpeg_cmd += [
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "libopus",
            "-b:a", "12k",
            "-application", "voip",
            "-threads", "1",
            "-map_metadata", "-1",
            "-progress", "pipe:1",
        ] + metadata_args + ["-y", output_path]

        result_code = await run_ffmpeg_with_progress(
            ffmpeg_cmd, duration, status_msg, title, user_id=user_id
        )

        if result_code != 0:
            # Check if cancelled by user
            if user_id not in _active_downloads:
                raise Exception("Download cancelled by user")

            # Fallback: try without progress pipe
            fallback_cmd = [
                "nice", "-n", "19",
                "ionice", "-c", "3",
            ] + [a for a in ffmpeg_cmd if a != "-progress" and a != "pipe:1"]
            fallback_result = subprocess.run(
                fallback_cmd, capture_output=True, text=True
            )
            if fallback_result.returncode != 0:
                # Last resort: no throttling at all
                bare_cmd = [a for a in ffmpeg_cmd if a != "-progress" and a != "pipe:1"]
                bare_result = subprocess.run(bare_cmd, capture_output=True, text=True)
                if bare_result.returncode != 0:
                    raise Exception(f"ffmpeg failed: {bare_result.stderr[:300]}")
            result_code = 0

        # Embed cover art via mutagen (ffmpeg's opus muxer doesn't support attached pics)
        if thumb_path and os.path.exists(output_path):
            try:
                audio = OggOpus(output_path)
                pic = Picture()
                pic.type = 3  # Front cover
                pic.mime = "image/jpeg"
                with open(thumb_path, "rb") as f:
                    pic.data = f.read()
                pic.width = 1280
                pic.height = 720
                pic.depth = 8
                pic.colors = 0
                # Encode as base64 per Vorbis comment spec
                pic_data = pic.write()
                encoded = base64.b64encode(pic_data).decode("ascii")
                audio["metadata_block_picture"] = [encoded]
                audio.save()
                logger.info("Cover art embedded successfully")
            except Exception as e:
                logger.warning(f"Failed to embed cover art: {e}")

        # Clean up source and thumbnail
        try:
            os.remove(source_path)
        except OSError:
            pass
        if thumb_path:
            try:
                os.remove(thumb_path)
            except OSError:
                pass

        if not os.path.exists(output_path):
            raise Exception("Output file not found after conversion")

        # ── Step 4: Check size, split if needed ──
        file_size = os.path.getsize(output_path)

        if file_size > MAX_FILE_SIZE:
            await status_msg.edit_text(
                f"📥 <b>{escape_html(title)}</b>\n\n"
                f"✂️ Файл {format_size(file_size)} — разбиваю на части…",
                parse_mode="HTML"
            )

            parts = split_audio_file(
                output_path, MAX_FILE_SIZE,
                f"{timestamp}_{safe_title}"
            )
        else:
            parts = [(output_path, 1, 1)]

        # ── Step 5: Send file(s) ──
        total_parts = parts[0][2]
        total_str = format_duration(duration)

        for part_path, part_num, total in parts:
            part_size = os.path.getsize(part_path)

            # Build caption
            caption_parts = [
                f"<b>{escape_html(title)}</b>",
                f"👤 {escape_html(uploader)}",
                f"⏱ {duration_str}",
            ]
            if total > 1:
                caption_parts.append(f"📦 Часть {part_num} из {total} — {format_size(part_size)}")
            else:
                caption_parts.append(f"📦 {format_size(part_size)} | Opus 12kbps")

            if short_desc and part_num == 1:
                caption_parts.append(f"\n{escape_html(short_desc)}")
                caption_parts.append(f"\n🔗 {webpage_url}")

            caption = "\n".join(caption_parts)

            # Update status before sending
            if total > 1:
                await status_msg.edit_text(
                    f"📤 <b>{escape_html(title)}</b>\n"
                    f"⬆️ Отправляю часть {part_num} из {total}…",
                    parse_mode="HTML"
                )
            else:
                await status_msg.edit_text(
                    f"📤 <b>{escape_html(title)}</b>\n"
                    f"📦 {format_size(part_size)} | ⏱ {duration_str}\n\n"
                    f"⬆️ Отправляю…",
                    parse_mode="HTML"
                )

            with open(part_path, "rb") as f:
                audio_title = f"{title[:240]} (ч.{part_num}/{total})" if total > 1 else title[:256]
                await update.message.reply_audio(
                    audio=f,
                    title=audio_title,
                    performer=uploader[:256],
                    duration=duration,
                    caption=caption,
                    parse_mode="HTML",
                    reply_to_message_id=update.message.message_id
                )

            # Remove part after sending
            try:
                os.remove(part_path)
            except OSError:
                pass

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp error: {e}")
        await status_msg.edit_text(
            f"❌ Не удалось скачать видео.\n"
            f"Проверь ссылку или попробуй позже.\n\n"
            f"<code>{escape_html(str(e)[:200])}</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Unexpected error: {traceback.format_exc()}")
        await status_msg.edit_text(
            f"❌ Ошибка: <code>{escape_html(str(e)[:200])}</code>\n\n"
            f"Попробуй другую ссылку или повтори позже.",
            parse_mode="HTML"
        )
    finally:
        # Clean up active download tracking
        _active_downloads.pop(user_id, None)
        _active_urls.discard(video_id)
        await status_msg.delete()


# ═══════════════════════════════════════════
# Main
# ═══════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        logger.error("❌ YT_AUDIO_BOT_TOKEN environment variable not set!")
        sys.exit(1)

    cleanup_old_files()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()

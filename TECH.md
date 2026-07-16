# 🛠 Technical Overview — YouTube → Audio Bot

## Architecture

The bot uses **python-telegram-bot v20+** with **asyncio**. All blocking I/O operations are offloaded to thread pool executors or use async subprocess to keep the event loop responsive.

```
Telegram API ← Polling → Application (concurrent_updates=True)
                                │
                    ┌───────────┴───────────┐
                    │   handle_message()    │
                    │   (per-update task)   │
                    └───────────┬───────────┘
                                │
                    ┌───────────┴───────────┐
                    │   _start_processing() │
                    │   (async pipeline)    │
                    └───────────────────────┘
```

## Key Design Decisions

### 1. Concurrent Updates (Anti-blocking)
`Application.builder().concurrent_updates(True)` — PTB by default processes updates **sequentially**. Without this flag, while one user's download is processing, all other commands (/cancel, new links from other users) would be queued by PTB itself and never reach our handler until the first task completes. With `concurrent_updates=True`, each update runs in its own asyncio task.

### 2. Async ffmpeg (non-blocking subprocess)
`asyncio.create_subprocess_exec()` instead of `subprocess.Popen()`. The synchronous `proc.wait(timeout=5)` blocked the event loop for 5 seconds every iteration — during which no other updates could be processed. Async subprocess releases the loop between checks.

### 3. Queue System (max 5)
When a user sends a link while another download is active:
- The request is added to `_queue` (max 5 items, anti-DDoS)
- User gets "Бот занят. Вы в очереди — ожидайте"
- When processing finishes, `_process_next_in_queue()` dequeues the next
- If queue is full (≥5): "Очередь переполнена. Попробуй позже."
- `/cancel` removes user from queue or kills active process
- Users in queue sending another link: "Дождитесь своей очереди"

### 4. Stage Tracking
`_active_downloads[user_id]["stage"]` tracks the current phase:
```
starting → extracting info → downloading → converting → splitting → sending → done
```
Used for status messages and queue awareness.

### 5. Disk Space Checks
Before any download:
1. **Minimum free space**: If < 200 MB free → error immediately
2. **File size check**: After info extraction, if source file size > free space - 200 MB → error
3. Error message tells user exact MB required

### 6. Progress Indicators
- **Download**: yt-dlp `progress_hooks` + `run_in_executor` + polling loop (ASCII bar, speed, ETA)
- **Conversion**: Timer-based estimate at ~10× realtime, updates every 5s

### 7. File Cleanup
- Files deleted immediately after sending via Telegram
- Cron: `.part` files (orphaned downloads) → cleaned after 15 min
- Cron: completed files (`.mp4`, `.opus`, `.jpg`) → cleaned after 60 min
- At bot startup and each new request: orphaned `.part` files removed

### 8. Cancel System
`/cancel` uses:
- `threading.Event()` flag checked during yt-dlp download (executor)
- `proc.kill()` on async ffmpeg subprocess
- Removes user from queue (if queued) or kills active task + cleans files
- After cancel, starts next item in queue

## Non-blocking Wrappers

| Operation | Method | Why |
|---|---|---|
| yt-dlp info extract | `run_in_executor` | yt-dlp is synchronous |
| yt-dlp download | `run_in_executor` | yt-dlp is synchronous |
| ffmpeg conversion | `asyncio.create_subprocess_exec` | Async IO, non-blocking wait |
| Thumbnail download | `run_in_executor` | urllib is synchronous |
| Thumbnail verify (PIL) | `run_in_executor` | PIL I/O is synchronous |
| ffprobe probe | `run_in_executor` | subprocess.run is blocking |
| split_audio_file | `run_in_executor` | subprocess.run is blocking |
| ffmpeg fallback | `run_in_executor` | subprocess.run is blocking |

## YouTube Client Strategy
- **Primary**: `player_client=android` — bypasses JS n-challenge, works without cookies
- **Limitation**: Android client only returns combined mp4 (format 18), no audio-only formats
- **Result**: yt-dlp downloads `worst` format, ffmpeg extracts audio and discards video

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `YT_AUDIO_BOT_TOKEN` | — | **Required.** Telegram Bot Token |
| `DOWNLOAD_DIR` | `/tmp/yt-audio-downloads` | Temp directory |
| `MAX_FILE_SIZE` | 45 MB | Split threshold |
| `CLEANUP_AGE` | 3600 (1h) | Max file age before cleanup |
| `LOG_DIR` | `/var/log/yt-audio-bot` | Log directory |
| `LOG_MAX_SIZE` | 1 MB | Rotating log file size |
| `LOG_BACKUP_COUNT` | 3 | Number of old log files |

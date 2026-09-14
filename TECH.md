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
The bot uses **CLI `yt-dlp`** (not the Python API) because the Python API can miss formats that the CLI sees — this was the root cause of "No video formats found" errors.

- **Primary**: `player_client=web` with cookies (`cookiefile`) — full format list
- **Fallback**: if web client returns no media formats (e.g. storyboard-only), automatically retries with `player_client=android`
- **Android client** returns combined mp4 (format 18), bypasses some n-challenge cases
- **Result**: downloads `worstaudio/worst` format, ffmpeg extracts audio and discards video
- Both functions (`_do_extract_info`, `_do_download`) collect per-client errors so failures show the real reason instead of "Unknown error"

## Cookie Authentication
- YouTube increasingly blocks anonymous access with "Sign in to confirm you're not a bot"
- Cookies are loaded from `YT_AUDIO_COOKIES` (default `/opt/yt-audio-bot/cookies.txt`)
- Export from Chrome: `python3 -m yt_dlp --cookies-from-browser chrome -o /dev/null --cookies /tmp/yt_cookies.txt https://youtu.be/dQw4w9WgXcQ`
- Upload to server: `scp /tmp/yt_cookies.txt root@<server>:/opt/yt-audio-bot/cookies.txt` then `systemctl restart yt-audio-bot`
- **Admin reminder**: the daily job (09:00) + startup check notify the admin when cookies are older than `YT_AUDIO_COOKIES_REMIND_DAYS` (default 14)

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
| `YT_AUDIO_ADMIN_ID` | — | Telegram user ID of bot admin. Admin is **always** treated as VIP |
| `YT_AUDIO_VIP_USERS` | — | Comma-separated user IDs with queue priority |
| `YT_AUDIO_COOKIES` | `/opt/yt-audio-bot/cookies.txt` | Path to YouTube cookies file |
| `YTDLP_PATH` | `/opt/yt-audio-bot/venv/bin/yt-dlp` | Path to yt-dlp binary |
| `YT_AUDIO_COOKIES_REMIND_DAYS` | 14 | After N days without refresh, admin gets a cookie-refresh reminder |
| `YT_AUDIO_DB` | `/opt/yt-audio-bot/bot.db` | SQLite database (file_id cache + usage stats) |
| `YT_AUDIO_CLIENTS` | `android,ios,tv,web,mweb` | YouTube player clients to try, in order |
| `YT_AUDIO_POT_ENABLED` | `1` | Use the bgutil PO token provider (`0` disables) |
| `YT_AUDIO_POT_URL` | `http://127.0.0.1:4416` | bgutil PO token provider base URL |
| `YT_AUDIO_JS_RUNTIME` | `node:/usr/local/bin/node` | JS runtime passed to yt-dlp via `--js-runtimes` |
| `YT_AUDIO_DOWNLOAD_TIMEOUT` | 1800 | Per-client download timeout (seconds) |
| `YT_AUDIO_EXTRACT_TIMEOUT` | 300 | Per-client info-extraction timeout (seconds) |
| `YT_AUDIO_ROUNDS` | 4 | Full retry rounds over the client list on anti-bot blocks |
| `YT_AUDIO_ROUND_DELAY` | 15 | Delay between retry rounds (seconds) |

### 11. YouTube Anti-Bot Resilience (PO Token + retries)
YouTube increasingly blocks datacenter IPs with *"Sign in to confirm you're not a bot"*.
The bot mitigates this with:
- **PO token provider** — [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
  HTTP server on `127.0.0.1:4416` (systemd `bgutil-pot-provider.service`), plus the
  `bgutil-ytdlp-pot-provider` yt-dlp plugin installed in the bot venv.
- **JS runtime** — Node is passed via `--js-runtimes` so yt-dlp can solve n-sig challenges.
- **Client rotation** — multiple player clients, mobile clients first.
- **Retry rounds** — if all clients fail with a retryable error, the whole list is retried
  after `YT_AUDIO_ROUND_DELAY` seconds, up to `YT_AUDIO_ROUNDS` times.
- **Cookies** — fresh YouTube cookies still matter; see `COOKIES_MAC.md` for exporting them
  from a Mac browser via `tools/export_youtube_cookies_macos.sh`.

> PO tokens improve but do **not** guarantee bypassing bot checks. If blocks persist,
> route yt-dlp through a residential/mobile proxy (`--proxy`).

### 9. Cookie Refresh Reminder
- **Admin** (`YT_AUDIO_ADMIN_ID`) is notified via Telegram when the cookies file is older than `YT_AUDIO_COOKIES_REMIND_DAYS` (default 14 days)
- Checked by a daily job (09:00) + once at bot startup
- Reminder is sent at most once per day to avoid spam
- The reminder includes the exact commands to re-export cookies from Chrome and upload them

### 10. Cloud Cache (file_id) + Usage Stats (SQLite)
When the bot sends an audio, Telegram stores it in its cloud and returns a stable **`file_id`**. The bot saves it to a local SQLite database (`YT_AUDIO_DB`, default `/opt/yt-audio-bot/bot.db`):

**`cached_files`** table — `video_id → file_id` (plus title, uploader, duration, parts):
- On a new request, the bot first checks this table
- If found → serves the audio **instantly** via `file_id` (no YouTube download, no ffmpeg, no CPU usage)
- Split files (parts) are cached as a JSON array of `file_id`s
- If a `file_id` goes stale (Telegram may invalidate after long inactivity) → bot drops the cache entry and does a fresh download

**`downloads`** table — usage statistics:
- Every request logs `user_id`, `video_id`, `title`, `cached` (0/1), timestamp
- `/stats` command (admin only) shows: total downloads, cache hit rate, unique users/videos, top-10 videos

**Benefits:** repeated requests for popular videos cost zero server resources (no re-download/re-encode); the audio lives in Telegram's cloud.

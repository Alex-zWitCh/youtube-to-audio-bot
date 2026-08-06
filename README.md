# 🎧 YouTube → Audio Bot

A Telegram bot that converts YouTube videos to ultra-compressed Opus audio (12 kbps, mono, 16 kHz). Perfect for audiobooks, lectures, podcasts, and interviews.

## Features

- 🔹 **Format:** Opus 12 kbps, mono, 16 kHz (speech-optimized)
- 🔹 **Cover art:** YouTube thumbnail embedded as album artwork
- 🔹 **Metadata:** title, author, description, source link embedded in file
- 🔹 **Auto-split:** files larger than 45 MB are split into numbered parts (`part 1 of 4`, etc.)
- 🔹 **Low CPU impact:** `nice -n 19` + `ionice -c 3` + single thread — minimal server load
- 🔹 **Progress indicator:** shows download speed + encoding progress with ETA
- 🔹 **Queue system:** max 5 waiting users — anti-DDoS protection
- 🔹 **Concurrent updates:** /cancel and new links work while processing
- 🔹 **Disk guard:** checks free space before each download
- 🔹 **Async architecture:** all blocking I/O in executors, non-blocking ffmpeg
- 🔹 **Public bot:** no registration required, anyone can send a link
- 🔹 **File cleanup:** files deleted after sending; cron cleans orphans
- 🔹 **Cloud cache:** audio `file_id` saved to SQLite — repeated requests for the same video are served instantly from Telegram cloud (no re-download/re-convert)
- 🔹 **Usage stats:** `/stats` (admin) shows downloads, cache hit rate, top videos


## How It Works

```
User sends YouTube link
  ├─ Cache hit? → serve instantly from Telegram cloud (file_id) ✅
  └─ Cache miss → Bot extracts info (web client + cookies)
       → Downloads audio (falls back to android client if needed)
       → ffmpeg converts to Opus 12 kbps mono 16 kHz (nice + ionice)
       → mutagen embeds cover art and metadata
       → If >45 MB: splits into parts
       → Sends file(s) via Telegram → saves file_id to cache → Deletes from server
```

> **Cookies:** YouTube increasingly requires authentication. Export cookies from your browser and place them in `cookies.txt` (see `YT_AUDIO_COOKIES`). The admin gets a Telegram reminder every 14 days to refresh them.

## Quick Start (Self-Hosting)

### Prerequisites

- Linux server (Ubuntu 24.04 LTS recommended)
- Python 3.12+
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))

### Quick Install

```bash
curl -sSL https://raw.githubusercontent.com/Alex-zWitCh/youtube-to-audio-bot/main/install.sh | sudo bash
```

Or with a token (no prompt):
```bash
curl -sSL https://raw.githubusercontent.com/Alex-zWitCh/youtube-to-audio-bot/main/install.sh | sudo bash -s -- YOUR_BOT_TOKEN
```

The script installs everything automatically: ffmpeg, deno, Python packages, systemd service, cleanup cron.

### Update

```bash
systemctl stop yt-audio-bot
curl -o /opt/yt-audio-bot/bot.py https://raw.githubusercontent.com/Alex-zWitCh/youtube-to-audio-bot/main/bot.py
systemctl start yt-audio-bot
```

## Bot Configuration

All configuration is via environment variables (set in the systemd service file):

| Variable | Default | Description |
|---|---|---|
| `YT_AUDIO_BOT_TOKEN` | — | **Required.** Telegram Bot Token |
| `DOWNLOAD_DIR` | `/tmp/yt-audio-downloads` | Temp directory for downloads |
| `MAX_FILE_SIZE` | `45 MB` | Split threshold |
| `CLEANUP_AGE` | `3600` (1 hour) | Max file age before cleanup |
| `YT_AUDIO_ADMIN_ID` | — | Telegram user ID of the bot admin (always VIP) |
| `YT_AUDIO_VIP_USERS` | — | Comma-separated Telegram user IDs with queue priority |
| `YT_AUDIO_COOKIES` | `/opt/yt-audio-bot/cookies.txt` | Path to YouTube cookies file |
| `YTDLP_PATH` | `/opt/yt-audio-bot/venv/bin/yt-dlp` | Path to the yt-dlp binary |
| `YT_AUDIO_COOKIES_REMIND_DAYS` | `14` | Admin gets a cookie-refresh reminder after this many days |
| `YT_AUDIO_DB` | `/opt/yt-audio-bot/bot.db` | Path to the SQLite database (cache + usage stats) |

## BotFather Setup

After starting the bot, configure it via [@BotFather](https://t.me/BotFather):

1. `/newbot` — create a new bot, get your token
2. Edit `yt-audio-bot.service` and set your token in `YT_AUDIO_BOT_TOKEN`
3. Start the bot: `systemctl start yt-audio-bot`
4. Configure in BotFather:

```
/setdescription  →  paste description text
/setabouttext    →  🎧 YouTube → Audio Bot — ultra-compressed Opus 12 kbps
/setcommands     →  start — Start the bot\nhelp — Show help\ncancel — Cancel current task\nstats — Usage statistics (admin)
/setuserpic      →  upload bot_icon.png
```

## Project Structure

```
youtube-to-audio-bot/
├── bot.py                  # Main bot script
├── bot_icon.png            # Bot icon (512×512)
├── install.sh              # Automated installer
├── yt-audio-bot.service    # systemd service file
├── yt-audio-cleanup        # Cron cleanup config
├── README.md               # This file
└── LICENSE
```

## Technical Details

### Audio Encoding Pipeline

1. **yt-dlp** (CLI, `web` client + cookies, falls back to `android`) downloads audio (`worstaudio/worst` format)
2. **ffmpeg** converts with:
   - Codec: libopus (speech-optimized `-application voip`)
   - Bitrate: 12 kbps
   - Channels: mono (`-ac 1`)
   - Sample rate: 16 kHz (`-ar 16000`)
   - Threads: 1
3. **mutagen** embeds YouTube thumbnail as cover art
4. If output > 45 MB: **ffmpeg** splits by duration using stream copy
5. Telegram stores the audio in its cloud; the returned **`file_id`** is saved to SQLite for instant reuse

### CPU Throttling

The encoding process uses Linux priority scheduling to minimize server impact:
```
nice -n 19 → ionice -c 3 → ffmpeg -threads 1
```

> **Note:** `nice -n 19` ensures the lowest scheduling priority, `ionice -c 3` sets idle I/O priority, and `ffmpeg -threads 1` limits to a single thread. Together, encoding won't interfere with other services even on low-end VPS.

### File Splitting

When the compressed audio exceeds 45 MB:
1. Total duration is calculated via `ffprobe`
2. Number of parts = `ceil(file_size / 45 MB)`
3. Each part is extracted using `ffmpeg -ss` / `-to` with stream copy
4. Files are sent as `Title_part1_of_4.opus`, `Title_part2_of_4.opus`, etc.

## Technical Notes

### Async Architecture
The bot uses `python-telegram-bot` with `concurrent_updates=True`. **PTB processes updates sequentially by default** — without this flag, while one user's download is processing, all other commands (/cancel, links from other users) would be queued by PTB itself and never reach the handler. With concurrent updates, each command runs in its own asyncio task.

### Non-blocking Operations
All blocking I/O runs in thread pool executors or uses async subprocess:
- **ffmpeg**: `asyncio.create_subprocess_exec` — event loop stays responsive
- **yt-dlp**: `run_in_executor` — download in background thread
- **ffprobe**: `run_in_executor`
- **Thumbnail**: `run_in_executor` (urllib + PIL)

This ensures `/cancel`, queue, and messages from other users are processed instantly during conversion.

### Queue & Anti-DDoS
- Max **5 queued** requests — additional users get "Queue full, try later"
- **VIP users** (`YT_AUDIO_VIP_USERS`): always enter queue, kick the last regular user if full
- Same user sending multiple links while queued: "Wait your turn"
- `/cancel` removes from queue or kills active process
- After completion, next queue item starts automatically

### Disk Space Guard
Before each download:
1. Minimum **200 MB** free required to start processing
2. Source file size must fit within free space minus 200 MB margin
3. If either check fails: user gets clear error with exact MB requirements

### File Cleanup Schedule
| File type | Age | Cron interval |
|---|---|---|
| `.part` (orphaned) | > 15 min | Every 5 min |
| `.mp4`, `.opus`, `.jpg` (completed) | > 60 min | Every 5 min |

## License

MIT

---

## 🇷🇺 Русская документация

# 🎧 YouTube → Audio Bot

Telegram-бот, который конвертирует YouTube-видео в максимально сжатый Opus-аудиофайл (12 kbps, моно, 16 кГц). Идеально подходит для аудиокниг, лекций, подкастов и интервью.

### Возможности

- 🔹 **Формат:** Opus 12 kbps, моно, 16 кГц (оптимизирован для речи)
- 🔹 **Обложка:** превью с YouTube встраивается как обложка альбома
- 🔹 **Метаданные:** название, автор, описание, ссылка на оригинал в файле
- 🔹 **Авто-сплит:** файлы больше 45 МБ разбиваются на части (`часть 1 из 4` и т.д.)
- 🔹 **Низкая нагрузка:** `nice -n 19` + `ionice -c 3` + один поток (минимальная нагрузка на сервер)
- 🔹 **Индикатор прогресса:** скорость скачивания + прогресс конвертации с ETA
- 🔹 **Публичный бот:** регистрация не требуется
- 🔹 **Очистка:** файлы удаляются сразу после отправки; cron чистит остатки каждый час
- 🔹 **Облачный кэш:** `file_id` сохраняется в SQLite — повторные запросы того же видео отдаются мгновенно из облака Telegram (без скачивания и конвертации)
- 🔹 **Статистика:** `/stats` (для админа) — загрузки, % кэш-попаданий, топ видео
- 🔹 **Напоминание о cookies:** админ получает уведомление раз в 14 дней, когда пора обновить куки

### Как это работает

```
Пользователь отправляет ссылку
  ├─ Есть в кэше? → мгновенно отдаёт из облака Telegram (file_id) ✅
  └─ Нет в кэше → Бот получает инфо (web client + cookies)
       → Скачивает аудио (fallback на android при необходимости)
       → ffmpeg конвертирует в Opus 12 kbps моно 16 кГц (nice + ionice)
       → mutagen встраивает обложку и метаданные
       → Если >45 МБ: разбивает на части
       → Отправляет файл(ы) → сохраняет file_id в кэш → Удаляет с сервера
```

> **Cookies:** YouTube требует авторизации. Экспортируйте куки из браузера в `cookies.txt` (см. `YT_AUDIO_COOKIES`). Админ получает напоминание каждые 14 дней о необходимости обновления.

### Быстрая установка

```bash
curl -sSL https://raw.githubusercontent.com/Alex-zWitCh/youtube-to-audio-bot/main/install.sh | sudo bash
```

Или с токеном (без запроса):
```bash
curl -sSL https://raw.githubusercontent.com/Alex-zWitCh/youtube-to-audio-bot/main/install.sh | sudo bash -s -- YOUR_BOT_TOKEN
```

Скрипт сам установит всё необходимое: ffmpeg, deno, Python-зависимости, создаст systemd сервис, настроит cron очистки.

### Настройка в BotFather

После запуска бота, настрой его через [@BotFather](https://t.me/BotFather):

```
/setdescription  →  вставить описание (русское или английское)
/setabouttext    →  🎧 YouTube → Audio Bot — максимально сжатое аудио
/setcommands     →  start — Запустить бота\nhelp — Помощь
/setuserpic      →  загрузить bot_icon.png
```

### Конфигурация

| Переменная | По умолчанию | Описание |
|---|---|---|
| `YT_AUDIO_BOT_TOKEN` | — | **Обязательно.** Токен бота Telegram |
| `DOWNLOAD_DIR` | `/tmp/yt-audio-downloads` | Временная папка для загрузок |
| `MAX_FILE_SIZE` | `45 MB` | Порог для сплита |
| `CLEANUP_AGE` | `3600` (1 час) | Макс. возраст файла перед очисткой |
| `YT_AUDIO_VIP_USERS` | — | ID пользователей Telegram через запятую (приоритет очереди) |

### Детали

**Аудио-кодек:** Opus через libopus, 12 kbps, моно (`-ac 1`), 16 kHz (`-ar 16000`), оптимизация речи (`-application voip`).

**Прогресс скачивания:** yt-dlp через progress_hooks + asyncio executor, обновление каждые 5 сек с ASCII-прогрессбаром.

**Длительность при сплите:** каждая часть получает свою реальную длительность через ffprobe, а не общую.

**CPU Throttling:** `nice -n 19 → ionice -c 3 → ffmpeg -threads 1` — минимальный приоритет планировщика и idle I/O, конвертация не мешает другим сервисам.

**Сплит файлов:** `ceil(размер / 45 MB)` частей, каждая вырезается через `ffmpeg -ss`/`-to` с stream copy. Имена: `Название_часть1_из_4.opus`, `Название_часть2_из_4.opus`...

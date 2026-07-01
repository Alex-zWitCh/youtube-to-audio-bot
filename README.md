# 🎧 YouTube → Audio Bot

A Telegram bot that converts YouTube videos to ultra-compressed Opus audio (12 kbps, mono, 16 kHz). Perfect for audiobooks, lectures, podcasts, and interviews.

## Features

- 🔹 **Format:** Opus 12 kbps, mono, 16 kHz (speech-optimized)
- 🔹 **Cover art:** YouTube thumbnail embedded as album artwork
- 🔹 **Metadata:** title, author, description, source link embedded in file
- 🔹 **Auto-split:** files larger than 45 MB are split into numbered parts (`part 1 of 4`, etc.)
- 🔹 **Low CPU impact:** `nice -n 19` + `ionice -c 3` + single thread (минимальная нагрузка на сервер)
- 🔹 **Progress indicator:** shows download speed + encoding progress with ETA
- 🔹 **Public bot:** no registration required, anyone can send a link
- 🔹 **File cleanup:** files are deleted immediately after sending; cron cleans orphans every hour

## How It Works

```
User sends YouTube link → Bot extracts info → Downloads audio (android client)
  → ffmpeg converts to Opus 12 kbps mono 16 kHz (nice + ionice)
  → mutagen embeds cover art and metadata
  → If >45 MB: splits into parts
  → Sends file(s) via Telegram → Deletes from server
```

## Quick Start (Self-Hosting)

### Prerequisites

- Linux server (Ubuntu 24.04 LTS recommended)
- Python 3.12+
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))

### Quick Install

```bash
curl -sSL https://raw.githubusercontent.com/Alex-zWitCh/youtube-to-audio-bot/main/install.sh | sudo bash
```

Или с токеном (не будет запроса):
```bash
curl -sSL https://raw.githubusercontent.com/Alex-zWitCh/youtube-to-audio-bot/main/install.sh | sudo bash -s -- YOUR_BOT_TOKEN
```

Скрипт сам установит всё необходимое: ffmpeg, deno, Python-зависимости, создаст systemd сервис, настроит cron очистки.

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

## BotFather Setup

After starting the bot, configure it via [@BotFather](https://t.me/BotFather):

1. `/newbot` — create a new bot, get your token
2. Edit `yt-audio-bot.service` and set your token in `YT_AUDIO_BOT_TOKEN`
3. Start the bot: `systemctl start yt-audio-bot`
4. Configure in BotFather:

```
/setdescription  →  paste description text
/setabouttext    →  🎧 YouTube → Audio Bot — ultra-compressed Opus 12 kbps
/setcommands     →  start — Start the bot\nhelp — Show help
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

1. **yt-dlp** (android client) downloads combined mp4 (`worst` format, no cookies needed)
2. **ffmpeg** converts with:
   - Codec: libopus (speech-optimized `-application voip`)
   - Bitrate: 12 kbps
   - Channels: mono (`-ac 1`)
   - Sample rate: 16 kHz (`-ar 16000`)
   - Threads: 1
3. **mutagen** embeds YouTube thumbnail as cover art
4. If output > 45 MB: **ffmpeg** splits by duration using stream copy

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

### Как это работает

```
Пользователь отправляет ссылку → Бот получает инфо → Скачивает аудио (android)
  → ffmpeg конвертирует в Opus 12 kbps моно 16 кГц (nice + ionice)
  → mutagen встраивает обложку и метаданные
  → Если >45 МБ: разбивает на части
  → Отправляет файл(ы) через Telegram → Удаляет с сервера
```

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

### Детали

**Аудио-кодек:** Opus через libopus, 12 kbps, моно (`-ac 1`), 16 kHz (`-ar 16000`), оптимизация речи (`-application voip`).

**Прогресс скачивания:** yt-dlp через progress_hooks + asyncio executor, обновление каждые 5 сек с ASCII-прогрессбаром.

**Длительность при сплите:** каждая часть получает свою реальную длительность через ffprobe, а не общую.

**CPU Throttling:** `nice -n 19 → ionice -c 3 → ffmpeg -threads 1` — минимальный приоритет планировщика и idle I/O, конвертация не мешает другим сервисам.

**Сплит файлов:** `ceil(размер / 45 MB)` частей, каждая вырезается через `ffmpeg -ss`/`-to` с stream copy. Имена: `Название_часть1_из_4.opus`, `Название_часть2_из_4.opus`...

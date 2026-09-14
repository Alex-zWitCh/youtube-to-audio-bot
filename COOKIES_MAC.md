# Обновление cookies для YouTube с MacBook

Бот работает на VPS `rom.zwitch.ru`, но вход в YouTube выполняется только с вашего
Mac. Ниже — как экспортировать cookies из браузера на Mac и установить их на сервер.

> Cookies — это секрет, равный доступу к вашему аккаунту. Не коммитьте их в Git
> (файл `cookies.txt` уже в `.gitignore`) и не пересылайте в открытом виде.

## Предпосылки

- На Mac вы **залогинены в YouTube** в одном из браузеров
  (Chrome, Safari, Firefox, Brave, Edge и т.п.).
- Есть доступ к серверу по SSH (`root@rom.zwitch.ru`).
- Установлен Python 3 (`python3 --version`). Скрипт сам поставит `yt-dlp`, если его нет.

## Способ 1: скрипт (рекомендуется)

Скопируйте репозиторий или только скрипт на Mac, затем:

```bash
chmod +x tools/export_youtube_cookies_macos.sh

# Экспорт из Chrome и загрузка на сервер (перезапустит бота):
./tools/export_youtube_cookies_macos.sh --browser chrome --upload
```

Скрипт:
1. берёт cookies из браузера через `yt-dlp --cookies-from-browser`;
2. оставляет только домены YouTube/Google;
3. проверяет наличие auth-cookies (`SAPISID`, `__Secure-1PSID`, `SID` и т.д.);
4. при `--upload` копирует файл на сервер, ставит права `400` и перезапускает сервис.

Полезные варианты:

```bash
./tools/export_youtube_cookies_macos.sh --browser safari
./tools/export_youtube_cookies_macos.sh --browser firefox --upload
./tools/export_youtube_cookies_macos.sh --host root@rom.zwitch.ru --upload
```

Результат на Mac: `~/yt_cookies.youtube.txt`.

### Если macOS просит разрешения
- **Chrome/Brave/Edge:** выдайте Терминалу «Полный доступ к диску»
  (System Settings → Privacy & Security → Full Disk Access), затем перезапустите Терминал.
- **Keychain:** при первом чтении Chrome может запросить пароль от связки ключей —
  разрешите.
- **Safari:** может потребоваться закрыть Safari перед экспортом.

## Способ 2: вручную

```bash
# 1. Экспорт из браузера
yt-dlp --cookies-from-browser chrome --cookies ~/yt_cookies.raw.txt \
  --skip-download "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# 2. Оставить только YouTube/Google
{ echo "# Netscape HTTP Cookie File"; \
  awk -F'\t' 'NF>=7 && ($1 ~ /youtube|google|googlevideo|ytimg/) {print}' \
  ~/yt_cookies.raw.txt; } > ~/yt_cookies.youtube.txt

# 3. Загрузить и установить на сервер
scp ~/yt_cookies.youtube.txt root@rom.zwitch.ru:/tmp/yt_cookies.upload
ssh root@rom.zwitch.ru \
  "install -m 400 /tmp/yt_cookies.upload /opt/yt-audio-bot/cookies.txt && \
   rm -f /tmp/yt_cookies.upload && systemctl restart yt-audio-bot"
```

## Проверка

На сервере:

```bash
systemctl is-active yt-audio-bot
/opt/yt-audio-bot/venv/bin/yt-dlp --simulate --no-warnings \
  --extractor-args "youtube:player_client=tv" \
  --cookies /opt/yt-audio-bot/cookies.txt \
  "https://youtu.be/dQw4w9WgXcQ"
```

Ожидаемо: строка `[info] ... Downloading 1 format(s): ...` без
`Sign in to confirm you're not a bot`.

Затем отправьте боту любую ссылку и проверьте, что аудио приходит с кнопками оценок.

## Частота обновления

YouTube периодически инвалидирует сессию. Обновляйте cookies:
- при появлении ошибок `Sign in to confirm you're not a bot` / `No video formats found`;
- профилактически раз в ~14 дней (бот напомнит администратору).

## Связанные файлы

- `tools/export_youtube_cookies_macos.sh` — скрипт экспорта/загрузки.
- `bot.py` — переменные `YT_AUDIO_COOKIES`, `YT_AUDIO_CLIENTS`, `YT_AUDIO_POT_URL`.
- `TECH.md` — раздел Cookie Authentication.

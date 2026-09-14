#!/usr/bin/env bash
#
# Export YouTube/Google cookies from a macOS browser and (optionally) upload
# them to the bot server.
#
# Run this ON YOUR MAC (where you are logged into YouTube).
#
# Usage:
#   ./export_youtube_cookies_macos.sh [--browser chrome] [--upload] [--host root@rom.zwitch.ru]
#
# Examples:
#   ./export_youtube_cookies_macos.sh                       # export only -> ~/yt_cookies.youtube.txt
#   ./export_youtube_cookies_macos.sh --browser safari      # export from Safari
#   ./export_youtube_cookies_macos.sh --upload              # export and upload to the server
#
# Supported browsers: chrome, brave, chromium, edge, firefox, opera, safari, vivaldi, whale
#
set -euo pipefail

BROWSER="chrome"
UPLOAD=0
SERVER_HOST="root@rom.zwitch.ru"
REMOTE_COOKIES="/opt/yt-audio-bot/cookies.txt"
REMOTE_SERVICE="yt-audio-bot"
OUT="$HOME/yt_cookies.raw.txt"
FILTERED="$HOME/yt_cookies.youtube.txt"
TEST_URL="https://www.youtube.com/watch?v=dQw4w9WgXcQ"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --browser) BROWSER="$2"; shift 2 ;;
        --upload)  UPLOAD=1; shift ;;
        --host)    SERVER_HOST="$2"; shift 2 ;;
        --out)     FILTERED="$2"; shift 2 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

red()  { printf '\033[0;31m%s\033[0m\n' "$1"; }
grn()  { printf '\033[0;32m%s\033[0m\n' "$1"; }
ylw()  { printf '\033[1;33m%s\033[0m\n' "$1"; }

echo "🎧 YouTube cookies export (macOS)"
echo "Browser: $BROWSER"
echo

# ── 1. Make sure yt-dlp is available ──
YTDLP=""
if command -v yt-dlp >/dev/null 2>&1; then
    YTDLP="yt-dlp"
elif python3 -m yt_dlp --version >/dev/null 2>&1; then
    YTDLP="python3 -m yt_dlp"
else
    ylw "yt-dlp not found — installing into a local virtualenv..."
    VENV="$HOME/.cache/yt-cookies-venv"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q -U yt-dlp
    YTDLP="$VENV/bin/yt-dlp"
fi
echo "Using: $YTDLP ($($YTDLP --version 2>/dev/null || echo '?'))"

# ── 2. Export cookies from the browser ──
echo
echo "Exporting cookies from '$BROWSER'..."
echo "If macOS asks for Keychain / Full Disk Access, allow it."
rm -f "$OUT"
# yt-dlp writes the cookie jar even if extraction itself fails.
$YTDLP --cookies-from-browser "$BROWSER" --cookies "$OUT" \
    --skip-download --no-warnings "$TEST_URL" >/dev/null 2>&1 || true

if [[ ! -s "$OUT" ]]; then
    red "Failed to export cookies from '$BROWSER'."
    echo "Tips:"
    echo "  • Make sure you are logged into YouTube in that browser."
    echo "  • For Chrome, grant Terminal 'Full Disk Access' in System Settings → Privacy & Security."
    echo "  • Try another browser: --browser safari | firefox | brave | edge"
    exit 1
fi

# ── 3. Keep only YouTube/Google cookies ──
{
    echo "# Netscape HTTP Cookie File"
    echo "# Exported from macOS browser: $BROWSER"
    awk -F'\t' 'NF>=7 && ($1 ~ /youtube|youtu\.be|google|googlevideo|ytimg/) {print}' "$OUT"
} > "$FILTERED"

TOTAL=$(grep -c -v '^#' "$FILTERED" || true)
echo "Kept $TOTAL YouTube/Google cookies -> $FILTERED"

# ── 4. Verify authentication cookies are present ──
if grep -Eiq 'SAPISID|__Secure-3PAPISID|__Secure-1PSID|(^|[^A-Za-z])SID([^A-Za-z]|$)' "$FILTERED"; then
    grn "Auth cookies found (logged-in session)."
else
    ylw "No authentication cookies found — you may not be logged into YouTube in '$BROWSER'."
    ylw "The file was still created, but downloads may stay limited."
fi

# ── 5. Optionally upload to the server ──
if [[ "$UPLOAD" -eq 1 ]]; then
    echo
    echo "Uploading to $SERVER_HOST ..."
    scp "$FILTERED" "$SERVER_HOST:/tmp/yt_cookies.upload"
    ssh "$SERVER_HOST" "install -m 400 /tmp/yt_cookies.upload '$REMOTE_COOKIES' \
        && rm -f /tmp/yt_cookies.upload \
        && systemctl restart '$REMOTE_SERVICE' \
        && sleep 1 && systemctl is-active '$REMOTE_SERVICE'"
    grn "Cookies installed on the server and $REMOTE_SERVICE restarted."
else
    echo
    echo "Next steps:"
    echo "  1. Upload:  scp \"$FILTERED\" $SERVER_HOST:$REMOTE_COOKIES"
    echo "  2. Install: ssh $SERVER_HOST \"chmod 400 $REMOTE_COOKIES && systemctl restart $REMOTE_SERVICE\""
    echo "Or re-run this script with --upload."
fi

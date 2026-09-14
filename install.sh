#!/bin/bash
#
# 🎧 YouTube → Audio Bot — Automated Installer
# =============================================
# This script installs and configures the bot on a fresh Ubuntu 24.04 server.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/Alex-zWitCh/youtube-to-audio-bot/main/install.sh | bash
#
# Or with custom token:
#   curl -sSL https://raw.githubusercontent.com/Alex-zWitCh/youtube-to-audio-bot/main/install.sh | bash -s -- YOUR_BOT_TOKEN
#
# Prerequisites:
#   - Ubuntu 24.04 LTS (or similar Debian-based)
#   - Root access (sudo)
#   - Telegram Bot Token from @BotFather

set -euo pipefail

# ─── Colors ──────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ─── Configuration ───────────────────────────────────────
BOT_DIR="/opt/yt-audio-bot"
VENV_DIR="$BOT_DIR/venv"
DOWNLOAD_DIR="/tmp/yt-audio-downloads"
SERVICE_NAME="yt-audio-bot"
BOT_REPO="https://raw.githubusercontent.com/Alex-zWitCh/youtube-to-audio-bot/main"

# ─── Helper Functions ────────────────────────────────────
log()  { echo -e "${GREEN}[✓]${NC} $1"; }
info() { echo -e "${BLUE}[i]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; }

check_root() {
    if [[ $EUID -ne 0 ]]; then
        err "This script must be run as root (use sudo)"
        exit 1
    fi
}

# ─── Main Installation ───────────────────────────────────

echo ""
echo "  🎧 YouTube → Audio Bot — Installer"
echo "  ==================================="
echo ""

check_root

# Get bot token
BOT_TOKEN="${1:-}"
if [[ -z "$BOT_TOKEN" ]]; then
    echo -n "Enter your Telegram Bot Token (from @BotFather): "
    read -r BOT_TOKEN
    echo ""
    if [[ -z "$BOT_TOKEN" ]]; then
        err "Bot token is required!"
        exit 1
    fi
fi

# Get admin Telegram ID (optional, for /stats and VIP priority)
ADMIN_ID="${2:-}"
if [[ -z "$ADMIN_ID" ]]; then
    echo -n "Enter your Telegram user ID for admin access (optional, press Enter to skip): "
    read -r ADMIN_ID
    echo ""
fi

# ── Step 1: System packages ──
info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq ffmpeg python3-venv python3-pip 2>&1 | tail -1
log "System packages installed"

# ── Step 1b: Install deno (JS runtime for yt-dlp) ──
if ! command -v deno &>/dev/null; then
    info "Installing deno (JavaScript runtime for yt-dlp)..."
    curl -fsSL https://deno.land/install.sh | sh 2>&1 | tail -1
    # Add deno to PATH for current session
    export DENO_INSTALL="$HOME/.deno"
    export PATH="$DENO_INSTALL/bin:$PATH"
    log "deno installed"
else
    info "deno already installed"
fi

# ── Step 2: Python virtual environment ──
info "Setting up Python virtual environment..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
log "Virtual environment created"

# ── Step 3: Python packages ──
info "Installing Python packages..."
pip install -q yt-dlp python-telegram-bot Pillow mutagen 2>&1 | tail -1
log "Python packages installed"

# ── Step 4: Download bot script ──
info "Downloading bot script..."
mkdir -p "$BOT_DIR"
curl -sS -o "$BOT_DIR/bot.py" "$BOT_REPO/bot.py"
chmod +x "$BOT_DIR/bot.py"
log "Bot script downloaded"

# ── Step 5: Create temp directory ──
info "Creating download directory..."
mkdir -p "$DOWNLOAD_DIR"
chmod 777 "$DOWNLOAD_DIR"
log "Download directory created"

# ── Step 6: systemd service ──
info "Creating systemd service..."
cat > "/etc/systemd/system/$SERVICE_NAME.service" << UNIT
[Unit]
Description=YouTube to Audio Telegram Bot
After=network.target
Wants=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$BOT_DIR
Environment="YT_AUDIO_BOT_TOKEN=$BOT_TOKEN"
$( [[ -n "$ADMIN_ID" ]] && echo "Environment=\"YT_AUDIO_ADMIN_ID=$ADMIN_ID\""
 [[ -n "$ADMIN_ID" ]] && echo "Environment=\"YT_AUDIO_VIP_USERS=$ADMIN_ID\"" )
Environment="YT_AUDIO_COOKIES=$BOT_DIR/cookies.txt"
Environment="YTDLP_PATH=$VENV_DIR/bin/yt-dlp"
Environment="YT_AUDIO_COOKIES_REMIND_DAYS=14"
Environment="YT_AUDIO_DB=$BOT_DIR/bot.db"
Environment="YT_AUDIO_CLIENTS=android,mweb,ios,web,tv"
Environment="YT_AUDIO_POT_ENABLED=1"
Environment="YT_AUDIO_POT_URL=http://127.0.0.1:4416"
Environment="YT_AUDIO_JS_RUNTIME=node:/usr/local/bin/node"
Environment="YT_AUDIO_DOWNLOAD_TIMEOUT=1800"
Environment="YT_AUDIO_EXTRACT_TIMEOUT=300"
Environment="YT_AUDIO_ROUNDS=3"
Environment="YT_AUDIO_ROUND_DELAY=10"
Environment="YT_AUDIO_PROXIES="
Environment="YT_AUDIO_PROXY="
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/root/.deno/bin"
ExecStart=$VENV_DIR/bin/python3 $BOT_DIR/bot.py
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl start "$SERVICE_NAME"
log "systemd service created and started"

# ── Step 6b: PO token provider (bgutil) for YouTube ──
info "Setting up YouTube PO token provider (bgutil)..."
if command -v docker &>/dev/null; then
    docker rm -f bgutil-provider >/dev/null 2>&1 || true
    docker run -d --name bgutil-provider --init --restart unless-stopped \
        -p 127.0.0.1:4416:4416 brainicism/bgutil-ytdlp-pot-provider >/dev/null
    "$VENV_DIR/bin/pip" install -q -U bgutil-ytdlp-pot-provider
    log "PO token provider running on 127.0.0.1:4416"
else
    warn "Docker not found — PO token provider not installed (YouTube may require it)"
fi

# ── Step 7: Cron cleanup ──
info "Setting up hourly cleanup..."
cat > "/etc/cron.d/$SERVICE_NAME-cleanup" << CRON
# Clean up old YouTube audio downloads every hour
0 * * * * root find $DOWNLOAD_DIR -type f -mmin +60 -delete 2>/dev/null
CRON
chmod 644 "/etc/cron.d/$SERVICE_NAME-cleanup"
log "Cleanup cron installed"

# ── Step 8: Verify ──
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    log "Bot is running!"
    echo ""
    echo "  ┌──────────────────────────────────────────────┐"
    echo "  │  🎧 YouTube → Audio Bot installed!           │"
    echo "  │                                              │"
    echo "  │  Service:  $SERVICE_NAME"
    echo "  │  Script:   $BOT_DIR/bot.py"
    echo "  │  Logs:     journalctl -u $SERVICE_NAME -f    │"
    echo "  │  Restart:  systemctl restart $SERVICE_NAME   │"
    echo "  │                                              │"
    echo "  │  Cookies (required for YouTube):             │"
    echo "  │  1. Export from Chrome:                      │"
    echo "  │     yt-dlp --cookies-from-browser chrome \\   │"
    echo "  │       -o /dev/null --cookies cookies.txt \\   │"
    echo "  │       https://youtu.be/dQw4w9WgXcQ           │"
    echo "  │  2. Upload: scp cookies.txt \\               │"
    echo "  │     root@SERVER:$BOT_DIR/cookies.txt  │"
    echo "  │  3. Restart: systemctl restart $SERVICE_NAME │"
    echo "  │                                              │"
    echo "  │  BotFather setup:                            │"
    echo "  │  1. /setdescription → paste description      │"
    echo "  │  2. /setuserpic → upload bot_icon.png        │"
    echo "  │  3. /setcommands → start, help, cancel       │"
    echo "  └──────────────────────────────────────────────┘"
    echo ""
else
    err "Bot failed to start! Check logs: journalctl -u $SERVICE_NAME"
    exit 1
fi

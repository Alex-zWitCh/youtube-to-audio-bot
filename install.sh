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
    echo "  │  BotFather setup:                            │"
    echo "  │  1. /setdescription → paste description      │"
    echo "  │  2. /setuserpic → upload bot_icon.png        │"
    echo "  │  3. /setcommands → start, help               │"
    echo "  └──────────────────────────────────────────────┘"
    echo ""
else
    err "Bot failed to start! Check logs: journalctl -u $SERVICE_NAME"
    exit 1
fi

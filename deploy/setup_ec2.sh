#!/usr/bin/env bash
# Zilong Encoding Bot — Ubuntu 22.04/24.04 bootstrap (EC2 / any VPS).
#
# Usage (on the server, with the repo already copied/cloned to ~/zilong-encoding-bot):
#   bash deploy/setup_ec2.sh
#
# What it does:
#   1. Installs FFmpeg (with libass) and Python 3.12 (deadsnakes on 22.04)
#   2. Creates a venv and installs requirements
#   3. Creates .env from .env.example IF MISSING (you fill in the secrets)
#   4. Installs + enables a systemd service (stopped until .env is ready)
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/zilong-encoding-bot}"
SERVICE_NAME="zilong-bot"

cd "$APP_DIR"

echo "==> Installing system packages"
sudo apt-get update -qq
# build-essential + python3-dev provide gcc/headers for the TgCrypto C
# extension (MTProto speedup); without them pip cannot build its wheel.
sudo apt-get install -y -qq ffmpeg mediainfo fonts-dejavu-core \
    software-properties-common build-essential python3-dev

if ! command -v python3.12 >/dev/null 2>&1; then
    echo "==> Python 3.12 not found — adding deadsnakes PPA (Ubuntu 22.04)"
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3.12 python3.12-venv
else
    sudo apt-get install -y -qq python3.12-venv 2>/dev/null || true
fi

echo "==> FFmpeg build check"
ffmpeg -version | head -1
# Capture first: piping into `grep -q` under pipefail turns grep's early
# exit into a spurious SIGPIPE failure. Some builds also print the filter
# list to stderr, so merge streams.
FILTERS="$(ffmpeg -hide_banner -filters 2>&1 || true)"
if grep -q " subtitles " <<<"$FILTERS"; then
    echo "    libass/subtitles filter: OK"
else
    echo "    ERROR: FFmpeg has no subtitles filter (libass)"
    exit 1
fi

echo "==> Creating venv + installing dependencies"
python3.12 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "==> Created $APP_DIR/.env — EDIT IT with your credentials before starting."
fi

echo "==> Installing systemd service: $SERVICE_NAME"
sudo tee /etc/systemd/system/${SERVICE_NAME}.service >/dev/null <<UNIT
[Unit]
Description=Zilong Encoding Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m bot
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30
# Encoding is CPU-heavy; keep the bot from starving sshd.
Nice=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null

echo
echo "Setup complete. Next steps:"
echo "  1. nano $APP_DIR/.env    # fill in API_ID, API_HASH, BOT_TOKEN,"
echo "                           # OWNER_ID, SEEDR_* (incl. SEEDR_PROXY on EC2)"
echo "  2. sudo systemctl start $SERVICE_NAME"
echo "  3. journalctl -u $SERVICE_NAME -f"

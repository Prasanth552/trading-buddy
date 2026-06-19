#!/usr/bin/env bash
# Trading Buddy — one-time server setup (Ubuntu). Run from the project folder.
#   cd ~/Trading-Buddy && bash deploy/setup.sh
set -e

echo "==> Installing system packages..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

echo "==> Creating virtualenv + installing dependencies..."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "==> Done."
echo "Next:"
echo "  1. Create .env here (copy your values; see .env.example)."
echo "  2. Verify:    .venv/bin/python main.py --init"
echo "  3. Install services: bash deploy/install-services.sh"

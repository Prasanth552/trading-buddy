#!/bin/bash
set -e
cd ~/Trading-Buddy

echo "[deploy] Pulling latest code..."
git pull

echo "[deploy] Restarting services..."
sudo systemctl restart channel-listener.service channel-dashboard.service

sleep 3
echo ""
echo "[deploy] Service status:"
sudo systemctl is-active channel-listener.service && echo "  listener: running" || echo "  listener: FAILED"
sudo systemctl is-active channel-dashboard.service && echo "  dashboard: running" || echo "  dashboard: FAILED"
echo ""
echo "[deploy] Done at $(TZ=Asia/Kolkata date '+%Y-%m-%d %H:%M IST')"

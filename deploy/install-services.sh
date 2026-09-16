#!/usr/bin/env bash
# Install + start the two always-on services (scheduler + dashboard).
#   cd ~/Trading-Buddy && bash deploy/install-services.sh
set -e

sudo cp deploy/trading-buddy.service /etc/systemd/system/
sudo cp deploy/trading-buddy-dashboard.service /etc/systemd/system/
sudo cp deploy/trading-buddy-signals.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trading-buddy.service
sudo systemctl enable --now trading-buddy-dashboard.service
sudo systemctl enable --now trading-buddy-signals.service

echo "==> Services started. Useful commands:"
echo "  sudo systemctl status trading-buddy            # scheduler status"
echo "  sudo systemctl status trading-buddy-dashboard  # dashboard status"
echo "  sudo systemctl status trading-buddy-signals    # signal poster status"
echo "  journalctl -u trading-buddy -f                 # live scheduler logs"
echo "  journalctl -u trading-buddy-signals -f         # live signal poster logs"
echo "  sudo systemctl restart trading-buddy           # restart after changes"

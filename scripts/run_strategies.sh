#!/bin/bash
cd ~/Trading-Buddy
set -a && source .env && set +a

# Skip weekends
DOW=$(date +%u)
if [ "$DOW" -ge 6 ]; then exit 0; fi

echo "$(date) — Running strategies..."

# Index straddles
.venv/bin/python3 -m src.strategy.live_runner --lots 1 >> /tmp/strategy_run.log 2>&1

# Stock credit spreads
.venv/bin/python3 -m src.strategy.stock_runner --lots 1 >> /tmp/strategy_run.log 2>&1

echo "$(date) — Done" >> /tmp/strategy_run.log

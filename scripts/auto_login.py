#!/usr/bin/env python3
"""Daily Upstox session refresh — run via cron/systemd before market open."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from src.broker.upstox_data import automated_login, UpstoxDataError
from src.utils.logging import get_logger

log = get_logger("auto_login")

try:
    client = automated_login()
    profile = client.profile()
    log.info("Session valid — user: %s", profile.get("user_name", "?"))
    print(f"✅ Upstox session refreshed for {profile.get('user_name', '?')}")
except UpstoxDataError as exc:
    log.error("Auto-login failed: %s", exc)
    print(f"❌ Auto-login failed: {exc}", file=sys.stderr)
    sys.exit(1)

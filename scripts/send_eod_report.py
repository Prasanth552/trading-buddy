#!/usr/bin/env python3
"""Send EOD report for a given date (default: today).

Usage: .venv/bin/python3 scripts/send_eod_report.py [--date 2026-08-24] [--dry-run]
"""
import sys, os, argparse
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import config
from src.notify.channel_listener import _build_eod_report, send_eod_report

IST = ZoneInfo(config.TIMEZONE)

parser = argparse.ArgumentParser()
parser.add_argument("--date", default=None, help="Date YYYY-MM-DD (default: today)")
parser.add_argument("--dry-run", action="store_true", help="Print report without sending")
args = parser.parse_args()

target_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")

if args.dry_run:
    print(_build_eod_report(target_date))
else:
    print(f"Sending EOD report for {target_date}...")
    send_eod_report(target_date)
    print("Done!")

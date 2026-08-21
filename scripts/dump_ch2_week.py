#!/usr/bin/env python3
"""Dump CH2 (G Prime) messages for a date range from Telegram.

Usage: .venv/bin/python3 scripts/dump_ch2_week.py --from 2026-08-20 --to 2026-08-21
"""
import sys, os, asyncio, argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")

parser = argparse.ArgumentParser()
parser.add_argument("--from", dest="from_date", required=True, help="Start date YYYY-MM-DD")
parser.add_argument("--to", dest="to_date", required=True, help="End date YYYY-MM-DD (inclusive)")
parser.add_argument("--channel", default="-1004326641027", help="Telethon entity ID")
parser.add_argument("--outdir", default="/tmp", help="Output directory")
args = parser.parse_args()

CHANNEL = int(args.channel)

from telethon import TelegramClient

API_ID = int(os.environ.get("TELEGRAM_API_ID", 0))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
SESSION = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "telegram_user")

async def dump_day(client, date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    start = datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=IST).astimezone(UTC)
    end = datetime(dt.year, dt.month, dt.day, 23, 59, 59, tzinfo=IST).astimezone(UTC)

    msgs = []
    async for msg in client.iter_messages(CHANNEL, offset_date=end, reverse=False, limit=500):
        if msg.date.replace(tzinfo=UTC) < start:
            break
        if msg.date.replace(tzinfo=UTC) > end:
            continue
        if msg.text:
            utc_ts = msg.date.strftime("%H:%M:%S")
            msgs.append(f"[{utc_ts}] {msg.text}")

    msgs.reverse()

    outfile = os.path.join(args.outdir, f"ch2_{date_str.replace('-','')}.txt")
    with open(outfile, "w") as f:
        f.write("\n---\n".join(msgs))

    print(f"  {date_str}: {len(msgs)} messages -> {outfile}")
    return outfile, len(msgs)

async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        print("ERROR: Telegram session not authorized. Use data/telegram_user.session")
        return

    from_dt = datetime.strptime(args.from_date, "%Y-%m-%d")
    to_dt = datetime.strptime(args.to_date, "%Y-%m-%d")

    print(f"Dumping CH2 messages: {args.from_date} to {args.to_date}")
    print()

    current = from_dt
    files = []
    while current <= to_dt:
        date_str = current.strftime("%Y-%m-%d")
        # Skip weekends
        if current.weekday() < 5:
            f, n = await dump_day(client, date_str)
            if n > 0:
                files.append((date_str, f))
        current += timedelta(days=1)

    await client.disconnect()
    print(f"\nDumped {len(files)} trading days.")

asyncio.run(main())

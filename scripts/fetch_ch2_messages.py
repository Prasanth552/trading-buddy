#!/usr/bin/env python3
"""Fetch today's messages from CH2 (G Prime) channel via Telethon.
Must kill channel_listener first (session lock).

Usage: .venv/bin/python3 scripts/fetch_ch2_messages.py [--date 2026-08-25]
"""
import sys, os, asyncio, argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

parser = argparse.ArgumentParser()
parser.add_argument("--date", default=None)
parser.add_argument("--limit", type=int, default=200)
args = parser.parse_args()

target_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")
year, month, day = [int(x) for x in target_date.split("-")]
day_start = datetime(year, month, day, 0, 0, 0, tzinfo=IST)
day_end = day_start + timedelta(days=1)

api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
api_hash = os.getenv("TELEGRAM_API_HASH", "")
ch2_id = int(os.getenv("SIGNAL_CHANNEL2_ID", "0"))

session_path = os.path.join(os.path.dirname(__file__), "..", "data", "telegram_reader.session")

async def main():
    from telethon import TelegramClient

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        print("ERROR: Telethon session not authorized")
        return

    # Convert to Telethon entity format
    entity = ch2_id
    if ch2_id < 0 and not str(ch2_id).startswith("-100"):
        entity = int(f"-100{abs(ch2_id)}")

    print(f"Fetching CH2 messages for {target_date} (entity={entity})...\n")

    messages = []
    async for msg in client.iter_messages(entity, limit=args.limit, offset_date=day_end):
        if msg.date.astimezone(IST) < day_start:
            break
        if msg.text:
            messages.append(msg)

    messages.reverse()

    for msg in messages:
        ts = msg.date.astimezone(IST).strftime("%H:%M:%S")
        reply = ""
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            reply = f" [REPLY to #{msg.reply_to.reply_to_msg_id}]"
        print(f"━━━ #{msg.id} @ {ts}{reply} ━━━")
        print(msg.text)
        print()

    print(f"Total: {len(messages)} messages")
    await client.disconnect()

asyncio.run(main())

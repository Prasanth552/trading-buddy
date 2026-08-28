#!/usr/bin/env python3
"""Fetch today's messages from CH1 + CH2 channels via Telethon.
Must kill channel_listener first (session lock).

Usage: .venv/bin/python3 scripts/fetch_messages.py [--date 2026-08-26] [--channel ch1|ch2|all]
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
parser.add_argument("--channel", default="all", choices=["ch1", "ch2", "all"])
parser.add_argument("--limit", type=int, default=1000)
parser.add_argument("--output", "-o", default=None, help="Save output to file")
args = parser.parse_args()

target_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")
year, month, day = [int(x) for x in target_date.split("-")]
day_start = datetime(year, month, day, 0, 0, 0, tzinfo=IST)
day_end = day_start + timedelta(days=1)

api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
api_hash = os.getenv("TELEGRAM_API_HASH", "")
session_path = os.path.join(os.path.dirname(__file__), "..", "data", "telegram_reader.session")

CHANNELS = {}
if args.channel in ("ch1", "all"):
    ch1 = os.getenv("SIGNAL_CHANNEL_ID", "")
    if ch1:
        CHANNELS["CH1"] = int(ch1)
if args.channel in ("ch2", "all"):
    ch2 = os.getenv("SIGNAL_CHANNEL2_ID", "")
    if ch2:
        CHANNELS["CH2"] = int(ch2)


async def main():
    from telethon import TelegramClient

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        print("ERROR: Telethon session not authorized")
        return

    import io
    out_buf = io.StringIO() if args.output else None

    def out(line=""):
        print(line)
        if out_buf is not None:
            out_buf.write(line + "\n")

    for label, ch_id in CHANNELS.items():
        entity = ch_id
        if ch_id < 0 and not str(ch_id).startswith("-100"):
            entity = int(f"-100{abs(ch_id)}")

        out(f"\n{'=' * 80}")
        out(f"  {label} messages for {target_date} (entity={entity})")
        out(f"{'=' * 80}\n")

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
            out(f"━━━ #{msg.id} @ {ts}{reply} ━━━")
            out(msg.text)
            out()

        out(f"Total {label}: {len(messages)} messages")

    if out_buf and args.output:
        with open(args.output, "w") as f:
            f.write(out_buf.getvalue())
        print(f"\nSaved to {args.output}")

    await client.disconnect()

asyncio.run(main())

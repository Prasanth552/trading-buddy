#!/usr/bin/env python3
"""Dump last 30 days of messages from a Telegram channel for analysis."""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
CHANNEL_ID = -1002402465834


async def main():
    from telethon import TelegramClient
    client = TelegramClient(
        'data/telegram_user.session',
        int(os.getenv("TELEGRAM_API_ID", "0")),
        os.getenv("TELEGRAM_API_HASH", ""),
    )
    await client.start(phone=os.getenv("TELEGRAM_PHONE", ""))
    cutoff = (datetime.now(IST) - timedelta(days=30)).date()
    msgs = await client.get_messages(CHANNEL_ID, limit=5000)
    count = 0
    for m in reversed(msgs):
        ts = m.date.astimezone(IST)
        if ts.date() < cutoff:
            continue
        text = (m.text or "").strip()
        if not text:
            continue
        count += 1
        print(f"--- MSG {count} | {ts.strftime('%Y-%m-%d %H:%M')} ---")
        print(text)
        print()
    print(f"Total messages: {count}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

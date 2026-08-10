#!/usr/bin/env python3
"""Debug: fetch today's Channel 2 messages and test the parser."""
import asyncio, os, sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
CHANNEL_ID = -1001547107686

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.notify.channel_listener import parse_signal_ch2

async def main():
    from telethon import TelegramClient
    client = TelegramClient(
        'data/telegram_user.session',
        int(os.getenv("TELEGRAM_API_ID", "0")),
        os.getenv("TELEGRAM_API_HASH", ""),
    )
    await client.start(phone=os.getenv("TELEGRAM_PHONE", ""))
    entity = await client.get_entity(CHANNEL_ID)

    today = datetime.now(IST).date()
    msgs = await client.get_messages(entity, limit=50)

    print(f"=== Channel 2 messages today ({today}) ===\n")
    count = 0
    for m in reversed(msgs):
        ts = m.date.astimezone(IST)
        if ts.date() != today:
            continue
        text = m.text or ""
        if not text.strip():
            continue
        count += 1

        sig = parse_signal_ch2(text)
        status = "PARSED" if sig else "SKIPPED"

        print(f"[{ts.strftime('%H:%M')}] {status}")
        print(f"  {text[:150]}")
        if sig:
            print(f"  → {sig.action} {sig.symbol} {int(sig.strike)} {sig.option_type} "
                  f"trigger={sig.trigger_price} SL={sig.stop_loss} targets={sig.targets}")
        print()

    print(f"Total messages today: {count}")
    await client.disconnect()

asyncio.run(main())

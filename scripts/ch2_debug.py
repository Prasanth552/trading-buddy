#!/usr/bin/env python3
"""Debug: dump all Channel 2 messages from last 15 days to see what we're missing."""
import asyncio, os, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
CHANNEL_ID = -1001547107686

async def main():
    from telethon import TelegramClient
    client = TelegramClient(
        'data/telegram_user.session',
        int(os.getenv("TELEGRAM_API_ID", "0")),
        os.getenv("TELEGRAM_API_HASH", ""),
    )
    await client.start(phone=os.getenv("TELEGRAM_PHONE", ""))
    entity = await client.get_entity(CHANNEL_ID)
    
    cutoff = datetime.now(IST) - timedelta(days=15)
    msgs = await client.get_messages(entity, limit=500)
    
    from scripts.analyze_channel2 import parse_ch2_signal
    
    total = 0
    parsed_count = 0
    skipped = []
    
    for m in reversed(msgs):
        ts = m.date.astimezone(IST)
        if ts < cutoff:
            continue
        text = m.text or ""
        if not text.strip():
            continue
        total += 1
        sig = parse_ch2_signal(text)
        if sig:
            parsed_count += 1
        else:
            # Show skipped messages that look like they might be signals
            upper = text.upper()
            if any(kw in upper for kw in ["BUY", "SELL", "CE", "PE", "TARGET", "TGT", "NIFTY", "SENSEX"]):
                skipped.append((ts, text[:200]))
    
    print(f"Messages in last 15 days: {total}")
    print(f"Parsed as signals: {parsed_count}")
    print(f"\nSkipped messages that contain signal keywords:")
    print("=" * 80)
    for ts, txt in skipped:
        print(f"\n[{ts.strftime('%Y-%m-%d %H:%M')}]")
        print(txt)
        print("-" * 80)
    
    await client.disconnect()

asyncio.run(main())

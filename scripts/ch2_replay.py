"""Replay today's CH2 Telegram messages through the parser."""
import asyncio
import os
import sys
from datetime import datetime, date
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

async def main():
    from telethon import TelegramClient
    import config

    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    session = os.getenv("TELEGRAM_SESSION", "anon")
    ch2_id = os.getenv("SIGNAL_CHANNEL2_ID", "")

    if not ch2_id:
        print("SIGNAL_CHANNEL2_ID not set")
        return

    client = TelegramClient(session, api_id, api_hash)
    await client.start()

    entity = await client.get_entity(int(ch2_id))
    today = date(2026, 9, 23)
    start = datetime(today.year, today.month, today.day, 0, 0, tzinfo=IST)
    end = datetime(today.year, today.month, today.day, 23, 59, tzinfo=IST)

    print(f"Fetching CH2 messages for {today}...\n")

    messages = []
    async for msg in client.iter_messages(entity, offset_date=end, reverse=True):
        if msg.date.astimezone(IST).date() < today:
            continue
        if msg.date.astimezone(IST).date() > today:
            break
        if msg.text:
            messages.append((msg.date.astimezone(IST), msg.text))

    print(f"Found {len(messages)} text messages\n")

    # Now replay through parser
    from src.notify.channel_listener import parse_signal_ch2
    # Reset parser state
    import src.notify.channel_listener as cl
    cl._ch2_pending = None

    signals = []
    for ts, text in messages:
        t_str = ts.strftime("%H:%M:%S")
        sig = parse_signal_ch2(text)
        pending = cl._ch2_pending
        status = ""
        if sig:
            status = f"  >>> SIGNAL: {sig.action} {sig.symbol} {int(sig.strike)}{sig.option_type} trigger={sig.trigger_price:.0f} SL={sig.stop_loss:.0f} TGT={sig.targets[:3]}"
            signals.append((ts, sig))
        elif pending:
            status = f"  [buffered: {pending.get('symbol','')} trigger={pending.get('trigger',0):.0f}]"

        print(f"[{t_str}] {text[:100]}")
        if status:
            print(status)
        print()

    print(f"\n{'='*60}")
    print(f"TOTAL SIGNALS PARSED: {len(signals)}")
    for ts, sig in signals:
        print(f"  {ts.strftime('%H:%M')} | {sig.action} {sig.symbol} {int(sig.strike)}{sig.option_type} "
              f"trigger={sig.trigger_price:.0f} SL={sig.stop_loss:.0f} TGT={sig.targets[:3]}")

    await client.disconnect()

asyncio.run(main())

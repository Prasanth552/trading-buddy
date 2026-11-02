#!/usr/bin/env python3
"""Fetch ALL October 2026 messages from DT - student practice calls channel."""
import asyncio, os, sys, json
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
api_hash = os.getenv("TELEGRAM_API_HASH", "")
ch1_id = int(os.getenv("SIGNAL_CHANNEL_ID", "0"))

if ch1_id > 0:
    entity_id = int(f"-100{ch1_id}")
elif not str(ch1_id).startswith("-100"):
    entity_id = int(f"-100{abs(ch1_id)}")
else:
    entity_id = ch1_id

oct_start = datetime(2026, 10, 1, 0, 0, 0, tzinfo=IST)
nov_start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=IST)

async def main():
    from telethon import TelegramClient
    session_path = os.path.join(os.path.dirname(__file__), "..", "data", "telegram_user.session")
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()

    authorized = await client.is_user_authorized()
    print(f"Authorized: {authorized}")
    if not authorized:
        print("ERROR: Session expired. Need to re-authorize.")
        await client.disconnect()
        return

    messages = []
    async for msg in client.iter_messages(entity_id, limit=5000, offset_date=nov_start):
        ts = msg.date.astimezone(IST)
        if ts < oct_start:
            break
        if msg.text:
            messages.append({
                "id": msg.id,
                "ts": ts.isoformat(),
                "text": msg.text,
                "reply_to": msg.reply_to.reply_to_msg_id if msg.reply_to else None,
            })

    messages.reverse()
    print(f"Total messages: {len(messages)}")

    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "ch1_oct_messages.json")
    with open(out_path, "w") as f:
        json.dump(messages, f, indent=2, ensure_ascii=False)
    print(f"Saved to {out_path}")

    for m in messages[:5]:
        print(f"\n--- #{m['id']} @ {m['ts']} ---")
        print(m['text'][:200])

    await client.disconnect()

asyncio.run(main())

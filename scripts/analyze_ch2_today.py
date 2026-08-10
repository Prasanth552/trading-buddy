#!/usr/bin/env python3
"""Analyze Channel 2 signals for today against actual candle data."""
import asyncio, os, sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from src.broker.upstox_data import UpstoxData
from src.notify.channel_listener import calc_charges, parse_signal_ch2
from scripts.analyze_channel2 import resolve_instrument, simulate_trade

IST = ZoneInfo("Asia/Kolkata")
CHANNEL_ID = -1001547107686
LOT_MULTIPLIER = 3

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

    signals = []
    for m in reversed(msgs):
        ts = m.date.astimezone(IST)
        if ts.date() != today:
            continue
        text = m.text or ""
        sig = parse_signal_ch2(text)
        if sig:
            signals.append({"sig": sig, "ts": ts})

    await client.disconnect()

    if not signals:
        print("No signals found today.")
        return

    ud = UpstoxData()
    print(f"\n{'=' * 90}")
    print(f"CHANNEL 2 — TODAY ({today}) — {LOT_MULTIPLIER} lots, ₹2K floor")
    print(f"{'=' * 90}")

    total_net = 0
    total_charges = 0
    total_gross = 0
    wins = 0
    losses = 0

    for s in signals:
        sig = s["sig"]
        ts = s["ts"]
        entry_time = ts.strftime("%H:%M")

        key, lot_size = resolve_instrument(ud, sig.symbol, sig.strike, sig.option_type, today)
        if not key:
            print(f"\n  SKIP: Could not resolve {sig.symbol} {int(sig.strike)} {sig.option_type}")
            continue

        entry = sig.trigger_price if sig.trigger_price > 0 else 0
        if entry <= 0:
            continue

        qty = lot_size * LOT_MULTIPLIER
        sl = sig.stop_loss
        target = sig.targets[0]

        try:
            d = ud._get(f"/v3/historical-candle/intraday/{key}/minutes/1")
            candles = d.get("data", {}).get("candles", [])
            candles.sort(key=lambda c: c[0])
        except Exception as e:
            print(f"\n  SKIP: Candle fetch failed: {e}")
            continue

        if not candles:
            print(f"\n  SKIP: No candles for {sig.symbol}")
            continue

        result, exit_price, exit_time, gross, charges, net, high, low = simulate_trade(
            candles, entry, sl, target, qty, entry_time
        )

        total_gross += gross
        total_charges += charges
        total_net += net
        if net > 0:
            wins += 1
        else:
            losses += 1

        sign = "+" if net >= 0 else ""
        print(f"\n{'─' * 90}")
        print(f"  {sig.symbol} {int(sig.strike)} {sig.option_type}  |  Qty: {qty}")
        print(f"  Entry: {entry} @ {entry_time} | SL: {sl} | Target: {target}")
        print(f"  Targets: {sig.targets}")
        print(f"  Day range: Low {low} → High {high}")
        print(f"  → {result:>14} @ {exit_price} at {exit_time}  |  Gross: {gross:+,.0f}  Charges: {charges:.0f}  Net: {sign}{net:,.0f}")

    trade_count = wins + losses
    print(f"\n{'=' * 90}")
    if trade_count:
        print(f"  TODAY SUMMARY")
        print(f"  Trades: {trade_count} | Wins: {wins} | Losses: {losses} | "
              f"Win rate: {wins/trade_count*100:.0f}%")
        print(f"  Total gross: {total_gross:+,.0f}")
        print(f"  Total charges: {total_charges:,.0f}")
        print(f"  Total net: {total_net:+,.0f}")
    print(f"{'=' * 90}")

asyncio.run(main())

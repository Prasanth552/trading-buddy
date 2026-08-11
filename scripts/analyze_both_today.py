#!/usr/bin/env python3
"""Analyze today's trades for both channels against actual candle data."""
import asyncio, os, sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from src.broker.upstox_data import UpstoxData
from src.notify.channel_listener import calc_charges, parse_signal, parse_signal_ch2
from scripts.analyze_channel2 import resolve_instrument, simulate_trade

IST = ZoneInfo("Asia/Kolkata")
CH1_ID = int(os.getenv("SIGNAL_CHANNEL_ID", "0"))
CH2_ID = -1001547107686

async def fetch_signals(client, channel_id, parser, today):
    if channel_id > 0:
        channel_id = int(f"-100{channel_id}")
    entity = await client.get_entity(channel_id)
    msgs = await client.get_messages(entity, limit=100)
    signals = []
    for m in reversed(msgs):
        ts = m.date.astimezone(IST)
        if ts.date() != today:
            continue
        text = m.text or ""
        sig = parser(text)
        if sig:
            signals.append({"sig": sig, "ts": ts, "raw": text[:100]})
    return signals

def run_simulation(ud, signals, lot_mult, channel_name, today):
    total_net = 0
    total_charges = 0
    total_gross = 0
    wins = 0
    losses = 0
    results = []

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

        qty = lot_size * lot_mult
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
        print(f"  {sig.symbol} {int(sig.strike)} {sig.option_type}  |  Qty: {qty}  |  {lot_mult} lot(s)")
        print(f"  Entry: {entry} @ {entry_time} | SL: {sl} | Target: {target}")
        print(f"  Day range: Low {low} → High {high}")
        print(f"  → {result:>14} @ {exit_price} at {exit_time}  |  Gross: {gross:+,.0f}  Charges: {charges:.0f}  Net: {sign}{net:,.0f}")

    trade_count = wins + losses
    return trade_count, wins, losses, total_gross, total_charges, total_net

async def main():
    from telethon import TelegramClient
    client = TelegramClient(
        'data/telegram_user.session',
        int(os.getenv("TELEGRAM_API_ID", "0")),
        os.getenv("TELEGRAM_API_HASH", ""),
    )
    await client.start(phone=os.getenv("TELEGRAM_PHONE", ""))
    today = datetime.now(IST).date()

    # Channel 1 uses LLM parser, but for analysis we use it
    ch1_signals = await fetch_signals(client, CH1_ID, parse_signal, today)
    ch2_signals = await fetch_signals(client, CH2_ID, parse_signal_ch2, today)
    await client.disconnect()

    ud = UpstoxData()

    # --- Channel 1 ---
    print(f"\n{'=' * 90}")
    print(f"CHANNEL 1 — {today} — 1 lot, Stock Options")
    print(f"{'=' * 90}")

    if ch1_signals:
        tc1, w1, l1, g1, c1, n1 = run_simulation(ud, ch1_signals, 1, "CH1", today)
        print(f"\n{'─' * 90}")
        if tc1:
            print(f"  CH1 SUMMARY: {tc1} trades | {w1}W {l1}L | Gross: {g1:+,.0f} | Charges: {c1:,.0f} | Net: {n1:+,.0f}")
    else:
        print("  No signals parsed from Channel 1 today.")
        tc1, n1, c1 = 0, 0, 0

    # --- Channel 2 ---
    print(f"\n{'=' * 90}")
    print(f"CHANNEL 2 — {today} — 3 lots, Index Options")
    print(f"{'=' * 90}")

    if ch2_signals:
        tc2, w2, l2, g2, c2, n2 = run_simulation(ud, ch2_signals, 3, "CH2", today)
        print(f"\n{'─' * 90}")
        if tc2:
            print(f"  CH2 SUMMARY: {tc2} trades | {w2}W {l2}L | Gross: {g2:+,.0f} | Charges: {c2:,.0f} | Net: {n2:+,.0f}")
    else:
        print("  No signals parsed from Channel 2 today.")
        tc2, n2, c2 = 0, 0, 0

    # --- Combined ---
    print(f"\n{'=' * 90}")
    print(f"  COMBINED: Net {n1+n2:+,.0f}  (CH1: {n1:+,.0f} + CH2: {n2:+,.0f})  |  Charges: {c1+c2:,.0f}")
    print(f"{'=' * 90}")

asyncio.run(main())

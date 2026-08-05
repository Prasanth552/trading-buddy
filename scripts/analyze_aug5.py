#!/usr/bin/env python3
"""Analyze Aug 5 trades against actual 1-min candle data with ₹2K profit target."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from src.broker.upstox_data import UpstoxData
from src.notify.channel_listener import calc_charges

ud = UpstoxData()

PROFIT_TARGET = 2000

trades = [
    {"name": "BHARTIARTL 1960 CE", "key": "NSE_FO|76597",
     "entry_time": "09:16", "entry_ltp": 58.10, "sl": 50.0, "lot": 475},
    {"name": "INDIGO 5300 CE", "key": "NSE_FO|107342",
     "entry_time": "09:17", "entry_ltp": 280.0, "sl": 250.0, "lot": 150},
    {"name": "PNBHOUSING 1100 CE #1", "key": "NSE_FO|137827",
     "entry_time": "09:22", "entry_ltp": 58.20, "sl": 51.0, "lot": 650},
    {"name": "JUBLFOOD 470 CE", "key": "NSE_FO|113751",
     "entry_time": "09:38", "entry_ltp": 22.85, "sl": 20.0, "lot": 1250},
    {"name": "NYKAA 340 PE #1", "key": "NSE_FO|130736",
     "entry_time": "09:57", "entry_ltp": 15.55, "sl": 14.3, "lot": 3125},
    {"name": "NYKAA 340 PE #2", "key": "NSE_FO|130736",
     "entry_time": "09:58", "entry_ltp": 14.95, "sl": 14.3, "lot": 3125},
    {"name": "PNBHOUSING 1100 CE #2", "key": "NSE_FO|137827",
     "entry_time": "10:04", "entry_ltp": 74.0, "sl": 68.0, "lot": 650},
]

# Fetch candles for each unique instrument key
candle_cache = {}
for t in trades:
    k = t["key"]
    if k not in candle_cache:
        try:
            d = ud._get(f"/v3/historical-candle/intraday/{k}/minutes/1")
            candles = d.get("data", {}).get("candles", [])
            candles.sort(key=lambda c: c[0])
            candle_cache[k] = candles
            print(f"Fetched {len(candles)} candles for {k}")
        except Exception as e:
            print(f"Failed to fetch {k}: {e}")
            candle_cache[k] = []

print("\n" + "="*80)
print(f"AUG 5 ANALYSIS — 1 lot, ₹{PROFIT_TARGET} profit target, auto-SL")
print("="*80)

total_net = 0
total_charges = 0
total_gross = 0

for t in trades:
    candles = candle_cache.get(t["key"], [])
    entry = t["entry_ltp"]
    sl = t["sl"]
    qty = t["lot"]
    entry_time = t["entry_time"]

    # Calculate target price needed for ₹2K net profit
    # We need to estimate: (exit - entry) * qty - charges >= 2000
    # Approximate: charges ~= 150 for typical trade
    # So gross needed ~= 2150, price move = 2150/qty
    # But let's be precise: iterate to find the exit price
    target_exit = entry + 0.05
    for _ in range(10000):
        gross = (target_exit - entry) * qty
        ch = calc_charges(entry, target_exit, qty)["total"]
        if gross - ch >= PROFIT_TARGET:
            break
        target_exit += 0.05
    target_exit = round(target_exit, 2)

    print(f"\n{'─'*70}")
    print(f"  {t['name']}")
    print(f"  Entry: {entry} @ {entry_time} | SL: {sl} | Target exit: {target_exit} (for ₹{PROFIT_TARGET} net)")
    print(f"  Qty: {qty} | Need +{round(target_exit - entry, 2)} pts")

    # Scan candles after entry time
    result = "OPEN (no exit)"
    exit_price = None
    exit_time = None
    high_seen = entry
    low_seen = entry

    for c in candles:
        ts = c[0]  # ISO timestamp
        time_part = ts[11:16]  # HH:MM
        if time_part < entry_time:
            continue

        o, h, l, cl = c[1], c[2], c[3], c[4]
        high_seen = max(high_seen, h)
        low_seen = min(low_seen, l)

        # Check SL first (worst case: SL hit on same candle as target)
        if l <= sl:
            exit_price = sl
            exit_time = time_part
            result = "SL HIT"
            break

        # Check target
        if h >= target_exit:
            exit_price = target_exit
            exit_time = time_part
            result = "TARGET HIT"
            break

    if exit_price:
        gross = (exit_price - entry) * qty
        ch = calc_charges(entry, exit_price, qty)
        net = gross - ch["total"]
        total_gross += gross
        total_charges += ch["total"]
        total_net += net
        sign = "+" if net >= 0 else ""
        print(f"  → {result} @ {exit_price} at {exit_time}")
        print(f"  Gross: {gross:+.0f} | Charges: {ch['total']:.0f} | Net: {sign}{net:.0f}")
        print(f"  (Brk: {ch['brokerage']}, STT: {ch['stt']:.1f}, Txn: {ch['exchange_txn']:.1f}, GST: {ch['gst']:.1f})")
    else:
        print(f"  → {result} | High seen: {high_seen} | Low seen: {low_seen}")
        # If still open, show unrealized at last candle
        if candles:
            last = candles[-1][4]  # close of last candle
            gross = (last - entry) * qty
            ch = calc_charges(entry, last, qty)
            net = gross - ch["total"]
            print(f"  Last price: {last} | Unrealized gross: {gross:+.0f} | net: {net:+.0f}")

print(f"\n{'='*70}")
print(f"  TOTAL GROSS: {total_gross:+.0f}")
print(f"  TOTAL CHARGES: {total_charges:.0f}")
print(f"  TOTAL NET: {total_net:+.0f}")
print(f"{'='*70}")

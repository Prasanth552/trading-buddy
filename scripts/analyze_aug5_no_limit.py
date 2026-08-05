#!/usr/bin/env python3
"""Aug 5 analysis: SL only, NO profit target cap. Hold until SL or market close."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from src.broker.upstox_data import UpstoxData
from src.notify.channel_listener import calc_charges

ud = UpstoxData()

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

print("\n" + "=" * 80)
print("AUG 5 ANALYSIS — 1 lot, NO profit target, SL only (hold till SL or 15:29)")
print("=" * 80)

total_net = 0
total_charges = 0
total_gross = 0

for t in trades:
    candles = candle_cache.get(t["key"], [])
    entry = t["entry_ltp"]
    sl = t["sl"]
    qty = t["lot"]
    entry_time = t["entry_time"]

    print(f"\n{'─' * 70}")
    print(f"  {t['name']}")
    print(f"  Entry: {entry} @ {entry_time} | SL: {sl} | Qty: {qty}")
    print(f"  Max SL loss: {(sl - entry) * qty:+.0f} gross")

    exit_price = None
    exit_time = None
    result = "OPEN"
    high_seen = entry
    low_seen = entry

    for c in candles:
        ts = c[0]
        time_part = ts[11:16]
        if time_part < entry_time:
            continue

        o, h, l, cl = c[1], c[2], c[3], c[4]
        high_seen = max(high_seen, h)
        low_seen = min(low_seen, l)

        if l <= sl:
            exit_price = sl
            exit_time = time_part
            result = "SL HIT"
            break

        # Market close at 15:29 — square off at close price
        if time_part >= "15:29":
            exit_price = cl
            exit_time = time_part
            result = "MARKET CLOSE"
            break

    if exit_price is None and candles:
        last = candles[-1]
        exit_price = last[4]
        exit_time = last[0][11:16]
        result = f"LAST CANDLE ({exit_time})"

    if exit_price:
        gross = (exit_price - entry) * qty
        ch = calc_charges(entry, exit_price, qty)
        net = gross - ch["total"]
        total_gross += gross
        total_charges += ch["total"]
        total_net += net
        sign = "+" if net >= 0 else ""
        print(f"  High seen: {high_seen} (+{high_seen - entry:.2f}) | Low seen: {low_seen} ({low_seen - entry:.2f})")
        print(f"  → {result} @ {exit_price} at {exit_time}")
        print(f"  Gross: {gross:+,.0f} | Charges: {ch['total']:.0f} | Net: {sign}{net:,.0f}")
    else:
        print(f"  → No candle data")

print(f"\n{'=' * 70}")
print(f"  TOTAL GROSS: {total_gross:+,.0f}")
print(f"  TOTAL CHARGES: {total_charges:,.0f}")
print(f"  TOTAL NET: {total_net:+,.0f}")
print(f"{'=' * 70}")

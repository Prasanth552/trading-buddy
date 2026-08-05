#!/usr/bin/env python3
"""Aug 5: ₹2K target only, NO SL. Compare with target+SL. Real candle data."""
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

def calc_target_exit(entry, qty):
    target_exit = entry + 0.05
    for _ in range(10000):
        gross = (target_exit - entry) * qty
        ch = calc_charges(entry, target_exit, qty)["total"]
        if gross - ch >= PROFIT_TARGET:
            break
        target_exit += 0.05
    return round(target_exit, 2)

def simulate(candles, entry, sl, qty, entry_time, use_sl):
    target_exit = calc_target_exit(entry, qty)
    exit_price = None
    exit_time = None
    result = None
    high_seen = entry
    low_seen = entry

    for c in candles:
        time_part = c[0][11:16]
        if time_part < entry_time:
            continue
        o, h, l, cl = c[1], c[2], c[3], c[4]
        high_seen = max(high_seen, h)
        low_seen = min(low_seen, l)

        if use_sl and l <= sl:
            exit_price = sl
            exit_time = time_part
            result = "SL HIT"
            break
        if h >= target_exit:
            exit_price = target_exit
            exit_time = time_part
            result = "TARGET HIT"
            break
        if time_part >= "15:29":
            exit_price = cl
            exit_time = time_part
            result = "MARKET CLOSE"
            break

    if exit_price is None and candles:
        exit_price = candles[-1][4]
        exit_time = candles[-1][0][11:16]
        result = "LAST CANDLE"

    if exit_price:
        gross = (exit_price - entry) * qty
        ch = calc_charges(entry, exit_price, qty)
        net = gross - ch["total"]
        return result, exit_price, exit_time, gross, ch["total"], net, high_seen, low_seen, target_exit
    return None, None, None, 0, 0, 0, high_seen, low_seen, target_exit


print("\n" + "=" * 85)
print("AUG 5 — SIDE BY SIDE: ₹2K target+SL vs ₹2K target only (no SL)")
print("=" * 85)

total_with_sl = 0
total_no_sl = 0
total_ch_sl = 0
total_ch_nosl = 0

for t in trades:
    candles = candle_cache.get(t["key"], [])
    entry = t["entry_ltp"]
    sl = t["sl"]
    qty = t["lot"]
    et = t["entry_time"]

    r1, ep1, et1, g1, c1, n1, h1, l1, tgt = simulate(candles, entry, sl, qty, et, use_sl=True)
    r2, ep2, et2, g2, c2, n2, h2, l2, _   = simulate(candles, entry, sl, qty, et, use_sl=False)

    total_with_sl += n1
    total_no_sl += n2
    total_ch_sl += c1
    total_ch_nosl += c2

    print(f"\n{'─' * 85}")
    print(f"  {t['name']}")
    print(f"  Entry: {entry} @ {et} | SL: {sl} | Target: {tgt} | Qty: {qty}")
    print(f"  Day range after entry: Low {l2} → High {h2}")
    print()
    sign1 = "+" if n1 >= 0 else ""
    sign2 = "+" if n2 >= 0 else ""
    print(f"  WITH SL:    {r1:>14} @ {ep1} at {et1}  |  Net: {sign1}{n1:,.0f}")
    print(f"  NO SL:      {r2:>14} @ {ep2} at {et2}  |  Net: {sign2}{n2:,.0f}")
    diff = n2 - n1
    sdiff = "+" if diff >= 0 else ""
    print(f"  Difference: {sdiff}{diff:,.0f}")

print(f"\n{'=' * 85}")
print(f"  TOTAL WITH SL:     {total_with_sl:+,.0f}  (charges: {total_ch_sl:,.0f})")
print(f"  TOTAL NO SL:       {total_no_sl:+,.0f}  (charges: {total_ch_nosl:,.0f})")
print(f"  REMOVING SL SAVES: {total_no_sl - total_with_sl:+,.0f}")
print(f"{'=' * 85}")

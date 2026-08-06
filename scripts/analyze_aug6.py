#!/usr/bin/env python3
"""Aug 6: ₹2K target vs channel's actual SL+target."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from src.broker.upstox_data import UpstoxData
from src.notify.channel_listener import calc_charges

ud = UpstoxData()
PROFIT_TARGET = 2000

trades = [
    {"name": "GVT&D 4200 CE", "key": "NSE_FO|97233",
     "entry_time": "09:22", "entry_ltp": 215.0, "sl": 180.0, "target": 225.0, "lot": 125},
    {"name": "POWERGRID 290 PE", "key": "NSE_FO|138983",
     "entry_time": "09:25", "entry_ltp": 17.30, "sl": 15.4, "target": 19.0, "lot": 1900},
    {"name": "HAL 4600 CE #1", "key": "NSE_FO|97469",
     "entry_time": "09:32", "entry_ltp": 315.0, "sl": 280.0, "target": 325.0, "lot": 150},
    {"name": "HAL 4600 CE #2", "key": "NSE_FO|97469",
     "entry_time": "09:33", "entry_ltp": 324.6, "sl": 300.0, "target": 345.0, "lot": 150},
    {"name": "CHOLAFIN 1880 CE", "key": "NSE_FO|82650",
     "entry_time": "09:47", "entry_ltp": 69.15, "sl": 62.0, "target": 73.0, "lot": 625},
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

def calc_2k_target(entry, qty):
    target_exit = entry + 0.05
    for _ in range(10000):
        gross = (target_exit - entry) * qty
        ch = calc_charges(entry, target_exit, qty)["total"]
        if gross - ch >= PROFIT_TARGET:
            break
        target_exit += 0.05
    return round(target_exit, 2)

def simulate(candles, entry, sl, target_price, qty, entry_time):
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

        if sl is not None and l <= sl:
            exit_price = sl
            exit_time = time_part
            result = "SL HIT"
            break
        if target_price is not None and h >= target_price:
            exit_price = target_price
            exit_time = time_part
            result = "TARGET HIT"
            break
        if time_part >= "15:29":
            exit_price = cl
            exit_time = time_part
            result = "MKT CLOSE"
            break

    if exit_price is None and candles:
        exit_price = candles[-1][4]
        exit_time = candles[-1][0][11:16]
        result = "LAST CANDLE"

    if exit_price:
        gross = (exit_price - entry) * qty
        ch = calc_charges(entry, exit_price, qty)
        net = gross - ch["total"]
        return result, exit_price, exit_time, gross, ch["total"], net, high_seen, low_seen
    return None, None, None, 0, 0, 0, high_seen, low_seen


print("\n" + "=" * 90)
print("AUG 6 — ₹2K AUTO-TARGET vs CHANNEL'S SL + TARGET")
print("=" * 90)

total_bot = 0
total_channel = 0
total_ch_bot = 0
total_ch_channel = 0

for t in trades:
    candles = candle_cache.get(t["key"], [])
    entry = t["entry_ltp"]
    sl = t["sl"]
    ch_target = t["target"]
    qty = t["lot"]
    et = t["entry_time"]
    auto_target = calc_2k_target(entry, qty)

    # Bot: ₹2K target + SL
    r1, ep1, et1, g1, c1, n1, h1, l1 = simulate(candles, entry, sl, auto_target, qty, et)
    # Channel: channel SL + channel target
    r2, ep2, et2, g2, c2, n2, h2, l2 = simulate(candles, entry, sl, ch_target, qty, et)

    total_bot += n1
    total_channel += n2
    total_ch_bot += c1
    total_ch_channel += c2

    ch_gross = (ch_target - entry) * qty

    print(f"\n{'─' * 90}")
    print(f"  {t['name']}")
    print(f"  Entry: {entry} @ {et} | SL: {sl} | Qty: {qty}")
    print(f"  Channel target: {ch_target} (gross: +{ch_gross:,.0f}) | ₹2K auto-target: {auto_target}")
    print(f"  Day range after entry: Low {l2} → High {h2}")
    print()
    sign1 = "+" if n1 >= 0 else ""
    sign2 = "+" if n2 >= 0 else ""
    print(f"  ₹2K + SL (bot):       {r1:>14} @ {ep1} at {et1}  |  Net: {sign1}{n1:,.0f}")
    print(f"  Channel SL + Target:  {r2:>14} @ {ep2} at {et2}  |  Net: {sign2}{n2:,.0f}")
    diff = n2 - n1
    sdiff = "+" if diff >= 0 else ""
    print(f"  Difference: {sdiff}{diff:,.0f}")

print(f"\n{'=' * 90}")
print(f"  ₹2K + SL (bot):       {total_bot:+,.0f}  (charges: {total_ch_bot:,.0f})")
print(f"  Channel SL + Target:  {total_channel:+,.0f}  (charges: {total_ch_channel:,.0f})")
print(f"  Channel is:           {total_channel - total_bot:+,.0f} vs bot")
print(f"{'=' * 90}")

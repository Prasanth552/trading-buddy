#!/usr/bin/env python3
"""Aug 6 analysis: ₹2K target vs no limit (SL only) vs no SL (target only)."""
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
     "entry_time": "09:22", "entry_ltp": 215.0, "sl": 180.0, "lot": 125},
    {"name": "POWERGRID 290 PE", "key": "NSE_FO|138983",
     "entry_time": "09:25", "entry_ltp": 17.30, "sl": 15.4, "lot": 1900},
    {"name": "HAL 4600 CE #1", "key": "NSE_FO|97469",
     "entry_time": "09:32", "entry_ltp": 315.0, "sl": 280.0, "lot": 150},
    {"name": "HAL 4600 CE #2", "key": "NSE_FO|97469",
     "entry_time": "09:33", "entry_ltp": 324.6, "sl": 300.0, "lot": 150},
    {"name": "CHOLAFIN 1880 CE", "key": "NSE_FO|82650",
     "entry_time": "09:47", "entry_ltp": 69.15, "sl": 62.0, "lot": 625},
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

def simulate(candles, entry, sl, qty, entry_time, use_sl, use_target):
    target_exit = calc_target_exit(entry, qty) if use_target else None
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
        if use_target and h >= target_exit:
            exit_price = target_exit
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
        return result, exit_price, exit_time, gross, ch["total"], net, high_seen, low_seen, target_exit
    return None, None, None, 0, 0, 0, high_seen, low_seen, target_exit


print("\n" + "=" * 90)
print("AUG 6 — THREE SCENARIOS COMPARED")
print("=" * 90)

totals = {"2k_target": 0, "no_limit": 0, "target_only": 0}
total_ch = {"2k_target": 0, "no_limit": 0, "target_only": 0}

for t in trades:
    candles = candle_cache.get(t["key"], [])
    entry = t["entry_ltp"]
    sl = t["sl"]
    qty = t["lot"]
    et = t["entry_time"]

    # Scenario 1: ₹2K target + SL (current bot)
    r1, ep1, et1, g1, c1, n1, h1, l1, tgt = simulate(candles, entry, sl, qty, et, use_sl=True, use_target=True)
    # Scenario 2: No limit — SL only, no profit target
    r2, ep2, et2, g2, c2, n2, h2, l2, _ = simulate(candles, entry, sl, qty, et, use_sl=True, use_target=False)
    # Scenario 3: Target only, no SL
    r3, ep3, et3, g3, c3, n3, h3, l3, _ = simulate(candles, entry, sl, qty, et, use_sl=False, use_target=True)

    totals["2k_target"] += n1
    totals["no_limit"] += n2
    totals["target_only"] += n3
    total_ch["2k_target"] += c1
    total_ch["no_limit"] += c2
    total_ch["target_only"] += c3

    print(f"\n{'─' * 90}")
    print(f"  {t['name']}")
    print(f"  Entry: {entry} @ {et} | SL: {sl} | ₹2K target: {tgt} | Qty: {qty}")
    print(f"  Day range after entry: Low {l2} → High {h2}")
    print()
    for label, r, ep, etm, n in [
        ("₹2K + SL (bot)", r1, ep1, et1, n1),
        ("SL only (no cap)", r2, ep2, et2, n2),
        ("Target only (no SL)", r3, ep3, et3, n3),
    ]:
        sign = "+" if n >= 0 else ""
        print(f"  {label:>22}: {r:>14} @ {ep} at {etm}  |  Net: {sign}{n:,.0f}")

print(f"\n{'=' * 90}")
print(f"  ₹2K + SL (current bot):  {totals['2k_target']:+,.0f}  (charges: {total_ch['2k_target']:,.0f})")
print(f"  SL only (no profit cap): {totals['no_limit']:+,.0f}  (charges: {total_ch['no_limit']:,.0f})")
print(f"  Target only (no SL):     {totals['target_only']:+,.0f}  (charges: {total_ch['target_only']:,.0f})")
print(f"{'=' * 90}")

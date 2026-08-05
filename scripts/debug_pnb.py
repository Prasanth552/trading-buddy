#!/usr/bin/env python3
"""Dump PNBHOUSING 1100 CE candles to verify highs/lows."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from src.broker.upstox_data import UpstoxData
ud = UpstoxData()

key = "NSE_FO|137827"
d = ud._get(f"/v3/historical-candle/intraday/{key}/minutes/1")
candles = d.get("data", {}).get("candles", [])
candles.sort(key=lambda c: c[0])

print(f"PNBHOUSING 1100 CE — {len(candles)} candles")
print(f"{'Time':>8}  {'Open':>8}  {'High':>8}  {'Low':>8}  {'Close':>8}  {'Vol':>10}")
print("-" * 60)

day_high = 0
day_low = 99999
for c in candles:
    ts = c[0][11:16]
    o, h, l, cl, v = c[1], c[2], c[3], c[4], c[5]
    day_high = max(day_high, h)
    day_low = min(day_low, l)
    marker = ""
    if h == day_high:
        marker += " << DAY HIGH"
    if l == day_low:
        marker += " << DAY LOW"
    print(f"{ts:>8}  {o:>8.2f}  {h:>8.2f}  {l:>8.2f}  {cl:>8.2f}  {v:>10}{marker}")

print(f"\nDay high: {day_high}  Day low: {day_low}")

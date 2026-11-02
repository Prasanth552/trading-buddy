from src.broker.upstox_data import UpstoxData, load_cached_token
from datetime import datetime
from dateutil.parser import parse as dtparse
import config

token = load_cached_token()
ud = UpstoxData(access_token=token)
nifty_key = config.UPSTOX_INDEX_KEYS.get("NSE:NIFTY 50")

today = datetime(2026, 10, 21)
candles = ud.historical_data(nifty_key, today.replace(hour=9, minute=15), today.replace(hour=15, minute=31), "5minute")

def get_dt(c):
    d = c.get("date") or c.get("timestamp")
    return dtparse(str(d)) if isinstance(d, str) else d

prev_candles = ud.historical_data(nifty_key, datetime(2026, 10, 20, 15, 25), datetime(2026, 10, 20, 15, 31), "1minute")
prev_close = prev_candles[-1]["close"]
day_open = candles[0]["open"]

print(f"Prev close: {prev_close}")
print(f"Day open:   {day_open}")
print(f"Gap:        {(day_open - prev_close) / prev_close * 100:+.2f}%")
print()

header = f"{'Time':8} {'Close':>10} {'vs Open':>10} {'vs Prev':>10}"
print(header)
print("-" * len(header))

day_high = 0
day_low = 99999
reversal_time = None

for c in candles:
    dt = get_dt(c)
    t = dt.strftime("%H:%M")
    day_high = max(day_high, c["high"])
    day_low = min(day_low, c["low"])
    vs_open = (c["close"] - day_open) / day_open * 100
    vs_prev = (c["close"] - prev_close) / prev_close * 100
    marker = ""
    if c["close"] < day_open and reversal_time is None:
        reversal_time = t
        marker = " ← crossed below open"
    if c["close"] < prev_close and marker == "":
        marker = " ← below prev close"
    print(f"{t:8} {c['close']:10.1f} {vs_open:+9.2f}% {vs_prev:+9.2f}%{marker}")

print(f"\nDay high: {day_high:.1f} | Day low: {day_low:.1f}")
print(f"Final:    {candles[-1]['close']:.1f} ({(candles[-1]['close'] - prev_close) / prev_close * 100:+.2f}%)")
if reversal_time:
    print(f"\nNIFTY crossed BELOW open at: {reversal_time}")

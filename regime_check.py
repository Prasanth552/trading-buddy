from src.broker.upstox_data import UpstoxData, load_cached_token
from datetime import datetime
from dateutil.parser import parse as dtparse
import config

token = load_cached_token()
ud = UpstoxData(access_token=token)
nifty_key = config.UPSTOX_INDEX_KEYS.get("NSE:NIFTY 50")

today = datetime(2026, 10, 21)
from_dt = today.replace(hour=9, minute=15)
to_dt = today.replace(hour=9, minute=35)
candles = ud.historical_data(nifty_key, from_dt, to_dt, "1minute")

def get_dt(c):
    d = c.get("date") or c.get("timestamp")
    return dtparse(str(d)) if isinstance(d, str) else d

# First 15-min candle (09:15-09:29)
first15 = [c for c in candles if get_dt(c).minute < 30 and get_dt(c).hour == 9]
open_price = first15[0]["open"]
close_15 = first15[-1]["close"]
high_15 = max(c["high"] for c in first15)
low_15 = min(c["low"] for c in first15)
range_pts = high_15 - low_15
range_pct = range_pts / open_price * 100
direction = "BULLISH" if close_15 > open_price else "BEARISH"
print(f"\n1) First 15-min candle: open={open_price} close={close_15} range={range_pts:.0f}pts ({range_pct:.2f}%)")
print(f"   Direction: {direction}, Range > 0.24%: {range_pct > 0.24}")
score1 = 1 if close_15 > open_price else -1
if range_pct < 0.24:
    score1 = 0
    print("   Score: 0 (range too small, ignored)")

# Previous close (Oct 20)
prev_dt = datetime(2026, 10, 20)
prev_candles = ud.historical_data(nifty_key, prev_dt.replace(hour=15, minute=25), prev_dt.replace(hour=15, minute=31), "1minute")
prev_close = prev_candles[-1]["close"] if prev_candles else None
nifty_930 = close_15
print(f"\n2) NIFTY vs previous close:")
print(f"   Previous close: {prev_close}")
if prev_close:
    change_pct = (nifty_930 - prev_close) / prev_close * 100
    print(f"   NIFTY at 09:30: {nifty_930}, change: {change_pct:+.2f}%")
    print(f"   > 0.5% move: {abs(change_pct) > 0.5}")
    score2 = 1 if change_pct > 0.5 else (-1 if change_pct < -0.5 else 0)
else:
    score2 = 0

# VWAP at 09:30
cum_vol = 0
cum_tp_vol = 0
for c in first15:
    tp = (c["high"] + c["low"] + c["close"]) / 3
    v = c.get("volume", 0) or 1
    cum_vol += v
    cum_tp_vol += tp * v
vwap = cum_tp_vol / cum_vol if cum_vol else 0
print(f"\n3) NIFTY vs VWAP at 09:30:")
print(f"   VWAP: {vwap:.1f}, NIFTY: {nifty_930}")
print(f"   Price vs VWAP: {'ABOVE' if nifty_930 > vwap else 'BELOW'}")
score3 = 1 if nifty_930 > vwap else -1

total = score1 + score2 + score3
print(f"\n{'='*30}")
print(f"Signal 1 (15-min candle): {score1:+d}")
print(f"Signal 2 (vs prev close): {score2:+d}")
print(f"Signal 3 (vs VWAP):       {score3:+d}")
print(f"TOTAL SCORE: {total:+d}")
if total >= 2:
    print("VERDICT: BULLISH → run OEL only")
elif total <= -2:
    print("VERDICT: BEARISH → run OEH only")
else:
    print("VERDICT: NEUTRAL → skip both OEH and OEL")

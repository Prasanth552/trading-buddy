"""Check what happens to the EQUITY price after MOM signal candles."""
from src.storage.db import get_conn
from src.broker.upstox_data import UpstoxData, load_cached_token
from datetime import datetime, timedelta
from dateutil.parser import parse as dtparse
import config

token = load_cached_token()
ud = UpstoxData(access_token=token)

with get_conn() as conn:
    rows = conn.execute('''SELECT id, symbol, side, qty, price, exit_price, pnl, status, peak_price, ts, broker_key
        FROM trades WHERE channel='mom' AND date(ts)='2026-10-21' ORDER BY id''').fetchall()

print(f"Analyzing equity follow-through for {len(rows)} MOM trades\n")
print(f"{'ID':<5} {'Time':5} {'Symbol':15} {'CE/PE':5} {'SigCandle':>10} {'Next5m':>8} {'Next10m':>9} {'Next15m':>9} {'Peak':>7} {'ActPnL':>7}")
print("-" * 100)

fno_list = config.FNO_STOCK_LIST if hasattr(config, 'FNO_STOCK_LIST') else None
instrument_keys = config.UPSTOX_INSTRUMENT_KEYS if hasattr(config, 'UPSTOX_INSTRUMENT_KEYS') else {}

for r in rows:
    tid, sym, side, qty, entry, exit_p, pnl, status, peak_pnl, ts, bkey = r

    # Parse the symbol to get stock name and CE/PE
    parts = sym.split()
    stock = parts[0]
    opt_type = parts[-1] if len(parts) >= 3 else "?"

    entry_time = dtparse(str(ts))

    # Get equity key
    eq_key = instrument_keys.get(f"NSE_EQ:{stock}") or f"NSE_EQ|{stock}"

    # Fetch equity candles around entry time
    from_dt = entry_time - timedelta(minutes=10)
    to_dt = entry_time + timedelta(minutes=20)

    try:
        candles = ud.historical_data(eq_key, from_dt, to_dt, "1minute")
        if not candles:
            print(f"{tid:<5} {entry_time.strftime('%H:%M'):5} {stock:15} {opt_type:5} {'no data':>10}")
            continue

        # Find the signal candle (the one just before or at entry time)
        entry_min = entry_time.strftime("%H:%M")

        # Get prices at entry time and after
        prices = []
        for c in candles:
            ct = dtparse(str(c.get('date') or c.get('timestamp')))
            prices.append((ct, c['close'], c['open'], c['high'], c['low']))

        # Find signal candle (closest before entry)
        signal_idx = None
        for i, (ct, cl, op, hi, lo) in enumerate(prices):
            if ct <= entry_time:
                signal_idx = i

        if signal_idx is None:
            print(f"{tid:<5} {entry_time.strftime('%H:%M'):5} {stock:15} {opt_type:5} {'no signal':>10}")
            continue

        sig_close = prices[signal_idx][1]
        sig_open = prices[signal_idx][2]
        sig_body = (sig_close - sig_open) / sig_open * 100

        # Get prices 5, 10, 15 min after signal
        def get_future(mins):
            target = prices[signal_idx][0] + timedelta(minutes=mins)
            for ct, cl, op, hi, lo in prices:
                if ct >= target:
                    chg = (cl - sig_close) / sig_close * 100
                    return chg
            return None

        m5 = get_future(5)
        m10 = get_future(10)
        m15 = get_future(15)

        # For PE, negative equity move = good
        direction = "bearish" if opt_type == "PE" else "bullish"

        print(f"{tid:<5} {entry_time.strftime('%H:%M'):5} {stock:15} {opt_type:5} {sig_body:>+9.2f}% {m5 or 0:>+7.2f}% {m10 or 0:>+8.2f}% {m15 or 0:>+8.2f}% {peak_pnl or 0:>+7.0f} {pnl or 0:>+7.0f}")

    except Exception as e:
        print(f"{tid:<5} {entry_time.strftime('%H:%M'):5} {stock:15} {opt_type:5} ERROR: {e}")

import time
time.sleep(1)  # rate limit

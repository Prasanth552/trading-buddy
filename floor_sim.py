"""Simulate floor-based exits for today's MOM trades using option premium tick data."""
from src.storage.db import get_conn
from src.broker.upstox_data import UpstoxData, load_cached_token
from datetime import datetime, timedelta
from dateutil.parser import parse as dtparse
import config

token = load_cached_token()
ud = UpstoxData(access_token=token)

CURRENT_FLOORS = [500, 1000, 1500, 2000, 3500, 5000, 6000]
NEW_FLOORS = [200, 400, 700, 1000, 1500, 2000, 3500]

with get_conn() as conn:
    rows = conn.execute('''SELECT id, symbol, side, qty, price, exit_price, pnl, status,
        stop_price, peak_price, ts, broker_key
        FROM trades WHERE channel='mom' AND date(ts)='2026-10-21' ORDER BY id''').fetchall()

def simulate_floor_exit(candles, entry_price, qty, sl_price, floors, charges_pct=0.006):
    """Walk through 1-min candles and simulate floor-based exit."""
    peak_pnl = 0
    current_floor = 0  # no floor yet

    for c in candles:
        ltp = c["close"]
        gross = (ltp - entry_price) * qty
        charges = abs(gross) * charges_pct
        net = gross - charges

        if net > peak_pnl:
            peak_pnl = net

        # Check which floor we've reached
        new_floor = 0
        for f in sorted(floors, reverse=True):
            if peak_pnl >= f:
                new_floor = f
                break
        if new_floor > current_floor:
            current_floor = new_floor

        # Exit if net drops TO or BELOW current floor (matches real logic line 4438)
        if current_floor > 0 and net <= current_floor:
            return net, "FLOOR", peak_pnl, current_floor

        # SL check
        if ltp <= sl_price:
            return net, "SL", peak_pnl, current_floor

    # End of data — still open
    ltp = candles[-1]["close"]
    gross = (ltp - entry_price) * qty
    charges = abs(gross) * charges_pct
    net = gross - charges
    return net, "EOD", peak_pnl, current_floor

print(f"Simulating floor exits for {len(rows)} MOM trades")
print(f"Current floors: {CURRENT_FLOORS}")
print(f"New floors:     {NEW_FLOORS}")
print()

header = f"{'ID':<5} {'Symbol':17} {'Qty':>5} {'Entry':>7} {'ActPnL':>8} {'Peak':>7} | {'CurFloor':>9} {'CurExit':>8} | {'NewFloor':>9} {'NewExit':>8} | {'Diff':>7}"
print(header)
print("-" * len(header))

total_actual = 0
total_cur = 0
total_new = 0
errors = 0

for r in rows:
    tid, sym, side, qty, entry, exit_p, pnl, status, sl, peak_pnl_db, ts, bkey = r
    actual = pnl or 0
    total_actual += actual

    # Get option broker_key to fetch option candles
    if not bkey:
        print(f"{tid:<5} {sym:17} — no broker_key, skipping")
        errors += 1
        continue

    entry_time = dtparse(str(ts))

    # Fetch 1-min option candles from entry to 15:30
    try:
        from_dt = entry_time - timedelta(minutes=1)
        to_dt = entry_time.replace(hour=15, minute=30)

        # Try both historical and intraday endpoints
        candles = ud.historical_data(bkey, from_dt, to_dt, "1minute")
        if not candles:
            # Try intraday
            candles = ud.intraday_candles(bkey, "1minute") if hasattr(ud, 'intraday_candles') else []

        if not candles or len(candles) < 2:
            print(f"{tid:<5} {sym:17} — no candle data ({len(candles) if candles else 0})")
            errors += 1
            continue

        # Filter candles after entry
        post_entry = []
        for c in candles:
            ct = dtparse(str(c.get("date") or c.get("timestamp")))
            if ct >= entry_time:
                post_entry.append(c)

        if not post_entry:
            print(f"{tid:<5} {sym:17} — no post-entry candles")
            errors += 1
            continue

        # Simulate with current floors
        cur_pnl, cur_reason, cur_peak, cur_fl = simulate_floor_exit(
            post_entry, entry, qty, sl or 0, CURRENT_FLOORS)

        # Simulate with new floors
        new_pnl, new_reason, new_peak, new_fl = simulate_floor_exit(
            post_entry, entry, qty, sl or 0, NEW_FLOORS)

        total_cur += cur_pnl
        total_new += new_pnl

        diff = new_pnl - cur_pnl

        print(f"{tid:<5} {sym:17} {qty:>5} {entry:>7.2f} {actual:>+8.0f} {cur_peak:>+7.0f} | {cur_fl:>5}→{cur_reason:4} {cur_pnl:>+8.0f} | {new_fl:>5}→{new_reason:4} {new_pnl:>+8.0f} | {diff:>+7.0f}")

    except Exception as e:
        print(f"{tid:<5} {sym:17} — ERROR: {e}")
        errors += 1

print("-" * len(header))
print(f"{'TOTALS':23} {'':>5} {'':>7} {total_actual:>+8.0f} {'':>7} | {'':>9} {total_cur:>+8.0f} | {'':>9} {total_new:>+8.0f} | {total_new - total_cur:>+7.0f}")
print(f"\nErrors/skipped: {errors}")
print(f"\nActual PnL:         {total_actual:>+10,.0f}")
print(f"Sim current floors: {total_cur:>+10,.0f}")
print(f"Sim new floors:     {total_new:>+10,.0f}")
print(f"Improvement:        {total_new - total_cur:>+10,.0f}")

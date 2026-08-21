#!/usr/bin/env python3
"""Analyze today's CH2 trades from DB against actual candle data.

Uses the REAL instrument keys and entry times from the trades DB,
fetches 5-min candles, and walks them to verify outcomes.

Usage: .venv/bin/python3 scripts/analyze_ch2_db.py [--date 2026-08-21]
"""
import sys, os, re, time as _time, argparse, sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config
    from src.broker.upstox_data import UpstoxData, load_cached_token
except ImportError:
    print("ERROR: Run from Trading-Buddy root with .venv/bin/python3")
    sys.exit(1)

IST = ZoneInfo("Asia/Kolkata")

parser = argparse.ArgumentParser()
parser.add_argument("--date", default=None, help="Date YYYY-MM-DD (default: today)")
args = parser.parse_args()

target_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")

# --- Load trades from DB ---
db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "trading_buddy.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT id, ts, symbol, side, qty, price, status, exit_price, pnl,
           stop_price, target_price, broker_key, channel, charges
    FROM trades
    WHERE ts >= ? AND ts < ? AND channel = 'ch2'
    ORDER BY ts
""", (f"{target_date}T00:00:00", f"{target_date}T23:59:59")).fetchall()

print(f"CH2 trades from DB: {len(rows)}")
if not rows:
    sys.exit(0)

# --- Init Upstox ---
token = load_cached_token()
if not token:
    print("ERROR: No Upstox token")
    sys.exit(1)

client = UpstoxData()

print()
print("=" * 140)
print(f"CH2 SIGNAL ANALYSIS — {target_date} — DB trades vs Actual Candles")
print("=" * 140)
print()

hdr = (f"  {'#':<4} {'Time':<6} {'Symbol':<22} {'Entry':>7} {'SL':>7} {'TGT':>7} "
       f"{'CandleO':>7} {'High':>7} {'Low':>7} "
       f"{'DB Result':<12} {'DB P&L':>8} {'Candle Result':<14} {'Candle P&L':>9}")
print(hdr)
print("  " + "─" * 138)

total_db_pnl = 0
total_candle_pnl = 0
db_wins = 0
db_losses = 0
candle_wins = 0
candle_losses = 0

for row in rows:
    trade_id = row["id"]
    ts = row["ts"]
    symbol = row["symbol"]
    entry = row["price"]
    sl = row["stop_price"]
    tgt = row["target_price"]
    qty = row["qty"]
    db_status = row["status"]
    db_exit = row["exit_price"]
    db_pnl = row["pnl"]
    broker_key = row["broker_key"]
    charges = row["charges"] or 0

    total_db_pnl += db_pnl

    if "CLOSED" in db_status and db_pnl >= 0:
        db_wins += 1
    else:
        db_losses += 1

    # Parse entry time
    entry_time = ts[11:16]  # HH:MM
    entry_h = int(entry_time[:2])
    entry_m = int(entry_time[3:5])

    # Fetch 5-min candles from entry time to EOD using ACTUAL broker key
    dt_parts = [int(x) for x in target_date.split("-")]
    from_dt = datetime(dt_parts[0], dt_parts[1], dt_parts[2], entry_h, entry_m, 0, tzinfo=IST)
    to_dt = datetime(dt_parts[0], dt_parts[1], dt_parts[2], 23, 30, 0, tzinfo=IST)

    candles = None
    for interval in ("5minute", "15minute", "day"):
        try:
            candles = client.historical_data(broker_key, from_dt, to_dt, interval)
            _time.sleep(0.25)
        except Exception:
            _time.sleep(0.5)
            continue
        if candles:
            break

    if not candles:
        print(f"  {trade_id:<4} {entry_time:<6} {symbol:<22} {entry:>7.1f} {sl:>7.1f} {tgt:>7.1f} "
              f"{'':>7} {'':>7} {'':>7} "
              f"{db_status:<12} {db_pnl:>+8.0f} {'NO_DATA':<14}")
        continue

    # Filter candles at/after entry time
    filtered = [c for c in candles if c["date"][11:16] >= entry_time]
    if not filtered:
        filtered = candles

    first_candle = filtered[0]
    candle_open = first_candle["open"]

    # Walk candles from entry to check TGT/SL
    max_high = 0
    min_low = 999999
    c_result = "OPEN"
    c_exit = candle_open

    for c in filtered:
        max_high = max(max_high, c["high"])
        min_low = min(min_low, c["low"])

        tgt_hit = c["high"] >= tgt if tgt else False
        sl_hit = c["low"] <= sl if sl else False

        if tgt_hit and sl_hit:
            # Both in same candle — mark uncertain
            c_result = "UNCERTAIN"
            # Use whichever is closer to open to guess direction
            tgt_dist = abs(c["open"] - tgt)
            sl_dist = abs(c["open"] - sl)
            if tgt_dist < sl_dist:
                c_exit = tgt
                c_result = "TGT?"
            else:
                c_exit = sl
                c_result = "SL?"
            break
        elif tgt_hit:
            c_result = "TGT"
            c_exit = tgt
            break
        elif sl_hit:
            c_result = "SL"
            c_exit = sl
            break

    if c_result == "OPEN":
        c_exit = filtered[-1]["close"]
        c_result = "EOD"

    c_pnl = (c_exit - entry) * qty - charges
    total_candle_pnl += c_pnl

    if c_pnl >= 0:
        candle_wins += 1
    else:
        candle_losses += 1

    # Match indicator
    db_dir = "W" if db_pnl >= 0 else "L"
    c_dir = "W" if c_pnl >= 0 else "L"
    match = "✓" if db_dir == c_dir else "✗"

    print(f"  {trade_id:<4} {entry_time:<6} {symbol:<22} {entry:>7.1f} {sl:>7.1f} {tgt:>7.1f} "
          f"{candle_open:>7.1f} {max_high:>7.1f} {min_low:>7.1f} "
          f"{db_status:<12} {db_pnl:>+8.0f} {c_result:<14} {c_pnl:>+9.0f} {match}")

# --- Summary ---
print()
print("=" * 140)
print(f"  SUMMARY — {target_date}")
print("=" * 140)
print()
print(f"  Total trades:     {len(rows)}")
print()
print(f"  DB Results:       {db_wins}W / {db_losses}L  ({db_wins/(db_wins+db_losses)*100:.0f}% WR)")
print(f"  DB Total P&L:     ₹{total_db_pnl:+,.0f}")
print()
print(f"  Candle Results:   {candle_wins}W / {candle_losses}L  ({candle_wins/(candle_wins+candle_losses)*100:.0f}% WR)")
print(f"  Candle Total P&L: ₹{total_candle_pnl:+,.0f}")
print()
print(f"  Avg DB P&L:       ₹{total_db_pnl/len(rows):+,.0f}")
print(f"  Avg Candle P&L:   ₹{total_candle_pnl/len(rows):+,.0f}")
print()
print("  DB = actual bot trades. Candle = 5-min candle walk verification.")
print("  ✓ = DB and candle agree on W/L. ✗ = disagree (candle too coarse).")
print("=" * 140)

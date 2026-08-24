#!/usr/bin/env python3
"""Analyze trades with fixed ₹ profit target per trade using actual candle flow.

For each DB trade: fetch 5-min candles, walk from entry, exit at fixed ₹ target or SL.

Usage: .venv/bin/python3 scripts/analyze_fixed_target.py --date 2026-08-24 --target 1500 --lots 2
"""
import sys, os, time as _time, argparse, sqlite3
from datetime import datetime
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
parser.add_argument("--target", type=float, default=1500, help="Fixed profit target per trade in ₹ (default: 1500)")
parser.add_argument("--lots", type=int, default=2, help="Number of lots (default: 2)")
parser.add_argument("--channel", default="ch2", help="Channel (default: ch2)")
parser.add_argument("--daily-cap", type=float, default=0, help="Daily profit cap in ₹ (0=no cap)")
args = parser.parse_args()

target_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")
dt_parts = [int(x) for x in target_date.split("-")]
year, month, day = dt_parts

LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 40,
    "MIDCPNIFTY": 50, "CRUDEOIL": 100, "GOLD": 100, "SILVER": 30,
    "NATURALGAS": 250, "EICHERMOT": 150, "LODHA": 1000, "MFSL": 1600,
    "MUTHOOTFIN": 1000, "INDIGO": 300, "TRENT": 625, "PAYTM": 1600,
    "ABB": 250, "BSE": 250, "LT": 300, "TITAN": 375, "BRITANNIA": 200,
    "HAL": 300, "MCX": 900, "POLYCAB": 200, "PERSISTENT": 200,
    "APOLLOHOSP": 250, "BAJAJAUTO": 250, "CUMMINSIND": 400,
    "SIEMENS": 275, "PIIND": 300, "RADICO": 1200, "AMBER": 200,
    "MARUTI": 100, "KEI": 200, "DIXON": 200, "LTIM": 200,
    "HEROMOTOCO": 150, "BHARTIARTL": 475, "HINDALCO": 1500,
    "ULTRACEMCO": 100, "MANAPPURAM": 4000, "LTF": 2816, "CANBK": 2700,
}
DEFAULT_LOT = 400

db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "trading_buddy.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT id, ts, symbol, side, qty, price, exit_price, pnl, status,
           stop_price, target_price, broker_key, channel, charges
    FROM trades WHERE ts >= ? AND ts < ? AND channel=?
    ORDER BY ts
""", (f"{target_date}T00:00:00", f"{target_date}T23:59:59", args.channel)).fetchall()

print(f"DB trades: {len(rows)} ({args.channel.upper()}, {target_date})")
if not rows:
    sys.exit(0)

token = load_cached_token()
if not token:
    print("ERROR: No Upstox token"); sys.exit(1)
client = UpstoxData()

import re

def get_base_symbol(symbol):
    m = re.match(r"([A-Z]+)", symbol.upper().replace(" ", ""))
    return m.group(1) if m else symbol.upper()

print()
print("=" * 160)
print(f"FIXED TARGET ANALYSIS — {target_date} — {args.channel.upper()} — ₹{args.target:,.0f}/trade target, {args.lots} lots")
print("=" * 160)
print()
print(f"  {'#':<4} {'Time':<6} {'Symbol':<24} {'Entry':>7} {'SL':>7} {'ChTGT':>7} "
      f"{'Qty':>5} {'NeedPts':>7} {'MyTGT':>7} "
      f"{'Peak':>7} {'Low':>7} {'HitTGT':>6} {'HitSL':>5} "
      f"{'Exit':>7} {'Result':<8} {'P&L':>8} {'DB P&L':>8}")
print("  " + "─" * 158)

wins = losses = nodata = 0
total_pnl = 0
total_db_pnl = 0
day_pnl = 0

for row in rows:
    trade_id = row["id"]
    ts = row["ts"]
    symbol = row["symbol"]
    entry = row["price"]
    sl = row["stop_price"]
    ch_tgt = row["target_price"]
    broker_key = row["broker_key"]
    db_pnl = row["pnl"] or 0
    db_status = row["status"] or ""
    total_db_pnl += db_pnl

    entry_time = (ts or "")[11:16]
    entry_h = int(entry_time[:2]) if entry_time else 9
    entry_m = int(entry_time[3:5]) if entry_time else 15

    base_sym = get_base_symbol(symbol)
    lot_size = LOT_SIZES.get(base_sym, DEFAULT_LOT)
    is_index = base_sym in ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY")
    trade_lots = 3 if is_index else args.lots
    qty = lot_size * trade_lots
    need_pts = args.target / qty

    my_tgt = entry + need_pts

    # Daily cap check
    if args.daily_cap > 0 and day_pnl >= args.daily_cap:
        print(f"  {trade_id:<4} {entry_time:<6} {symbol:<24} {entry:>7.1f} {sl:>7.1f} {ch_tgt:>7.1f} "
              f"{qty:>5} {need_pts:>7.1f} {my_tgt:>7.1f} "
              f"{'':>7} {'':>7} {'':>6} {'':>5} "
              f"{'':>7} {'CAP_HIT':<8} {'':>8} {db_pnl:>+8,.0f}")
        continue

    # Fetch candles
    from_dt = datetime(year, month, day, 9, 15, 0, tzinfo=IST)
    if base_sym in ("CRUDEOIL", "CRUDE", "GOLD", "SILVER", "NATURALGAS"):
        to_dt = datetime(year, month, day, 23, 30, 0, tzinfo=IST)
    else:
        to_dt = datetime(year, month, day, 15, 30, 0, tzinfo=IST)

    candles = None
    for interval in ("5minute", "15minute"):
        try:
            candles = client.historical_data(broker_key, from_dt, to_dt, interval)
            _time.sleep(0.25)
        except Exception:
            _time.sleep(0.5)
            continue
        if candles:
            break

    if not candles:
        print(f"  {trade_id:<4} {entry_time:<6} {symbol:<24} {entry:>7.1f} {sl:>7.1f} {ch_tgt:>7.1f} "
              f"{qty:>5} {need_pts:>7.1f} {my_tgt:>7.1f} "
              f"{'':>7} {'':>7} {'':>6} {'':>5} "
              f"{'':>7} {'NO_DATA':<8} {'':>8} {db_pnl:>+8,.0f}")
        nodata += 1
        continue

    # Filter candles from entry time
    filtered = [c for c in candles if c["date"][11:16] >= entry_time]
    if not filtered:
        filtered = candles

    # Walk candles with MY target
    max_high = 0
    min_low = 999999
    result = "OPEN"
    exit_price = entry
    tgt_hit = False
    sl_hit = False
    hit_tgt_time = ""
    hit_sl_time = ""

    for c in filtered:
        max_high = max(max_high, c["high"])
        min_low = min(min_low, c["low"])

        t_hit = c["high"] >= my_tgt
        s_hit = c["low"] <= sl

        if t_hit and s_hit:
            # Both in same candle — conservative: assume SL
            result = "BOTH"
            exit_price = sl
            break
        elif t_hit:
            result = "TGT"
            exit_price = my_tgt
            tgt_hit = True
            hit_tgt_time = c["date"][11:16]
            break
        elif s_hit:
            result = "SL"
            exit_price = sl
            sl_hit = True
            hit_sl_time = c["date"][11:16]
            break

    if result == "OPEN":
        exit_price = filtered[-1]["close"]
        result = "EOD"

    pnl = (exit_price - entry) * qty

    if pnl >= 0:
        wins += 1
        icon = "W"
    else:
        losses += 1
        icon = "L"

    total_pnl += pnl
    day_pnl += pnl

    tgt_str = "Y" if tgt_hit else ("?" if result == "BOTH" else "")
    sl_str = "Y" if sl_hit else ("?" if result == "BOTH" else "")

    print(f"  {trade_id:<4} {entry_time:<6} {symbol:<24} {entry:>7.1f} {sl:>7.1f} {ch_tgt:>7.1f} "
          f"{qty:>5} {need_pts:>7.1f} {my_tgt:>7.1f} "
          f"{max_high:>7.1f} {min_low:>7.1f} {tgt_str:>6} {sl_str:>5} "
          f"{exit_price:>7.1f} [{icon}] {result:<5} {pnl:>+8,.0f} {db_pnl:>+8,.0f}")

# Summary
print()
print("=" * 160)
print(f"  SUMMARY — {target_date} — {args.channel.upper()}")
print("=" * 160)
print()
total = wins + losses
if total > 0:
    print(f"  Trades:           {total} (+ {nodata} no data)")
    print(f"  Winners:          {wins}")
    print(f"  Losers:           {losses}")
    print(f"  Win Rate:         {wins}/{total} = {wins/total*100:.0f}%")
    print()
    print(f"  Fixed TGT P&L:    ₹{total_pnl:+,.0f}  (₹{args.target:,.0f}/trade, {args.lots} lots / 3 lots for index)")
    print(f"  Actual DB P&L:    ₹{total_db_pnl:+,.0f}  (channel targets, 3 lots)")
    print(f"  Difference:       ₹{total_pnl - total_db_pnl:+,.0f}")
    print()
    print(f"  Avg P&L/trade:    ₹{total_pnl/total:+,.0f}")
print()
print(f"  Strategy: Exit at ₹{args.target:,.0f} profit or SL hit, whichever first")
print(f"  BOTH = my TGT and SL hit in same 5-min candle → assume SL (conservative)")
print("=" * 160)

#!/usr/bin/env python3
"""Analyze CH1 trades with correct monthly (September) expiry contracts.

For each CH1 trade today: resolve to monthly expiry, fetch candles, walk with ₹1500 target.

Usage: .venv/bin/python3 scripts/analyze_ch1_monthly.py --date 2026-08-24
"""
import sys, os, re, time as _time, argparse, sqlite3, calendar
from datetime import datetime, date
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config
    from src.broker.upstox_data import UpstoxData, load_cached_token
    from src.broker.upstox_client import _expiry_to_date
except ImportError:
    print("ERROR: Run from Trading-Buddy root with .venv/bin/python3")
    sys.exit(1)

IST = ZoneInfo("Asia/Kolkata")

parser = argparse.ArgumentParser()
parser.add_argument("--date", default=None)
parser.add_argument("--target", type=float, default=1500)
parser.add_argument("--lots", type=int, default=1)
args = parser.parse_args()

target_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")
dt_parts = [int(x) for x in target_date.split("-")]
year, month, day = dt_parts
today = date(year, month, day)

# Load DB trades
db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "trading_buddy.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT id, ts, symbol, price, stop_price, target_price, exit_price, pnl, status, broker_key, qty
    FROM trades WHERE ts >= ? AND ts < ? AND channel='ch1' ORDER BY ts
""", (f"{target_date}T00:00:00", f"{target_date}T23:59:59")).fetchall()

print(f"CH1 trades from DB: {len(rows)}")
if not rows:
    sys.exit(0)

# Load instruments
token = load_cached_token()
if not token:
    print("ERROR: No Upstox token"); sys.exit(1)

client = UpstoxData()
master = client._load_master()

def is_monthly(exp_date):
    last_day = calendar.monthrange(exp_date.year, exp_date.month)[1]
    return exp_date.day >= last_day - 7

def resolve_monthly(symbol_str):
    """Parse 'MUTHOOTFIN 3000 CE' and find monthly expiry instrument."""
    parts = symbol_str.strip().split()
    if len(parts) < 3:
        return None, None, None, None
    opt_type = parts[-1]  # CE or PE
    strike = float(parts[-2])
    sym = "".join(parts[:-2]).upper()

    candidates = []
    for inst in master:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO", "MCX_FO"):
            continue
        if inst.get("asset_symbol", "").upper() != sym:
            continue
        if inst.get("instrument_type") != opt_type:
            continue
        if abs(float(inst.get("strike_price", -1)) - strike) > 0.01:
            continue
        exp = _expiry_to_date(inst.get("expiry"))
        if exp is None or exp < today:
            continue
        candidates.append((exp, inst))

    if not candidates:
        return None, None, None, None

    # Pick monthly
    monthly_cands = [(e, i) for e, i in candidates if is_monthly(e)]
    if monthly_cands:
        monthly_cands.sort(key=lambda x: x[0])
        chosen = monthly_cands[0]
    else:
        candidates.sort(key=lambda x: x[0])
        chosen = candidates[-1]  # farthest expiry as fallback

    inst = chosen[1]
    exp = chosen[0]
    inst_key = inst.get("instrument_key")
    lot_size = int(inst.get("lot_size", 1)) or 1
    return inst_key, lot_size, exp, sym

LOT_SIZES = {
    "MUTHOOTFIN": 1000, "MANAPPURAM": 4000, "LTF": 2816, "CANBK": 2700,
    "NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20,
}

print()
print("=" * 170)
print(f"CH1 MONTHLY EXPIRY ANALYSIS — {target_date} — ₹{args.target:,.0f}/trade target, {args.lots} lot(s)")
print("=" * 170)
print()
print(f"  {'#':<4} {'Time':<6} {'Symbol':<24} {'WeekE':>7} {'MonthE':>7} {'SL':>7} {'ChTGT':>7} "
      f"{'Qty':>5} {'NeedPts':>7} {'MyTGT':>7} "
      f"{'Peak':>7} {'Low':>7} {'Result':<8} {'₹1.5K P&L':>10} {'DB P&L':>8} {'Expiry':<10}")
print("  " + "─" * 168)

wins = losses = nodata = 0
total_pnl = 0
total_db_pnl = 0

for row in rows:
    trade_id = row["id"]
    ts = row["ts"]
    symbol = row["symbol"]
    week_entry = row["price"]
    sl_raw = row["stop_price"]
    ch_tgt = row["target_price"]
    db_pnl = row["pnl"] or 0
    total_db_pnl += db_pnl
    entry_time = (ts or "")[11:16]
    entry_h = int(entry_time[:2]) if entry_time else 9
    entry_m = int(entry_time[3:5]) if entry_time else 15

    inst_key, lot_size, exp_date, base_sym = resolve_monthly(symbol)

    if not inst_key:
        print(f"  {trade_id:<4} {entry_time:<6} {symbol:<24} {week_entry:>7.1f} {'':>7} {sl_raw:>7.1f} {ch_tgt:>7.1f} "
              f"{'':>5} {'':>7} {'':>7} {'':>7} {'':>7} {'NO_INST':<8} {'':>10} {db_pnl:>+8,.0f}")
        nodata += 1
        continue

    qty = lot_size * args.lots
    need_pts = args.target / qty
    exp_str = exp_date.strftime("%d-%b") if exp_date else "?"

    # Fetch candles
    from_dt = datetime(year, month, day, 9, 15, 0, tzinfo=IST)
    to_dt = datetime(year, month, day, 15, 30, 0, tzinfo=IST)

    candles = None
    for interval in ("5minute", "15minute"):
        try:
            candles = client.historical_data(inst_key, from_dt, to_dt, interval)
            _time.sleep(0.3)
        except Exception:
            _time.sleep(0.5)
            continue
        if candles:
            break

    if not candles:
        print(f"  {trade_id:<4} {entry_time:<6} {symbol:<24} {week_entry:>7.1f} {'':>7} {sl_raw:>7.1f} {ch_tgt:>7.1f} "
              f"{qty:>5} {need_pts:>7.1f} {'':>7} {'':>7} {'':>7} {'NO_DATA':<8} {'':>10} {db_pnl:>+8,.0f} {exp_str}")
        nodata += 1
        continue

    # Filter candles from entry time
    filtered = [c for c in candles if c["date"][11:16] >= entry_time]
    if not filtered:
        filtered = candles

    month_entry = filtered[0]["open"]
    my_tgt = month_entry + need_pts

    # SL: use the channel's SL directly (it's for the monthly contract)
    sl = sl_raw

    # Walk candles
    max_high = 0
    min_low = 999999
    result = "OPEN"
    exit_price = month_entry

    for c in filtered:
        max_high = max(max_high, c["high"])
        min_low = min(min_low, c["low"])

        t_hit = c["high"] >= my_tgt
        s_hit = c["low"] <= sl

        if t_hit and s_hit:
            result = "BOTH"
            exit_price = sl  # conservative
            break
        elif t_hit:
            result = "TGT"
            exit_price = my_tgt
            break
        elif s_hit:
            result = "SL"
            exit_price = sl
            break

    if result == "OPEN":
        exit_price = filtered[-1]["close"]
        result = "EOD"

    pnl = (exit_price - month_entry) * qty

    if pnl >= 0:
        wins += 1
        icon = "W"
    else:
        losses += 1
        icon = "L"

    total_pnl += pnl

    print(f"  {trade_id:<4} {entry_time:<6} {symbol:<24} {week_entry:>7.1f} {month_entry:>7.1f} {sl:>7.1f} {ch_tgt:>7.1f} "
          f"{qty:>5} {need_pts:>7.2f} {my_tgt:>7.1f} "
          f"{max_high:>7.1f} {min_low:>7.1f} [{icon}] {result:<5} {pnl:>+10,.0f} {db_pnl:>+8,.0f} {exp_str}")

# Summary
print()
print("=" * 170)
print(f"  SUMMARY — {target_date} — CH1 (Monthly Expiry)")
print("=" * 170)
print()
total = wins + losses
if total > 0:
    print(f"  Trades:           {total} (+ {nodata} no data)")
    print(f"  Winners:          {wins}")
    print(f"  Losers:           {losses}")
    print(f"  Win Rate:         {wins}/{total} = {wins/total*100:.0f}%")
    print()
    print(f"  Monthly P&L:      ₹{total_pnl:+,.0f}  (₹{args.target:,.0f}/trade target, {args.lots} lot)")
    print(f"  Actual DB P&L:    ₹{total_db_pnl:+,.0f}  (wrong expiry)")
    print(f"  Difference:       ₹{total_pnl - total_db_pnl:+,.0f}")
print()
print(f"  WeekE = what bot bought (weekly expiry). MonthE = correct Sept expiry candle open.")
print(f"  SL/TGT are from the channel signal (designed for monthly contracts).")
print("=" * 170)

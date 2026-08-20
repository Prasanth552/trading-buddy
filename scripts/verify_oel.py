#!/usr/bin/env python3
"""Verify Open=Low (OEL) backtest with actual Upstox candle data.

Strategy: If a stock's Open == Low, buy at 9:15 open, sell at 3:30 close.
This script fetches real daily candles to verify the P&L.

Usage:
  .venv/bin/python3 scripts/verify_oel.py <csv_file> [--today-only] [--largecap-only]

CSV format: Date,Symbol,Marketcapname,Sector
"""
import sys, os, re, csv, io, time as _time, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    import config
    from src.broker.upstox_data import UpstoxData, load_cached_token
except ImportError:
    print("ERROR: Run from Trading-Buddy root with .venv/bin/python3")
    sys.exit(1)

IST = ZoneInfo("Asia/Kolkata")

parser = argparse.ArgumentParser()
parser.add_argument("csv_file", help="Path to OEL CSV")
parser.add_argument("--today-only", action="store_true", help="Only verify today's trades")
parser.add_argument("--largecap-only", action="store_true", help="Only largecap stocks")
parser.add_argument("--cap", type=float, default=100000, help="Capital per trade (default ₹1L)")
parser.add_argument("--max-trades", type=int, default=5, help="Max trades per day (default 5)")
parser.add_argument("--days", type=int, default=0, help="Last N days only (0=all)")
args = parser.parse_args()

# --- Read CSV ---
with open(args.csv_file, encoding="utf-8-sig") as f:
    raw = f.read().replace('"', '')

reader = csv.DictReader(io.StringIO(raw))
all_rows = list(reader)

# Filter out Indices/ETFs
rows = [r for r in all_rows if r["Sector"] != "Indices"]
if args.largecap_only:
    rows = [r for r in rows if r["Marketcapname"] == "Largecap"]

# Parse dates
for r in rows:
    r["_date"] = datetime.strptime(r["Date"], "%d-%m-%Y")

rows.sort(key=lambda r: r["_date"])

if args.today_only:
    today_str = datetime.now(IST).strftime("%d-%m-%Y")
    rows = [r for r in rows if r["Date"] == today_str]
    print(f"Filtering to today ({today_str}): {len(rows)} trades")
elif args.days > 0:
    cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(days=args.days)
    rows = [r for r in rows if r["_date"] >= cutoff]
    print(f"Filtering to last {args.days} days: {len(rows)} trades")

if not rows:
    print("No trades to verify!")
    sys.exit(0)

# Group by date, limit per day
by_date = defaultdict(list)
for r in rows:
    by_date[r["Date"]].append(r)

selected = []
for date in sorted(by_date.keys(), key=lambda d: datetime.strptime(d, "%d-%m-%Y")):
    day_rows = by_date[date]
    # Prefer largecap first
    day_rows.sort(key=lambda r: {"Largecap": 0, "Midcap": 1, "Smallcap": 2}.get(r["Marketcapname"], 3))
    selected.extend(day_rows[:args.max_trades])

rows = selected
print(f"\nTotal trades to verify: {len(rows)} ({len(by_date)} days, max {args.max_trades}/day)")

# --- Connect Upstox ---
token = load_cached_token()
if not token:
    print("ERROR: No Upstox token. Run auto-login.")
    sys.exit(1)

client = UpstoxData()
print("Loading instrument master...")
master = client._load_master()

# Build NSE_EQ lookup
eq_keys = {}
for inst in master:
    if inst.get("segment") == "NSE_EQ":
        tsym = (inst.get("trading_symbol") or "").upper()
        if tsym:
            eq_keys[tsym] = inst.get("instrument_key")

print(f"Master: {len(master)} instruments, {len(eq_keys)} NSE equities")

# --- Fetch and verify ---
candle_cache = {}


def get_daily_candle(symbol, date_str):
    """Fetch daily OHLC for a symbol on a specific date."""
    inst_key = eq_keys.get(symbol.upper())
    if not inst_key:
        return None, "NO_INST"

    cache_key = f"{inst_key}|{date_str}"
    if cache_key in candle_cache:
        return candle_cache[cache_key], "OK"

    dt = datetime.strptime(date_str, "%d-%m-%Y")
    from_dt = dt.replace(hour=9, minute=15)
    to_dt = dt.replace(hour=15, minute=35)

    try:
        candles = client.historical_data(inst_key, from_dt, to_dt, "day")
        _time.sleep(0.25)
    except Exception as e:
        err = str(e)
        if "429" in err or "rate" in err.lower():
            _time.sleep(2)
            try:
                candles = client.historical_data(inst_key, from_dt, to_dt, "day")
            except Exception:
                return None, "API_ERR"
        else:
            return None, "API_ERR"

    if candles:
        candle_cache[cache_key] = candles[0]
        return candles[0], "OK"
    return None, "NO_DATA"


print()
print("=" * 100)
print("OPEN = LOW STRATEGY VERIFICATION — ACTUAL CANDLE DATA")
print("=" * 100)
print(f"  Entry: Buy at 9:15 open price")
print(f"  Exit:  Sell at 3:30 close price")
print(f"  Capital per trade: ₹{args.cap:,.0f}")
print(f"  Max trades/day: {args.max_trades}")
print()

header = (f"  {'#':<4} {'Date':<12} {'Symbol':<15} {'Cap':<8} {'Open':>8} {'Close':>8} "
          f"{'Chg%':>7} {'P&L':>10} {'Verify'}")
print(header)
print("  " + "─" * 98)

daily_pnl = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0})
total_trades = 0
total_wins = 0
total_losses = 0
total_pnl = 0
no_data = 0
verified_oel = 0
not_oel = 0

for idx, r in enumerate(rows):
    symbol = r["Symbol"]
    date = r["Date"]
    cap = r["Marketcapname"][:4]

    candle, status = get_daily_candle(symbol, date)

    if candle is None:
        print(f"  {idx+1:<4} {date:<12} {symbol:<15} {cap:<8} {'':>8} {'':>8} "
              f"{'':>7} {'':>10} {status}")
        no_data += 1
        continue

    open_p = candle["open"]
    close_p = candle["close"]
    high_p = candle["high"]
    low_p = candle["low"]

    # Verify Open == Low
    oel_check = "OEL" if abs(open_p - low_p) < 0.05 else f"O≠L({low_p:.1f})"
    if abs(open_p - low_p) >= 0.05:
        not_oel += 1
    else:
        verified_oel += 1

    if open_p == 0:
        continue

    chg_pct = (close_p - open_p) / open_p * 100
    qty = int(args.cap / open_p)
    pnl = (close_p - open_p) * qty

    won = pnl > 0
    icon = "+" if won else "-"

    total_trades += 1
    total_pnl += pnl
    if won:
        total_wins += 1
    else:
        total_losses += 1

    d = daily_pnl[date]
    d["pnl"] += pnl
    d["trades"] += 1
    if won:
        d["wins"] += 1
    else:
        d["losses"] += 1

    print(f"  {idx+1:<4} {date:<12} {symbol:<15} {cap:<8} {open_p:>8.2f} {close_p:>8.2f} "
          f"{chg_pct:>+6.2f}% ₹{pnl:>+9,.0f} {oel_check} {icon}")

    if (idx + 1) % 50 == 0:
        print(f"\n  ... processed {idx+1}/{len(rows)} trades ...\n")

# --- Summary ---
print()
print("=" * 100)
print("RESULTS SUMMARY")
print("=" * 100)
print()
print(f"  Total trades verified:  {total_trades}")
print(f"  No data / skipped:      {no_data}")
print(f"  Open=Low confirmed:     {verified_oel}")
print(f"  Open≠Low (mismatch):    {not_oel}")
print()

if total_trades > 0:
    wr = total_wins / total_trades * 100
    print(f"  Winners:    {total_wins} ({wr:.0f}%)")
    print(f"  Losers:     {total_losses} ({100-wr:.0f}%)")
    print(f"  Total P&L:  ₹{total_pnl:+,.0f}")
    print(f"  Avg P&L/trade: ₹{total_pnl/total_trades:+,.0f}")
    print()

    n_days = len(daily_pnl)
    if n_days > 0:
        print(f"  Avg P&L/day: ₹{total_pnl/n_days:+,.0f}")
        print()

        print(f"  DAILY BREAKDOWN")
        print(f"  {'Date':<12} {'Trades':>7} {'Wins':>6} {'Loss':>6} {'Day P&L':>12}")
        print(f"  {'─'*12} {'─'*7} {'─'*6} {'─'*6} {'─'*12}")

        green = 0
        red = 0
        max_day = -999999
        min_day = 999999
        for date in sorted(daily_pnl.keys(), key=lambda d: datetime.strptime(d, "%d-%m-%Y")):
            d = daily_pnl[date]
            mark = "+" if d["pnl"] > 0 else "-"
            if d["pnl"] > 0:
                green += 1
            else:
                red += 1
            max_day = max(max_day, d["pnl"])
            min_day = min(min_day, d["pnl"])
            print(f"  {date:<12} {d['trades']:>7} {d['wins']:>6} {d['losses']:>6} "
                  f"₹{d['pnl']:>+11,.0f} {mark}")

        print()
        print(f"  Green days: {green}/{n_days} ({green/n_days*100:.0f}%)")
        print(f"  Red days:   {red}/{n_days} ({red/n_days*100:.0f}%)")
        print(f"  Best day:   ₹{max_day:+,.0f}")
        print(f"  Worst day:  ₹{min_day:+,.0f}")

print()
print("=" * 100)

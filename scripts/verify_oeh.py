#!/usr/bin/env python3
"""Verify Open=High (OEH) backtest with actual Upstox candle data.

Strategy: If a stock's Open == High at 9:15, it's bearish — stock opened at peak.
  - SHORT direction: pnl = (open - close), profits when stock falls
  - LONG direction:  pnl = (close - open), profits when stock rises

Default is SHORT (bearish thesis). Use --long to test buying instead.

Usage:
  .venv/bin/python3 scripts/verify_oeh.py <csv_file> [--largecap-only] [--days 30]
  .venv/bin/python3 scripts/verify_oeh.py <csv_file> --long   # test buying instead

CSV format: Date,Symbol,Marketcapname,Sector
"""
import sys, os, csv, io, time as _time, argparse
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
parser.add_argument("csv_file", help="Path to OEH CSV")
parser.add_argument("--today-only", action="store_true")
parser.add_argument("--largecap-only", action="store_true")
parser.add_argument("--long", action="store_true", help="Test BUY direction instead of SHORT")
parser.add_argument("--cap", type=float, default=100000, help="Capital per trade (default ₹1L)")
parser.add_argument("--max-trades", type=int, default=5, help="Max trades per day")
parser.add_argument("--days", type=int, default=0, help="Last N days only (0=all)")
parser.add_argument("--sector", type=str, default="", help="Filter by sector")
args = parser.parse_args()

direction = "LONG (BUY)" if args.long else "SHORT (bearish)"

# --- Read CSV ---
with open(args.csv_file, encoding="utf-8-sig") as f:
    raw = f.read().replace('"', '')

reader = csv.DictReader(io.StringIO(raw))
all_rows = list(reader)

rows = [r for r in all_rows if r["Sector"] != "Indices"]
if args.largecap_only:
    rows = [r for r in rows if r["Marketcapname"] == "Largecap"]
if args.sector:
    rows = [r for r in rows if args.sector.lower() in r["Sector"].lower()]

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

eq_keys = {}
for inst in master:
    if inst.get("segment") == "NSE_EQ":
        tsym = (inst.get("trading_symbol") or "").upper()
        if tsym:
            eq_keys[tsym] = inst.get("instrument_key")

print(f"Master: {len(master)} instruments, {len(eq_keys)} NSE equities")

# --- Fetch candles ---
candle_cache = {}


def get_daily_candle(symbol, date_str):
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


# --- Verify ---
print()
print("=" * 110)
print(f"OPEN = HIGH STRATEGY VERIFICATION — {direction}")
print("=" * 110)
if args.long:
    print(f"  Entry: BUY at 9:15 open price (testing if OEH stocks recover)")
    print(f"  Exit:  SELL at 3:30 close price")
else:
    print(f"  Entry: SHORT at 9:15 open price (OEH = bearish signal)")
    print(f"  Exit:  COVER at 3:30 close price")
print(f"  Capital per trade: ₹{args.cap:,.0f}")
print(f"  Max trades/day: {args.max_trades}")
print()

header = (f"  {'#':<4} {'Date':<12} {'Symbol':<15} {'Sector':<16} {'Open':>8} {'High':>8} "
          f"{'Low':>8} {'Close':>8} {'Chg%':>7} {'P&L':>10} {'Verify'}")
print(header)
print("  " + "-" * 108)

daily_pnl = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0})
total_trades = 0
total_wins = 0
total_losses = 0
total_pnl = 0
no_data = 0
verified_oeh = 0
not_oeh = 0
sector_pnl = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})

for idx, r in enumerate(rows):
    symbol = r["Symbol"]
    date = r["Date"]
    sector = r["Sector"][:14]

    candle, status = get_daily_candle(symbol, date)

    if candle is None:
        print(f"  {idx+1:<4} {date:<12} {symbol:<15} {sector:<16} {'':>8} {'':>8} "
              f"{'':>8} {'':>8} {'':>7} {'':>10} {status}")
        no_data += 1
        continue

    open_p = candle["open"]
    close_p = candle["close"]
    high_p = candle["high"]
    low_p = candle["low"]

    # Verify Open == High
    oeh_check = "OEH" if abs(open_p - high_p) < 0.05 else f"O!=H({high_p:.1f})"
    if abs(open_p - high_p) >= 0.05:
        not_oeh += 1
    else:
        verified_oeh += 1

    if open_p == 0:
        continue

    if args.long:
        pnl_per_share = close_p - open_p
    else:
        pnl_per_share = open_p - close_p

    chg_pct = (close_p - open_p) / open_p * 100
    qty = int(args.cap / open_p)
    pnl = pnl_per_share * qty

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

    s = sector_pnl[r["Sector"]]
    s["pnl"] += pnl
    s["trades"] += 1
    if won:
        s["wins"] += 1

    print(f"  {idx+1:<4} {date:<12} {symbol:<15} {sector:<16} {open_p:>8.2f} {high_p:>8.2f} "
          f"{low_p:>8.2f} {close_p:>8.2f} {chg_pct:>+6.2f}% ₹{pnl:>+9,.0f} {oeh_check} {icon}")

    if (idx + 1) % 50 == 0:
        print(f"\n  ... processed {idx+1}/{len(rows)} trades ...\n")

# --- Summary ---
print()
print("=" * 110)
print(f"RESULTS SUMMARY — {direction}")
print("=" * 110)
print()
print(f"  Total trades verified:  {total_trades}")
print(f"  No data / skipped:      {no_data}")
print(f"  Open=High confirmed:    {verified_oeh}")
print(f"  Open!=High (mismatch):  {not_oeh}")
print()

if total_trades > 0:
    wr = total_wins / total_trades * 100
    print(f"  Winners:       {total_wins} ({wr:.1f}%)")
    print(f"  Losers:        {total_losses} ({100-wr:.1f}%)")
    print(f"  Total P&L:     ₹{total_pnl:+,.0f}")
    print(f"  Avg P&L/trade: ₹{total_pnl/total_trades:+,.0f}")
    print()

    # Sector breakdown
    print(f"  SECTOR BREAKDOWN")
    print(f"  {'Sector':<22} {'Trades':>7} {'WR%':>6} {'Total P&L':>12} {'Avg P&L':>10}")
    print(f"  {'-'*22} {'-'*7} {'-'*6} {'-'*12} {'-'*10}")
    for sec in sorted(sector_pnl.keys(), key=lambda s: sector_pnl[s]["pnl"], reverse=True):
        s = sector_pnl[sec]
        sec_wr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
        print(f"  {sec:<22} {s['trades']:>7} {sec_wr:>5.0f}% ₹{s['pnl']:>+11,.0f} ₹{s['pnl']/s['trades']:>+9,.0f}")
    print()

    # Daily breakdown
    n_days = len(daily_pnl)
    if n_days > 0:
        print(f"  Avg P&L/day: ₹{total_pnl/n_days:+,.0f}")
        print()

        print(f"  DAILY BREAKDOWN")
        print(f"  {'Date':<12} {'Trades':>7} {'Wins':>6} {'Loss':>6} {'Day P&L':>12}")
        print(f"  {'-'*12} {'-'*7} {'-'*6} {'-'*6} {'-'*12}")

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
print("=" * 110)

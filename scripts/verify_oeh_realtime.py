#!/usr/bin/env python3
"""Real-time OEH backtest — what you'd ACTUALLY see at 9:30 AM.

Scans a universe of stocks with 5-min intraday candles.
At 9:30 AM (after 15 min), checks: has the stock's high stayed <= open?
If yes → short at 9:30 price, cover at 3:25 close.
Tracks stocks that break above open after 9:30 (losses the EOD backtest hides).

Usage:
  .venv/bin/python3 scripts/verify_oeh_realtime.py [--days 10] [--universe 50]
"""
import sys, os, time as _time, argparse
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
parser.add_argument("--days", type=int, default=10, help="Last N trading days (default 10)")
parser.add_argument("--universe", type=int, default=50, help="Number of stocks to scan (default 50)")
parser.add_argument("--max-trades", type=int, default=5, help="Max trades per day (default 5)")
parser.add_argument("--cap", type=float, default=100000, help="Capital per trade (default 1L)")
parser.add_argument("--wait-min", type=int, default=5, help="Minutes after open to check (default 5)")
parser.add_argument("--min-drop", type=float, default=0.3, help="Min drop %% to qualify (default 0.3)")
parser.add_argument("--blocklist", type=str, default="GODREJCP,GRASIM", help="Comma-separated blocklist")
parser.add_argument("--no-nifty-filter", action="store_true", help="Disable NIFTY trend filter")
args = parser.parse_args()

_BLOCKLIST = set(b.strip().upper() for b in args.blocklist.split(",") if b.strip())

NIFTY50 = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "BHARTIARTL",
    "SBIN", "ITC", "BAJFINANCE", "LT", "KOTAKBANK", "AXISBANK",
    "TITAN", "MARUTI", "SUNPHARMA", "HCLTECH", "WIPRO", "TATASTEEL",
    "ADANIENT", "CIPLA", "DRREDDY", "M&M", "ASIANPAINT", "HINDUNILVR",
    "NESTLEIND", "GRASIM", "ONGC", "ULTRACEMCO", "JSWSTEEL", "TRENT",
    "BAJAJFINSV", "VEDL", "HINDALCO", "BPCL", "HEROMOTOCO", "EICHERMOT",
    "TATAPOWER", "BEL", "NTPC", "POWERGRID", "COALINDIA", "PIDILITIND",
    "SHREECEM", "DABUR", "COLPAL", "GODREJCP", "AMBUJACEM", "BHEL",
    "DIVISLAB", "BRITANNIA",
]

EXTRA_FNO = [
    "LUPIN", "TORNTPHARM", "TATAELXSI", "RADICO", "KEI", "SRF",
    "FEDERALBNK", "LICHSGFIN", "MFSL", "BOSCHLTD", "CGPOWER",
    "HINDPETRO", "BLUESTARCO", "PATANJALI", "VOLTAS", "INDHOTEL",
    "CUMMINSIND", "PIIND", "COFORGE", "PERSISTENT", "JUBLFOOD",
    "MARICO", "IOC", "GAIL", "CONCOR", "ASHOKLEY", "MPHASIS",
    "AUROPHARMA", "OBEROIRLTY", "MANAPPURAM",
]

universe_list = NIFTY50 + EXTRA_FNO
universe = universe_list[:args.universe]
print(f"Universe: {len(universe)} stocks")

token = load_cached_token()
if not token:
    print("ERROR: No Upstox token.")
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

nifty_key = None
for inst in master:
    if inst.get("segment") == "NSE_INDEX" and "NIFTY 50" in (inst.get("name") or "").upper():
        nifty_key = inst.get("instrument_key")
        break
if not nifty_key:
    nifty_key = config.UPSTOX_INDEX_KEYS.get("NSE:NIFTY 50")

matched = [s for s in universe if s in eq_keys]
print(f"Matched {len(matched)}/{len(universe)} stocks in master")
print(f"NIFTY filter: {'ON' if nifty_key and not args.no_nifty_filter else 'OFF'}")

# Find last N trading days by scanning recent dates
today = datetime.now(IST).date()
trading_days = []
check_date = today - timedelta(days=1)
attempts = 0
while len(trading_days) < args.days and attempts < 180:
    if check_date.weekday() < 5:  # Mon-Fri
        trading_days.append(check_date)
    check_date -= timedelta(days=1)
    attempts += 1

trading_days.reverse()
print(f"Testing {len(trading_days)} trading days: {trading_days[0]} to {trading_days[-1]}")

check_time_min = args.wait_min
check_candle_count = check_time_min // 5  # 15 min = 3 candles

print(f"\nStrategy: at 9:{15+check_time_min:02d} AM, if stock's high hasn't crossed open → SHORT")
print(f"Entry: 9:{15+check_time_min:02d} price | Exit: 3:25 PM close")
print(f"Capital: ₹{args.cap:,.0f}/trade | Max {args.max_trades} trades/day")
print()


def fetch_intraday(symbol, date):
    inst_key = eq_keys.get(symbol)
    if not inst_key:
        return None

    from_dt = datetime.combine(date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(date, datetime.min.time()).replace(hour=15, minute=35)

    try:
        candles = client.historical_data(inst_key, from_dt, to_dt, "5minute")
        _time.sleep(0.3)
        return candles
    except Exception as e:
        err = str(e)
        if "429" in err or "rate" in err.lower():
            _time.sleep(3)
            try:
                candles = client.historical_data(inst_key, from_dt, to_dt, "5minute")
                return candles
            except Exception:
                pass
        return None


print("=" * 115)
print(f"REAL-TIME OEH BACKTEST — CHECK AT 9:{15+check_time_min:02d} AM")
print("=" * 115)
print()

header = (f"  {'#':<4} {'Date':<12} {'Symbol':<15} {'Open':>8} {'9:30 Hi':>8} "
          f"{'Entry':>8} {'Close':>8} {'Day Hi':>8} {'Chg%':>7} {'P&L':>10} {'Status'}")
print(header)
print("  " + "-" * 113)

daily_stats = defaultdict(lambda: {
    "candidates": 0, "traded": 0, "wins": 0, "losses": 0,
    "pnl": 0, "false_oeh": 0,
})
total_trades = 0
total_wins = 0
total_losses = 0
total_pnl = 0
total_scanned = 0
total_candidates = 0
total_false_oeh = 0
trade_num = 0
nifty_skipped = 0

for day in trading_days:
    day_candidates = []

    # NIFTY trend filter: skip day if NIFTY's first candle is green
    if nifty_key and not args.no_nifty_filter:
        nf_from = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=15)
        nf_to = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=25)
        try:
            nc = client.historical_data(nifty_key, nf_from, nf_to, "5minute")
            _time.sleep(0.3)
            if nc and len(nc) >= 1:
                if nc[0]["close"] > nc[0]["open"]:
                    print(f"  ---  {day.strftime('%d-%m-%Y')}  NIFTY GREEN (open={nc[0]['open']:.1f} → {nc[0]['close']:.1f}) — SKIPPED")
                    nifty_skipped += 1
                    continue
        except Exception:
            pass

    for sym in matched:
        if sym in _BLOCKLIST:
            continue
        total_scanned += 1
        candles = fetch_intraday(sym, day)
        if not candles or len(candles) < check_candle_count + 1:
            continue

        open_price = candles[0]["open"]
        if open_price <= 0:
            continue

        first_candles = candles[:check_candle_count]
        max_high_early = max(c["high"] for c in first_candles)

        if max_high_early > open_price + 0.05:
            continue

        entry_price = first_candles[-1]["close"]
        drop_pct = (open_price - entry_price) / open_price * 100

        if drop_pct < args.min_drop:
            continue

        total_candidates += 1

        exit_price = candles[-1]["close"]

        remaining = candles[check_candle_count:]
        max_high_after = max(c["high"] for c in remaining) if remaining else entry_price
        broke_above = max_high_after > open_price + 0.05

        day_high = max(c["high"] for c in candles)

        day_candidates.append({
            "symbol": sym,
            "open": open_price,
            "max_high_early": max_high_early,
            "entry": entry_price,
            "exit": exit_price,
            "day_high": day_high,
            "broke_above": broke_above,
            "drop_pct": drop_pct,
        })

    # Sort candidates: prefer ones that dropped most in first 15 min (strongest signal)
    day_candidates.sort(key=lambda x: x["drop_pct"], reverse=True)

    d = daily_stats[day.strftime("%d-%m-%Y")]
    d["candidates"] = len(day_candidates)

    for c in day_candidates[:args.max_trades]:
        trade_num += 1
        total_trades += 1
        d["traded"] += 1

        # SHORT P&L: entry - exit
        pnl_per_share = c["entry"] - c["exit"]
        qty = int(args.cap / c["entry"])
        pnl = pnl_per_share * qty
        chg_pct = (c["exit"] - c["entry"]) / c["entry"] * 100

        won = pnl > 0
        if won:
            total_wins += 1
            d["wins"] += 1
        else:
            total_losses += 1
            d["losses"] += 1

        if c["broke_above"]:
            total_false_oeh += 1
            d["false_oeh"] += 1

        total_pnl += pnl
        d["pnl"] += pnl

        status = ""
        if c["broke_above"]:
            status = f"BROKE ABOVE (hi={c['day_high']:.1f})"
        else:
            status = "TRUE OEH"

        icon = "+" if won else "X"

        print(f"  {trade_num:<4} {day.strftime('%d-%m-%Y'):<12} {c['symbol']:<15} "
              f"{c['open']:>8.2f} {c['max_high_early']:>8.2f} "
              f"{c['entry']:>8.2f} {c['exit']:>8.2f} {c['day_high']:>8.2f} "
              f"{chg_pct:>+6.2f}% ₹{pnl:>+9,.0f} {status} {icon}")

    if trade_num > 0 and trade_num % 25 == 0:
        print(f"\n  ... {trade_num} trades done, scanning {total_scanned} stock-days ...\n")

# --- Summary ---
print()
print("=" * 115)
print("RESULTS SUMMARY — REAL-TIME OEH (9:30 AM check)")
print("=" * 115)
print()
print(f"  Total stock-days scanned:   {total_scanned}")
print(f"  Days skipped (NIFTY green): {nifty_skipped}")
print(f"  OEH candidates at 9:{15+check_time_min:02d}:     {total_candidates} ({total_candidates/total_scanned*100:.1f}% of scans)" if total_scanned else "")
print(f"  Trades taken (max {args.max_trades}/day): {total_trades}")
print(f"  Broke above open after 9:{15+check_time_min:02d}: {total_false_oeh} ({total_false_oeh/total_trades*100:.1f}% false OEH)" if total_trades else "")
print()

if total_trades > 0:
    wr = total_wins / total_trades * 100
    print(f"  Winners:       {total_wins} ({wr:.1f}%)")
    print(f"  Losers:        {total_losses} ({100-wr:.1f}%)")
    print(f"  Total P&L:     ₹{total_pnl:+,.0f}")
    print(f"  Avg P&L/trade: ₹{total_pnl/total_trades:+,.0f}")
    print()

    n_days = len(daily_stats)
    if n_days > 0:
        print(f"  Avg P&L/day:   ₹{total_pnl/n_days:+,.0f}")
        print()

        print(f"  DAILY BREAKDOWN")
        print(f"  {'Date':<12} {'Cands':>6} {'Traded':>7} {'Wins':>6} {'Loss':>6} "
              f"{'False':>6} {'Day P&L':>12}")
        print(f"  {'-'*12} {'-'*6} {'-'*7} {'-'*6} {'-'*6} {'-'*6} {'-'*12}")

        green = 0
        red = 0
        for day in trading_days:
            ds = day.strftime("%d-%m-%Y")
            d = daily_stats[ds]
            mark = "+" if d["pnl"] > 0 else ("-" if d["pnl"] < 0 else "=")
            if d["pnl"] > 0:
                green += 1
            elif d["pnl"] < 0:
                red += 1
            print(f"  {ds:<12} {d['candidates']:>6} {d['traded']:>7} {d['wins']:>6} "
                  f"{d['losses']:>6} {d['false_oeh']:>6} ₹{d['pnl']:>+11,.0f} {mark}")

        print()
        print(f"  Green days: {green}/{n_days}")
        print(f"  Red days:   {red}/{n_days}")

print()
print("=" * 115)
print("NOTE: This is the REAL win rate. Unlike the EOD backtest, this only uses")
print("information available at 9:30 AM. Stocks that looked OEH at 9:30 but later")
print("broke above open are tracked as 'false OEH' — these are losses you'd take.")
print("=" * 115)

#!/usr/bin/env python3
"""Simulate OEH scan for a given date — what would have happened?

Scans OEH_UNIVERSE for Open=High, resolves ATM PEs, walks candles with
SL=30% and TGT=2x.

Usage: .venv/bin/python3 scripts/analyze_oeh_today.py [--date 2026-08-24]
"""
import sys, os, re, time as _time, argparse
from datetime import datetime, date, timedelta
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
parser.add_argument("--skip-filter", action="store_true", help="Skip NIFTY trend filter")
args = parser.parse_args()

target_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")
year, month, day = [int(x) for x in target_date.split("-")]
today_d = date(year, month, day)

OEH_TOLERANCE = 0.05
OEH_MIN_DROP_PCT = 0.3
OEH_SL_PCT = 0.30
OEH_TARGET_MULT = 2.0
OEH_MAX_TRADES = 5
OEH_BLOCKLIST = {"GODREJCP", "GRASIM"}

OEH_UNIVERSE = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "BHARTIARTL",
    "SBIN", "ITC", "BAJFINANCE", "LT", "KOTAKBANK", "AXISBANK",
    "TITAN", "MARUTI", "SUNPHARMA", "HCLTECH", "WIPRO", "TATASTEEL",
    "ADANIENT", "CIPLA", "DRREDDY", "M&M", "ASIANPAINT", "HINDUNILVR",
    "NESTLEIND", "ONGC", "ULTRACEMCO", "JSWSTEEL", "TRENT",
    "BAJAJFINSV", "VEDL", "HINDALCO", "BPCL", "HEROMOTOCO", "EICHERMOT",
    "TATAPOWER", "BEL", "NTPC", "POWERGRID", "COALINDIA", "PIDILITIND",
    "SHREECEM", "DABUR", "COLPAL", "AMBUJACEM", "BHEL",
    "DIVISLAB", "BRITANNIA",
]

LOT_SIZES = {
    "RELIANCE": 250, "TCS": 175, "HDFCBANK": 550, "INFY": 400,
    "ICICIBANK": 700, "BHARTIARTL": 475, "SBIN": 1500, "ITC": 1600,
    "BAJFINANCE": 125, "LT": 300, "KOTAKBANK": 400, "AXISBANK": 625,
    "TITAN": 375, "MARUTI": 100, "SUNPHARMA": 350, "HCLTECH": 350,
    "WIPRO": 1500, "TATASTEEL": 5500, "ADANIENT": 250, "CIPLA": 650,
    "DRREDDY": 125, "M&M": 350, "ASIANPAINT": 300, "HINDUNILVR": 300,
    "NESTLEIND": 200, "ONGC": 3250, "ULTRACEMCO": 100, "JSWSTEEL": 900,
    "TRENT": 625, "BAJAJFINSV": 500, "VEDL": 1550, "HINDALCO": 1500,
    "BPCL": 1800, "HEROMOTOCO": 150, "EICHERMOT": 150, "TATAPOWER": 1350,
    "BEL": 1500, "NTPC": 2250, "POWERGRID": 2700, "COALINDIA": 2100,
    "PIDILITIND": 500, "SHREECEM": 25, "DABUR": 1250, "COLPAL": 150,
    "AMBUJACEM": 1100, "BHEL": 2750, "DIVISLAB": 150, "BRITANNIA": 200,
}
DEFAULT_LOT = 400

token = load_cached_token()
if not token:
    print("ERROR: No Upstox token"); sys.exit(1)

client = UpstoxData()
master = client._load_master()

eq_keys = {}
for inst in master:
    if inst.get("segment") == "NSE_EQ":
        tsym = (inst.get("trading_symbol") or "").upper()
        if tsym:
            eq_keys[tsym] = inst.get("instrument_key")

from_dt = datetime(year, month, day, 9, 15, 0, tzinfo=IST)
to_dt_scan = datetime(year, month, day, 9, 25, 0, tzinfo=IST)
to_dt_full = datetime(year, month, day, 15, 30, 0, tzinfo=IST)

# --- NIFTY trend check ---
nifty_key = config.UPSTOX_INDEX_KEYS.get("NSE:NIFTY 50")
if nifty_key and not args.skip_filter:
    try:
        nc = client.historical_data(nifty_key, from_dt, to_dt_scan, "5minute")
        if nc and len(nc) >= 1:
            no, ncl = nc[0]["open"], nc[0]["close"]
            trend = "GREEN (bullish)" if ncl > no else "RED (bearish)"
            print(f"NIFTY 9:15-9:20: open={no:.1f} close={ncl:.1f} → {trend}")
            if ncl > no:
                print("OEH scanner would have SKIPPED today (NIFTY green).")
                print("Re-run with --skip-filter to simulate anyway.\n")
                sys.exit(0)
        _time.sleep(0.3)
    except Exception as e:
        print(f"Could not fetch NIFTY data: {e}")

# --- Scan for OEH candidates ---
print(f"\nScanning {len(OEH_UNIVERSE)} stocks for Open=High on {target_date}...")
candidates = []
scanned = 0

for sym in OEH_UNIVERSE:
    if sym in OEH_BLOCKLIST:
        continue
    inst_key = eq_keys.get(sym)
    if not inst_key:
        continue
    try:
        candles = client.historical_data(inst_key, from_dt, to_dt_scan, "5minute")
        _time.sleep(0.3)
    except Exception:
        _time.sleep(1)
        continue

    scanned += 1
    if not candles:
        continue

    open_price = candles[0]["open"]
    if open_price <= 0:
        continue

    max_high = candles[0]["high"]
    if max_high > open_price + OEH_TOLERANCE:
        continue

    entry_price = candles[0]["close"]
    drop_pct = (open_price - entry_price) / open_price * 100
    if drop_pct < OEH_MIN_DROP_PCT:
        continue

    candidates.append({
        "symbol": sym, "open": open_price, "entry": entry_price,
        "max_high": max_high, "drop_pct": drop_pct,
    })

candidates.sort(key=lambda x: x["drop_pct"], reverse=True)
top = candidates[:OEH_MAX_TRADES]

print(f"Scanned: {scanned}, OEH candidates: {len(candidates)}, Top {OEH_MAX_TRADES}: {len(top)}")

if not candidates:
    print("\nNo OEH candidates found today.")
    if candidates == [] and scanned > 0:
        print("(All stocks had high > open or drop < 0.3%)")
    sys.exit(0)

# --- Resolve ATM PE and walk candles ---
def resolve_atm_pe(sym):
    ltp_key = eq_keys.get(sym)
    if not ltp_key:
        return None, None, None
    try:
        c = client.historical_data(ltp_key, from_dt, to_dt_scan, "5minute")
        if not c:
            return None, None, None
        ltp = c[-1]["close"]
    except Exception:
        return None, None, None

    step = 50
    if ltp > 5000: step = 100
    elif ltp > 2000: step = 50
    elif ltp > 500: step = 10
    else: step = 5
    atm_strike = round(ltp / step) * step

    fo_candidates = []
    for inst in master:
        seg = inst.get("segment", "")
        if seg != "NSE_FO":
            continue
        if inst.get("asset_symbol", "").upper() != sym:
            continue
        if inst.get("instrument_type") != "PE":
            continue
        if abs(float(inst.get("strike_price", -1)) - atm_strike) > 0.01:
            continue
        exp = _expiry_to_date(inst.get("expiry"))
        if exp is None or exp < today_d:
            continue
        fo_candidates.append((exp, inst))

    if not fo_candidates:
        return None, None, None

    fo_candidates.sort(key=lambda x: x[0])
    inst = fo_candidates[0][1]
    return inst.get("instrument_key"), atm_strike, fo_candidates[0][0]


print()
print("=" * 150)
print(f"  OEH SIMULATION — {target_date} — SL={OEH_SL_PCT*100:.0f}% | TGT={OEH_TARGET_MULT:.0f}x | 1 lot")
print("=" * 150)
print(f"  {'#':<3} {'Symbol':<14} {'EqOpen':>8} {'EqHigh':>8} {'EqClose':>8} {'Drop%':>6} "
      f"{'Strike':>8} {'PEentry':>8} {'SL':>8} {'TGT':>8} {'Qty':>5} "
      f"{'PEpeak':>8} {'PElow':>8} {'Result':<8} {'P&L':>10}")
print("  " + "─" * 148)

total_pnl = 0
wins = losses = nodata = 0

for i, c in enumerate(top, 1):
    sym = c["symbol"]
    pe_key, strike, exp_date = resolve_atm_pe(sym)
    _time.sleep(0.3)

    if not pe_key:
        print(f"  {i:<3} {sym:<14} {c['open']:>8.1f} {c['max_high']:>8.1f} {c['entry']:>8.1f} {c['drop_pct']:>6.1f} "
              f"{'':>8} {'':>8} {'':>8} {'':>8} {'':>5} {'':>8} {'':>8} {'NO_PE':<8} {'':>10}")
        nodata += 1
        continue

    try:
        pe_candles = client.historical_data(pe_key, from_dt, to_dt_full, "5minute")
        _time.sleep(0.3)
    except Exception:
        pe_candles = None

    if not pe_candles:
        print(f"  {i:<3} {sym:<14} {c['open']:>8.1f} {c['max_high']:>8.1f} {c['entry']:>8.1f} {c['drop_pct']:>6.1f} "
              f"{strike:>8.0f} {'':>8} {'':>8} {'':>8} {'':>5} {'':>8} {'':>8} {'NO_DATA':<8} {'':>10}")
        nodata += 1
        continue

    # Entry at ~9:20 candle open
    scan_candles = [cd for cd in pe_candles if cd["date"][11:16] >= "09:20"]
    if not scan_candles:
        scan_candles = pe_candles

    pe_entry = scan_candles[0]["open"]
    sl = round(pe_entry * (1 - OEH_SL_PCT), 2)
    tgt = round(pe_entry * OEH_TARGET_MULT, 2)

    lot_size = LOT_SIZES.get(sym, DEFAULT_LOT)
    qty = lot_size * 1

    # Walk candles
    max_high = 0
    min_low = 999999
    result = "OPEN"
    exit_price = pe_entry

    for cd in scan_candles:
        max_high = max(max_high, cd["high"])
        min_low = min(min_low, cd["low"])

        t_hit = cd["high"] >= tgt
        s_hit = cd["low"] <= sl

        if t_hit and s_hit:
            result = "BOTH"
            exit_price = sl
            break
        elif t_hit:
            result = "TGT"
            exit_price = tgt
            break
        elif s_hit:
            result = "SL"
            exit_price = sl
            break

    if result == "OPEN":
        exit_price = scan_candles[-1]["close"]
        result = "EOD"

    pnl = (exit_price - pe_entry) * qty

    if pnl >= 0:
        wins += 1
        icon = "W"
    else:
        losses += 1
        icon = "L"

    total_pnl += pnl

    exp_str = exp_date.strftime("%d%b") if exp_date else "?"
    print(f"  {i:<3} {sym:<14} {c['open']:>8.1f} {c['max_high']:>8.1f} {c['entry']:>8.1f} {c['drop_pct']:>6.1f} "
          f"{strike:>8.0f} {pe_entry:>8.1f} {sl:>8.1f} {tgt:>8.1f} {qty:>5} "
          f"{max_high:>8.1f} {min_low:>8.1f} [{icon}] {result:<5} {pnl:>+10,.0f}")

print()
print("=" * 150)
total = wins + losses
if total > 0:
    print(f"  Trades: {total} ({nodata} no data) | Wins: {wins} | Losses: {losses} | Win Rate: {wins/total*100:.0f}%")
    print(f"  Total P&L: ₹{total_pnl:+,.0f}")
else:
    print("  No trades could be simulated.")
print(f"  Strategy: Buy ATM PE on OEH stocks | SL={OEH_SL_PCT*100:.0f}% | TGT={OEH_TARGET_MULT:.0f}x | 1 lot")
print("=" * 150)

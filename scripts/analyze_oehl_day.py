#!/usr/bin/env python3
"""Analyze OEH (Open=High) + OEL (Open=Low) stocks for a given day.

1. Scans universe using 1-min candles (9:15-9:20) to find candidates
2. OEH: High <= Open → bearish → buy ATM PE
3. OEL: Low >= Open → bullish → buy ATM CE
4. Backtests with floor logic, ₹1,500 max loss sizing

Usage: .venv/bin/python3 scripts/analyze_oehl_day.py [--date 2026-08-26]
"""
import sys, os, time as _time, argparse
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
PROFIT_FLOOR = 1500
TOLERANCE = 0.05
MIN_MOVE_PCT = 0.3
SL_PCT = 0.30
TARGET_MULT = 2.0
MAX_LOSS_PER_TRADE = 1500

UNIVERSE = [
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
BLOCKLIST = {"GODREJCP", "GRASIM"}

LOT_SIZES = {
    "RELIANCE": 250, "TCS": 175, "HDFCBANK": 550, "INFY": 400,
    "ICICIBANK": 700, "BHARTIARTL": 475, "SBIN": 1500, "ITC": 1600,
    "BAJFINANCE": 125, "LT": 300, "KOTAKBANK": 400, "AXISBANK": 625,
    "TITAN": 375, "MARUTI": 100, "SUNPHARMA": 700, "HCLTECH": 500,
    "WIPRO": 1600, "TATASTEEL": 1100, "ADANIENT": 250, "CIPLA": 650,
    "DRREDDY": 125, "M&M": 350, "ASIANPAINT": 300, "HINDUNILVR": 300,
    "NESTLEIND": 50, "ONGC": 3250, "ULTRACEMCO": 100, "JSWSTEEL": 900,
    "TRENT": 625, "BAJAJFINSV": 500, "VEDL": 1550, "HINDALCO": 1500,
    "BPCL": 1800, "HEROMOTOCO": 150, "EICHERMOT": 150, "TATAPOWER": 1350,
    "BEL": 1600, "NTPC": 2400, "POWERGRID": 2400, "COALINDIA": 2400,
    "PIDILITIND": 250, "SHREECEM": 25, "DABUR": 1250, "COLPAL": 200,
    "AMBUJACEM": 1000, "BHEL": 3300, "DIVISLAB": 100, "BRITANNIA": 200,
}
DEFAULT_LOT = 400

parser = argparse.ArgumentParser()
parser.add_argument("--date", default=None)
args = parser.parse_args()

if args.date:
    target_date = args.date
else:
    now = datetime.now(IST)
    target_date = now.strftime("%Y-%m-%d")

dt_parts = [int(x) for x in target_date.split("-")]
year, month, day = dt_parts
today_d = date(year, month, day)

token = load_cached_token()
if not token:
    print("ERROR: No Upstox token")
    sys.exit(1)

ud = UpstoxData(access_token=token)
master = ud._load_master()

eq_keys = {}
for inst in master:
    if inst.get("segment") == "NSE_EQ":
        tsym = (inst.get("trading_symbol") or "").upper()
        if tsym:
            eq_keys[tsym] = inst.get("instrument_key")

# 1-min candles from 9:15 to 9:20
from_dt_eq = datetime(year, month, day, 9, 15, tzinfo=IST)
to_dt_eq = datetime(year, month, day, 9, 20, tzinfo=IST)


def _build_strike_steps():
    sym_strikes = {}
    for inst in master:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO"):
            continue
        asym = (inst.get("asset_symbol") or "").upper()
        if not asym or inst.get("instrument_type") not in ("CE", "PE"):
            continue
        sp = float(inst.get("strike_price", 0))
        if sp > 0:
            sym_strikes.setdefault(asym, set()).add(sp)

    steps = {}
    for sym, strikes in sym_strikes.items():
        sorted_strikes = sorted(strikes)
        if len(sorted_strikes) >= 2:
            gaps = [sorted_strikes[i+1] - sorted_strikes[i] for i in range(min(10, len(sorted_strikes)-1))]
            steps[sym] = min(gaps)
        else:
            steps[sym] = 50
    return steps

STRIKE_STEPS = _build_strike_steps()


def resolve_option(sym, stock_price, opt_type):
    strike_step = STRIKE_STEPS.get(sym, 50)
    atm_strike = round(stock_price / strike_step) * strike_step

    options = []
    for inst in master:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO"):
            continue
        asym = (inst.get("asset_symbol") or "").upper()
        if asym != sym:
            continue
        if inst.get("instrument_type") != opt_type:
            continue
        sp = float(inst.get("strike_price", -1))
        exp = _expiry_to_date(inst.get("expiry"))
        if exp is None or exp < today_d:
            continue
        options.append((sp, exp, inst))

    if not options:
        return None, None, None, None

    options.sort(key=lambda x: (abs(x[0] - atm_strike), x[1]))
    best_strike = options[0][0]
    same_strike = [(sp, exp, inst) for sp, exp, inst in options if abs(sp - best_strike) < 0.01]
    same_strike.sort(key=lambda x: x[1])

    inst = same_strike[0][2]
    lot_size = int(inst.get("lot_size", 1)) or 1
    return inst.get("instrument_key"), lot_size, same_strike[0][1], best_strike


def walk_candles_floor(candles, entry, sl, tgt, qty):
    peak_pnl = 0
    max_high = 0
    min_low = 999999
    floor_armed = False
    tgt_valid = tgt and tgt > entry
    sl_valid = sl and sl < entry

    for c in candles:
        max_high = max(max_high, c["high"])
        min_low = min(min_low, c["low"])
        tgt_hit = tgt_valid and c["high"] >= tgt
        sl_hit = sl_valid and c["low"] <= sl
        low_pnl = (c["low"] - entry) * qty

        if tgt_hit and sl_hit:
            return tgt, "BOTH_TGT", max_high, min_low
        elif tgt_hit:
            return tgt, "TGT", max_high, min_low
        elif sl_hit:
            return sl, "SL", max_high, min_low
        elif floor_armed and low_pnl <= PROFIT_FLOOR:
            floor_price = entry + (PROFIT_FLOOR / qty)
            return floor_price, "FLOOR", max_high, min_low

        candle_peak_pnl = (c["high"] - entry) * qty
        peak_pnl = max(peak_pnl, candle_peak_pnl)
        if peak_pnl >= PROFIT_FLOOR:
            floor_armed = True

    return candles[-1]["close"], "EOD", max_high, min_low


# ============================================================
# Step 1: Scan for OEH + OEL candidates using 1-min candles
# ============================================================
print(f"\n{'='*130}")
print(f"  OEH + OEL Analysis — {target_date}")
print(f"  1-min candles 9:15–9:20 | SL={SL_PCT*100:.0f}% | TGT={TARGET_MULT}x | Floor ₹{PROFIT_FLOOR:,} | Max loss ₹{MAX_LOSS_PER_TRADE:,}")
print(f"{'='*130}")

oeh_candidates = []
oel_candidates = []
scanned = 0

for sym in UNIVERSE:
    if sym in BLOCKLIST:
        continue
    inst_key = eq_keys.get(sym)
    if not inst_key:
        continue
    try:
        candles = ud.historical_data(inst_key, from_dt_eq, to_dt_eq, "1minute")
        _time.sleep(0.3)
    except Exception:
        _time.sleep(1)
        try:
            candles = ud.historical_data(inst_key, from_dt_eq, to_dt_eq, "1minute")
        except Exception:
            continue

    scanned += 1
    if not candles or len(candles) < 1:
        continue

    open_price = candles[0]["open"]
    if open_price <= 0:
        continue

    # Aggregate 1-min candle data for 9:15-9:20
    max_high = max(c["high"] for c in candles)
    min_low = min(c["low"] for c in candles)
    last_close = candles[-1]["close"]

    candle_info = []
    for c in candles:
        ts = c["date"][11:16] if isinstance(c["date"], str) else c["date"].strftime("%H:%M")
        candle_info.append(f"{ts} O={c['open']:.1f} H={c['high']:.1f} L={c['low']:.1f} C={c['close']:.1f}")

    # OEH check: High <= Open across all candles
    if max_high <= open_price + TOLERANCE:
        drop_pct = (open_price - last_close) / open_price * 100
        if drop_pct >= MIN_MOVE_PCT:
            oeh_candidates.append({
                "symbol": sym, "open": open_price, "close": last_close,
                "high": max_high, "low": min_low, "move_pct": drop_pct,
                "candles": candle_info, "num_candles": len(candles),
            })

    # OEL check: Low >= Open across all candles
    if min_low >= open_price - TOLERANCE:
        rise_pct = (last_close - open_price) / open_price * 100
        if rise_pct >= MIN_MOVE_PCT:
            oel_candidates.append({
                "symbol": sym, "open": open_price, "close": last_close,
                "high": max_high, "low": min_low, "move_pct": rise_pct,
                "candles": candle_info, "num_candles": len(candles),
            })

oeh_candidates.sort(key=lambda x: x["move_pct"], reverse=True)
oel_candidates.sort(key=lambda x: x["move_pct"], reverse=True)

print(f"\n  Scanned {scanned} stocks")

# --- OEH List ---
print(f"\n{'─'*130}")
print(f"  📉 OEH (Open=High) Candidates — {len(oeh_candidates)} found → buy PE")
print(f"{'─'*130}")
if oeh_candidates:
    print(f"  {'#':<4} {'Symbol':<14} {'Open':>10} {'High':>10} {'Close':>10} {'Drop%':>7}  1-min Candles")
    print(f"  {'─'*120}")
    for i, c in enumerate(oeh_candidates, 1):
        candle_str = " | ".join(c["candles"][:5])
        print(f"  {i:<4} {c['symbol']:<14} {c['open']:>10.2f} {c['high']:>10.2f} "
              f"{c['close']:>10.2f} {c['move_pct']:>6.1f}%  {candle_str}")
else:
    print("  No OEH candidates")

# --- OEL List ---
print(f"\n{'─'*130}")
print(f"  📈 OEL (Open=Low) Candidates — {len(oel_candidates)} found → buy CE")
print(f"{'─'*130}")
if oel_candidates:
    print(f"  {'#':<4} {'Symbol':<14} {'Open':>10} {'Low':>10} {'Close':>10} {'Rise%':>7}  1-min Candles")
    print(f"  {'─'*120}")
    for i, c in enumerate(oel_candidates, 1):
        candle_str = " | ".join(c["candles"][:5])
        print(f"  {i:<4} {c['symbol']:<14} {c['open']:>10.2f} {c['low']:>10.2f} "
              f"{c['close']:>10.2f} {c['move_pct']:>6.1f}%  {candle_str}")
else:
    print("  No OEL candidates")


# ============================================================
# Step 2: Backtest trades
# ============================================================
from_dt_opt = datetime(year, month, day, 9, 15, tzinfo=IST)
to_dt_opt = datetime(year, month, day, 15, 30, tzinfo=IST)


def backtest_candidates(candidates, opt_type, label):
    print(f"\n{'='*130}")
    print(f"  {label} Trade Backtest")
    print(f"{'='*130}\n")

    if not candidates:
        print(f"  No {label} candidates to backtest")
        return 0, 0, 0, 0

    print(f"  {'#':<4} {'Symbol':<14} {'Strike':>8} {'Entry':>8} {'SL':>8} {'TGT':>8} "
          f"{'Lots':>5} {'Qty':>6} {'Peak':>8} {'Low':>8} {'Result':<8} {'P&L':>10}")
    print(f"  {'─'*108}")

    total_pnl = 0
    wins = 0
    losses = 0
    no_data = 0

    for i, c in enumerate(candidates[:5], 1):
        sym = c["symbol"]
        opt_key, lot_size, exp_date, atm_strike = resolve_option(sym, c["open"], opt_type)
        if not opt_key:
            print(f"  {i:<4} {sym:<14} {'?':>8} {'NO OPTION':<60}")
            no_data += 1
            continue

        lot_size_used = LOT_SIZES.get(sym, lot_size or DEFAULT_LOT)

        try:
            candles = ud.historical_data(opt_key, from_dt_opt, to_dt_opt, "5minute")
            _time.sleep(0.3)
        except Exception:
            _time.sleep(1)
            try:
                candles = ud.historical_data(opt_key, from_dt_opt, to_dt_opt, "5minute")
            except Exception:
                candles = None

        if not candles:
            print(f"  {i:<4} {sym:<14} {atm_strike:>8.0f} {'NO DATA':<60}")
            no_data += 1
            continue

        filtered = [x for x in candles if x["date"][11:16] >= "09:20"]
        if not filtered:
            filtered = candles

        entry = filtered[0]["open"]
        if entry <= 0:
            print(f"  {i:<4} {sym:<14} {atm_strike:>8.0f} {'ZERO ENTRY':<60}")
            no_data += 1
            continue

        sl = round(entry * (1 - SL_PCT), 2)
        tgt = round(entry * TARGET_MULT, 2)

        sl_per_unit = entry - sl
        if sl_per_unit <= 0:
            lots = 1
            qty = lot_size_used
        else:
            min_1lot_loss = sl_per_unit * lot_size_used
            if min_1lot_loss > MAX_LOSS_PER_TRADE:
                print(f"  {i:<4} {sym:<14} {atm_strike:>8.0f} {entry:>8.1f} {sl:>8.1f} {tgt:>8.1f} "
                      f"{'—':>5} {lot_size_used:>6} {'':>8} {'':>8} SKIP     {'1L=₹'+f'{min_1lot_loss:,.0f}':>10}")
                no_data += 1
                continue
            lots = max(1, int(MAX_LOSS_PER_TRADE / sl_per_unit / lot_size_used))
            qty = lots * lot_size_used

        exit_price, result, max_high, min_low = walk_candles_floor(filtered, entry, sl, tgt, qty)
        pnl = (exit_price - entry) * qty

        icon = "W" if pnl >= 0 else "L"
        if pnl >= 0:
            wins += 1
        else:
            losses += 1
        total_pnl += pnl

        print(f"  {i:<4} {sym:<14} {atm_strike:>8.0f} {entry:>8.1f} {sl:>8.1f} {tgt:>8.1f} "
              f"{lots:>5} {qty:>6} {max_high:>8.1f} {min_low:>8.1f} [{icon}] {result:<5} {pnl:>+10,.0f}")

    print(f"\n  {label}: {wins}W/{losses}L ({no_data} skip/no data) | P&L: ₹{total_pnl:+,.0f}")
    return total_pnl, wins, losses, no_data


oeh_pnl, oeh_w, oeh_l, oeh_nd = backtest_candidates(oeh_candidates, "PE", "OEH (buy PE)")
oel_pnl, oel_w, oel_l, oel_nd = backtest_candidates(oel_candidates, "CE", "OEL (buy CE)")

print(f"\n{'='*130}")
print(f"  GRAND TOTAL — {target_date}")
print(f"{'='*130}")
print(f"  OEH: {oeh_w}W/{oeh_l}L = ₹{oeh_pnl:+,.0f}")
print(f"  OEL: {oel_w}W/{oel_l}L = ₹{oel_pnl:+,.0f}")
print(f"  Combined: ₹{oeh_pnl + oel_pnl:+,.0f}")
print(f"{'='*130}")

"""Strategy Backtester v2 — Tuning the winning Short Straddle.

Iteration on v1 results:
  - Straddle SL30 was best (₹+3.62L, 71% green, Grade A)
  - ORB buying was Grade F — dropped
  - This version tests tuning knobs to push green% toward 80%

Tuning knobs tested:
  1. Entry time: 9:20, 9:30, 9:45, 10:00
  2. SL type: per-leg vs combined (total premium)
  3. Trailing SL: lock in profit once 40% premium captured
  4. Volatility filter: skip high-vol days (first-candle range > threshold)
  5. DTE filter: avoid 1-DTE days (day before expiry = gamma risk)

Usage:
  .venv/bin/python3 scripts/strategy_v2.py --year 2026 --months 1-8
"""
import os, sys, math, warnings, pickle, argparse
warnings.filterwarnings("ignore")

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from src.broker.upstox_data import UpstoxData
import config

IST = ZoneInfo("Asia/Kolkata")

INDEXES = {
    "NIFTY": {
        "key": "NSE_INDEX|Nifty 50",
        "lot_size": 75,
        "strike_step": 50,
        "iv_annual": 0.13,
        "vol_skip_range": 120,   # skip if first 15-min range > this (points)
    },
    "BANKNIFTY": {
        "key": "NSE_INDEX|Nifty Bank",
        "lot_size": 30,
        "strike_step": 100,
        "iv_annual": 0.17,
        "vol_skip_range": 250,
    },
    "SENSEX": {
        "key": "BSE_INDEX|SENSEX",
        "lot_size": 20,
        "strike_step": 100,
        "iv_annual": 0.13,
        "vol_skip_range": 400,
    },
}

LOTS = 1
CACHE_DIR = os.path.join(config.DATA_DIR, "ml_cache")

EXPIRY_WEEKDAY = {"NIFTY": 1, "BANKNIFTY": 2, "SENSEX": 4}  # Fri=4

def calc_charges(entry_p, exit_p, qty):
    buy_t = entry_p * qty
    sell_t = exit_p * qty
    tot_t = buy_t + sell_t
    brok = 40.0
    stt = sell_t * 0.001
    exch = tot_t * 0.000495
    sebi = tot_t * 0.000001
    stamp = buy_t * 0.00003
    gst = (brok + exch) * 0.18
    return brok + stt + exch + sebi + stamp + gst

def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs_call(S, K, T, sigma, r=0.07):
    if T <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)

def bs_put(S, K, T, sigma, r=0.07):
    if T <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)

def estimate_premium(spot, strike, opt_type, T_frac, iv):
    T = max(T_frac, 1e-6)
    if opt_type == "CE":
        return bs_call(spot, strike, T, iv)
    return bs_put(spot, strike, T, iv)

def round_strike(spot, step):
    return round(spot / step) * step

def fetch_day_candles(uclient, idx_name, ref_date, interval="5minute"):
    cache_file = os.path.join(CACHE_DIR, f"candles_{idx_name}_{ref_date}_{interval}.pkl")
    if os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            return pickle.load(f)
    idx = INDEXES[idx_name]
    from_dt = datetime(ref_date.year, ref_date.month, ref_date.day, 9, 0, tzinfo=IST)
    to_dt = datetime(ref_date.year, ref_date.month, ref_date.day, 16, 0, tzinfo=IST)
    candles = uclient.historical_data(idx["key"], from_dt, to_dt, interval)
    if candles and len(candles) > 5:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_file, "wb") as f:
            pickle.dump(candles, f)
    return candles

def candle_time_minutes(candle):
    ts = candle["date"]
    h = int(ts[11:13])
    m = int(ts[14:16])
    return (h - 9) * 60 + (m - 15)

def candle_time_hm(candle):
    ts = candle["date"]
    return int(ts[11:13]), int(ts[14:16])

def days_to_expiry(ref_date, idx_name):
    exp_wd = EXPIRY_WEEKDAY.get(idx_name, 3)
    current_wd = ref_date.weekday()
    diff = (exp_wd - current_wd) % 7
    return diff

def is_expiry_day(ref_date, idx_name):
    return days_to_expiry(ref_date, idx_name) == 0

def dte_fraction(ref_date, idx_name, minutes_into_day=0):
    dte = days_to_expiry(ref_date, idx_name)
    total_minutes_in_day = 375
    day_fraction = max(0, (total_minutes_in_day - minutes_into_day) / total_minutes_in_day)
    return (dte + day_fraction) / 365.0


# ─── Volatility filter ────────────────────────────────────────────────
def first_candle_range(candles):
    """Range of first 15-min candle (3 x 5min candles) — proxy for intraday volatility."""
    high = -float("inf")
    low = float("inf")
    count = 0
    for c in candles:
        h, m = candle_time_hm(c)
        if h == 9 and m < 15:
            continue
        if count >= 3:
            break
        high = max(high, c["high"])
        low = min(low, c["low"])
        count += 1
    if high == -float("inf"):
        return 0
    return high - low


def gap_pct(candles):
    """Opening gap percentage — large gaps = volatile day."""
    if len(candles) < 2:
        return 0
    first = None
    for c in candles:
        h, m = candle_time_hm(c)
        if h >= 9 and m >= 15:
            first = c
            break
    if not first:
        return 0
    prev_close = candles[0]["close"] if candles[0] != first else first["open"]
    return abs(first["open"] - prev_close) / prev_close * 100


# ═══════════════════════════════════════════════════════════════════════
# TUNED STRADDLE — parametric version with all knobs
# ═══════════════════════════════════════════════════════════════════════
def run_tuned_straddle(candles, idx_name, ref_date, *,
                       entry_hour=9, entry_min=20,
                       sl_pct=0.30,
                       combined_sl=False,    # True = SL on total premium, False = per-leg
                       trailing=False,       # trail SL once 40% captured
                       trail_lock_pct=0.40,  # lock once this much captured
                       trail_give_back=0.20, # give back this much from peak
                       vol_filter=False,
                       skip_1dte=False):
    """Highly configurable short straddle."""
    idx = INDEXES[idx_name]
    iv = idx["iv_annual"]
    lot_size = idx["lot_size"] * LOTS
    step = idx["strike_step"]

    # Volatility filter
    if vol_filter:
        fr = first_candle_range(candles)
        if fr > idx["vol_skip_range"]:
            return {"strategy": "skip", "date": str(ref_date), "index": idx_name,
                    "net_pnl": 0, "reason": f"vol_filter (range={fr:.0f})", "traded": False}

    # Skip day before expiry (high gamma risk)
    if skip_1dte and days_to_expiry(ref_date, idx_name) == 1:
        return {"strategy": "skip", "date": str(ref_date), "index": idx_name,
                "net_pnl": 0, "reason": "1-DTE skip", "traded": False}

    # Find entry candle
    entry_candle = None
    for c in candles:
        h, m = candle_time_hm(c)
        if h > entry_hour or (h == entry_hour and m >= entry_min):
            entry_candle = c
            break
    if not entry_candle:
        return None

    spot_entry = entry_candle["close"]
    atm = round_strike(spot_entry, step)
    mins_entry = candle_time_minutes(entry_candle)
    T_entry = dte_fraction(ref_date, idx_name, mins_entry)

    ce_entry_prem = estimate_premium(spot_entry, atm, "CE", T_entry, iv)
    pe_entry_prem = estimate_premium(spot_entry, atm, "PE", T_entry, iv)
    total_prem = ce_entry_prem + pe_entry_prem

    if total_prem < 10:
        return None

    # SL levels
    if combined_sl:
        combined_sl_level = total_prem * (1 + sl_pct)
    else:
        ce_sl = ce_entry_prem * (1 + sl_pct)
        pe_sl = pe_entry_prem * (1 + sl_pct)

    ce_alive = True
    pe_alive = True
    ce_exit_prem = None
    pe_exit_prem = None

    # Trailing state
    best_combined_profit = 0.0  # best unrealized profit seen so far
    trail_active = False

    e_idx = candles.index(entry_candle)
    for c in candles[e_idx + 1:]:
        h, m = candle_time_hm(c)
        mins = candle_time_minutes(c)
        T = dte_fraction(ref_date, idx_name, mins)
        spot = c["close"]
        spot_high = c["high"]
        spot_low = c["low"]

        # Current premium values
        ce_now = estimate_premium(spot, atm, "CE", T, iv)
        pe_now = estimate_premium(spot, atm, "PE", T, iv)
        ce_now_worst = estimate_premium(spot_high, atm, "CE", T, iv)
        pe_now_worst = estimate_premium(spot_low, atm, "PE", T, iv)

        # ── Combined SL mode ──
        if combined_sl:
            worst_total = ce_now_worst + pe_now_worst
            if worst_total >= combined_sl_level:
                ce_exit_prem = ce_now
                pe_exit_prem = pe_now
                ce_alive = pe_alive = False
                break
        else:
            # ── Per-leg SL mode ──
            if ce_alive and ce_now_worst >= ce_sl:
                ce_exit_prem = ce_sl
                ce_alive = False
            if pe_alive and pe_now_worst >= pe_sl:
                pe_exit_prem = pe_sl
                pe_alive = False

        # ── Trailing stop ──
        if trailing and ce_alive and pe_alive:
            current_profit = (total_prem - (ce_now + pe_now))
            profit_pct = current_profit / total_prem

            if profit_pct >= trail_lock_pct:
                trail_active = True

            best_combined_profit = max(best_combined_profit, current_profit)

            if trail_active:
                give_back = best_combined_profit * trail_give_back
                if current_profit < best_combined_profit - give_back and best_combined_profit > 0:
                    ce_exit_prem = ce_now
                    pe_exit_prem = pe_now
                    ce_alive = pe_alive = False
                    break

        # Exit by 3:10 PM
        if h >= 15 and m >= 10:
            if ce_alive:
                ce_exit_prem = ce_now
                ce_alive = False
            if pe_alive:
                pe_exit_prem = pe_now
                pe_alive = False
            break

    # Fallback exit
    if ce_exit_prem is None:
        last = candles[-1]
        T_last = dte_fraction(ref_date, idx_name, candle_time_minutes(last))
        ce_exit_prem = estimate_premium(last["close"], atm, "CE", T_last, iv)
    if pe_exit_prem is None:
        last = candles[-1]
        T_last = dte_fraction(ref_date, idx_name, candle_time_minutes(last))
        pe_exit_prem = estimate_premium(last["close"], atm, "PE", T_last, iv)

    ce_pnl = (ce_entry_prem - ce_exit_prem) * lot_size
    pe_pnl = (pe_entry_prem - pe_exit_prem) * lot_size
    charges = calc_charges(ce_entry_prem, ce_exit_prem, lot_size) + \
              calc_charges(pe_entry_prem, pe_exit_prem, lot_size)

    return {
        "strategy": "tuned",
        "index": idx_name,
        "date": str(ref_date),
        "net_pnl": round(ce_pnl + pe_pnl - charges, 2),
        "traded": True,
    }


# ═══════════════════════════════════════════════════════════════════════
# Report + Runner
# ═══════════════════════════════════════════════════════════════════════
def grade(pct_green):
    if pct_green >= 80: return "A+"
    if pct_green >= 70: return "A"
    if pct_green >= 60: return "B"
    if pct_green >= 50: return "C"
    return "F"

def summarize(name, results):
    traded = [r for r in results if r.get("traded", True) and r.get("net_pnl") is not None]
    if not traded:
        return None
    daily_pnl = defaultdict(float)
    for r in traded:
        daily_pnl[r["date"]] += r["net_pnl"]
    daily_vals = sorted(daily_pnl.items())
    vals = [v for _, v in daily_vals]
    green = sum(1 for v in vals if v > 0)
    total = len(vals)
    pct = green / total * 100 if total else 0
    net = sum(vals)
    avg = net / total if total else 0

    cum = peak = max_dd = 0
    for v in vals:
        cum += v
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    # Monthly
    monthly = defaultdict(float)
    monthly_days = defaultdict(lambda: [0, 0])
    for d_str, pnl in daily_pnl.items():
        mon = d_str[:7]
        monthly[mon] += pnl
        monthly_days[mon][1] += 1
        if pnl > 0:
            monthly_days[mon][0] += 1

    return {
        "name": name, "trades": len(traded), "days": total,
        "green": green, "pct_green": pct, "grade": grade(pct),
        "net_pnl": net, "avg_daily": avg, "max_dd": max_dd,
        "best": max(vals) if vals else 0, "worst": min(vals) if vals else 0,
        "monthly": monthly, "monthly_days": monthly_days,
        "skipped": sum(1 for r in results if not r.get("traded", True)),
    }

def print_report(s):
    if not s:
        return
    print(f"\n{'═' * 70}")
    print(f"  {s['name']}   [Grade: {s['grade']}]")
    print(f"{'═' * 70}")
    print(f"  Days: {s['days']} | Trades: {s['trades']} | Skipped: {s['skipped']}")
    print(f"  Green days: {s['green']}/{s['days']} ({s['pct_green']:.0f}%)")
    print(f"  Net P&L: ₹{s['net_pnl']:+,.0f}  |  Avg daily: ₹{s['avg_daily']:+,.0f}")
    print(f"  Max DD: ₹{s['max_dd']:,.0f}  |  Best: ₹{s['best']:+,.0f}  |  Worst: ₹{s['worst']:+,.0f}")

    print(f"\n  Monthly:")
    for mon in sorted(s["monthly"]):
        g_d, t_d = s["monthly_days"][mon]
        mp = s["monthly"][mon]
        pct = g_d / t_d * 100 if t_d else 0
        bar = "█" * int(pct / 5)
        print(f"    {mon}: ₹{mp:>+8,.0f}  ({g_d}/{t_d} = {pct:.0f}%) {bar}")
    print()


def run_v2(year, start_month, end_month):
    uclient = UpstoxData()
    os.makedirs(CACHE_DIR, exist_ok=True)
    trading_days = []
    d = date(year, start_month, 1)
    end = date(year, end_month + 1, 1) if end_month < 12 else date(year + 1, 1, 1)
    while d < end:
        if d.weekday() < 5:
            trading_days.append(d)
        d += timedelta(days=1)

    # ── Define all variants to test ──
    variants = {
        # Entry time tests (all with per-leg SL30, no filters)
        "entry_920_sl30":  dict(entry_hour=9, entry_min=20, sl_pct=0.30),
        "entry_930_sl30":  dict(entry_hour=9, entry_min=30, sl_pct=0.30),
        "entry_945_sl30":  dict(entry_hour=9, entry_min=45, sl_pct=0.30),
        "entry_1000_sl30": dict(entry_hour=10, entry_min=0, sl_pct=0.30),

        # SL type: combined vs per-leg
        "combined_sl30":   dict(entry_hour=9, entry_min=20, sl_pct=0.30, combined_sl=True),
        "combined_sl40":   dict(entry_hour=9, entry_min=20, sl_pct=0.40, combined_sl=True),
        "combined_sl50":   dict(entry_hour=9, entry_min=20, sl_pct=0.50, combined_sl=True),

        # Trailing stop
        "trail_920":       dict(entry_hour=9, entry_min=20, sl_pct=0.30, trailing=True),
        "trail_930":       dict(entry_hour=9, entry_min=30, sl_pct=0.30, trailing=True),

        # Volatility filter (skip high-vol days)
        "vf_920_sl30":     dict(entry_hour=9, entry_min=20, sl_pct=0.30, vol_filter=True),
        "vf_930_sl30":     dict(entry_hour=9, entry_min=30, sl_pct=0.30, vol_filter=True),

        # Volatility filter + trailing
        "vf_trail_920":    dict(entry_hour=9, entry_min=20, sl_pct=0.30, vol_filter=True, trailing=True),
        "vf_trail_930":    dict(entry_hour=9, entry_min=30, sl_pct=0.30, vol_filter=True, trailing=True),

        # Skip 1-DTE (day before expiry)
        "skip1dte_920":    dict(entry_hour=9, entry_min=20, sl_pct=0.30, skip_1dte=True),

        # Combined SL + vol filter + trailing (kitchen sink)
        "kitchen_sink":    dict(entry_hour=9, entry_min=30, sl_pct=0.35, combined_sl=True,
                                vol_filter=True, trailing=True),
    }

    results = {k: [] for k in variants}
    total = len(trading_days)

    for i, day in enumerate(trading_days):
        if i % 10 == 0:
            print(f"  Processing {day} ({i+1}/{total})...", flush=True)

        for idx_name in ["NIFTY", "BANKNIFTY", "SENSEX"]:
            candles = fetch_day_candles(uclient, idx_name, day)
            if not candles or len(candles) < 20:
                continue

            for vname, params in variants.items():
                r = run_tuned_straddle(candles, idx_name, day, **params)
                if r:
                    results[vname].append(r)

    # ── Reports ──
    print(f"\n{'━' * 70}")
    print(f"  TUNING RESULTS ({year}-{start_month:02d} to {year}-{end_month:02d})")
    print(f"  Indexes: NIFTY + BANKNIFTY | Lots: {LOTS}")
    print(f"{'━' * 70}")

    summaries = []
    for vname in variants:
        s = summarize(vname, results[vname])
        if s:
            summaries.append(s)
            print_report(s)

    # ── Comparison table ──
    summaries.sort(key=lambda s: s["pct_green"], reverse=True)

    print(f"\n{'━' * 70}")
    print(f"  COMPARISON (sorted by green%)")
    print(f"{'━' * 70}")
    print(f"  {'Variant':<22} {'Net P&L':>10} {'Avg/Day':>8} {'Green%':>7} {'MaxDD':>9} {'Grade':>6} {'Skip':>5}")
    print(f"  {'─' * 67}")
    for s in summaries:
        print(f"  {s['name']:<22} ₹{s['net_pnl']:>+9,.0f} ₹{s['avg_daily']:>+6,.0f}"
              f" {s['pct_green']:>5.0f}%  ₹{s['max_dd']:>7,.0f}  {s['grade']:>5}"
              f" {s['skipped']:>4}")

    # ── Scaling projection ──
    print(f"\n{'━' * 70}")
    print(f"  SCALING PROJECTION (top 3 by green%)")
    print(f"{'━' * 70}")
    for s in summaries[:3]:
        print(f"\n  {s['name']}:")
        for lots in [1, 2, 3, 5]:
            projected = s['avg_daily'] * lots
            print(f"    {lots} lot(s): ₹{projected:>+8,.0f}/day  |  ₹{projected * 22:>+10,.0f}/month")
    print()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--months", default="1-8")
    a = p.parse_args()

    parts = a.months.split("-")
    sm, em = int(parts[0]), int(parts[1])

    print(f"\n  Strategy Backtester v2 — Straddle Tuning")
    print(f"  Period: {a.year}-{sm:02d} to {a.year}-{em:02d}")
    print(f"  Testing {16} variants\n")

    run_v2(a.year, sm, em)

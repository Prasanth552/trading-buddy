"""Multi-Strategy Options Backtester — tests 4 strategies + hybrids on NIFTY/BANKNIFTY.

Strategies:
  1. Short Straddle (9:20 AM sell ATM CE+PE, SL-based exit)
  2. Short Strangle with Auto-Adjustments (sell OTM, adjust on 1% move)
  3. ORB — Opening Range Breakout (buy options on breakout)
  4. Expiry Day Theta Crush (sell ATM on expiry day only)

Uses simplified Black-Scholes to estimate option premiums from spot candles.

Usage:
  .venv/bin/python3 scripts/strategy_backtest.py --year 2026
  .venv/bin/python3 scripts/strategy_backtest.py --year 2026 --months 1-6
  .venv/bin/python3 scripts/strategy_backtest.py --year 2026 --strategy straddle
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

# ─── Index config ──────────────────────────────────────────────────────
INDEXES = {
    "NIFTY": {
        "key": "NSE_INDEX|Nifty 50",
        "lot_size": 75,
        "strike_step": 50,
        "strangle_offset": 150,   # points away from ATM for strangle
        "orb_skip_range": 40,     # skip ORB if range < 40 pts
        "iv_annual": 0.13,        # typical annualized IV
    },
    "BANKNIFTY": {
        "key": "NSE_INDEX|Nifty Bank",
        "lot_size": 30,
        "strike_step": 100,
        "strangle_offset": 300,
        "orb_skip_range": 80,
        "iv_annual": 0.17,
    },
    "SENSEX": {
        "key": "BSE_INDEX|SENSEX",
        "lot_size": 20,
        "strike_step": 100,
        "strangle_offset": 500,
        "orb_skip_range": 130,
        "iv_annual": 0.13,
    },
}

LOTS = 1          # lots per trade
CACHE_DIR = os.path.join(config.DATA_DIR, "ml_cache")

# Charges (same as channel_listener)
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


# ─── Option premium estimation (simplified Black-Scholes) ──────────────
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

def estimate_premium(spot, strike, opt_type, dte_fraction, iv):
    """Estimate option premium. dte_fraction = fraction of year remaining."""
    T = max(dte_fraction, 1e-6)
    if opt_type == "CE":
        return bs_call(spot, strike, T, iv)
    return bs_put(spot, strike, T, iv)

def round_strike(spot, step):
    return round(spot / step) * step


# ─── Data fetching with cache ──────────────────────────────────────────
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
    """Minutes since 9:15 AM from candle timestamp."""
    ts = candle["date"]
    h = int(ts[11:13])
    m = int(ts[14:16])
    return (h - 9) * 60 + (m - 15)


def candle_time_hm(candle):
    ts = candle["date"]
    return int(ts[11:13]), int(ts[14:16])


# ─── DTE estimation ───────────────────────────────────────────────────
# NIFTY weekly expiry = Tuesday, BANKNIFTY = Wednesday
EXPIRY_WEEKDAY = {"NIFTY": 1, "BANKNIFTY": 2, "SENSEX": 4}  # 0=Mon..4=Fri

def days_to_expiry(ref_date, idx_name):
    exp_wd = EXPIRY_WEEKDAY.get(idx_name, 3)
    current_wd = ref_date.weekday()
    diff = (exp_wd - current_wd) % 7
    if diff == 0:
        return 0  # expiry day
    return diff

def is_expiry_day(ref_date, idx_name):
    return days_to_expiry(ref_date, idx_name) == 0

def dte_fraction(ref_date, idx_name, minutes_into_day=0):
    """Fraction of year remaining for option pricing."""
    dte = days_to_expiry(ref_date, idx_name)
    # On expiry day, DTE goes from ~0.02 at 9:15 to ~0 at 15:30
    total_minutes_in_day = 375  # 9:15 to 15:30
    day_fraction = max(0, (total_minutes_in_day - minutes_into_day) / total_minutes_in_day)
    return (dte + day_fraction) / 365.0


# ═══════════════════════════════════════════════════════════════════════
# STRATEGY 1: Short Straddle (9:20 AM)
# ═══════════════════════════════════════════════════════════════════════
def run_short_straddle(candles, idx_name, ref_date, sl_pct=0.30):
    """Sell ATM CE + ATM PE at 9:20 AM, SL on each leg, exit by 3:10 PM."""
    idx = INDEXES[idx_name]
    iv = idx["iv_annual"]
    lot_size = idx["lot_size"] * LOTS
    step = idx["strike_step"]

    # Find 9:20 candle
    entry_candle = None
    for c in candles:
        h, m = candle_time_hm(c)
        if h == 9 and m >= 20:
            entry_candle = c
            break
    if not entry_candle:
        return None

    spot_entry = entry_candle["close"]
    atm_strike = round_strike(spot_entry, step)
    mins_entry = candle_time_minutes(entry_candle)
    T_entry = dte_fraction(ref_date, idx_name, mins_entry)

    ce_premium_entry = estimate_premium(spot_entry, atm_strike, "CE", T_entry, iv)
    pe_premium_entry = estimate_premium(spot_entry, atm_strike, "PE", T_entry, iv)
    total_premium = ce_premium_entry + pe_premium_entry

    if total_premium < 10:
        return None

    ce_sl = ce_premium_entry * (1 + sl_pct)
    pe_sl = pe_premium_entry * (1 + sl_pct)

    ce_alive = True
    pe_alive = True
    ce_exit_prem = None
    pe_exit_prem = None

    # Walk through candles after entry
    entry_idx = candles.index(entry_candle)
    for c in candles[entry_idx + 1:]:
        h, m = candle_time_hm(c)
        mins = candle_time_minutes(c)
        T = dte_fraction(ref_date, idx_name, mins)

        spot = c["close"]
        spot_high = c["high"]
        spot_low = c["low"]

        # Check CE leg (worst case for seller is spot going UP)
        if ce_alive:
            ce_prem_now = estimate_premium(spot_high, atm_strike, "CE", T, iv)
            if ce_prem_now >= ce_sl:
                ce_exit_prem = ce_sl
                ce_alive = False

        # Check PE leg (worst case for seller is spot going DOWN)
        if pe_alive:
            pe_prem_now = estimate_premium(spot_low, atm_strike, "PE", T, iv)
            if pe_prem_now >= pe_sl:
                pe_exit_prem = pe_sl
                pe_alive = False

        # Exit by 3:10 PM
        if h >= 15 and m >= 10:
            if ce_alive:
                ce_exit_prem = estimate_premium(spot, atm_strike, "CE", T, iv)
                ce_alive = False
            if pe_alive:
                pe_exit_prem = estimate_premium(spot, atm_strike, "PE", T, iv)
                pe_alive = False
            break

    # If somehow didn't exit (short day), use last candle
    if ce_exit_prem is None:
        last = candles[-1]
        T_last = dte_fraction(ref_date, idx_name, candle_time_minutes(last))
        ce_exit_prem = estimate_premium(last["close"], atm_strike, "CE", T_last, iv)
    if pe_exit_prem is None:
        last = candles[-1]
        T_last = dte_fraction(ref_date, idx_name, candle_time_minutes(last))
        pe_exit_prem = estimate_premium(last["close"], atm_strike, "PE", T_last, iv)

    # P&L for seller: entry premium - exit premium
    ce_pnl = (ce_premium_entry - ce_exit_prem) * lot_size
    pe_pnl = (pe_premium_entry - pe_exit_prem) * lot_size
    charges = calc_charges(ce_premium_entry, ce_exit_prem, lot_size) + \
              calc_charges(pe_premium_entry, pe_exit_prem, lot_size)

    return {
        "strategy": f"straddle_sl{int(sl_pct*100)}",
        "index": idx_name,
        "date": str(ref_date),
        "spot_entry": round(spot_entry, 2),
        "atm_strike": atm_strike,
        "ce_entry": round(ce_premium_entry, 2),
        "pe_entry": round(pe_premium_entry, 2),
        "total_premium": round(total_premium, 2),
        "ce_exit": round(ce_exit_prem, 2),
        "pe_exit": round(pe_exit_prem, 2),
        "ce_pnl": round(ce_pnl, 2),
        "pe_pnl": round(pe_pnl, 2),
        "charges": round(charges, 2),
        "net_pnl": round(ce_pnl + pe_pnl - charges, 2),
    }


# ═══════════════════════════════════════════════════════════════════════
# STRATEGY 2: Short Strangle with Auto-Adjustments
# ═══════════════════════════════════════════════════════════════════════
def run_short_strangle(candles, idx_name, ref_date, max_adjustments=3):
    """Sell OTM CE + PE at 9:25, adjust when any leg doubles. Exit 3:10 PM."""
    idx = INDEXES[idx_name]
    iv = idx["iv_annual"]
    lot_size = idx["lot_size"] * LOTS
    step = idx["strike_step"]
    offset = idx["strangle_offset"]

    # Find 9:25 candle
    entry_candle = None
    for c in candles:
        h, m = candle_time_hm(c)
        if h == 9 and m >= 25:
            entry_candle = c
            break
    if not entry_candle:
        return None

    total_pnl = 0.0
    total_charges = 0.0
    adjustments = 0
    trades = []

    def open_strangle(candle, ref_spot=None):
        spot = ref_spot or candle["close"]
        atm = round_strike(spot, step)
        ce_strike = atm + offset
        pe_strike = atm - offset
        mins = candle_time_minutes(candle)
        T = dte_fraction(ref_date, idx_name, mins)
        ce_prem = estimate_premium(spot, ce_strike, "CE", T, iv)
        pe_prem = estimate_premium(spot, pe_strike, "PE", T, iv)
        return {
            "ce_strike": ce_strike, "pe_strike": pe_strike,
            "ce_entry": ce_prem, "pe_entry": pe_prem,
            "entry_spot": spot, "entry_mins": mins,
        }

    pos = open_strangle(entry_candle)
    entry_idx = candles.index(entry_candle)

    for c in candles[entry_idx + 1:]:
        h, m = candle_time_hm(c)
        mins = candle_time_minutes(c)
        T = dte_fraction(ref_date, idx_name, mins)
        spot = c["close"]

        ce_prem_now = estimate_premium(spot, pos["ce_strike"], "CE", T, iv)
        pe_prem_now = estimate_premium(spot, pos["pe_strike"], "PE", T, iv)

        # Check if any leg doubled (adjustment trigger)
        ce_doubled = ce_prem_now >= pos["ce_entry"] * 2
        pe_doubled = pe_prem_now >= pos["pe_entry"] * 2

        need_adjust = (ce_doubled or pe_doubled) and adjustments < max_adjustments

        if need_adjust or (h >= 15 and m >= 10):
            # Close current position
            ce_pnl = (pos["ce_entry"] - ce_prem_now) * lot_size
            pe_pnl = (pos["pe_entry"] - pe_prem_now) * lot_size
            ch = calc_charges(pos["ce_entry"], ce_prem_now, lot_size) + \
                 calc_charges(pos["pe_entry"], pe_prem_now, lot_size)

            total_pnl += ce_pnl + pe_pnl
            total_charges += ch
            trades.append({
                "ce_strike": pos["ce_strike"], "pe_strike": pos["pe_strike"],
                "ce_pnl": round(ce_pnl, 2), "pe_pnl": round(pe_pnl, 2),
            })

            if h >= 15 and m >= 10:
                break

            # Re-enter at new level
            adjustments += 1
            pos = open_strangle(c)
            continue

    return {
        "strategy": "strangle_adj",
        "index": idx_name,
        "date": str(ref_date),
        "adjustments": adjustments,
        "num_trades": len(trades),
        "total_pnl": round(total_pnl, 2),
        "charges": round(total_charges, 2),
        "net_pnl": round(total_pnl - total_charges, 2),
    }


# ═══════════════════════════════════════════════════════════════════════
# STRATEGY 3: ORB — Opening Range Breakout
# ═══════════════════════════════════════════════════════════════════════
def run_orb(candles, idx_name, ref_date, range_minutes=30):
    """Buy options on breakout of first 30-min range. 2:1 R:R. Exit 2:30 PM."""
    idx = INDEXES[idx_name]
    iv = idx["iv_annual"]
    lot_size = idx["lot_size"] * LOTS
    step = idx["strike_step"]
    skip_range = idx["orb_skip_range"]

    # Build opening range (9:15 - 9:45)
    range_high = -float("inf")
    range_low = float("inf")
    range_end_idx = 0

    for i, c in enumerate(candles):
        h, m = candle_time_hm(c)
        if h == 9 and m < 15:
            continue
        if h > 9 or (h == 9 and m >= 15 + range_minutes):
            range_end_idx = i
            break
        range_high = max(range_high, c["high"])
        range_low = min(range_low, c["low"])

    if range_high == -float("inf"):
        return None

    orb_range = range_high - range_low
    if orb_range < skip_range:
        return {"strategy": "orb", "index": idx_name, "date": str(ref_date),
                "net_pnl": 0, "reason": f"range too small ({orb_range:.0f})", "traded": False}

    # Watch for breakout
    direction = None
    entry_spot = None
    entry_candle = None
    sl_spot = None

    for c in candles[range_end_idx:]:
        h, m = candle_time_hm(c)
        if h >= 14 and m >= 30:
            break  # no breakout today

        if c["close"] > range_high:
            direction = "LONG"
            entry_spot = c["close"]
            entry_candle = c
            sl_spot = range_low
            break
        elif c["close"] < range_low:
            direction = "SHORT"
            entry_spot = c["close"]
            entry_candle = c
            sl_spot = range_high
            break

    if direction is None:
        return {"strategy": "orb", "index": idx_name, "date": str(ref_date),
                "net_pnl": 0, "reason": "no breakout", "traded": False}

    # Buy ATM option in breakout direction
    risk = abs(entry_spot - sl_spot)
    target_move = risk * 2  # 2:1 R:R

    atm = round_strike(entry_spot, step)
    opt_type = "CE" if direction == "LONG" else "PE"
    mins_entry = candle_time_minutes(entry_candle)
    T_entry = dte_fraction(ref_date, idx_name, mins_entry)
    entry_premium = estimate_premium(entry_spot, atm, opt_type, T_entry, iv)

    if entry_premium < 5:
        return {"strategy": "orb", "index": idx_name, "date": str(ref_date),
                "net_pnl": 0, "reason": "premium too thin", "traded": False}

    # Walk through candles after entry
    exit_premium = None
    exit_reason = "time"
    e_idx = candles.index(entry_candle)

    for c in candles[e_idx + 1:]:
        h, m = candle_time_hm(c)
        mins = candle_time_minutes(c)
        T = dte_fraction(ref_date, idx_name, mins)
        spot = c["close"]

        if direction == "LONG":
            # SL: spot drops to range_low
            if c["low"] <= sl_spot:
                exit_premium = estimate_premium(sl_spot, atm, opt_type, T, iv)
                exit_reason = "sl"
                break
            # Target: spot goes up by 2x risk
            if c["high"] >= entry_spot + target_move:
                tgt_spot = entry_spot + target_move
                exit_premium = estimate_premium(tgt_spot, atm, opt_type, T, iv)
                exit_reason = "target"
                break
        else:
            if c["high"] >= sl_spot:
                exit_premium = estimate_premium(sl_spot, atm, opt_type, T, iv)
                exit_reason = "sl"
                break
            if c["low"] <= entry_spot - target_move:
                tgt_spot = entry_spot - target_move
                exit_premium = estimate_premium(tgt_spot, atm, opt_type, T, iv)
                exit_reason = "target"
                break

        # Exit by 2:30 PM
        if h >= 14 and m >= 30:
            exit_premium = estimate_premium(spot, atm, opt_type, T, iv)
            exit_reason = "time"
            break

    if exit_premium is None:
        last = candles[-1]
        T_last = dte_fraction(ref_date, idx_name, candle_time_minutes(last))
        exit_premium = estimate_premium(last["close"], atm, opt_type, T_last, iv)

    # P&L for buyer: exit - entry
    pnl = (exit_premium - entry_premium) * lot_size
    charges = calc_charges(entry_premium, exit_premium, lot_size)

    return {
        "strategy": "orb",
        "index": idx_name,
        "date": str(ref_date),
        "direction": direction,
        "entry_spot": round(entry_spot, 2),
        "orb_range": round(orb_range, 2),
        "entry_premium": round(entry_premium, 2),
        "exit_premium": round(exit_premium, 2),
        "exit_reason": exit_reason,
        "pnl": round(pnl, 2),
        "charges": round(charges, 2),
        "net_pnl": round(pnl - charges, 2),
        "traded": True,
    }


# ═══════════════════════════════════════════════════════════════════════
# STRATEGY 4: Expiry Day Theta Crush (0DTE)
# ═══════════════════════════════════════════════════════════════════════
def run_expiry_straddle(candles, idx_name, ref_date):
    """Sell ATM straddle on expiry day at 9:30 AM, exit 3:10 PM. Only on expiry days."""
    if not is_expiry_day(ref_date, idx_name):
        return {"strategy": "expiry_theta", "index": idx_name, "date": str(ref_date),
                "net_pnl": 0, "reason": "not expiry day", "traded": False}

    idx = INDEXES[idx_name]
    iv = idx["iv_annual"] * 1.15  # IV is usually elevated on expiry day
    lot_size = idx["lot_size"] * LOTS
    step = idx["strike_step"]

    # Find 9:30 candle
    entry_candle = None
    for c in candles:
        h, m = candle_time_hm(c)
        if h == 9 and m >= 30:
            entry_candle = c
            break
    if not entry_candle:
        return None

    spot_entry = entry_candle["close"]
    atm = round_strike(spot_entry, step)
    mins_entry = candle_time_minutes(entry_candle)
    T_entry = dte_fraction(ref_date, idx_name, mins_entry)

    ce_entry = estimate_premium(spot_entry, atm, "CE", T_entry, iv)
    pe_entry = estimate_premium(spot_entry, atm, "PE", T_entry, iv)
    total_prem = ce_entry + pe_entry

    # SL: if total premium doubles (combined)
    combined_sl = total_prem * 1.5  # 50% SL on combined

    e_idx = candles.index(entry_candle)
    ce_exit = pe_exit = None

    for c in candles[e_idx + 1:]:
        h, m = candle_time_hm(c)
        mins = candle_time_minutes(c)
        T = dte_fraction(ref_date, idx_name, mins)
        spot = c["close"]

        ce_now = estimate_premium(spot, atm, "CE", T, iv)
        pe_now = estimate_premium(spot, atm, "PE", T, iv)

        # Combined SL check
        if ce_now + pe_now >= combined_sl:
            ce_exit = ce_now
            pe_exit = pe_now
            break

        if h >= 15 and m >= 10:
            ce_exit = ce_now
            pe_exit = pe_now
            break

    if ce_exit is None:
        last = candles[-1]
        T_last = dte_fraction(ref_date, idx_name, candle_time_minutes(last))
        ce_exit = estimate_premium(last["close"], atm, "CE", T_last, iv)
        pe_exit = estimate_premium(last["close"], atm, "PE", T_last, iv)

    ce_pnl = (ce_entry - ce_exit) * lot_size
    pe_pnl = (pe_entry - pe_exit) * lot_size
    charges = calc_charges(ce_entry, ce_exit, lot_size) + \
              calc_charges(pe_entry, pe_exit, lot_size)

    return {
        "strategy": "expiry_theta",
        "index": idx_name,
        "date": str(ref_date),
        "spot_entry": round(spot_entry, 2),
        "total_premium": round(total_prem, 2),
        "ce_pnl": round(ce_pnl, 2),
        "pe_pnl": round(pe_pnl, 2),
        "charges": round(charges, 2),
        "net_pnl": round(ce_pnl + pe_pnl - charges, 2),
        "traded": True,
    }


# ═══════════════════════════════════════════════════════════════════════
# Runner + Report
# ═══════════════════════════════════════════════════════════════════════
def get_trading_days(year, start_month=1, end_month=9):
    """Generate weekday dates for the given range."""
    days = []
    d = date(year, start_month, 1)
    end = date(year, end_month + 1, 1) if end_month < 12 else date(year + 1, 1, 1)
    while d < end:
        if d.weekday() < 5:  # Mon-Fri
            days.append(d)
        d += timedelta(days=1)
    return days


def grade(pct_green):
    if pct_green >= 80:
        return "A+"
    if pct_green >= 70:
        return "A"
    if pct_green >= 60:
        return "B"
    if pct_green >= 50:
        return "C"
    return "F"


def print_strategy_report(name, results):
    """Print summary for one strategy."""
    traded = [r for r in results if r.get("traded", True) and r.get("net_pnl") is not None]
    if not traded:
        print(f"\n  {name}: NO TRADES\n")
        return

    pnls = [r["net_pnl"] for r in traded]
    daily_pnl = defaultdict(float)
    for r in traded:
        daily_pnl[r["date"]] += r["net_pnl"]

    daily_vals = list(daily_pnl.values())
    green_days = sum(1 for v in daily_vals if v > 0)
    red_days = sum(1 for v in daily_vals if v <= 0)
    total_days = len(daily_vals)
    pct_green = green_days / total_days * 100 if total_days else 0
    total_pnl = sum(pnls)
    avg_daily = total_pnl / total_days if total_days else 0
    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / len(pnls) * 100 if pnls else 0

    # Max drawdown
    cum = 0
    peak = 0
    max_dd = 0
    for v in daily_vals:
        cum += v
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)

    # Monthly breakdown
    monthly = defaultdict(float)
    monthly_days = defaultdict(lambda: [0, 0])  # [green, total]
    for d_str, pnl in daily_pnl.items():
        mon = d_str[:7]
        monthly[mon] += pnl
        monthly_days[mon][1] += 1
        if pnl > 0:
            monthly_days[mon][0] += 1

    g = grade(pct_green)

    print(f"\n{'═' * 65}")
    print(f"  {name}   [Grade: {g}]")
    print(f"{'═' * 65}")
    print(f"  Trading days: {total_days} | Trades: {len(pnls)}")
    print(f"  Green days: {green_days}/{total_days} ({pct_green:.0f}%)")
    print(f"  Win rate: {wins}/{len(pnls)} ({win_rate:.0f}%)")
    print(f"  Net P&L: ₹{total_pnl:+,.0f}")
    print(f"  Avg daily: ₹{avg_daily:+,.0f}")
    print(f"  Max drawdown: ₹{max_dd:,.0f}")
    if daily_vals:
        best = max(daily_vals)
        worst = min(daily_vals)
        print(f"  Best day: ₹{best:+,.0f} | Worst day: ₹{worst:+,.0f}")

    print(f"\n  Monthly:")
    for mon in sorted(monthly):
        g_d, t_d = monthly_days[mon]
        mp = monthly[mon]
        pct = g_d / t_d * 100 if t_d else 0
        bar = "█" * int(pct / 5)
        print(f"    {mon}: ₹{mp:>+8,.0f}  ({g_d}/{t_d} green = {pct:.0f}%) {bar}")
    print()


def run_all(year, start_month, end_month, strategy_filter=None):
    uclient = UpstoxData()
    os.makedirs(CACHE_DIR, exist_ok=True)
    trading_days = get_trading_days(year, start_month, end_month)

    strategies = {
        "straddle_sl25": [],    # Short straddle 25% SL
        "straddle_sl30": [],    # Short straddle 30% SL
        "strangle_adj": [],     # Short strangle with adjustments
        "orb": [],              # Opening range breakout
        "expiry_theta": [],     # Expiry day only
    }

    if strategy_filter:
        strategies = {k: v for k, v in strategies.items() if strategy_filter in k}

    total = len(trading_days)
    skipped = 0

    for i, day in enumerate(trading_days):
        if i % 10 == 0:
            print(f"  Processing {day} ({i+1}/{total})...", flush=True)

        for idx_name in ["NIFTY", "BANKNIFTY", "SENSEX"]:
            candles = fetch_day_candles(uclient, idx_name, day)
            if not candles or len(candles) < 20:
                skipped += 1
                continue

            if "straddle_sl25" in strategies:
                r = run_short_straddle(candles, idx_name, day, sl_pct=0.25)
                if r:
                    strategies["straddle_sl25"].append(r)

            if "straddle_sl30" in strategies:
                r = run_short_straddle(candles, idx_name, day, sl_pct=0.30)
                if r:
                    strategies["straddle_sl30"].append(r)

            if "strangle_adj" in strategies:
                r = run_short_strangle(candles, idx_name, day)
                if r:
                    strategies["strangle_adj"].append(r)

            if "orb" in strategies:
                r = run_orb(candles, idx_name, day)
                if r:
                    strategies["orb"].append(r)

            if "expiry_theta" in strategies:
                r = run_expiry_straddle(candles, idx_name, day)
                if r:
                    strategies["expiry_theta"].append(r)

    print(f"\n  Done. {skipped} day/index combos skipped (no data).\n")

    # ─── Individual strategy reports ────────────────────────────────
    print(f"\n{'━' * 65}")
    print(f"  INDIVIDUAL STRATEGY RESULTS ({year}-{start_month:02d} to {year}-{end_month:02d})")
    print(f"{'━' * 65}")

    for name, results in strategies.items():
        print_strategy_report(name, results)

    # ─── Hybrid combinations ───────────────────────────────────────
    print(f"\n{'━' * 65}")
    print(f"  HYBRID COMBINATIONS")
    print(f"{'━' * 65}")

    # Hybrid 1: Strangle + ORB
    if "strangle_adj" in strategies and "orb" in strategies:
        combined = strategies["strangle_adj"] + [r for r in strategies["orb"] if r.get("traded")]
        print_strategy_report("HYBRID: Strangle + ORB", combined)

    # Hybrid 2: Straddle (non-expiry) + Expiry theta
    if "straddle_sl30" in strategies and "expiry_theta" in strategies:
        non_expiry_straddle = []
        for r in strategies["straddle_sl30"]:
            d = date.fromisoformat(r["date"])
            idx = r["index"]
            if not is_expiry_day(d, idx):
                non_expiry_straddle.append(r)
        expiry_trades = [r for r in strategies["expiry_theta"] if r.get("traded")]
        combined2 = non_expiry_straddle + expiry_trades
        print_strategy_report("HYBRID: Straddle(non-exp) + ExpiryTheta", combined2)

    # Hybrid 3: Strangle + Expiry theta
    if "strangle_adj" in strategies and "expiry_theta" in strategies:
        non_expiry_strangle = []
        for r in strategies["strangle_adj"]:
            d = date.fromisoformat(r["date"])
            idx = r["index"]
            if not is_expiry_day(d, idx):
                non_expiry_strangle.append(r)
        expiry_trades = [r for r in strategies["expiry_theta"] if r.get("traded")]
        combined3 = non_expiry_strangle + expiry_trades
        print_strategy_report("HYBRID: Strangle(non-exp) + ExpiryTheta", combined3)

    # ─── Comparison table ──────────────────────────────────────────
    print(f"\n{'━' * 65}")
    print(f"  COMPARISON TABLE")
    print(f"{'━' * 65}")
    print(f"  {'Strategy':<35} {'Net P&L':>10} {'Green%':>7} {'Grade':>6}")
    print(f"  {'─' * 60}")

    all_strats = dict(strategies)
    if "strangle_adj" in strategies and "orb" in strategies:
        all_strats["HYBRID: Strangle+ORB"] = strategies["strangle_adj"] + \
            [r for r in strategies["orb"] if r.get("traded")]

    for name, results in all_strats.items():
        traded = [r for r in results if r.get("traded", True) and r.get("net_pnl") is not None]
        if not traded:
            continue
        daily_pnl = defaultdict(float)
        for r in traded:
            daily_pnl[r["date"]] += r["net_pnl"]
        daily_vals = list(daily_pnl.values())
        total_pnl = sum(daily_vals)
        green = sum(1 for v in daily_vals if v > 0)
        pct = green / len(daily_vals) * 100 if daily_vals else 0
        g = grade(pct)
        print(f"  {name:<35} ₹{total_pnl:>+9,.0f} {pct:>5.0f}%  {g:>5}")
    print()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--months", default="1-8", help="e.g. 1-6 or 3-9")
    p.add_argument("--strategy", default=None, help="filter: straddle, strangle, orb, expiry")
    a = p.parse_args()

    parts = a.months.split("-")
    sm, em = int(parts[0]), int(parts[1])

    print(f"\n  Multi-Strategy Backtester")
    print(f"  Period: {a.year}-{sm:02d} to {a.year}-{em:02d}")
    print(f"  Indexes: NIFTY, BANKNIFTY")
    print(f"  Lots: {LOTS} per trade\n")

    run_all(a.year, sm, em, a.strategy)

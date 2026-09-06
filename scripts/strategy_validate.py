"""Validate top 3 strategies against actual recent data — detailed trade log.

Shows candle-by-candle detail so you can cross-check against real option chain.

Usage:
  .venv/bin/python3 scripts/strategy_validate.py --from 2026-08-31 --to 2026-09-04
"""
import os, sys, math, warnings, pickle, argparse
warnings.filterwarnings("ignore")

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

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
        "vol_skip_range": 120,
    },
    "BANKNIFTY": {
        "key": "NSE_INDEX|Nifty Bank",
        "lot_size": 30,
        "strike_step": 100,
        "iv_annual": 0.17,
        "vol_skip_range": 250,
    },
}

LOTS = 1
CACHE_DIR = os.path.join(config.DATA_DIR, "ml_cache")
EXPIRY_WEEKDAY = {"NIFTY": 1, "BANKNIFTY": 2}

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
    if T <= 0: return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)

def bs_put(S, K, T, sigma, r=0.07):
    if T <= 0: return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)

def est_prem(spot, strike, opt_type, T_frac, iv):
    T = max(T_frac, 1e-6)
    return bs_call(spot, strike, T, iv) if opt_type == "CE" else bs_put(spot, strike, T, iv)

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

def candle_time_hm(candle):
    ts = candle["date"]
    return int(ts[11:13]), int(ts[14:16])

def candle_time_str(candle):
    return candle["date"][11:16]

def candle_time_minutes(candle):
    h, m = candle_time_hm(candle)
    return (h - 9) * 60 + (m - 15)

def days_to_expiry(ref_date, idx_name):
    exp_wd = EXPIRY_WEEKDAY.get(idx_name, 3)
    return (exp_wd - ref_date.weekday()) % 7

def dte_fraction(ref_date, idx_name, minutes_into_day=0):
    dte = days_to_expiry(ref_date, idx_name)
    total_minutes_in_day = 375
    day_fraction = max(0, (total_minutes_in_day - minutes_into_day) / total_minutes_in_day)
    return (dte + day_fraction) / 365.0

def first_candle_range(candles):
    high = -float("inf")
    low = float("inf")
    count = 0
    for c in candles:
        h, m = candle_time_hm(c)
        if h == 9 and m < 15: continue
        if count >= 3: break
        high = max(high, c["high"])
        low = min(low, c["low"])
        count += 1
    return high - low if high != -float("inf") else 0


# ═══════════════════════════════════════════════════════════════════════
# Detailed straddle runner — prints every step
# ═══════════════════════════════════════════════════════════════════════
def run_detailed(candles, idx_name, ref_date, *,
                 label, entry_hour, entry_min, sl_pct,
                 combined_sl=False, trailing=False, vol_filter=False):
    idx = INDEXES[idx_name]
    iv = idx["iv_annual"]
    lot_size = idx["lot_size"] * LOTS
    step = idx["strike_step"]

    dte = days_to_expiry(ref_date, idx_name)
    weekday = ref_date.strftime("%A")

    # Day overview
    day_open = candles[0]["open"]
    day_high = max(c["high"] for c in candles)
    day_low = min(c["low"] for c in candles)
    day_close = candles[-1]["close"]
    day_range = day_high - day_low
    day_change_pct = (day_close - day_open) / day_open * 100

    print(f"\n    {idx_name} | DTE={dte} | Open={day_open:.0f} High={day_high:.0f} "
          f"Low={day_low:.0f} Close={day_close:.0f}")
    print(f"    Range={day_range:.0f}pts | Change={day_change_pct:+.2f}%")

    # Vol filter check
    fr = first_candle_range(candles)
    print(f"    First 15-min range: {fr:.0f} pts (threshold={idx['vol_skip_range']})")
    if vol_filter and fr > idx["vol_skip_range"]:
        print(f"    >>> SKIPPED (vol filter)")
        return {"net_pnl": 0, "traded": False, "reason": "vol_filter"}

    # Find entry candle
    entry_candle = None
    for c in candles:
        h, m = candle_time_hm(c)
        if h > entry_hour or (h == entry_hour and m >= entry_min):
            entry_candle = c
            break
    if not entry_candle:
        print(f"    >>> No entry candle found")
        return {"net_pnl": 0, "traded": False, "reason": "no_entry"}

    spot_entry = entry_candle["close"]
    atm = round_strike(spot_entry, step)
    mins_entry = candle_time_minutes(entry_candle)
    T_entry = dte_fraction(ref_date, idx_name, mins_entry)

    ce_entry = est_prem(spot_entry, atm, "CE", T_entry, iv)
    pe_entry = est_prem(spot_entry, atm, "PE", T_entry, iv)
    total_prem = ce_entry + pe_entry

    print(f"\n    ENTRY @ {candle_time_str(entry_candle)}: Spot={spot_entry:.0f} ATM={atm}")
    print(f"    CE premium: ₹{ce_entry:.1f} | PE premium: ₹{pe_entry:.1f} | Total: ₹{total_prem:.1f}")

    if combined_sl:
        sl_level = total_prem * (1 + sl_pct)
        print(f"    Combined SL: total >= ₹{sl_level:.1f} ({sl_pct*100:.0f}% above entry)")
    else:
        ce_sl = ce_entry * (1 + sl_pct)
        pe_sl = pe_entry * (1 + sl_pct)
        print(f"    Per-leg SL: CE >= ₹{ce_sl:.1f} | PE >= ₹{pe_sl:.1f} ({sl_pct*100:.0f}%)")

    if trailing:
        print(f"    Trailing: lock at 40% profit, give back 20% from peak")

    ce_alive = pe_alive = True
    ce_exit_prem = pe_exit_prem = None
    exit_reason = "time"
    best_combined_profit = 0.0
    trail_active = False

    e_idx = candles.index(entry_candle)
    for c in candles[e_idx + 1:]:
        h, m = candle_time_hm(c)
        mins = candle_time_minutes(c)
        T = dte_fraction(ref_date, idx_name, mins)
        spot = c["close"]

        ce_now = est_prem(spot, atm, "CE", T, iv)
        pe_now = est_prem(spot, atm, "PE", T, iv)
        ce_worst = est_prem(c["high"], atm, "CE", T, iv)
        pe_worst = est_prem(c["low"], atm, "PE", T, iv)

        # SL checks
        if combined_sl:
            worst_total = ce_worst + pe_worst
            if worst_total >= sl_level:
                ce_exit_prem = ce_now
                pe_exit_prem = pe_now
                exit_reason = "combined_sl"
                print(f"\n    EXIT @ {candle_time_str(c)}: COMBINED SL HIT")
                print(f"    Spot={spot:.0f} | CE={ce_now:.1f} PE={pe_now:.1f} Total={ce_now+pe_now:.1f}")
                break
        else:
            if ce_alive and ce_worst >= ce_sl:
                ce_exit_prem = ce_sl
                ce_alive = False
                print(f"    {candle_time_str(c)}: CE SL hit (spot={c['high']:.0f}, CE={ce_worst:.1f} >= {ce_sl:.1f})")
            if pe_alive and pe_worst >= pe_sl:
                pe_exit_prem = pe_sl
                pe_alive = False
                print(f"    {candle_time_str(c)}: PE SL hit (spot={c['low']:.0f}, PE={pe_worst:.1f} >= {pe_sl:.1f})")

        # Trailing
        if trailing and ce_alive and pe_alive:
            current_profit = total_prem - (ce_now + pe_now)
            profit_pct = current_profit / total_prem
            best_combined_profit = max(best_combined_profit, current_profit)
            if profit_pct >= 0.40:
                trail_active = True
            if trail_active and best_combined_profit > 0:
                give_back = best_combined_profit * 0.20
                if current_profit < best_combined_profit - give_back:
                    ce_exit_prem = ce_now
                    pe_exit_prem = pe_now
                    exit_reason = "trailing"
                    print(f"\n    EXIT @ {candle_time_str(c)}: TRAILING STOP")
                    print(f"    Peak profit ₹{best_combined_profit:.1f}, current ₹{current_profit:.1f}")
                    break

        # Time exit
        if h >= 15 and m >= 10:
            if ce_alive:
                ce_exit_prem = ce_now
            if pe_alive:
                pe_exit_prem = pe_now
            exit_reason = "time_3:10"
            print(f"\n    EXIT @ {candle_time_str(c)}: TIME EXIT (3:10 PM)")
            print(f"    Spot={spot:.0f} | CE={ce_now:.1f} PE={pe_now:.1f}")
            break

    # Fallback
    if ce_exit_prem is None:
        last = candles[-1]
        T_last = dte_fraction(ref_date, idx_name, candle_time_minutes(last))
        ce_exit_prem = est_prem(last["close"], atm, "CE", T_last, iv)
    if pe_exit_prem is None:
        last = candles[-1]
        T_last = dte_fraction(ref_date, idx_name, candle_time_minutes(last))
        pe_exit_prem = est_prem(last["close"], atm, "PE", T_last, iv)

    ce_pnl = (ce_entry - ce_exit_prem) * lot_size
    pe_pnl = (pe_entry - pe_exit_prem) * lot_size
    charges = calc_charges(ce_entry, ce_exit_prem, lot_size) + \
              calc_charges(pe_entry, pe_exit_prem, lot_size)
    net = ce_pnl + pe_pnl - charges

    print(f"\n    P&L: CE ₹{ce_pnl:+,.0f} + PE ₹{pe_pnl:+,.0f} - charges ₹{charges:,.0f} = ₹{net:+,.0f}")
    print(f"    Exit reason: {exit_reason}")

    result = "GREEN" if net > 0 else "RED"
    print(f"    >>> {result}")

    return {"net_pnl": round(net, 2), "traded": True, "exit_reason": exit_reason,
            "ce_pnl": round(ce_pnl, 2), "pe_pnl": round(pe_pnl, 2)}


def run_validation(from_date, to_date):
    uclient = UpstoxData()
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Build date range
    days = []
    d = from_date
    while d <= to_date:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)

    strategies = {
        "kitchen_sink": dict(
            entry_hour=9, entry_min=30, sl_pct=0.35,
            combined_sl=True, trailing=True, vol_filter=True,
        ),
        "vf_920_sl30": dict(
            entry_hour=9, entry_min=20, sl_pct=0.30,
            combined_sl=False, trailing=False, vol_filter=True,
        ),
        "entry_945_sl30": dict(
            entry_hour=9, entry_min=45, sl_pct=0.30,
            combined_sl=False, trailing=False, vol_filter=False,
        ),
    }

    # Track daily results per strategy
    strat_daily = {s: {} for s in strategies}

    for day in days:
        weekday = day.strftime("%A")
        print(f"\n{'═' * 70}")
        print(f"  {day} ({weekday})")
        print(f"{'═' * 70}")

        for sname, params in strategies.items():
            print(f"\n  ── {sname} ──")
            day_pnl = 0.0
            day_traded = False

            for idx_name in ["NIFTY", "BANKNIFTY"]:
                candles = fetch_day_candles(uclient, idx_name, day)
                if not candles or len(candles) < 20:
                    print(f"    {idx_name}: NO DATA")
                    continue

                r = run_detailed(candles, idx_name, day, label=sname, **params)
                if r.get("traded"):
                    day_pnl += r["net_pnl"]
                    day_traded = True

            if day_traded:
                result = "GREEN" if day_pnl > 0 else "RED"
                print(f"\n  {sname} DAY TOTAL: ₹{day_pnl:+,.0f} [{result}]")
                strat_daily[sname][str(day)] = day_pnl
            else:
                print(f"\n  {sname} DAY TOTAL: SKIPPED (no trades)")
                strat_daily[sname][str(day)] = 0

    # ── Summary ──
    print(f"\n\n{'━' * 70}")
    print(f"  WEEKLY SUMMARY: {from_date} to {to_date}")
    print(f"{'━' * 70}")

    for sname in strategies:
        daily = strat_daily[sname]
        traded_days = {d: p for d, p in daily.items() if p != 0 or
                       any(True for _ in [])}  # all days
        vals = list(daily.values())
        total = sum(vals)
        green = sum(1 for v in vals if v > 0)
        traded = sum(1 for v in vals if v != 0)

        print(f"\n  {sname}:")
        for d in sorted(daily):
            v = daily[d]
            tag = "GREEN" if v > 0 else ("RED" if v < 0 else "SKIP")
            day_name = date.fromisoformat(d).strftime("%a")
            print(f"    {d} ({day_name}): ₹{v:>+8,.0f}  [{tag}]")

        print(f"    ────────────────────────────")
        print(f"    Total: ₹{total:+,.0f} | Green: {green}/{len(vals)} | Traded: {traded}/{len(vals)}")

        # Scale projections
        if traded > 0:
            avg = total / len(vals)
            print(f"    Avg/day: ₹{avg:+,.0f}")
            for lots in [1, 3, 5]:
                print(f"      {lots} lot(s): ₹{avg * lots:+,.0f}/day → ₹{avg * lots * 22:+,.0f}/month")

    print()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="from_date", required=True)
    p.add_argument("--to", dest="to_date", required=True)
    a = p.parse_args()

    fd = date.fromisoformat(a.from_date)
    td = date.fromisoformat(a.to_date)

    print(f"\n  Strategy Validation — Detailed Trade Log")
    print(f"  Period: {fd} to {td}")
    print(f"  Strategies: kitchen_sink, vf_920_sl30, entry_945_sl30")
    print(f"  Indexes: NIFTY + BANKNIFTY | Lots: {LOTS}\n")

    run_validation(fd, td)

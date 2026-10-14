#!/usr/bin/env python3
"""Backtest: 9:20 AM Short Straddle on Nifty 50 — v2 with adjustments.

Improvements over v1:
1. Move SL to cost: when one leg SL hits, other leg's SL → entry price
2. Combined SL: total straddle loss capped at 50% of total premium collected
3. Wider per-leg SL options tested
"""
import asyncio, sys, os, argparse
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo(config.TIMEZONE)
NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"
NIFTY_LOT = 75
STRIKE_STEP = 50
ENTRY_TIME = dtime(9, 20)
EXIT_TIME = dtime(15, 15)
SLIPPAGE = 0.50
NUM_LOTS = 2
QTY = NIFTY_LOT * NUM_LOTS  # 150

# Strategy variants to test
VARIANTS = [
    {"name": "SL25_plain",    "leg_sl": 0.25, "move_sl_to_cost": False, "combined_sl": None},
    {"name": "SL25_moveSL",   "leg_sl": 0.25, "move_sl_to_cost": True,  "combined_sl": None},
    {"name": "SL25_comb50",   "leg_sl": 0.25, "move_sl_to_cost": True,  "combined_sl": 0.50},
    {"name": "SL40_moveSL",   "leg_sl": 0.40, "move_sl_to_cost": True,  "combined_sl": None},
    {"name": "SL50_moveSL",   "leg_sl": 0.50, "move_sl_to_cost": True,  "combined_sl": None},
]


def parse_ts(ts_str):
    if "+" in ts_str or ts_str.endswith("Z"):
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(IST)
    return datetime.fromisoformat(ts_str).replace(tzinfo=IST)


async def fetch_candles(ud, key, from_dt, to_dt):
    try:
        c = await asyncio.wait_for(
            asyncio.to_thread(ud.historical_data, key, from_dt, to_dt, "1minute"),
            timeout=15
        )
        return c if c else []
    except Exception:
        return []


def simulate_variant(v, ce_candles, pe_candles, ce_sell, pe_sell):
    """Simulate one variant. Returns (ce_pnl, pe_pnl, ce_reason, pe_reason)."""
    leg_sl_pct = v["leg_sl"]
    move_to_cost = v["move_sl_to_cost"]
    combined_sl_pct = v["combined_sl"]

    ce_sl_price = ce_sell * (1 + leg_sl_pct)
    pe_sl_price = pe_sell * (1 + leg_sl_pct)
    total_premium = ce_sell + pe_sell
    combined_sl_abs = total_premium * combined_sl_pct if combined_sl_pct else None

    ce_exit = pe_exit = None
    ce_reason = pe_reason = "eod"

    # Build minute-aligned timeline
    # Merge CE and PE candles by time
    ce_by_time = {}
    pe_by_time = {}
    all_times = set()
    for c in ce_candles:
        t = parse_ts(c["date"])
        if t.time() >= ENTRY_TIME:
            ce_by_time[t] = c
            all_times.add(t)
    for c in pe_candles:
        t = parse_ts(c["date"])
        if t.time() >= ENTRY_TIME:
            pe_by_time[t] = c
            all_times.add(t)

    for t in sorted(all_times):
        if ce_exit is not None and pe_exit is not None:
            break

        ce_c = ce_by_time.get(t)
        pe_c = pe_by_time.get(t)

        # Current unrealized P&L
        ce_cur = ce_c["high"] if ce_c and ce_exit is None else (ce_exit if ce_exit else ce_sell)
        pe_cur = pe_c["high"] if pe_c and pe_exit is None else (pe_exit if pe_exit else pe_sell)

        # Check combined SL first
        if combined_sl_abs and ce_exit is None and pe_exit is None:
            ce_loss = (ce_cur - ce_sell) if ce_c else 0
            pe_loss = (pe_cur - pe_sell) if pe_c else 0
            if (ce_loss + pe_loss) * QTY >= combined_sl_abs * QTY:
                # Exit both
                ce_exit = ce_cur + SLIPPAGE
                pe_exit = pe_cur + SLIPPAGE
                ce_reason = pe_reason = "comb_sl"
                break

        # Check CE leg SL
        if ce_exit is None and ce_c and ce_c["high"] >= ce_sl_price:
            ce_exit = ce_sl_price + SLIPPAGE
            ce_reason = "sl"
            if move_to_cost and pe_exit is None:
                pe_sl_price = pe_sell  # move PE SL to cost (breakeven)

        # Check PE leg SL
        if pe_exit is None and pe_c and pe_c["high"] >= pe_sl_price:
            pe_exit = pe_sl_price + SLIPPAGE
            pe_reason = "sl"
            if move_to_cost and ce_exit is None:
                ce_sl_price = ce_sell  # move CE SL to cost (breakeven)

        # EOD exit
        if t.time() >= EXIT_TIME:
            if ce_exit is None and ce_c:
                ce_exit = ce_c["close"] + SLIPPAGE
                ce_reason = "eod"
            if pe_exit is None and pe_c:
                pe_exit = pe_c["close"] + SLIPPAGE
                pe_reason = "eod"

    # Fallback
    if ce_exit is None:
        ce_exit = ce_sell  # breakeven
    if pe_exit is None:
        pe_exit = pe_sell

    ce_pnl = (ce_sell - ce_exit) * QTY
    pe_pnl = (pe_sell - pe_exit) * QTY
    return ce_pnl, pe_pnl, ce_reason, pe_reason


async def run_one_day(ud, DATE, nifty_option_keys):
    from_dt = datetime.combine(DATE, dtime(9, 15), tzinfo=IST)
    to_dt = datetime.combine(DATE, dtime(15, 30), tzinfo=IST)

    nifty_candles = await fetch_candles(ud, NIFTY_INDEX_KEY, from_dt, to_dt)
    if len(nifty_candles) < 10:
        return None

    spot_at_920 = None
    for c in nifty_candles:
        t = parse_ts(c["date"])
        if t.time() >= ENTRY_TIME:
            spot_at_920 = c["close"]
            break
    if spot_at_920 is None:
        return None

    atm_strike = round(spot_at_920 / STRIKE_STEP) * STRIKE_STEP

    ce_info = nifty_option_keys.get(atm_strike, {}).get("CE")
    pe_info = nifty_option_keys.get(atm_strike, {}).get("PE")
    if not ce_info or not pe_info:
        return None

    ce_candles, pe_candles = await asyncio.gather(
        fetch_candles(ud, ce_info["key"], from_dt, to_dt),
        fetch_candles(ud, pe_info["key"], from_dt, to_dt),
    )
    if len(ce_candles) < 10 or len(pe_candles) < 10:
        return None

    # Entry prices
    ce_entry = pe_entry = None
    for c in ce_candles:
        t = parse_ts(c["date"])
        if t.time() >= ENTRY_TIME:
            ce_entry = c["close"]
            break
    for c in pe_candles:
        t = parse_ts(c["date"])
        if t.time() >= ENTRY_TIME:
            pe_entry = c["close"]
            break
    if ce_entry is None or pe_entry is None:
        return None

    ce_sell = ce_entry - SLIPPAGE
    pe_sell = pe_entry - SLIPPAGE

    # Run all variants
    results = {}
    for v in VARIANTS:
        ce_pnl, pe_pnl, ce_r, pe_r = simulate_variant(v, ce_candles, pe_candles, ce_sell, pe_sell)
        results[v["name"]] = {
            "pnl": ce_pnl + pe_pnl,
            "ce_pnl": ce_pnl, "pe_pnl": pe_pnl,
            "ce_reason": ce_r, "pe_reason": pe_r,
        }

    return {"date": DATE, "spot": spot_at_920, "atm": atm_strike,
            "ce_sell": ce_sell, "pe_sell": pe_sell, "variants": results}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="d_from", required=True)
    parser.add_argument("--to", dest="d_to", required=True)
    args = parser.parse_args()
    d_from = datetime.strptime(args.d_from, "%Y-%m-%d").date()
    d_to = datetime.strptime(args.d_to, "%Y-%m-%d").date()

    token = load_cached_token() or os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        print("ERROR: No token"); return

    ud = UpstoxData(access_token=token)
    master = ud._load_master()
    print(f"Loaded {len(master)} instruments", flush=True)

    from collections import defaultdict
    nifty_option_keys = defaultdict(dict)
    for inst in master:
        if inst.get("segment") != "NSE_FO":
            continue
        asset = (inst.get("asset_symbol") or inst.get("name") or "").upper()
        if asset != "NIFTY":
            continue
        ot = inst.get("instrument_type", "").upper()
        if ot not in ("CE", "PE"):
            continue
        strike = float(inst.get("strike_price") or 0)
        if strike <= 0:
            continue
        ikey = inst.get("instrument_key")
        expiry = inst.get("expiry")
        existing = nifty_option_keys[strike].get(ot)
        if existing is None or (expiry and existing.get("expiry") and expiry < existing["expiry"]):
            nifty_option_keys[strike][ot] = {"key": ikey, "expiry": expiry}

    print(f"Nifty strikes: {len(nifty_option_keys)}", flush=True)

    # Header
    vnames = [v["name"] for v in VARIANTS]
    hdr = f"{'Date':>12} {'Spot':>8} {'ATM':>6} {'Prem':>6}"
    for vn in vnames:
        hdr += f" {vn:>14}"
    print(hdr, flush=True)
    print("-" * len(hdr), flush=True)

    # Accumulators
    totals = {vn: {"pnl": 0, "wins": 0, "losses": 0, "days": 0,
                   "running": 0, "peak": 0, "max_dd": 0} for vn in vnames}

    d = d_from
    while d <= d_to:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue

        res = await run_one_day(ud, d, nifty_option_keys)
        if res is None:
            d += timedelta(days=1)
            continue

        row = f"{res['date'].isoformat():>12} {res['spot']:>8.1f} {res['atm']:>6.0f} {res['ce_sell']+res['pe_sell']:>6.0f}"
        for vn in vnames:
            vr = res["variants"][vn]
            pnl = vr["pnl"]
            row += f" {pnl:>+14,.0f}"
            t = totals[vn]
            t["pnl"] += pnl
            t["days"] += 1
            if pnl > 0: t["wins"] += 1
            else: t["losses"] += 1
            t["running"] += pnl
            if t["running"] > t["peak"]: t["peak"] = t["running"]
            dd = t["peak"] - t["running"]
            if dd > t["max_dd"]: t["max_dd"] = dd

        print(row, flush=True)
        d += timedelta(days=1)

    # Summary
    print(f"\n{'='*80}")
    print(f"SUMMARY: {d_from} to {d_to} | {QTY} units ({NUM_LOTS} lots)")
    print(f"{'='*80}")
    print(f"{'Variant':>16} {'Days':>5} {'PnL':>10} {'Avg/d':>8} {'WR%':>5} {'MaxDD':>10}")
    print("-" * 60)
    for vn in vnames:
        t = totals[vn]
        if t["days"] == 0:
            continue
        wr = 100 * t["wins"] / t["days"]
        print(f"{vn:>16} {t['days']:>5} {t['pnl']:>+10,.0f} {t['pnl']/t['days']:>+8,.0f} {wr:>4.0f}% {t['max_dd']:>10,.0f}")


if __name__ == "__main__":
    asyncio.run(main())

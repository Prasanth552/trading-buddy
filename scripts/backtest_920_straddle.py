#!/usr/bin/env python3
"""Backtest: 9:20 AM Short Straddle on Nifty 50.

Sell ATM CE + ATM PE at 9:20 AM.
SL: 25% rise in premium per leg (leg exits independently).
Exit: 3:15 PM or SL hit.
Capital: margin ~₹1.2L per lot (1 lot = 75 units).
"""
import asyncio, sys, os, argparse, time as _time
from datetime import datetime, timedelta, time as dtime, date as dt_date
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
SL_PCT = 0.25          # 25% SL per leg
SLIPPAGE_PER_UNIT = 0.50  # ₹0.50 slippage on each leg entry+exit
MARGIN_PER_LOT = 120000   # approx margin needed
NUM_LOTS = 2              # trade 2 lots (₹2.4L margin on ₹3L capital)


def parse_ts(ts_str):
    if "+" in ts_str or ts_str.endswith("Z"):
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(IST)
    return datetime.fromisoformat(ts_str).replace(tzinfo=IST)


async def fetch_candles(ud, key, from_dt, to_dt):
    """Fetch 1-min candles synchronously (wrapped for consistency)."""
    try:
        c = await asyncio.wait_for(
            asyncio.to_thread(ud.historical_data, key, from_dt, to_dt, "1minute"),
            timeout=15
        )
        return c if c else []
    except Exception:
        return []


async def run_one_day(ud, DATE, nifty_option_keys):
    """Run straddle backtest for one day. Returns dict or None (holiday)."""
    from_dt = datetime.combine(DATE, dtime(9, 15), tzinfo=IST)
    to_dt = datetime.combine(DATE, dtime(15, 30), tzinfo=IST)

    # 1. Fetch Nifty index candles
    nifty_candles = await fetch_candles(ud, NIFTY_INDEX_KEY, from_dt, to_dt)
    if len(nifty_candles) < 10:
        return None  # holiday or no data

    # 2. Find spot price at 9:20
    spot_at_920 = None
    for c in nifty_candles:
        t = parse_ts(c["date"])
        if t.time() >= ENTRY_TIME:
            spot_at_920 = c["close"]
            break
    if spot_at_920 is None:
        return None

    # 3. ATM strike = nearest 50-point
    atm_strike = round(spot_at_920 / STRIKE_STEP) * STRIKE_STEP

    # 4. Get CE and PE instrument keys
    ce_info = nifty_option_keys.get(atm_strike, {}).get("CE")
    pe_info = nifty_option_keys.get(atm_strike, {}).get("PE")
    if not ce_info or not pe_info:
        print(f"  No option keys for strike {atm_strike}", flush=True)
        return None

    # 5. Fetch CE and PE candles
    ce_candles, pe_candles = await asyncio.gather(
        fetch_candles(ud, ce_info["key"], from_dt, to_dt),
        fetch_candles(ud, pe_info["key"], from_dt, to_dt),
    )
    if len(ce_candles) < 10 or len(pe_candles) < 10:
        print(f"  Insufficient option candles CE={len(ce_candles)} PE={len(pe_candles)}", flush=True)
        return None

    # 6. Find entry prices at 9:20
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

    # Apply slippage: selling, so we get less (price - slippage)
    ce_sell = ce_entry - SLIPPAGE_PER_UNIT
    pe_sell = pe_entry - SLIPPAGE_PER_UNIT
    straddle_premium = ce_sell + pe_sell
    qty = NIFTY_LOT * NUM_LOTS

    # 7. Simulate with SL per leg
    ce_sl = ce_sell * (1 + SL_PCT)   # exit CE if price rises above this
    pe_sl = pe_sell * (1 + SL_PCT)   # exit PE if price rises above this

    ce_exit = None
    ce_exit_time = None
    ce_exit_reason = "eod"
    pe_exit = None
    pe_exit_time = None
    pe_exit_reason = "eod"

    # Walk through candles
    for c in ce_candles:
        t = parse_ts(c["date"])
        if t.time() < ENTRY_TIME:
            continue
        if ce_exit is not None:
            continue
        # Check SL (high of candle hits SL)
        if c["high"] >= ce_sl:
            ce_exit = ce_sl + SLIPPAGE_PER_UNIT  # buy back at SL + slippage
            ce_exit_time = t
            ce_exit_reason = "sl"
        elif t.time() >= EXIT_TIME:
            ce_exit = c["close"] + SLIPPAGE_PER_UNIT  # buy back at close + slippage
            ce_exit_time = t
            ce_exit_reason = "eod"

    for c in pe_candles:
        t = parse_ts(c["date"])
        if t.time() < ENTRY_TIME:
            continue
        if pe_exit is not None:
            continue
        if c["high"] >= pe_sl:
            pe_exit = pe_sl + SLIPPAGE_PER_UNIT
            pe_exit_time = t
            pe_exit_reason = "sl"
        elif t.time() >= EXIT_TIME:
            pe_exit = c["close"] + SLIPPAGE_PER_UNIT
            pe_exit_time = t
            pe_exit_reason = "eod"

    # If somehow no exit (shouldn't happen), use last candle
    if ce_exit is None:
        ce_exit = ce_candles[-1]["close"] + SLIPPAGE_PER_UNIT
        ce_exit_reason = "eod"
    if pe_exit is None:
        pe_exit = pe_candles[-1]["close"] + SLIPPAGE_PER_UNIT
        pe_exit_reason = "eod"

    # PnL = (sell - buy_back) * qty per leg
    ce_pnl = (ce_sell - ce_exit) * qty
    pe_pnl = (pe_sell - pe_exit) * qty
    total_pnl = ce_pnl + pe_pnl

    return {
        "date": DATE,
        "spot": spot_at_920,
        "atm": atm_strike,
        "ce_entry": ce_sell,
        "pe_entry": pe_sell,
        "premium": straddle_premium,
        "ce_exit": ce_exit,
        "pe_exit": pe_exit,
        "ce_reason": ce_exit_reason,
        "pe_reason": pe_exit_reason,
        "ce_pnl": ce_pnl,
        "pe_pnl": pe_pnl,
        "total_pnl": total_pnl,
        "qty": qty,
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="d_from", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="d_to", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()

    d_from = datetime.strptime(args.d_from, "%Y-%m-%d").date()
    d_to = datetime.strptime(args.d_to, "%Y-%m-%d").date()

    token = load_cached_token()
    if not token:
        token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        print("ERROR: No Upstox token"); return

    ud = UpstoxData(access_token=token)
    master = ud._load_master()
    print(f"Loaded {len(master)} instruments", flush=True)

    # Build Nifty option map: strike → {CE: {key, expiry}, PE: {key, expiry}}
    import re
    from collections import defaultdict
    nifty_option_keys = defaultdict(dict)

    for inst in master:
        seg = inst.get("segment")
        if seg != "NSE_FO":
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
        # Keep nearest expiry
        existing = nifty_option_keys[strike].get(ot)
        if existing is None or (expiry and existing.get("expiry") and expiry < existing["expiry"]):
            nifty_option_keys[strike][ot] = {"key": ikey, "expiry": expiry}

    print(f"Nifty option strikes: {len(nifty_option_keys)}", flush=True)

    # Header
    print(f"\n{'Date':>12} {'Spot':>8} {'ATM':>6} {'CE_sell':>8} {'PE_sell':>8} "
          f"{'CE_exit':>8} {'PE_exit':>8} {'CE_rsn':>6} {'PE_rsn':>6} "
          f"{'CE_PnL':>8} {'PE_PnL':>8} {'Total':>9}", flush=True)
    print("-" * 120, flush=True)

    results = []
    d = d_from
    while d <= d_to:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue

        res = await run_one_day(ud, d, nifty_option_keys)
        if res is None:
            d += timedelta(days=1)
            continue

        results.append(res)
        r = res
        print(f"{r['date'].isoformat():>12} {r['spot']:>8.1f} {r['atm']:>6.0f} "
              f"{r['ce_entry']:>8.2f} {r['pe_entry']:>8.2f} "
              f"{r['ce_exit']:>8.2f} {r['pe_exit']:>8.2f} "
              f"{r['ce_reason']:>6} {r['pe_reason']:>6} "
              f"{r['ce_pnl']:>+8.0f} {r['pe_pnl']:>+8.0f} {r['total_pnl']:>+9.0f}",
              flush=True)

        d += timedelta(days=1)

    if not results:
        print("\nNo trading days found."); return

    # Summary
    total_pnl = sum(r["total_pnl"] for r in results)
    wins = sum(1 for r in results if r["total_pnl"] > 0)
    losses = sum(1 for r in results if r["total_pnl"] <= 0)
    days = len(results)

    # Drawdown
    running = 0
    peak = 0
    max_dd = 0
    for r in results:
        running += r["total_pnl"]
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    # SL stats
    ce_sl_count = sum(1 for r in results if r["ce_reason"] == "sl")
    pe_sl_count = sum(1 for r in results if r["pe_reason"] == "sl")
    both_sl = sum(1 for r in results if r["ce_reason"] == "sl" and r["pe_reason"] == "sl")

    print(f"\n{'='*60}")
    print(f"SUMMARY: {d_from} to {d_to}")
    print(f"{'='*60}")
    print(f"  Trading days: {days}")
    print(f"  Net PnL:      ₹{total_pnl:+,.0f}")
    print(f"  Avg/day:      ₹{total_pnl/days:+,.0f}")
    print(f"  Win days:     {wins}/{days} ({100*wins/days:.0f}%)")
    print(f"  Loss days:    {losses}/{days}")
    print(f"  Max drawdown: ₹{max_dd:,.0f}")
    print(f"  CE SL hits:   {ce_sl_count}/{days}")
    print(f"  PE SL hits:   {pe_sl_count}/{days}")
    print(f"  Both SL:      {both_sl}/{days}")
    print(f"  Lots:         {NUM_LOTS} ({NIFTY_LOT*NUM_LOTS} units)")
    print(f"  Slippage:     ₹{SLIPPAGE_PER_UNIT}/unit per leg")
    print(f"  Margin used:  ~₹{MARGIN_PER_LOT*NUM_LOTS:,}")


if __name__ == "__main__":
    asyncio.run(main())

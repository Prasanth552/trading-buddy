#!/usr/bin/env python3
"""Backtest OEH + ORB for today — shows what results would look like
if both scanners ran cleanly from 9:15 with no restarts.

Usage (on VM):
    PYTHONPATH=. .venv/bin/python3 scripts/backtest_today_oeh_orb.py
"""
from __future__ import annotations
import sys, os, time as _t
from datetime import datetime, date
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from scripts.backtest_all5 import (
    _load_master_data, _prefetch_equity_candles, _run_strategy_sim,
    scan_oeh_at_tick, scan_orb_at_tick, _parallel_fetch,
    CAPITAL, OEH_LOSS_CAP, OEH_PROFIT_CAP, OEH_SCAN_START, OEH_SCAN_END,
    ORB_LOSS_CAP, ORB_PROFIT_CAP, ORB_SCAN_START, ORB_SCAN_END,
    BLOCKLIST,
)
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo("Asia/Kolkata")


def main():
    today = date.today()
    print(f"\n{'#' * 80}")
    print(f"  OEH + ORB Backtest for {today} ({today.strftime('%A')})")
    print(f"  Capital: ₹{CAPITAL:,.0f} per strategy")
    print(f"{'#' * 80}")

    token = load_cached_token()
    if not token:
        print("ERROR: No Upstox token for today!")
        return
    ud = UpstoxData(access_token=token)

    print("\n  Loading instrument master...")
    master = ud._load_master()
    eq_keys, universe, opt_master, lot_sizes = _load_master_data(ud, master)
    print(f"  Universe: {len(universe)} stocks")

    from_dt = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(today, datetime.min.time()).replace(hour=15, minute=30)

    print("  Fetching 5-min equity candles...")
    t0 = _t.time()
    candles_5m = _prefetch_equity_candles(ud, eq_keys, universe, from_dt, to_dt, "5minute")
    print(f"  Fetched {len(candles_5m)} stocks in {_t.time() - t0:.1f}s")

    # --- OEH ---
    print(f"\n  {'='*70}")
    print(f"  OEH — Open=High (bearish, rescans every 5 min, 09:20–15:20)")
    print(f"  Capital: ₹{CAPITAL:,.0f} | Loss cap: ₹{OEH_LOSS_CAP:,} | Profit cap: ₹{OEH_PROFIT_CAP:,}")
    print(f"  {'='*70}")
    oeh_results = _run_strategy_sim(
        "OEH", today, ud, opt_master, lot_sizes, candles_5m,
        scan_fn=lambda tick: scan_oeh_at_tick(candles_5m, universe, tick),
        loss_cap=OEH_LOSS_CAP, profit_cap=OEH_PROFIT_CAP,
        scan_start=OEH_SCAN_START, scan_end=OEH_SCAN_END,
        scan_interval_min=5, hard_exit_time=None, verbose=True,
    )

    # --- ORB ---
    print(f"\n  {'='*70}")
    print(f"  ORB — Opening Range Breakout (rescans every 5 min, 09:25–14:30)")
    print(f"  Capital: ₹{CAPITAL:,.0f} | Loss cap: ₹{ORB_LOSS_CAP:,} | Profit cap: ₹{ORB_PROFIT_CAP:,}")
    print(f"  {'='*70}")
    orb_results = _run_strategy_sim(
        "ORB", today, ud, opt_master, lot_sizes, candles_5m,
        scan_fn=lambda tick: scan_orb_at_tick(candles_5m, universe, tick),
        loss_cap=ORB_LOSS_CAP, profit_cap=ORB_PROFIT_CAP,
        scan_start=ORB_SCAN_START, scan_end=ORB_SCAN_END,
        scan_interval_min=5, hard_exit_time=None, verbose=True,
    )

    # --- Summary ---
    print(f"\n  {'='*70}")
    print(f"  COMBINED SUMMARY — {today}")
    print(f"  {'='*70}")

    for label, results in [("OEH", oeh_results), ("ORB", orb_results)]:
        if not results:
            print(f"  {label}: No trades")
            continue
        pnl = sum(r["pnl"] for r in results)
        wins = sum(1 for r in results if r["pnl"] > 0)
        losses = len(results) - wins
        peak_sum = sum(r["peak"] for r in results)
        leak = peak_sum - pnl
        print(f"  {label}: {len(results)} trades | {wins}W/{losses}L | PnL: ₹{pnl:>+,.0f} | Peak: ₹{peak_sum:>+,.0f} | Leak: ₹{leak:>,.0f}")

    all_results = (oeh_results or []) + (orb_results or [])
    if all_results:
        grand = sum(r["pnl"] for r in all_results)
        grand_peak = sum(r["peak"] for r in all_results)
        print(f"\n  GRAND TOTAL: ₹{grand:>+,.0f}  (Peak potential: ₹{grand_peak:>+,.0f})")
    else:
        print("\n  No trades at all today.")


if __name__ == "__main__":
    main()

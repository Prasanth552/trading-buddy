"""Test ML model on truly unseen data.

Trains model on data up to a cutoff date, then tests on N trading days after.

Usage:
  .venv/bin/python3 scripts/ml_unseen_test.py --cutoff 2026-08-22 --test-days 5
"""
import os, sys, json, warnings
warnings.filterwarnings("ignore", category=FutureWarning)

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from src.ml.bot import (
    compute_features, train_model, _fetch_spot, _label_spot,
    FEATURE_COLS, INDEXES, SPOT_KEYS, LOT_SIZES, STRIKE_STEPS, LOTS,
    FLOOR, MAX_LOSS, SLIPPAGE_PCT, MAX_TRADES_PER_DAY,
    DAILY_LOSS_CAP, SCAN_START_HOUR, SCAN_START_MIN, SCAN_END_HOUR,
    SCAN_END_MIN, SKIP_HOURS, ITM_MIN, ITM_MAX, PE_DELTA, CACHE_DIR,
)
from src.broker.upstox_data import UpstoxData

IST = ZoneInfo("Asia/Kolkata")


def run_unseen_test(cutoff_date, test_days=5, min_conf=0.50):
    print(f"\n{'='*60}")
    print(f"  UNSEEN DATA TEST")
    print(f"  Train up to: {cutoff_date}")
    print(f"  Test: next {test_days} trading days")
    print(f"  Confidence threshold: {min_conf*100:.0f}%")
    print(f"{'='*60}\n")

    uclient = UpstoxData()
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Train models with cutoff
    models = {}
    for idx in INDEXES:
        print(f"Training {idx} model (data up to {cutoff_date})...")
        model = train_model(idx, end_dt=cutoff_date)
        if model:
            models[idx] = model
            print(f"  ✓ {idx} model trained")
        else:
            print(f"  ✗ {idx} model failed")

    if not models:
        print("No models trained!")
        return

    # Find test trading days after cutoff
    test_dates = []
    d = cutoff_date + timedelta(days=1)
    while len(test_dates) < test_days:
        if d.weekday() < 5:
            # Check if market was open (try to get candles)
            has_data = False
            for idx in INDEXES:
                candles = _fetch_spot(uclient, idx, d)
                if candles and len(candles) > 5:
                    has_data = True
                    break
            if has_data:
                test_dates.append(d)
        d += timedelta(days=1)
        if d > cutoff_date + timedelta(days=30):
            break

    print(f"\nTest dates: {[str(d) for d in test_dates]}\n")

    # Run through each test day
    grand_total_pnl = 0
    grand_trades = 0
    grand_wins = 0
    daily_results = []

    for test_date in test_dates:
        # Get prev day data
        prev_date = test_date - timedelta(days=1)
        while prev_date.weekday() >= 5:
            prev_date -= timedelta(days=1)

        prev_day_data = {}
        for idx in INDEXES:
            candles = _fetch_spot(uclient, idx, prev_date)
            if candles and len(candles) > 1:
                day_open = candles[0]["open"]
                day_close = candles[-1]["close"]
                day_high = max(c["high"] for c in candles)
                day_low = min(c["low"] for c in candles)
                prev_day_data[idx] = {
                    "change_pct": ((day_close - day_open) / day_open) * 100,
                    "range_pct": ((day_high - day_low) / day_open) * 100,
                }

        day_pnl = 0
        day_trades = 0
        day_wins = 0

        print(f"{'─'*60}")
        print(f"  {test_date} ({test_date.strftime('%A')})")
        print(f"{'─'*60}")

        for idx in INDEXES:
            if idx not in models:
                continue

            model = models[idx]
            candles = _fetch_spot(uclient, idx, test_date)
            if not candles or len(candles) < 10:
                continue

            df = compute_features(candles)
            if df.empty:
                continue

            prev = prev_day_data.get(idx, {})
            df["prev_day_change_pct"] = prev.get("change_pct", 0)
            df["prev_day_range_pct"] = prev.get("range_pct", 0)

            idx_losses = 0
            idx_trades = 0

            for _, row in df.iterrows():
                h = int(row["hour"])
                m = int(row.get("minute", 0))

                if h < SCAN_START_HOUR or (h == SCAN_START_HOUR and m < SCAN_START_MIN):
                    continue
                if h > SCAN_END_HOUR or (h == SCAN_END_HOUR and m > SCAN_END_MIN):
                    continue
                if h in SKIP_HOURS:
                    continue
                if row["candle_num"] < 5:
                    continue
                if idx_losses >= DAILY_LOSS_CAP:
                    break
                if day_trades >= MAX_TRADES_PER_DAY:
                    break

                features = row[FEATURE_COLS].to_frame().T
                features = features.replace([np.inf, -np.inf], np.nan).fillna(0)
                prob = model.predict_proba(features)[0][1]

                if prob < min_conf:
                    continue

                spot = row["close"]
                candle_idx = int(row["candle_num"])
                label, pnl = _label_spot(candles, candle_idx, idx)
                time_str = candles[candle_idx]["date"][11:16]

                day_trades += 1
                idx_trades += 1
                day_pnl += pnl
                if pnl > 0:
                    day_wins += 1
                if pnl < 0:
                    idx_losses += 1

                result = "✅" if pnl > 0 else "❌"
                print(f"  {time_str} | {idx} PE | conf={prob*100:.0f}% | "
                      f"spot={spot:.0f} | P&L=₹{pnl:+,.0f} {result}")

        if day_trades == 0:
            print(f"  No signals")

        grand_total_pnl += day_pnl
        grand_trades += day_trades
        grand_wins += day_wins
        daily_results.append({
            "date": str(test_date),
            "trades": day_trades,
            "wins": day_wins,
            "pnl": round(day_pnl, 2),
        })

    # Summary
    print(f"\n{'='*60}")
    print(f"  UNSEEN TEST SUMMARY")
    print(f"{'='*60}")
    print(f"  Period: {test_dates[0]} → {test_dates[-1]}" if test_dates else "  No test days")
    print(f"  Days traded: {sum(1 for d in daily_results if d['trades'] > 0)}/{len(test_dates)}")
    print(f"  Total trades: {grand_trades}")
    if grand_trades:
        print(f"  Wins: {grand_wins} | Losses: {grand_trades - grand_wins}")
        print(f"  Win Rate: {grand_wins/grand_trades*100:.0f}%")
    print(f"  Net P&L: ₹{grand_total_pnl:+,.0f}")
    if test_dates:
        trading_days = sum(1 for d in daily_results if d['trades'] > 0)
        if trading_days:
            print(f"  Avg P&L/day: ₹{grand_total_pnl/trading_days:+,.0f}")
    print()

    print("  Daily breakdown:")
    for d in daily_results:
        wr = f"{d['wins']/d['trades']*100:.0f}%" if d['trades'] else "—"
        print(f"    {d['date']} | {d['trades']} trades | WR {wr} | ₹{d['pnl']:+,.0f}")
    print()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--cutoff", required=True, help="Train up to this date (YYYY-MM-DD)")
    p.add_argument("--test-days", type=int, default=5)
    p.add_argument("--min-conf", type=float, default=0.50)
    a = p.parse_args()

    cutoff = datetime.strptime(a.cutoff, "%Y-%m-%d").date()
    run_unseen_test(cutoff, a.test_days, a.min_conf)

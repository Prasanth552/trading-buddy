"""Run ML model on a specific date's candles to see what signals it would generate.

Usage:
  .venv/bin/python3 scripts/ml_backtest_day.py --date 2026-09-04
"""
import os, sys, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from src.ml.bot import (
    compute_features, load_model, _fetch_spot, _label_spot,
    FEATURE_COLS, INDEXES, SPOT_KEYS, LOT_SIZES, STRIKE_STEPS, LOTS,
    FLOOR, MAX_LOSS, SLIPPAGE_PCT, MIN_CONFIDENCE, MAX_TRADES_PER_DAY,
    DAILY_LOSS_CAP, SCAN_START_HOUR, SCAN_START_MIN, SCAN_END_HOUR,
    SCAN_END_MIN, SKIP_HOURS, ITM_MIN, ITM_MAX, PE_DELTA, CACHE_DIR,
    MODEL_DIR,
)
from src.broker.upstox_data import UpstoxData
from src.notify.channel_listener import calc_charges

IST = ZoneInfo("Asia/Kolkata")


def run_day(target_date, indexes=None, verbose=False, min_conf=None):
    indexes = indexes or INDEXES
    uclient = UpstoxData()
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Load prev day data
    prev_date = target_date - timedelta(days=1)
    while prev_date.weekday() >= 5:
        prev_date -= timedelta(days=1)

    prev_day_data = {}
    for idx in indexes:
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

    total_pnl = 0
    total_trades = 0
    wins = 0

    for idx in indexes:
        model = load_model(idx)
        if not model:
            print(f"  ⚠ No model for {idx}")
            continue

        # Try cache first, then fetch directly
        candles = _fetch_spot(uclient, idx, target_date)
        if not candles or len(candles) < 10:
            # Direct fetch without cache
            spot_key = SPOT_KEYS.get(idx)
            print(f"  Fetching {idx} ({spot_key}) for {target_date}...")
            from_dt = datetime(target_date.year, target_date.month, target_date.day, 9, 15, 0, tzinfo=IST)
            to_dt = datetime(target_date.year, target_date.month, target_date.day, 15, 30, 0, tzinfo=IST)
            try:
                candles = uclient.historical_data(spot_key, from_dt, to_dt, "5minute")
                print(f"  Got {len(candles) if candles else 0} candles")
                if candles:
                    cache_path = os.path.join(CACHE_DIR, f"spot_{idx}_{target_date}.json")
                    with open(cache_path, "w") as f:
                        json.dump(candles, f)
            except Exception as e:
                print(f"  ⚠ Fetch error: {e}")
                candles = None
        if not candles or len(candles) < 10:
            print(f"  ⚠ No candles for {idx} on {target_date}")
            continue

        df = compute_features(candles)
        if df.empty:
            continue

        prev = prev_day_data.get(idx, {})
        df["prev_day_change_pct"] = prev.get("change_pct", 0)
        df["prev_day_range_pct"] = prev.get("range_pct", 0)

        day_losses = 0
        day_trades = 0
        print(f"\n{'='*60}")
        print(f"  {idx} — {target_date}")
        print(f"{'='*60}")

        for i, row in df.iterrows():
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
            if day_losses >= DAILY_LOSS_CAP:
                break
            if day_trades >= MAX_TRADES_PER_DAY:
                break

            features = row[FEATURE_COLS].to_frame().T
            features = features.replace([np.inf, -np.inf], np.nan).fillna(0)
            prob = model.predict_proba(features)[0][1]
            threshold = min_conf if min_conf is not None else MIN_CONFIDENCE
            time_str = candles[int(row["candle_num"])]["date"][11:16]

            if verbose:
                print(f"  {time_str} | spot={row['close']:.0f} | conf={prob*100:.1f}%"
                      + (" ◀ SIGNAL" if prob >= threshold else ""))

            if prob < threshold:
                continue

            # Signal!
            spot = row["close"]
            step = STRIKE_STEPS.get(idx, 50)
            itm_depth = (ITM_MIN + ITM_MAX) // 2
            strike = round((spot + itm_depth) / step) * step
            lot_size = LOT_SIZES.get(idx, 75)
            qty = lot_size * LOTS
            est_entry = itm_depth * 0.85 * (1 + SLIPPAGE_PCT / 100)

            # Simulate exit
            candle_idx = int(row["candle_num"])
            label, pnl = _label_spot(candles, candle_idx, idx)
            time_str = candles[candle_idx]["date"][11:16]

            day_trades += 1
            total_trades += 1
            if pnl > 0:
                wins += 1
            if pnl < 0:
                day_losses += 1
            total_pnl += pnl

            time_str = candles[candle_idx]["date"][11:16]
            result = "✅ WIN" if pnl > 0 else "❌ LOSS"
            print(f"  {time_str} | {idx} PE {strike} | conf={prob*100:.0f}% | "
                  f"spot={spot:.0f} | entry=₹{est_entry:.0f} | "
                  f"P&L=₹{pnl:+,.0f} {result}")

        if day_trades == 0:
            print(f"  No signals (confidence < {MIN_CONFIDENCE*100:.0f}%)")

    print(f"\n{'='*60}")
    print(f"  SUMMARY — {target_date}")
    print(f"{'='*60}")
    print(f"  Trades: {total_trades} | Wins: {wins} | Losses: {total_trades - wins}")
    if total_trades:
        print(f"  Win Rate: {wins/total_trades*100:.0f}%")
    print(f"  Net P&L: ₹{total_pnl:+,.0f}")
    print()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--index", default=None)
    p.add_argument("--verbose", "-v", action="store_true", help="Show all candle confidence scores")
    p.add_argument("--min-conf", type=float, default=None, help="Override min confidence (0-1)")
    a = p.parse_args()

    dt = datetime.strptime(a.date, "%Y-%m-%d").date()
    idxs = [a.index] if a.index else None
    run_day(dt, idxs, verbose=a.verbose, min_conf=a.min_conf)

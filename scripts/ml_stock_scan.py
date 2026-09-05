"""Scan all liquid F&O stocks + indexes for ML pattern profitability.

Trains per-symbol XGBoost models, tests on unseen dates, ranks results.
Uses PE_DELTA approximation for options P&L (same as index model).

Usage:
  .venv/bin/python3 scripts/ml_stock_scan.py --train-end 2026-08-22 --test-days 5
  .venv/bin/python3 scripts/ml_stock_scan.py --train-end 2026-08-22 --test-days 5 --symbols RELIANCE,HDFCBANK
"""
import os, sys, json, time, warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass

import numpy as np
import pandas as pd
from xgboost import XGBClassifier

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from src.ml.bot import compute_features, FEATURE_COLS, CACHE_DIR
from src.broker.upstox_data import UpstoxData
from src.notify.channel_listener import calc_charges
import config

IST = ZoneInfo("Asia/Kolkata")

# ─── All tradable symbols ───────────────────────────────────────────────
SYMBOLS = {
    # Indexes
    "NIFTY":      {"key": "NSE_INDEX|Nifty 50",      "lot": 75, "step": 50,  "lots": 3},
    "SENSEX":     {"key": "BSE_INDEX|SENSEX",         "lot": 20, "step": 100, "lots": 3},
    "BANKNIFTY":  {"key": "NSE_INDEX|Nifty Bank",     "lot": 15, "step": 100, "lots": 3},
    # F&O stocks
    "RELIANCE":   {"key": "NSE_EQ|INE002A01018",      "lot": 250,"step": 20,  "lots": 1},
    "HDFCBANK":   {"key": "NSE_EQ|INE040A01034",      "lot": 550,"step": 10,  "lots": 1},
    "ICICIBANK":  {"key": "NSE_EQ|INE090A01021",      "lot": 700,"step": 10,  "lots": 1},
    "INFY":       {"key": "NSE_EQ|INE009A01021",      "lot": 400,"step": 10,  "lots": 1},
    "TCS":        {"key": "NSE_EQ|INE467B01029",      "lot": 175,"step": 20,  "lots": 1},
    "SBIN":       {"key": "NSE_EQ|INE062A01020",      "lot": 1500,"step": 5,  "lots": 1},
    "AXISBANK":   {"key": "NSE_EQ|INE238A01034",      "lot": 625,"step": 10,  "lots": 1},
    "KOTAKBANK":  {"key": "NSE_EQ|INE237A01036",      "lot": 400,"step": 10,  "lots": 1},
    "ITC":        {"key": "NSE_EQ|INE154A01025",      "lot": 1600,"step": 5,  "lots": 1},
    "LT":         {"key": "NSE_EQ|INE018A01030",      "lot": 150,"step": 20,  "lots": 1},
    "BHARTIARTL": {"key": "NSE_EQ|INE397D01024",      "lot": 475,"step": 10,  "lots": 1},
    "HINDUNILVR": {"key": "NSE_EQ|INE030A01027",      "lot": 300,"step": 10,  "lots": 1},
    "MARUTI":     {"key": "NSE_EQ|INE585B01010",      "lot": 100,"step": 50,  "lots": 1},
    "TATAMOTORS": {"key": "NSE_EQ|INE155A01022",      "lot": 1400,"step": 5,  "lots": 1},
    "BAJFINANCE": {"key": "NSE_EQ|INE296A01032",      "lot": 125,"step": 50,  "lots": 1},
}

# ─── Config ──────────────────────────────────────────────────────────────
TRAIN_DAYS = 40
PE_DELTA = 0.85
ITM_MIN, ITM_MAX = 300, 900
SLIPPAGE_PCT = 0.5
FLOOR = 4000
MAX_LOSS = 4000
DAILY_LOSS_CAP = 2
MAX_TRADES = 5
SCAN_START = (9, 20)
SCAN_END = (10, 30)
SKIP_HOURS = {11, 12, 13, 14, 15}
MIN_CONF = 0.50


def fetch_candles(uclient, sym, ref_date):
    info = SYMBOLS[sym]
    cache = os.path.join(CACHE_DIR, f"spot_{sym}_{ref_date}.json")
    if os.path.exists(cache):
        with open(cache) as f:
            return json.load(f)
    y, m, d = ref_date.year, ref_date.month, ref_date.day
    from_dt = datetime(y, m, d, 9, 15, 0, tzinfo=IST)
    to_dt = datetime(y, m, d, 15, 30, 0, tzinfo=IST)
    try:
        candles = uclient.historical_data(info["key"], from_dt, to_dt, "5minute")
        time.sleep(0.3)
        if candles and len(candles) > 5:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cache, "w") as f:
                json.dump(candles, f)
            return candles
    except Exception as e:
        pass
    return None


def label_trade(candles, idx, sym):
    info = SYMBOLS[sym]
    if idx >= len(candles) - 1:
        return None, 0
    spot = candles[idx]["close"]
    lot_size = info["lot"]
    qty = lot_size * info["lots"]

    # For stocks, ITM depth scales with price
    price = spot
    if price < 500:
        itm_depth = 30
    elif price < 2000:
        itm_depth = 80
    elif price < 5000:
        itm_depth = 200
    else:
        itm_depth = (ITM_MIN + ITM_MAX) / 2

    est_entry = itm_depth * PE_DELTA * (1 + SLIPPAGE_PCT / 100)
    sl_price = est_entry * 0.50
    tgt_price = est_entry * 1.25
    peak_pnl = 0
    floor_armed = False

    for c in candles[idx + 1:]:
        opt_worst = -(c["high"] - spot) * PE_DELTA
        opt_best = -(c["low"] - spot) * PE_DELTA
        opt_p_worst = est_entry + opt_worst
        opt_p_best = est_entry + opt_best

        w_pnl = (opt_p_worst - est_entry) * qty
        if MAX_LOSS > 0 and w_pnl <= -MAX_LOSS:
            exit_p = est_entry - (MAX_LOSS / qty)
            gross = (exit_p - est_entry) * qty
            charges = calc_charges(est_entry, exit_p, qty)["total"]
            pnl = gross - charges
            return (1 if pnl > 0 else 0), pnl

        if opt_p_worst <= sl_price:
            gross = (sl_price - est_entry) * qty
            charges = calc_charges(est_entry, sl_price, qty)["total"]
            pnl = gross - charges
            return (1 if pnl > 0 else 0), pnl

        if opt_p_best >= tgt_price:
            gross = (tgt_price - est_entry) * qty
            charges = calc_charges(est_entry, tgt_price, qty)["total"]
            pnl = gross - charges
            return (1 if pnl > 0 else 0), pnl

        b_pnl = (opt_p_best - est_entry) * qty
        peak_pnl = max(peak_pnl, b_pnl)
        if peak_pnl >= FLOOR:
            floor_armed = True
        if floor_armed:
            cur = (opt_p_worst - est_entry) * qty
            if cur <= FLOOR:
                floor_p = est_entry + (FLOOR / qty)
                gross = (floor_p - est_entry) * qty
                charges = calc_charges(est_entry, floor_p, qty)["total"]
                pnl = gross - charges
                return (1 if pnl > 0 else 0), pnl

    last_move = -(candles[-1]["close"] - spot) * PE_DELTA
    eod_p = est_entry + last_move
    gross = (eod_p - est_entry) * qty
    charges = calc_charges(est_entry, eod_p, qty)["total"]
    pnl = gross - charges
    return (1 if pnl > 0 else 0), pnl


def train_symbol(uclient, sym, end_dt):
    start_dt = end_dt - timedelta(days=int(TRAIN_DAYS * 1.6))
    rows = []
    prev_change = 0.0
    prev_range = 0.0
    current = start_dt
    day_count = 0

    while current <= end_dt:
        if current.weekday() >= 5:
            current += timedelta(days=1)
            continue

        candles = fetch_candles(uclient, sym, current)
        if not candles or len(candles) < 10:
            current += timedelta(days=1)
            continue

        day_count += 1
        df = compute_features(candles)
        if df.empty:
            current += timedelta(days=1)
            continue

        df["prev_day_change_pct"] = prev_change
        df["prev_day_range_pct"] = prev_range

        day_open = candles[0]["open"]
        day_close = candles[-1]["close"]
        day_high = max(c["high"] for c in candles)
        day_low = min(c["low"] for c in candles)
        prev_change = ((day_close - day_open) / day_open) * 100
        prev_range = ((day_high - day_low) / day_open) * 100

        for _, row in df.iterrows():
            h = int(row["hour"])
            if h < 9 or (h == 9 and int(row["minute"]) < 20):
                continue
            if h in SKIP_HOURS or h >= 15:
                continue
            if row["candle_num"] < 5:
                continue

            label, pnl = label_trade(candles, int(row["candle_num"]), sym)
            if label is None:
                continue
            feat = row[FEATURE_COLS].to_dict()
            feat["label"] = label
            rows.append(feat)

        current += timedelta(days=1)

    if len(rows) < 50:
        return None, day_count, len(rows)

    data = pd.DataFrame(rows)
    X = data[FEATURE_COLS].replace([np.inf, -np.inf], np.nan).fillna(0)
    y = data["label"]
    pos = y.sum()
    neg = len(y) - pos
    if pos < 10 or neg < 10:
        return None, day_count, len(rows)

    model = XGBClassifier(
        n_estimators=200, max_depth=5, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        scale_pos_weight=neg / pos if pos > 0 else 1,
        eval_metric="logloss", verbosity=0,
        use_label_encoder=False,
    )
    model.fit(X, y)
    wr = pos / len(y) * 100
    return model, day_count, len(rows)


def test_symbol(uclient, sym, model, test_dates, min_conf):
    info = SYMBOLS[sym]
    total_pnl = 0
    total_trades = 0
    wins = 0
    daily = []

    for test_date in test_dates:
        prev_date = test_date - timedelta(days=1)
        while prev_date.weekday() >= 5:
            prev_date -= timedelta(days=1)

        prev_candles = fetch_candles(uclient, sym, prev_date)
        prev_change = 0
        prev_range = 0
        if prev_candles and len(prev_candles) > 1:
            do = prev_candles[0]["open"]
            dc = prev_candles[-1]["close"]
            dh = max(c["high"] for c in prev_candles)
            dl = min(c["low"] for c in prev_candles)
            prev_change = ((dc - do) / do) * 100
            prev_range = ((dh - dl) / do) * 100

        candles = fetch_candles(uclient, sym, test_date)
        if not candles or len(candles) < 10:
            continue

        df = compute_features(candles)
        if df.empty:
            continue

        df["prev_day_change_pct"] = prev_change
        df["prev_day_range_pct"] = prev_range

        day_pnl = 0
        day_trades = 0
        day_wins = 0
        day_losses = 0

        for _, row in df.iterrows():
            h = int(row["hour"])
            m = int(row.get("minute", 0))
            if h < SCAN_START[0] or (h == SCAN_START[0] and m < SCAN_START[1]):
                continue
            if h > SCAN_END[0] or (h == SCAN_END[0] and m > SCAN_END[1]):
                continue
            if h in SKIP_HOURS:
                continue
            if row["candle_num"] < 5:
                continue
            if day_losses >= DAILY_LOSS_CAP:
                break
            if day_trades >= MAX_TRADES:
                break

            features = row[FEATURE_COLS].to_frame().T
            features = features.replace([np.inf, -np.inf], np.nan).fillna(0)
            prob = model.predict_proba(features)[0][1]

            if prob < min_conf:
                continue

            candle_idx = int(row["candle_num"])
            label, pnl = label_trade(candles, candle_idx, sym)
            t = candles[candle_idx]["date"][11:16]

            day_trades += 1
            day_pnl += pnl
            if pnl > 0:
                day_wins += 1
            if pnl < 0:
                day_losses += 1

        total_pnl += day_pnl
        total_trades += day_trades
        wins += day_wins
        daily.append({"date": str(test_date), "trades": day_trades,
                       "wins": day_wins, "pnl": round(day_pnl, 2)})

    return total_pnl, total_trades, wins, daily


def find_trading_days(uclient, after_date, count, ref_sym="NIFTY"):
    dates = []
    d = after_date + timedelta(days=1)
    while len(dates) < count and d < after_date + timedelta(days=45):
        if d.weekday() < 5:
            candles = fetch_candles(uclient, ref_sym, d)
            if candles and len(candles) > 5:
                dates.append(d)
        d += timedelta(days=1)
    return dates


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--train-end", required=True, help="Train data cutoff (YYYY-MM-DD)")
    p.add_argument("--test-days", type=int, default=5)
    p.add_argument("--min-conf", type=float, default=0.50)
    p.add_argument("--symbols", default=None, help="Comma-separated symbols (default: all)")
    a = p.parse_args()

    train_end = datetime.strptime(a.train_end, "%Y-%m-%d").date()
    syms = a.symbols.split(",") if a.symbols else list(SYMBOLS.keys())

    print(f"\n{'═'*70}")
    print(f"  ML STOCK SCANNER — COMPREHENSIVE ANALYSIS")
    print(f"  Train period: ~{TRAIN_DAYS} days ending {train_end}")
    print(f"  Test: {a.test_days} trading days after cutoff")
    print(f"  Confidence: {a.min_conf*100:.0f}%")
    print(f"  Symbols: {len(syms)}")
    print(f"{'═'*70}\n")

    uclient = UpstoxData()
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Find test dates first
    print("Finding test trading days...")
    test_dates = find_trading_days(uclient, train_end, a.test_days)
    print(f"Test dates: {[str(d) for d in test_dates]}\n")

    if not test_dates:
        print("No test dates found!")
        return

    results = []

    for sym in syms:
        print(f"{'─'*70}")
        print(f"  {sym} (lot={SYMBOLS[sym]['lot']} × {SYMBOLS[sym]['lots']})")
        print(f"{'─'*70}")

        # Train
        print(f"  Training...", end=" ", flush=True)
        model, days, samples = train_symbol(uclient, sym, train_end)
        if model is None:
            print(f"FAILED ({days} days, {samples} samples — too few)")
            results.append({
                "symbol": sym, "status": "no_model",
                "train_days": days, "samples": samples,
            })
            continue
        print(f"OK ({days} days, {samples} samples)")

        # Test
        print(f"  Testing on {len(test_dates)} unseen days...", end=" ", flush=True)
        pnl, trades, wins, daily = test_symbol(uclient, sym, model, test_dates, a.min_conf)
        wr = wins / trades * 100 if trades else 0
        print(f"{trades} trades, {wins}W/{trades-wins}L, {wr:.0f}% WR, ₹{pnl:+,.0f}")

        for d in daily:
            dwr = f"{d['wins']}/{d['trades']}" if d['trades'] else "—"
            print(f"    {d['date']} | {dwr} | ₹{d['pnl']:+,.0f}")

        results.append({
            "symbol": sym, "status": "ok",
            "train_days": days, "samples": samples,
            "trades": trades, "wins": wins,
            "losses": trades - wins, "wr": round(wr, 1),
            "pnl": round(pnl, 2),
            "daily": daily,
        })

    # ─── RANKINGS ────────────────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print(f"  RANKINGS — sorted by net P&L")
    print(f"{'═'*70}")
    print(f"  {'Symbol':<14} {'Trades':>6} {'WR':>5} {'Net P&L':>12} {'Avg/Day':>10} {'Status'}")
    print(f"  {'─'*14} {'─'*6} {'─'*5} {'─'*12} {'─'*10} {'─'*10}")

    tradable = [r for r in results if r["status"] == "ok" and r["trades"] > 0]
    tradable.sort(key=lambda x: x["pnl"], reverse=True)

    total_pnl = 0
    total_trades = 0
    total_wins = 0
    profitable_syms = []

    for r in tradable:
        trading_days = sum(1 for d in r["daily"] if d["trades"] > 0)
        avg_day = r["pnl"] / trading_days if trading_days else 0
        status = "✅" if r["pnl"] > 0 else "❌"
        print(f"  {r['symbol']:<14} {r['trades']:>6} {r['wr']:>4.0f}% ₹{r['pnl']:>+10,.0f} ₹{avg_day:>+8,.0f} {status}")
        total_pnl += r["pnl"]
        total_trades += r["trades"]
        total_wins += r["wins"]
        if r["pnl"] > 0:
            profitable_syms.append(r["symbol"])

    no_model = [r for r in results if r["status"] == "no_model"]
    no_trades = [r for r in results if r["status"] == "ok" and r["trades"] == 0]

    if no_trades:
        for r in no_trades:
            print(f"  {r['symbol']:<14} {'0':>6} {'—':>5} {'₹0':>12} {'₹0':>10} ⚪ no signals")
    if no_model:
        for r in no_model:
            print(f"  {r['symbol']:<14} {'—':>6} {'—':>5} {'—':>12} {'—':>10} ⛔ no model")

    print(f"\n  {'─'*60}")
    overall_wr = total_wins / total_trades * 100 if total_trades else 0
    print(f"  COMBINED: {total_trades} trades, {overall_wr:.0f}% WR, ₹{total_pnl:+,.0f}")
    if test_dates:
        print(f"  Avg/day: ₹{total_pnl/len(test_dates):+,.0f}")
    print(f"  Profitable symbols: {', '.join(profitable_syms)}")
    print(f"  Recommended portfolio: top symbols with WR ≥ 60% and positive P&L")
    print()


if __name__ == "__main__":
    main()

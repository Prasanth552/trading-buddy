"""Consistent ML Strategy — universal model, full-year walk-forward, daily signal ranking.

Key differences from ml_stock_scan.py:
  1. ONE model trained on ALL symbols' data (not per-symbol)
  2. 6-month training windows (not 40 days)
  3. Walk-forward: train months 1-6, test month 7, slide forward
  4. Daily signal ranking: pick top N signals by confidence (not all above threshold)
  5. Consistency metrics: % profitable days, max drawdown, Sharpe, win streaks

Usage:
  .venv/bin/python3 scripts/ml_consistent.py --year 2026
  .venv/bin/python3 scripts/ml_consistent.py --year 2026 --budget 3
  .venv/bin/python3 scripts/ml_consistent.py --year 2026 --symbols NIFTY,SENSEX,ITC,BHARTIARTL
"""
import os, sys, json, time, math, warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

import numpy as np
import pandas as pd
from xgboost import XGBClassifier

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from src.ml.bot import FEATURE_COLS, CACHE_DIR
from src.broker.upstox_data import UpstoxData
import config

IST = ZoneInfo("Asia/Kolkata")


def _charges_total(entry_p, exit_p, qty):
    """Inlined calc_charges — returns total only (no dict alloc)."""
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


def _rolling_mean(arr, w, min_p=None):
    """Simple rolling mean using cumsum — 10x faster than pandas."""
    if min_p is None:
        min_p = w
    n = len(arr)
    out = np.full(n, np.nan)
    cs = np.concatenate(([0.0], np.nancumsum(arr)))
    for i in range(min_p - 1, n):
        start = max(0, i - w + 1)
        cnt = i - start + 1
        out[i] = (cs[i + 1] - cs[start]) / cnt
    return out


def _rolling_std(arr, w, min_p=None):
    """Rolling std using two-pass approach."""
    if min_p is None:
        min_p = w
    n = len(arr)
    out = np.full(n, np.nan)
    for i in range(min_p - 1, n):
        start = max(0, i - w + 1)
        out[i] = np.std(arr[start:i + 1], ddof=1) if (i - start) > 0 else 0.0
    return out


def compute_features_fast(candles):
    """Pure-numpy feature computation — ~10x faster than pandas version."""
    n = len(candles)
    if n < 5:
        return None

    op = np.array([c["open"] for c in candles], dtype=np.float64)
    hi = np.array([c["high"] for c in candles], dtype=np.float64)
    lo = np.array([c["low"] for c in candles], dtype=np.float64)
    cl = np.array([c["close"] for c in candles], dtype=np.float64)
    vol = np.array([c.get("volume", 0) for c in candles], dtype=np.float64)
    hours = np.array([int(c["date"][11:13]) for c in candles], dtype=np.int32)
    minutes = np.array([int(c["date"][14:16]) for c in candles], dtype=np.int32)
    candle_num = np.arange(n, dtype=np.int32)

    spot_open = op[0]

    gap_pct = ((cl - spot_open) / spot_open) * 100
    prev_cl = np.concatenate(([cl[0]], cl[:-1]))
    gap_from_prev_close = ((op - prev_cl) / (prev_cl + 1e-10)) * 100
    body = cl - op
    body_pct = (body / (op + 1e-10)) * 100
    rng = hi - lo
    rng_pct = (rng / (op + 1e-10)) * 100
    upper_wick = hi - np.maximum(op, cl)
    lower_wick = np.minimum(op, cl) - lo
    wick_ratio = upper_wick / (lower_wick + 0.01)
    body_to_range = np.abs(body) / (rng + 0.01)

    sma5 = _rolling_mean(cl, 5)
    sma10 = _rolling_mean(cl, 10)
    sma20 = _rolling_mean(cl, 20)
    close_vs_sma5 = ((cl - sma5) / (sma5 + 1e-10)) * 100
    close_vs_sma10 = ((cl - sma10) / (sma10 + 1e-10)) * 100
    close_vs_sma20 = ((cl - sma20) / (sma20 + 1e-10)) * 100
    prev_sma5 = np.concatenate(([sma5[0]], sma5[:-1]))
    prev_sma10 = np.concatenate(([sma10[0]], sma10[:-1]))
    sma5_slope = (sma5 - prev_sma5) / (prev_sma5 + 1e-10) * 100
    sma10_slope = (sma10 - prev_sma10) / (prev_sma10 + 1e-10) * 100

    roc_1 = np.concatenate(([0.0], np.diff(cl) / (cl[:-1] + 1e-10) * 100))
    roc_3 = np.full(n, 0.0)
    roc_3[3:] = (cl[3:] - cl[:-3]) / (cl[:-3] + 1e-10) * 100
    roc_5 = np.full(n, 0.0)
    roc_5[5:] = (cl[5:] - cl[:-5]) / (cl[:-5] + 1e-10) * 100
    roc_10 = np.full(n, 0.0)
    roc_10[10:] = (cl[10:] - cl[:-10]) / (cl[:-10] + 1e-10) * 100

    delta = np.concatenate(([0.0], np.diff(cl)))
    gain = np.clip(delta, 0, None)
    loss = np.clip(-delta, 0, None)
    avg_gain = _rolling_mean(gain, 14, 5)
    avg_loss = _rolling_mean(loss, 14, 5)
    rs = avg_gain / (avg_loss + 1e-10)
    rsi_14 = 100 - (100 / (1 + rs))

    high_low = hi - lo
    high_pc = np.abs(hi - prev_cl)
    low_pc = np.abs(lo - prev_cl)
    tr = np.maximum(high_low, np.maximum(high_pc, low_pc))
    atr_14 = _rolling_mean(tr, 14, 5)
    atr_pct = (atr_14 / (cl + 1e-10)) * 100

    bb_mid = _rolling_mean(cl, 20, 5)
    bb_std = _rolling_std(cl, 20, 5)
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    bb_pct = (cl - bb_lower) / (bb_upper - bb_lower + 1e-10)
    bb_width = ((bb_upper - bb_lower) / (bb_mid + 1e-10)) * 100

    cum_vol = np.cumsum(vol)
    cum_vwap = np.cumsum(cl * vol)
    vwap = cum_vwap / (cum_vol + 1)
    close_vs_vwap = ((cl - vwap) / (vwap + 1e-10)) * 100

    vol_sma10 = _rolling_mean(vol, 10, 3)
    vol_ratio = vol / (vol_sma10 + 1)
    vol_surge = (vol > vol_sma10 * 1.5).astype(np.float64)

    is_red = (cl < op).astype(np.int32)
    consec_red = np.zeros(n, dtype=np.float64)
    for i in range(n):
        if is_red[i]:
            consec_red[i] = (consec_red[i - 1] + 1) if i > 0 else 1
    is_green = (cl > op).astype(np.int32)
    consec_green = np.zeros(n, dtype=np.float64)
    for i in range(n):
        if is_green[i]:
            consec_green[i] = (consec_green[i - 1] + 1) if i > 0 else 1

    day_high = np.maximum.accumulate(hi)
    day_low = np.minimum.accumulate(lo)
    pct_from_day_high = ((cl - day_high) / (day_high + 1e-10)) * 100
    pct_from_day_low = ((cl - day_low) / (day_low + 1e-10)) * 100
    day_range_pct = ((day_high - day_low) / (spot_open + 1e-10)) * 100

    minutes_since_open = (hours - 9) * 60 + minutes - 15
    is_first_hour = (minutes_since_open <= 60).astype(np.float64)
    is_last_hour = (hours >= 15).astype(np.float64)

    return {
        "open": op, "high": hi, "low": lo, "close": cl, "volume": vol,
        "hour": hours, "minute": minutes, "candle_num": candle_num,
        "gap_pct": gap_pct, "gap_from_prev_close": gap_from_prev_close,
        "candle_body_pct": body_pct, "candle_range_pct": rng_pct,
        "upper_wick": upper_wick, "lower_wick": lower_wick,
        "wick_ratio": wick_ratio, "body_to_range": body_to_range,
        "close_vs_sma5": close_vs_sma5, "close_vs_sma10": close_vs_sma10,
        "close_vs_sma20": close_vs_sma20,
        "sma5_slope": sma5_slope, "sma10_slope": sma10_slope,
        "roc_1": roc_1, "roc_3": roc_3, "roc_5": roc_5, "roc_10": roc_10,
        "rsi_14": rsi_14, "atr_pct": atr_pct,
        "bb_pct": bb_pct, "bb_width": bb_width,
        "close_vs_vwap": close_vs_vwap,
        "vol_ratio": vol_ratio, "vol_surge": vol_surge,
        "consec_red": consec_red, "consec_green": consec_green,
        "pct_from_day_high": pct_from_day_high, "pct_from_day_low": pct_from_day_low,
        "day_range_pct": day_range_pct,
        "minutes_since_open": minutes_since_open.astype(np.float64),
        "is_first_hour": is_first_hour, "is_last_hour": is_last_hour,
        "prev_day_change_pct": None, "prev_day_range_pct": None,
        "candle_num_f": candle_num.astype(np.float64),
        "hour_f": hours.astype(np.float64),
    }


# ─── Symbol universe ─────────────────────────────────────────────────────
SYMBOLS = {
    # ─── Indexes ──────────────────────────────────────────────────────
    "NIFTY":       {"key": "NSE_INDEX|Nifty 50",      "lot": 75,   "step": 50,  "lots": 3},
    "SENSEX":      {"key": "BSE_INDEX|SENSEX",         "lot": 20,   "step": 100, "lots": 3},
    "BANKNIFTY":   {"key": "NSE_INDEX|Nifty Bank",     "lot": 15,   "step": 100, "lots": 3},
    # ─── Large-cap F&O stocks ─────────────────────────────────────────
    "RELIANCE":    {"key": "NSE_EQ|INE002A01018",      "lot": 250,  "step": 20,  "lots": 1},
    "HDFCBANK":    {"key": "NSE_EQ|INE040A01034",      "lot": 550,  "step": 10,  "lots": 1},
    "ICICIBANK":   {"key": "NSE_EQ|INE090A01021",      "lot": 700,  "step": 10,  "lots": 1},
    "INFY":        {"key": "NSE_EQ|INE009A01021",      "lot": 400,  "step": 10,  "lots": 1},
    "TCS":         {"key": "NSE_EQ|INE467B01029",      "lot": 175,  "step": 20,  "lots": 1},
    "SBIN":        {"key": "NSE_EQ|INE062A01020",      "lot": 1500, "step": 5,   "lots": 1},
    "AXISBANK":    {"key": "NSE_EQ|INE238A01034",      "lot": 625,  "step": 10,  "lots": 1},
    "KOTAKBANK":   {"key": "NSE_EQ|INE237A01036",      "lot": 400,  "step": 10,  "lots": 1},
    "ITC":         {"key": "NSE_EQ|INE154A01025",      "lot": 1600, "step": 5,   "lots": 1},
    "LT":          {"key": "NSE_EQ|INE018A01030",      "lot": 150,  "step": 20,  "lots": 1},
    "BHARTIARTL":  {"key": "NSE_EQ|INE397D01024",      "lot": 475,  "step": 10,  "lots": 1},
    "HINDUNILVR":  {"key": "NSE_EQ|INE030A01027",      "lot": 300,  "step": 10,  "lots": 1},
    "MARUTI":      {"key": "NSE_EQ|INE585B01010",      "lot": 100,  "step": 50,  "lots": 1},
    "TATAMOTORS":  {"key": "NSE_EQ|INE155A01022",      "lot": 1400, "step": 5,   "lots": 1},
    "BAJFINANCE":  {"key": "NSE_EQ|INE296A01032",      "lot": 125,  "step": 50,  "lots": 1},
    # ─── IT / Tech ───────────────────────────────────────────────────
    "HCLTECH":     {"key": "NSE_EQ|INE860A01027",      "lot": 350,  "step": 10,  "lots": 1},
    "WIPRO":       {"key": "NSE_EQ|INE075A01022",      "lot": 1500, "step": 5,   "lots": 1},
    "TECHM":       {"key": "NSE_EQ|INE669C01036",      "lot": 600,  "step": 10,  "lots": 1},
    # ─── Pharma ──────────────────────────────────────────────────────
    "SUNPHARMA":   {"key": "NSE_EQ|INE044A01036",      "lot": 350,  "step": 10,  "lots": 1},
    # DRREDDY skipped — ISIN changed, Upstox rejects INE089A01023
    "CIPLA":       {"key": "NSE_EQ|INE059A01026",      "lot": 650,  "step": 10,  "lots": 1},
    "DIVISLAB":    {"key": "NSE_EQ|INE361B01024",      "lot": 100,  "step": 20,  "lots": 1},
    "APOLLOHOSP":  {"key": "NSE_EQ|INE437A01024",      "lot": 125,  "step": 50,  "lots": 1},
    # ─── Consumer ────────────────────────────────────────────────────
    "TITAN":       {"key": "NSE_EQ|INE280A01028",      "lot": 175,  "step": 20,  "lots": 1},
    "ASIANPAINT":  {"key": "NSE_EQ|INE021A01026",      "lot": 300,  "step": 10,  "lots": 1},
    # NESTLEIND skipped — ISIN changed, Upstox rejects INE239A01016
    "BAJAJFINSV":  {"key": "NSE_EQ|INE918I01026",      "lot": 500,  "step": 10,  "lots": 1},
    "EICHERMOT":   {"key": "NSE_EQ|INE066A01021",      "lot": 175,  "step": 20,  "lots": 1},
    # ─── Energy / Infra / Metals ─────────────────────────────────────
    "ADANIENT":    {"key": "NSE_EQ|INE423A01024",      "lot": 500,  "step": 10,  "lots": 1},
    "POWERGRID":   {"key": "NSE_EQ|INE752E01010",      "lot": 2700, "step": 2,   "lots": 1},
    "NTPC":        {"key": "NSE_EQ|INE733E01010",      "lot": 2775, "step": 2,   "lots": 1},
    "ONGC":        {"key": "NSE_EQ|INE213A01029",      "lot": 3075, "step": 2,   "lots": 1},
    "COALINDIA":   {"key": "NSE_EQ|INE522F01014",      "lot": 2100, "step": 2,   "lots": 1},
    "JSWSTEEL":    {"key": "NSE_EQ|INE019A01038",      "lot": 900,  "step": 5,   "lots": 1},
    "TATASTEEL":   {"key": "NSE_EQ|INE081A01020",      "lot": 1500, "step": 5,   "lots": 1},
    # BPCL skipped — ISIN changed, Upstox rejects INE541A01028
    "GRASIM":      {"key": "NSE_EQ|INE047A01021",      "lot": 475,  "step": 10,  "lots": 1},
    "ULTRACEMCO":  {"key": "NSE_EQ|INE481G01011",      "lot": 50,   "step": 50,  "lots": 1},
    "M_M":         {"key": "NSE_EQ|INE101A01026",      "lot": 350,  "step": 10,  "lots": 1},
    "INDUSINDBK":  {"key": "NSE_EQ|INE095A01012",      "lot": 500,  "step": 10,  "lots": 1},
    "HEROMOTOCO":  {"key": "NSE_EQ|INE158A01026",      "lot": 150,  "step": 20,  "lots": 1},
    "TATACONSUM":  {"key": "NSE_EQ|INE192A01025",      "lot": 900,  "step": 5,   "lots": 1},
    "HINDALCO":    {"key": "NSE_EQ|INE038A01020",      "lot": 1075, "step": 5,   "lots": 1},
    "SBILIFE":     {"key": "NSE_EQ|INE123W01016",      "lot": 375,  "step": 10,  "lots": 1},
    "HDFCLIFE":    {"key": "NSE_EQ|INE795G01014",      "lot": 1100, "step": 5,   "lots": 1},
}

# ─── Trading config ──────────────────────────────────────────────────────
PE_DELTA = 0.85
ITM_MIN, ITM_MAX = 300, 900
SLIPPAGE_PCT = 0.5
FLOOR = 4000
MAX_LOSS = 4000
DAILY_LOSS_CAP = 2
SCAN_START = (9, 20)
SCAN_END = (10, 30)
SKIP_HOURS = {11, 12, 13, 14, 15}

# Extended features for universal model
EXTENDED_FEATURES = FEATURE_COLS + [
    "sym_avg_range",      # symbol's avg daily range (volatility proxy)
    "sym_avg_volume",     # symbol's avg volume (liquidity proxy)
    "spot_magnitude",     # log(spot price) — helps model adapt to different price scales
]


def fetch_candles(uclient, sym, ref_date):
    info = SYMBOLS[sym]
    cache = os.path.join(CACHE_DIR, f"spot_{sym}_{ref_date}.json")
    if os.path.exists(cache):
        with open(cache) as f:
            return json.load(f)
    if uclient is None:
        return None
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
    except Exception:
        pass
    return None


def label_trade(candles, idx, sym):
    info = SYMBOLS[sym]
    if idx >= len(candles) - 1:
        return None, 0
    spot = candles[idx]["close"]
    qty = info["lot"] * info["lots"]

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
            return (1 if gross - _charges_total(est_entry, exit_p, qty) > 0 else 0), gross - _charges_total(est_entry, exit_p, qty)

        if opt_p_worst <= sl_price:
            gross = (sl_price - est_entry) * qty
            return (1 if gross - _charges_total(est_entry, sl_price, qty) > 0 else 0), gross - _charges_total(est_entry, sl_price, qty)

        if opt_p_best >= tgt_price:
            gross = (tgt_price - est_entry) * qty
            return (1 if gross - _charges_total(est_entry, tgt_price, qty) > 0 else 0), gross - _charges_total(est_entry, tgt_price, qty)

        b_pnl = (opt_p_best - est_entry) * qty
        peak_pnl = max(peak_pnl, b_pnl)
        if peak_pnl >= FLOOR:
            floor_armed = True
        if floor_armed:
            cur = (opt_p_worst - est_entry) * qty
            if cur <= FLOOR:
                floor_p = est_entry + (FLOOR / qty)
                gross = (floor_p - est_entry) * qty
                return (1 if gross - _charges_total(est_entry, floor_p, qty) > 0 else 0), gross - _charges_total(est_entry, floor_p, qty)

    last_move = -(candles[-1]["close"] - spot) * PE_DELTA
    eod_p = est_entry + last_move
    gross = (eod_p - est_entry) * qty
    ch = _charges_total(est_entry, eod_p, qty)
    return (1 if gross - ch > 0 else 0), gross - ch


def collect_day_data(uclient, sym, ref_date, prev_change=0, prev_range=0, sym_stats=None):
    """Collect features + labels for one symbol on one day — numpy-fast version."""
    candles = fetch_candles(uclient, sym, ref_date)
    if not candles or len(candles) < 10:
        return [], candles

    f = compute_features_fast(candles)
    if f is None:
        return [], candles

    n = len(candles)
    stats = sym_stats or {}
    avg_range = stats.get("avg_range", 1.0)
    avg_vol = stats.get("avg_volume", 1.0)
    spot_mag = np.log(np.clip(f["close"], 1, None))

    # Vectorized time filter
    time_mins = f["hour"] * 60 + f["minute"]
    start_mins = SCAN_START[0] * 60 + SCAN_START[1]
    end_mins = SCAN_END[0] * 60 + SCAN_END[1]
    mask = (time_mins >= start_mins) & (time_mins <= end_mins) & (f["candle_num"] >= 5)
    for skip_h in SKIP_HOURS:
        mask &= (f["hour"] != skip_h)

    indices = np.where(mask)[0]
    if len(indices) == 0:
        return [], candles

    rows = []
    for i in indices:
        cidx = int(f["candle_num"][i])
        label, pnl = label_trade(candles, cidx, sym)
        if label is None:
            continue
        feat = {}
        for col in FEATURE_COLS:
            if col == "candle_num":
                feat[col] = float(f["candle_num_f"][i])
            elif col == "hour":
                feat[col] = float(f["hour_f"][i])
            elif col == "prev_day_change_pct":
                feat[col] = prev_change
            elif col == "prev_day_range_pct":
                feat[col] = prev_range
            else:
                feat[col] = float(f[col][i])
        feat["sym_avg_range"] = avg_range
        feat["sym_avg_volume"] = avg_vol
        feat["spot_magnitude"] = float(spot_mag[i])
        feat["label"] = label
        feat["pnl"] = pnl
        feat["symbol"] = sym
        feat["candle_idx"] = cidx
        feat["time"] = candles[cidx]["date"][11:16]
        feat["spot"] = float(f["close"][i])
        rows.append(feat)

    return rows, candles


def get_trading_days(uclient, start_date, end_date, ref_sym="NIFTY"):
    """Get all trading days in range."""
    days = []
    d = start_date
    while d <= end_date:
        if d.weekday() < 5:
            candles = fetch_candles(uclient, ref_sym, d)
            if candles and len(candles) > 5:
                days.append(d)
        d += timedelta(days=1)
    return days


def compute_symbol_stats(uclient, sym, days):
    """Compute avg daily range and volume for a symbol over given days."""
    ranges = []
    volumes = []
    for d in days[-30:]:  # last 30 days
        candles = fetch_candles(uclient, sym, d)
        if candles and len(candles) > 5:
            h = max(c["high"] for c in candles)
            l = min(c["low"] for c in candles)
            o = candles[0]["open"]
            ranges.append((h - l) / o * 100)
            volumes.append(sum(c.get("volume", 0) for c in candles))
    return {
        "avg_range": np.mean(ranges) if ranges else 1.0,
        "avg_volume": np.log1p(np.mean(volumes)) if volumes else 1.0,
    }


def _process_symbol(args):
    """Worker function for parallel feature pre-computation."""
    import pickle as _pkl
    sym, days_list, dd, stats, cache_dir = args
    sym_cache = os.path.join(cache_dir, f"feat_{sym}.pkl")
    if os.path.exists(sym_cache):
        with open(sym_cache, "rb") as f:
            return sym, _pkl.load(f)
    result = []
    for d in days_list:
        pc, pr = dd.get((sym, d), (0, 0))
        rows, _ = collect_day_data(None, sym, d, pc, pr, stats)
        for r in rows:
            r["_date"] = d
        result.extend(rows)
    with open(sym_cache, "wb") as f:
        _pkl.dump(result, f)
    return sym, result


def train_universal_model(all_rows):
    """Train ONE XGBoost model on all symbols' pooled data."""
    if len(all_rows) < 200:
        return None

    X = np.array(
        [[r.get(k, 0) for k in EXTENDED_FEATURES] for r in all_rows],
        dtype=np.float64
    )
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    y = np.array([r["label"] for r in all_rows], dtype=np.int32)
    pos = int(y.sum())
    neg = len(y) - pos
    if pos < 30 or neg < 30:
        return None

    model = XGBClassifier(
        n_estimators=100, max_depth=3, learning_rate=0.1,
        subsample=0.8, colsample_bytree=0.7,
        min_child_weight=5,
        reg_alpha=0.1, reg_lambda=1.0,
        scale_pos_weight=neg / pos if pos > 0 else 1,
        eval_metric="logloss", verbosity=0,
        use_label_encoder=False,
        tree_method="hist",
    )
    model.fit(X, y)
    return model


def run_walkforward(uclient, symbols, year, daily_budget=3, train_months=6):
    """Walk-forward test: train on N months, test next month, slide forward."""
    print(f"\n{'═'*70}")
    print(f"  CONSISTENT ML STRATEGY — WALK-FORWARD ANALYSIS")
    print(f"  Year: {year}")
    print(f"  Symbols: {', '.join(symbols)}")
    print(f"  Daily budget: {daily_budget} signals")
    print(f"  Train window: {train_months} months")
    print(f"{'═'*70}\n")

    os.makedirs(CACHE_DIR, exist_ok=True)

    # Collect all trading days for the year + prior months for training
    start_month = 13 - train_months  # e.g. 6-month window → month 7
    train_start = date(year - 1, start_month, 1) if start_month >= 1 else date(year - 2, start_month + 12, 1)
    today = date.today()
    test_end = min(date(year, 12, 31), today - timedelta(days=1))

    print("Collecting trading days (this takes a while)...")
    all_days = get_trading_days(uclient, train_start, test_end)
    print(f"Found {len(all_days)} trading days from {all_days[0]} to {all_days[-1]}\n")

    # Group by month
    months = defaultdict(list)
    for d in all_days:
        months[(d.year, d.month)].append(d)

    sorted_months = sorted(months.keys())

    # Find test months (only months in target year)
    test_months = [(y, m) for y, m in sorted_months if y == year]
    if not test_months:
        print("No test months found!")
        return

    # Precompute prev-day data for all symbols on all days
    print(f"Building feature cache for {len(symbols)} symbols × {len(all_days)} days...")
    day_data = {}  # (sym, date) -> (prev_change, prev_range)
    valid_symbols = []
    for si, sym in enumerate(symbols):
        prev_change = 0
        prev_range = 0
        fetched = 0
        consecutive_fails = 0
        skipped_early = False
        for d in all_days:
            day_data[(sym, d)] = (prev_change, prev_range)
            candles = fetch_candles(uclient, sym, d)
            if candles and len(candles) > 1:
                fetched += 1
                consecutive_fails = 0
                o = candles[0]["open"]
                c = candles[-1]["close"]
                h = max(c_["high"] for c_ in candles)
                l = min(c_["low"] for c_ in candles)
                prev_change = ((c - o) / o) * 100
                prev_range = ((h - l) / o) * 100
            else:
                consecutive_fails += 1
                if consecutive_fails >= 10 and fetched == 0:
                    skipped_early = True
                    break
        if skipped_early:
            print(f"  [{si+1}/{len(symbols)}] {sym}: 0 days — BAD ISIN, skipped early")
        elif fetched > len(all_days) * 0.5:
            valid_symbols.append(sym)
            print(f"  [{si+1}/{len(symbols)}] {sym}: {fetched}/{len(all_days)} days ✓")
        else:
            print(f"  [{si+1}/{len(symbols)}] {sym}: {fetched}/{len(all_days)} days ✗ SKIPPED")

    symbols = valid_symbols
    print(f"\n  {len(symbols)} symbols with sufficient data\n")

    # ─── PRE-COMPUTE all features + labels ONCE (with disk cache) ─────
    import pickle
    from concurrent.futures import ProcessPoolExecutor, as_completed
    feature_cache_file = os.path.join(CACHE_DIR, f"features_{year}_{len(symbols)}sym.pkl")
    all_rows_by_date = defaultdict(list)

    if os.path.exists(feature_cache_file):
        print(f"Loading cached features from {feature_cache_file}...")
        with open(feature_cache_file, "rb") as f:
            all_rows_by_date = pickle.load(f)
        print(f"  Loaded — {sum(len(v) for v in all_rows_by_date.values())} total feature rows\n")
    else:
        print(f"Pre-computing features for {len(symbols)} symbols × {len(all_days)} days...")
        sym_stats_global = {}
        for sym in symbols:
            sym_stats_global[sym] = compute_symbol_stats(uclient, sym, all_days)

        import multiprocessing as mp
        n_workers = min(mp.cpu_count(), 4, len(symbols))
        print(f"  Using {n_workers} parallel workers...")

        work = [(sym, all_days, dict(day_data), sym_stats_global.get(sym), CACHE_DIR)
                for sym in symbols]

        done_count = 0
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(_process_symbol, w): w[0] for w in work}
            for fut in as_completed(futures):
                sym_name = futures[fut]
                done_count += 1
                sym_rows = fut.result()
                _, rows = sym_rows
                for r in rows:
                    all_rows_by_date[r["_date"]].append(r)
                print(f"  [{done_count}/{len(symbols)}] {sym_name}: {len(rows)} rows ✓",
                      flush=True)

        total_rows = sum(len(v) for v in all_rows_by_date.values())
        print(f"  Done — {total_rows} total feature rows")
        print(f"  Saving cache to {feature_cache_file}...")
        with open(feature_cache_file, "wb") as f:
            pickle.dump(dict(all_rows_by_date), f)
        print(f"  Cached ✓\n")

    # Walk-forward loop
    all_daily_pnl = []
    all_daily_details = []
    monthly_results = []

    for test_ym in test_months:
        test_y, test_m = test_ym
        test_days = months[test_ym]
        if not test_days:
            continue

        # Training data: all months before this test month, up to train_months
        train_end_idx = sorted_months.index(test_ym)
        train_start_idx = max(0, train_end_idx - train_months)
        train_yms = sorted_months[train_start_idx:train_end_idx]

        if len(train_yms) < 3:
            print(f"  Skipping {test_y}-{test_m:02d}: not enough training months")
            continue

        train_days = []
        for ym in train_yms:
            train_days.extend(months[ym])

        # Slice pre-computed rows for training
        print(f"  Training for {test_y}-{test_m:02d} "
              f"({len(train_days)} train days, {len(test_days)} test days)...",
              end=" ", flush=True)

        train_rows = []
        for d in train_days:
            train_rows.extend(all_rows_by_date.get(d, []))

        model = train_universal_model(train_rows)
        if model is None:
            print(f"FAILED ({len(train_rows)} samples)")
            continue

        pos_rate = sum(1 for r in train_rows if r["label"] == 1) / len(train_rows) * 100
        print(f"OK ({len(train_rows)} samples, {pos_rate:.0f}% positive)")

        # Test: for each day, score all signals, rank, pick top N
        month_pnl = 0
        month_trades = 0
        month_wins = 0

        for test_date in test_days:
            day_signals = all_rows_by_date.get(test_date, [])
            if not day_signals:
                continue

            # Batch scoring — one predict_proba call for ALL signals
            feat_matrix = np.array(
                [[row.get(k, 0) for k in EXTENDED_FEATURES] for row in day_signals],
                dtype=np.float64
            )
            feat_matrix = np.nan_to_num(feat_matrix, nan=0.0, posinf=0.0, neginf=0.0)
            probs = model.predict_proba(feat_matrix)[:, 1]
            for i, row in enumerate(day_signals):
                row["confidence"] = float(probs[i])

            # Rank by confidence, pick top N with ONE per symbol + min confidence
            min_conf = 0.60
            day_signals.sort(key=lambda x: x["confidence"], reverse=True)
            selected = []
            seen_syms = set()
            for sig in day_signals:
                if sig["confidence"] < min_conf:
                    break
                if sig["symbol"] in seen_syms:
                    continue
                seen_syms.add(sig["symbol"])
                selected.append(sig)
                if len(selected) >= daily_budget:
                    break

            day_pnl = 0
            day_trades = 0
            day_wins = 0
            trade_details = []

            for sig in selected:
                day_trades += 1
                day_pnl += sig["pnl"]
                if sig["pnl"] > 0:
                    day_wins += 1
                result = "✅" if sig["pnl"] > 0 else "❌"
                trade_details.append(
                    f"    {sig['time']} | {sig['symbol']:<12} | "
                    f"conf={sig['confidence']*100:.0f}% | "
                    f"P&L=₹{sig['pnl']:+,.0f} {result}"
                )

            month_pnl += day_pnl
            month_trades += day_trades
            month_wins += day_wins
            all_daily_pnl.append(day_pnl)
            all_daily_details.append({
                "date": str(test_date),
                "trades": day_trades,
                "wins": day_wins,
                "pnl": round(day_pnl, 2),
                "details": trade_details,
            })

        m_wr = month_wins / month_trades * 100 if month_trades else 0
        profitable_days = sum(1 for d in all_daily_details[-len(test_days):]
                              if d["pnl"] > 0)
        month_result = {
            "month": f"{test_y}-{test_m:02d}",
            "days": len(test_days),
            "trades": month_trades,
            "wins": month_wins,
            "wr": round(m_wr, 1),
            "pnl": round(month_pnl, 2),
            "profitable_days": profitable_days,
            "total_days": len(test_days),
        }
        monthly_results.append(month_result)
        print(f"    → {month_trades} trades, {m_wr:.0f}% WR, "
              f"₹{month_pnl:+,.0f} | "
              f"{profitable_days}/{len(test_days)} days green")

    # ─── DETAILED DAILY RESULTS ──────────────────────────────────────────
    print(f"\n{'═'*70}")
    print(f"  DAILY RESULTS")
    print(f"{'═'*70}")
    for dd in all_daily_details:
        status = "🟢" if dd["pnl"] > 0 else ("🔴" if dd["pnl"] < 0 else "⚪")
        print(f"  {dd['date']} | {dd['trades']}T {dd['wins']}W | ₹{dd['pnl']:+,.0f} {status}")
        for line in dd["details"]:
            print(line)

    # ─── MONTHLY SUMMARY ─────────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print(f"  MONTHLY SUMMARY")
    print(f"{'═'*70}")
    print(f"  {'Month':<10} {'Trades':>6} {'WR':>5} {'P&L':>12} {'Green Days':>12}")
    print(f"  {'─'*10} {'─'*6} {'─'*5} {'─'*12} {'─'*12}")
    for mr in monthly_results:
        print(f"  {mr['month']:<10} {mr['trades']:>6} {mr['wr']:>4.0f}% "
              f"₹{mr['pnl']:>+10,.0f} {mr['profitable_days']}/{mr['total_days']}")

    # ─── CONSISTENCY METRICS ─────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print(f"  CONSISTENCY METRICS")
    print(f"{'═'*70}")

    total_days = len(all_daily_pnl)
    if total_days == 0:
        print("  No test days!")
        return

    total_pnl = sum(all_daily_pnl)
    avg_daily = total_pnl / total_days
    green_days = sum(1 for p in all_daily_pnl if p > 0)
    red_days = sum(1 for p in all_daily_pnl if p < 0)
    flat_days = sum(1 for p in all_daily_pnl if p == 0)

    # Max drawdown
    cumulative = np.cumsum(all_daily_pnl)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = cumulative - running_max
    max_dd = drawdowns.min()
    max_dd_idx = drawdowns.argmin()

    # Sharpe ratio (annualized, daily)
    daily_std = np.std(all_daily_pnl) if len(all_daily_pnl) > 1 else 1
    sharpe = (avg_daily / daily_std) * math.sqrt(252) if daily_std > 0 else 0

    # Consecutive losing days
    max_consec_loss = 0
    cur_loss = 0
    for p in all_daily_pnl:
        if p < 0:
            cur_loss += 1
            max_consec_loss = max(max_consec_loss, cur_loss)
        else:
            cur_loss = 0

    # Win streak
    max_consec_win = 0
    cur_win = 0
    for p in all_daily_pnl:
        if p > 0:
            cur_win += 1
            max_consec_win = max(max_consec_win, cur_win)
        else:
            cur_win = 0

    total_trades = sum(d["trades"] for d in all_daily_details)
    total_wins = sum(d["wins"] for d in all_daily_details)
    trade_wr = total_wins / total_trades * 100 if total_trades else 0

    best_day = max(all_daily_pnl)
    worst_day = min(all_daily_pnl)

    print(f"  Total trading days: {total_days}")
    print(f"  Green days: {green_days} ({green_days/total_days*100:.0f}%)")
    print(f"  Red days: {red_days} ({red_days/total_days*100:.0f}%)")
    print(f"  Flat days: {flat_days}")
    print()
    print(f"  Total trades: {total_trades}")
    print(f"  Trade win rate: {trade_wr:.0f}%")
    print(f"  Avg trades/day: {total_trades/total_days:.1f}")
    print()
    print(f"  Net P&L: ₹{total_pnl:+,.0f}")
    print(f"  Avg daily P&L: ₹{avg_daily:+,.0f}")
    print(f"  Best day: ₹{best_day:+,.0f}")
    print(f"  Worst day: ₹{worst_day:+,.0f}")
    print()
    print(f"  Max drawdown: ₹{max_dd:+,.0f}")
    print(f"  Sharpe ratio: {sharpe:.2f}")
    print(f"  Max consecutive losses: {max_consec_loss} days")
    print(f"  Max consecutive wins: {max_consec_win} days")
    print()

    # Grade
    if green_days / total_days >= 0.65 and avg_daily >= 5000 and sharpe >= 2:
        grade = "A — PRODUCTION READY"
    elif green_days / total_days >= 0.55 and avg_daily >= 3000 and sharpe >= 1:
        grade = "B — PROMISING, needs tuning"
    elif avg_daily > 0:
        grade = "C — MARGINAL, not recommended"
    else:
        grade = "F — LOSING STRATEGY"

    print(f"  GRADE: {grade}")
    print()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, default=2026, help="Target year for walk-forward test")
    p.add_argument("--budget", type=int, default=3, help="Max signals per day (2-5)")
    p.add_argument("--train-months", type=int, default=6, help="Training window in months")
    p.add_argument("--symbols", default=None, help="Comma-separated symbols (default: top 7)")
    a = p.parse_args()

    syms = a.symbols.split(",") if a.symbols else list(SYMBOLS.keys())
    uclient = UpstoxData()
    run_walkforward(uclient, syms, a.year, a.budget, a.train_months)

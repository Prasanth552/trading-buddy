#!/usr/bin/env python3
"""ML-based PE signal finder — learns what candle conditions predict profitable PE entries.

Instead of hand-written rules, this uses XGBoost on 40+ features per candle.
Walk-forward validation: train on 60 days, test on 20 days, repeat.

Usage:
  .venv/bin/python3 scripts/ml_strategy.py --days 90 --end 2026-09-03 --index NIFTY --lots 3
  .venv/bin/python3 scripts/ml_strategy.py --days 90 --end 2026-09-03 --index NIFTY --lots 3 --predict-today
"""
import sys, os, json, time as _time, argparse, math, warnings
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

parser = argparse.ArgumentParser(description="ML PE signal finder")
parser.add_argument("--days", type=int, default=180, help="Total history days for train+test")
parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
parser.add_argument("--index", default="NIFTY", help="Index to analyze")
parser.add_argument("--lots", type=int, default=3)
parser.add_argument("--max-loss", type=float, default=6000)
parser.add_argument("--floor", type=float, default=2000)
parser.add_argument("--itm-min", type=int, default=300)
parser.add_argument("--itm-max", type=int, default=900)
parser.add_argument("--slippage", type=float, default=0.5)
parser.add_argument("--train-days", type=int, default=40, help="Training window (trading days)")
parser.add_argument("--test-days", type=int, default=10, help="Test window (trading days)")
parser.add_argument("--min-confidence", type=float, default=0.70, help="Min model confidence to trade")
parser.add_argument("--max-trades-per-day", type=int, default=5)
parser.add_argument("--skip-hours", default="12,13")
parser.add_argument("--predict-today", action="store_true", help="Generate signals for today using trained model")
parser.add_argument("--cache-dir", default=None, help="Cache candle data to disk")
args = parser.parse_args()

end_date_str = args.end or datetime.now(IST).strftime("%Y-%m-%d")
end_date = date(*[int(x) for x in end_date_str.split("-")])
start_date = end_date - timedelta(days=args.days - 1)
SKIP_HOURS = set(int(h) for h in args.skip_hours.split(",") if h.strip())

try:
    import numpy as np
    import pandas as pd
except ImportError:
    print("ERROR: numpy and pandas required"); sys.exit(1)

try:
    from xgboost import XGBClassifier
except ImportError:
    print("ERROR: xgboost not installed. Run: .venv/bin/pip install xgboost")
    sys.exit(1)

try:
    from sklearn.metrics import classification_report, accuracy_score
    from sklearn.model_selection import TimeSeriesSplit
except ImportError:
    print("ERROR: scikit-learn not installed. Run: .venv/bin/pip install scikit-learn")
    sys.exit(1)

try:
    import config
    from src.broker.upstox_data import UpstoxData, load_cached_token
    from src.broker.upstox_client import _expiry_to_date
    from src.notify.channel_listener import calc_charges
except ImportError as e:
    print(f"ERROR: {e}"); sys.exit(1)

token = load_cached_token()
if not token:
    print("ERROR: No Upstox token"); sys.exit(1)
uclient = UpstoxData()
master = uclient._load_master()

SPOT_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
}
LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 40, "MIDCPNIFTY": 50}
STRIKE_STEPS = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25}

CACHE_DIR = args.cache_dir or os.path.join(os.path.dirname(__file__), "..", "data", "ml_cache")
os.makedirs(CACHE_DIR, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════
#  DATA FETCHING (with disk cache)
# ═══════════════════════════════════════════════════════════════════════════

def _cache_path(kind, symbol, ref_date):
    return os.path.join(CACHE_DIR, f"{kind}_{symbol}_{ref_date}.json")


def fetch_spot_candles(index_sym, ref_date):
    cp = _cache_path("spot", index_sym, ref_date)
    if os.path.exists(cp):
        with open(cp) as f:
            return json.load(f)

    spot_key = SPOT_KEYS.get(index_sym)
    if not spot_key:
        return None
    y, m, d = ref_date.year, ref_date.month, ref_date.day
    from_dt = datetime(y, m, d, 9, 15, 0, tzinfo=IST)
    to_dt = datetime(y, m, d, 15, 30, 0, tzinfo=IST)
    try:
        candles = uclient.historical_data(spot_key, from_dt, to_dt, "5minute")
        _time.sleep(0.25)
        if candles:
            with open(cp, "w") as f:
                json.dump(candles, f)
        return candles
    except Exception:
        _time.sleep(0.5)
        return None


def find_pe_strike(index_sym, spot_price):
    step = STRIKE_STEPS.get(index_sym, 50)
    target_depth = (args.itm_min + args.itm_max) // 2
    raw_strike = spot_price + target_depth
    return round(raw_strike / step) * step


def find_option_instrument(index_sym, strike, ref_date):
    candidates = []
    for inst in master:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO"):
            continue
        if inst.get("asset_symbol", "").upper() != index_sym:
            continue
        if inst.get("instrument_type") != "PE":
            continue
        if abs(float(inst.get("strike_price", -1)) - strike) > 0.01:
            continue
        exp = _expiry_to_date(inst.get("expiry"))
        if exp is None or exp < ref_date:
            continue
        candidates.append((exp, inst))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1].get("instrument_key"), candidates[0][0]


def fetch_option_candles(inst_key, ref_date):
    cp = _cache_path("opt", inst_key.replace("|", "_"), ref_date)
    if os.path.exists(cp):
        with open(cp) as f:
            return json.load(f)

    y, m, d = ref_date.year, ref_date.month, ref_date.day
    from_dt = datetime(y, m, d, 9, 15, 0, tzinfo=IST)
    to_dt = datetime(y, m, d, 15, 30, 0, tzinfo=IST)
    for interval in ("5minute", "15minute"):
        try:
            candles = uclient.historical_data(inst_key, from_dt, to_dt, interval)
            _time.sleep(0.25)
            if candles:
                with open(cp, "w") as f:
                    json.dump(candles, f)
                return candles
        except Exception:
            _time.sleep(0.5)
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  FEATURE ENGINE — 40+ features per candle
# ═══════════════════════════════════════════════════════════════════════════

def compute_features(candles):
    """Compute features for every candle in the day. Returns a DataFrame."""
    if not candles or len(candles) < 5:
        return pd.DataFrame()

    df = pd.DataFrame(candles)
    df["time"] = df["date"].str[11:16]
    df["hour"] = df["time"].str[:2].astype(int)
    df["minute"] = df["time"].str[3:5].astype(int)
    df["candle_num"] = range(len(df))

    spot_open = df["open"].iloc[0]

    # --- Price-based ---
    df["gap_pct"] = ((df["close"] - spot_open) / spot_open) * 100
    df["gap_from_prev_close"] = ((df["open"] - df["close"].shift(1)) / df["close"].shift(1)) * 100
    df["candle_body"] = df["close"] - df["open"]
    df["candle_body_pct"] = (df["candle_body"] / df["open"]) * 100
    df["candle_range"] = df["high"] - df["low"]
    df["candle_range_pct"] = (df["candle_range"] / df["open"]) * 100
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["wick_ratio"] = df["upper_wick"] / (df["lower_wick"] + 0.01)
    df["body_to_range"] = abs(df["candle_body"]) / (df["candle_range"] + 0.01)

    # --- Moving averages ---
    df["sma_5"] = df["close"].rolling(5).mean()
    df["sma_10"] = df["close"].rolling(10).mean()
    df["sma_20"] = df["close"].rolling(20).mean()
    df["close_vs_sma5"] = ((df["close"] - df["sma_5"]) / df["sma_5"]) * 100
    df["close_vs_sma10"] = ((df["close"] - df["sma_10"]) / df["sma_10"]) * 100
    df["close_vs_sma20"] = ((df["close"] - df["sma_20"]) / df["sma_20"]) * 100
    df["sma5_slope"] = df["sma_5"].diff() / df["sma_5"].shift(1) * 100
    df["sma10_slope"] = df["sma_10"].diff() / df["sma_10"].shift(1) * 100

    # --- Momentum ---
    df["roc_1"] = df["close"].pct_change(1) * 100
    df["roc_3"] = df["close"].pct_change(3) * 100
    df["roc_5"] = df["close"].pct_change(5) * 100
    df["roc_10"] = df["close"].pct_change(10) * 100

    # --- RSI (14-period) ---
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(14, min_periods=5).mean()
    avg_loss = loss.rolling(14, min_periods=5).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    df["rsi_14"] = 100 - (100 / (1 + rs))

    # --- ATR (average true range, 14-period) ---
    high_low = df["high"] - df["low"]
    high_pc = abs(df["high"] - df["close"].shift(1))
    low_pc = abs(df["low"] - df["close"].shift(1))
    tr = pd.concat([high_low, high_pc, low_pc], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(14, min_periods=5).mean()
    df["atr_pct"] = (df["atr_14"] / df["close"]) * 100

    # --- Bollinger Bands ---
    df["bb_mid"] = df["close"].rolling(20, min_periods=5).mean()
    bb_std = df["close"].rolling(20, min_periods=5).std()
    df["bb_upper"] = df["bb_mid"] + 2 * bb_std
    df["bb_lower"] = df["bb_mid"] - 2 * bb_std
    df["bb_pct"] = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"] + 1e-10)
    df["bb_width"] = ((df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]) * 100

    # --- VWAP ---
    df["cum_vol"] = df["volume"].cumsum()
    df["cum_vwap"] = (df["close"] * df["volume"]).cumsum()
    df["vwap"] = df["cum_vwap"] / (df["cum_vol"] + 1)
    df["close_vs_vwap"] = ((df["close"] - df["vwap"]) / df["vwap"]) * 100

    # --- Volume features ---
    df["vol_sma_10"] = df["volume"].rolling(10, min_periods=3).mean()
    df["vol_ratio"] = df["volume"] / (df["vol_sma_10"] + 1)
    df["vol_surge"] = (df["volume"] > df["vol_sma_10"] * 1.5).astype(int)

    # --- Consecutive down candles ---
    df["is_red"] = (df["close"] < df["open"]).astype(int)
    df["consec_red"] = df["is_red"].groupby((df["is_red"] != df["is_red"].shift()).cumsum()).cumcount() + 1
    df.loc[df["is_red"] == 0, "consec_red"] = 0

    df["is_green"] = (df["close"] > df["open"]).astype(int)
    df["consec_green"] = df["is_green"].groupby((df["is_green"] != df["is_green"].shift()).cumsum()).cumcount() + 1
    df.loc[df["is_green"] == 0, "consec_green"] = 0

    # --- Day's high/low context ---
    df["day_high_so_far"] = df["high"].cummax()
    df["day_low_so_far"] = df["low"].cummin()
    df["pct_from_day_high"] = ((df["close"] - df["day_high_so_far"]) / df["day_high_so_far"]) * 100
    df["pct_from_day_low"] = ((df["close"] - df["day_low_so_far"]) / df["day_low_so_far"]) * 100
    df["day_range_pct"] = ((df["day_high_so_far"] - df["day_low_so_far"]) / spot_open) * 100

    # --- Time features ---
    df["minutes_since_open"] = (df["hour"] - 9) * 60 + df["minute"] - 15
    df["is_first_hour"] = (df["minutes_since_open"] <= 60).astype(int)
    df["is_last_hour"] = (df["hour"] >= 15).astype(int)

    # --- Previous day context (filled by caller) ---
    df["prev_day_change_pct"] = 0.0
    df["prev_day_range_pct"] = 0.0
    df["prev_close"] = spot_open

    # Drop helper columns
    df.drop(columns=["cum_vol", "cum_vwap", "is_red", "is_green"], inplace=True, errors="ignore")

    return df


FEATURE_COLS = [
    "gap_pct", "gap_from_prev_close", "candle_body_pct", "candle_range_pct",
    "upper_wick", "lower_wick", "wick_ratio", "body_to_range",
    "close_vs_sma5", "close_vs_sma10", "close_vs_sma20",
    "sma5_slope", "sma10_slope",
    "roc_1", "roc_3", "roc_5", "roc_10",
    "rsi_14", "atr_pct",
    "bb_pct", "bb_width",
    "close_vs_vwap",
    "vol_ratio", "vol_surge",
    "consec_red", "consec_green",
    "pct_from_day_high", "pct_from_day_low", "day_range_pct",
    "minutes_since_open", "is_first_hour", "is_last_hour",
    "prev_day_change_pct", "prev_day_range_pct",
    "candle_num", "hour",
]


# ═══════════════════════════════════════════════════════════════════════════
#  LABELING — simulate PE entry at each candle, label as win/loss
# ═══════════════════════════════════════════════════════════════════════════

PE_DELTA = 0.85  # deep ITM PE delta approximation

def label_candle_spot(spot_candles, candle_idx, index_sym):
    """Label using spot movement — no option master needed.
    Deep ITM PE (~600pt ITM) has delta ~0.85, so:
      option_price ≈ spot_price * delta_factor (for price level)
      option_move ≈ -spot_move * PE_DELTA (PE gains when spot drops)

    Simulates SL/TGT/floor on estimated option prices.
    Returns (label, estimated_pnl)."""
    if candle_idx >= len(spot_candles) - 1:
        return None, 0

    spot_at_entry = spot_candles[candle_idx]["close"]
    lot_size = LOT_SIZES.get(index_sym, 75)
    qty = lot_size * args.lots

    # Estimate option entry price: deep ITM PE ≈ ITM_depth * some factor
    itm_depth = (args.itm_min + args.itm_max) / 2
    est_opt_entry = itm_depth * 0.85  # rough option premium for deep ITM
    entry = est_opt_entry * (1 + args.slippage / 100)

    sl_price = entry * 0.50
    tgt_price = entry * 1.25

    peak_pnl = 0
    floor_armed = False

    remaining = spot_candles[candle_idx + 1:]
    if not remaining:
        return None, 0

    for c in remaining:
        # Spot moved by this much since entry
        spot_move = c["low"] - spot_at_entry
        # PE option moves opposite to spot
        opt_low_est = entry + (-spot_move * PE_DELTA)  # worst case this candle
        spot_move_high = c["high"] - spot_at_entry
        opt_high_est = entry + (-spot_move_high * PE_DELTA)  # actually this is worst for PE

        # For PE: spot down = option up, spot up = option down
        # opt price change ≈ -(spot_change) * delta
        opt_change_at_low = -(c["high"] - spot_at_entry) * PE_DELTA  # worst for PE (spot went up)
        opt_change_at_high = -(c["low"] - spot_at_entry) * PE_DELTA  # best for PE (spot went down)

        opt_price_worst = entry + opt_change_at_low
        opt_price_best = entry + opt_change_at_high

        # Hard SL cap check
        worst_pnl = (opt_price_worst - entry) * qty
        if args.max_loss > 0 and worst_pnl <= -args.max_loss:
            exit_price = entry - (args.max_loss / qty)
            net = _calc_pnl(entry, exit_price, qty)
            return (1 if net > 0 else 0), net

        # SL hit
        if opt_price_worst <= sl_price:
            net = _calc_pnl(entry, sl_price, qty)
            return (1 if net > 0 else 0), net

        # TGT hit
        if opt_price_best >= tgt_price:
            net = _calc_pnl(entry, tgt_price, qty)
            return (1 if net > 0 else 0), net

        # Floor logic
        best_pnl = (opt_price_best - entry) * qty
        peak_pnl = max(peak_pnl, best_pnl)
        if peak_pnl >= args.floor:
            floor_armed = True
        if floor_armed:
            cur_pnl = (opt_price_worst - entry) * qty
            if cur_pnl <= args.floor:
                floor_price = entry + (args.floor / qty)
                net = _calc_pnl(entry, floor_price, qty)
                return (1 if net > 0 else 0), net

    # EOD — use last candle's close
    last_spot_move = -(remaining[-1]["close"] - spot_at_entry) * PE_DELTA
    eod_price = entry + last_spot_move
    net = _calc_pnl(entry, eod_price, qty)
    return (1 if net > 0 else 0), net


def _calc_pnl(entry, exit_price, qty):
    gross = (exit_price - entry) * qty
    charges = calc_charges(entry, exit_price, qty)["total"]
    return gross - charges


# ═══════════════════════════════════════════════════════════════════════════
#  DATA COLLECTION — build feature+label dataset across all days
# ═══════════════════════════════════════════════════════════════════════════

def collect_features(index_sym, date_start, date_end):
    """Collect features for every tradeable candle. No labels — those depend on params.
    Cached to disk so subsequent runs with different params are instant."""
    feat_cache = os.path.join(CACHE_DIR, f"features_{index_sym}_{date_start}_{date_end}.csv")
    candles_cache = os.path.join(CACHE_DIR, f"spot_candles_{index_sym}_{date_start}_{date_end}.json")

    if os.path.exists(feat_cache):
        print(f"  Loading cached features: {feat_cache}")
        df = pd.read_csv(feat_cache)
        print(f"  Loaded {len(df)} candle samples")
        # Load cached spot candles for labeling
        spot_by_date = {}
        if os.path.exists(candles_cache):
            with open(candles_cache) as f:
                spot_by_date = json.load(f)
        return df, spot_by_date

    all_rows = []
    spot_by_date = {}
    prev_close = None
    prev_range_pct = 0.0
    prev_change_pct = 0.0

    current = date_start
    day_count = 0

    while current <= date_end:
        if current.weekday() >= 5:
            current += timedelta(days=1)
            continue

        spot_candles = fetch_spot_candles(index_sym, current)
        if not spot_candles or len(spot_candles) < 10:
            current += timedelta(days=1)
            continue

        day_count += 1
        sys.stdout.write(f"\r  Fetching {index_sym} {current} (day {day_count})...    ")
        sys.stdout.flush()

        spot_by_date[str(current)] = spot_candles

        df = compute_features(spot_candles)
        if df.empty:
            current += timedelta(days=1)
            continue

        if prev_close is not None:
            df["prev_day_change_pct"] = prev_change_pct
            df["prev_day_range_pct"] = prev_range_pct
            df["prev_close"] = prev_close

        day_open = spot_candles[0]["open"]
        day_close = spot_candles[-1]["close"]
        day_high = max(c["high"] for c in spot_candles)
        day_low = min(c["low"] for c in spot_candles)
        prev_change_pct = ((day_close - day_open) / day_open) * 100
        prev_range_pct = ((day_high - day_low) / day_open) * 100
        prev_close = day_close

        for idx, row in df.iterrows():
            hour = int(row["hour"])
            minute = int(row["minute"])
            if hour < 9 or (hour == 9 and minute < 20):
                continue
            if hour >= 15 and minute >= 25:
                continue
            if row["candle_num"] < 5:
                continue

            feat = {col: row[col] for col in FEATURE_COLS if col in row.index}
            feat["date"] = str(current)
            feat["time"] = row["time"]
            feat["candle_idx"] = int(row["candle_num"])
            all_rows.append(feat)

        current += timedelta(days=1)

    feat_df = pd.DataFrame(all_rows)
    feat_df.to_csv(feat_cache, index=False)
    with open(candles_cache, "w") as f:
        json.dump(spot_by_date, f)
    print(f"\r  Cached {len(feat_df)} candle features across {day_count} days for {index_sym}       ")
    return feat_df, spot_by_date


def apply_labels(feat_df, spot_by_date, index_sym):
    """Apply labels based on current params (floor, max-loss, skip-hours). Fast — no API calls."""
    labels = []
    pnls = []
    keep = []

    for _, row in feat_df.iterrows():
        hour = int(row["hour"])
        if hour in SKIP_HOURS:
            keep.append(False)
            labels.append(0)
            pnls.append(0)
            continue

        d = row["date"]
        candles = spot_by_date.get(d)
        if not candles:
            keep.append(False)
            labels.append(0)
            pnls.append(0)
            continue

        label, pnl = label_candle_spot(candles, int(row["candle_idx"]), index_sym)
        if label is None:
            keep.append(False)
            labels.append(0)
            pnls.append(0)
        else:
            keep.append(True)
            labels.append(label)
            pnls.append(pnl)

    feat_df = feat_df.copy()
    feat_df["label"] = labels
    feat_df["pnl"] = pnls
    return feat_df[keep].reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════
#  WALK-FORWARD TRAINING + TESTING
# ═══════════════════════════════════════════════════════════════════════════

def walk_forward_test(df):
    """Train on train_days, test on test_days, slide forward. Returns results."""
    if df.empty or len(df) < 100:
        print("  Not enough data for walk-forward test")
        return None, None

    dates = sorted(df["date"].unique())
    print(f"\n  Walk-forward: {len(dates)} trading days, {len(df)} samples")
    print(f"  Label distribution: {df['label'].value_counts().to_dict()}")
    print(f"  Win rate in data: {df['label'].mean():.1%}\n")

    all_test_results = []
    models = []

    train_window = args.train_days
    test_window = args.test_days

    fold = 0
    i = 0
    while i + train_window + test_window <= len(dates):
        fold += 1
        train_dates = dates[i:i + train_window]
        test_dates = dates[i + train_window:i + train_window + test_window]

        train_df = df[df["date"].isin(train_dates)]
        test_df = df[df["date"].isin(test_dates)].copy()

        if len(train_df) < 50 or len(test_df) < 10:
            i += test_window
            continue

        X_train = train_df[FEATURE_COLS].replace([np.inf, -np.inf], np.nan).fillna(0)
        y_train = train_df["label"]
        X_test = test_df[FEATURE_COLS].replace([np.inf, -np.inf], np.nan).fillna(0)
        y_test = test_df["label"]

        model = XGBClassifier(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            min_child_weight=5,
            scale_pos_weight=len(y_train[y_train == 0]) / max(len(y_train[y_train == 1]), 1),
            eval_metric="logloss",
            verbosity=0,
            random_state=42,
        )
        model.fit(X_train, y_train)

        probs = model.predict_proba(X_test)[:, 1]
        test_df["confidence"] = probs
        test_df["predicted"] = (probs >= args.min_confidence).astype(int)

        high_conf = test_df[test_df["predicted"] == 1]

        if len(high_conf) == 0:
            print(f"  Fold {fold}: train {train_dates[0]}→{train_dates[-1]} | "
                  f"test {test_dates[0]}→{test_dates[-1]} | "
                  f"NO signals above {args.min_confidence:.0%} confidence")
            i += test_window
            continue

        daily_trades = high_conf.groupby("date").apply(
            lambda g: g.nlargest(args.max_trades_per_day, "confidence")
        ).reset_index(drop=True)

        wins = daily_trades["label"].sum()
        total = len(daily_trades)
        net_pnl = daily_trades["pnl"].sum()
        wr = wins / total if total > 0 else 0

        print(f"  Fold {fold}: train {train_dates[0]}→{train_dates[-1]} | "
              f"test {test_dates[0]}→{test_dates[-1]} | "
              f"signals={total} {wins}W/{total-wins}L ({wr:.0%}) "
              f"P&L=₹{net_pnl:+,.0f}")

        all_test_results.append(daily_trades)
        models.append(model)

        i += test_window

    if not all_test_results:
        print("  No walk-forward results")
        return None, None

    combined = pd.concat(all_test_results, ignore_index=True)
    return combined, models[-1]


def print_results(results_df):
    """Print detailed results from walk-forward test."""
    if results_df is None or results_df.empty:
        return

    total = len(results_df)
    wins = results_df["label"].sum()
    losses = total - wins
    wr = wins / total if total > 0 else 0
    net_pnl = results_df["pnl"].sum()
    avg_win = results_df[results_df["label"] == 1]["pnl"].mean() if wins > 0 else 0
    avg_loss = results_df[results_df["label"] == 0]["pnl"].mean() if losses > 0 else 0

    print(f"\n{'='*80}")
    print(f"  WALK-FORWARD RESULTS — {args.index}")
    print(f"{'='*80}")
    print(f"  Total signals:    {total}")
    print(f"  Win rate:         {wr:.1%} ({wins}W / {losses}L)")
    print(f"  Net P&L:          ₹{net_pnl:+,.0f}")
    print(f"  Avg win:          ₹{avg_win:+,.0f}")
    print(f"  Avg loss:         ₹{avg_loss:+,.0f}")
    print(f"  Min confidence:   {args.min_confidence:.0%}")
    print(f"  Max trades/day:   {args.max_trades_per_day}")

    # Daily breakdown
    daily = results_df.groupby("date").agg(
        trades=("pnl", "count"),
        wins=("label", "sum"),
        pnl=("pnl", "sum"),
    )
    daily["wr"] = daily["wins"] / daily["trades"]

    print(f"\n  --- Daily P&L ---")
    green = 0
    red = 0
    max_dd = 0
    equity = 0
    peak = 0
    for d, row in daily.iterrows():
        tag = "[+]" if row["pnl"] >= 0 else "[-]"
        print(f"  {d}  {int(row['trades'])} trades  "
              f"{int(row['wins'])}W/{int(row['trades'] - row['wins'])}L  "
              f"P&L: ₹{row['pnl']:>+10,.0f}  {tag}")
        equity += row["pnl"]
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)
        if row["pnl"] >= 0:
            green += 1
        else:
            red += 1

    print(f"\n  Green days: {green} | Red days: {red}")
    print(f"  Max drawdown: ₹{max_dd:,.0f}")
    print(f"  Final equity: ₹{equity:+,.0f}")
    if max_dd > 0:
        print(f"  Calmar ratio: {equity / max_dd:.2f}")

    # By hour
    by_hour = results_df.copy()
    by_hour["hour_bucket"] = by_hour["time"].str[:2].astype(int)
    hourly = by_hour.groupby("hour_bucket").agg(
        trades=("pnl", "count"),
        wins=("label", "sum"),
        pnl=("pnl", "sum"),
    )
    print(f"\n  --- By Hour ---")
    print(f"  {'Hour':<8} {'Trades':<8} {'Win%':<8} {'P&L':>10}")
    print(f"  {'─'*36}")
    for h, row in hourly.iterrows():
        wr_h = row["wins"] / row["trades"] if row["trades"] > 0 else 0
        print(f"  {h:02d}:xx    {int(row['trades']):<8} {wr_h:>5.0%}  ₹{row['pnl']:>+10,.0f}")

    # Feature importance
    print(f"\n  --- Top 15 Features ---")

    print(f"{'='*80}")


def print_feature_importance(model):
    if model is None:
        return
    importances = model.feature_importances_
    feat_imp = sorted(zip(FEATURE_COLS, importances), key=lambda x: -x[1])
    for name, imp in feat_imp[:15]:
        bar = "█" * int(imp * 100)
        print(f"  {name:<25} {imp:.3f}  {bar}")
    print(f"{'='*80}")


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print(f"{'='*80}")
    print(f"  ML STRATEGY FINDER — {args.index}")
    print(f"  Period: {start_date} → {end_date} ({args.days} days)")
    print(f"  Train: {args.train_days}d | Test: {args.test_days}d | Min confidence: {args.min_confidence:.0%}")
    print(f"  {args.lots}L | ₹{args.max_loss:,.0f} SL cap | ₹{args.floor:,.0f} floor")
    print(f"  Slippage: {args.slippage}% | Charges: real")
    print(f"{'='*80}\n")

    print("  Phase 1: Loading/fetching features (cached after first run)...\n")
    feat_df, spot_by_date = collect_features(args.index, start_date, end_date)

    if feat_df.empty:
        print("  No data collected!")
        return

    print(f"\n  Phase 1b: Applying labels with current params (floor=₹{args.floor:,.0f}, SL=₹{args.max_loss:,.0f}, skip={SKIP_HOURS})...")
    dataset = apply_labels(feat_df, spot_by_date, args.index)
    print(f"  Labeled {len(dataset)} tradeable candles")

    print(f"\n  Phase 2: Walk-forward training + testing...\n")
    results, best_model = walk_forward_test(dataset)

    print_results(results)
    print_feature_importance(best_model)

    if results is not None and not results.empty:
        results_path = os.path.join(CACHE_DIR, f"results_{args.index}_{start_date}_{end_date}.csv")
        results.to_csv(results_path, index=False)
        print(f"\n  Results saved: {results_path}")


if __name__ == "__main__":
    main()

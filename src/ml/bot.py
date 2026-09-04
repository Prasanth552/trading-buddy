"""ML Scanner Bot — live paper-trade bot using XGBoost-trained PE signals.

Runs during market hours (09:15–10:30). Fetches live 5-min candles,
computes features, and generates PE signals when model confidence > threshold.

Architecture:
  1. On startup (or daily retrain), trains XGBoost on last N trading days
  2. Every 5 minutes during trading window, fetches latest candles
  3. Computes features on new candle, runs through model
  4. If confidence > threshold → paper trade the PE signal
  5. Monitors open paper trades for SL/TGT/floor exits
  6. Stores everything in SQLite for dashboard

Usage:
  .venv/bin/python3 -m src.ml.bot                   # run live scanner
  .venv/bin/python3 -m src.ml.bot --retrain-only     # just retrain model
"""
import os, sys, json, time, logging, pickle, asyncio
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field, asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv()

import config
from src.storage import db

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("ml_bot")

# ─── Config ──────────────────────────────────────────────────────────────
INDEXES = ["NIFTY", "SENSEX"]
LOTS = 3
FLOOR = 4000
MAX_LOSS = 4000
SLIPPAGE_PCT = 0.5
MIN_CONFIDENCE = 0.70
MAX_TRADES_PER_DAY = 5
DAILY_LOSS_CAP = 2
TRAIN_DAYS = 40
SCAN_START_HOUR = 9
SCAN_START_MIN = 20
SCAN_END_HOUR = 10
SCAN_END_MIN = 30
SKIP_HOURS = {11, 12, 13, 14, 15}
POLL_INTERVAL = 300  # 5 minutes
ITM_MIN = 300
ITM_MAX = 900
PE_DELTA = 0.85

MODEL_DIR = os.path.join(config.DATA_DIR, "ml_models")
CACHE_DIR = os.path.join(config.DATA_DIR, "ml_cache")

SPOT_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "SENSEX": "BSE_INDEX|SENSEX",
}
LOT_SIZES = {"NIFTY": 75, "SENSEX": 20}
STRIKE_STEPS = {"NIFTY": 50, "SENSEX": 100}

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


# ─── Data classes ────────────────────────────────────────────────────────

@dataclass
class MLSignal:
    ts: str
    index: str
    spot: float
    confidence: float
    strike: int = 0
    entry: float = 0.0
    sl: float = 0.0
    tgt: float = 0.0
    qty: int = 0
    status: str = "PENDING"  # PENDING → OPEN → CLOSED
    exit_price: float = 0.0
    pnl: float = 0.0
    exit_reason: str = ""
    peak_pnl: float = 0.0
    floor_armed: bool = False


# ─── Feature Engine (same as ml_strategy.py) ─────────────────────────────

def compute_features(candles):
    import numpy as np
    import pandas as pd

    if not candles or len(candles) < 5:
        return pd.DataFrame()

    df = pd.DataFrame(candles)
    df["time"] = df["date"].str[11:16]
    df["hour"] = df["time"].str[:2].astype(int)
    df["minute"] = df["time"].str[3:5].astype(int)
    df["candle_num"] = range(len(df))

    spot_open = df["open"].iloc[0]

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

    df["sma_5"] = df["close"].rolling(5).mean()
    df["sma_10"] = df["close"].rolling(10).mean()
    df["sma_20"] = df["close"].rolling(20).mean()
    df["close_vs_sma5"] = ((df["close"] - df["sma_5"]) / df["sma_5"]) * 100
    df["close_vs_sma10"] = ((df["close"] - df["sma_10"]) / df["sma_10"]) * 100
    df["close_vs_sma20"] = ((df["close"] - df["sma_20"]) / df["sma_20"]) * 100
    df["sma5_slope"] = df["sma_5"].diff() / df["sma_5"].shift(1) * 100
    df["sma10_slope"] = df["sma_10"].diff() / df["sma_10"].shift(1) * 100

    df["roc_1"] = df["close"].pct_change(1) * 100
    df["roc_3"] = df["close"].pct_change(3) * 100
    df["roc_5"] = df["close"].pct_change(5) * 100
    df["roc_10"] = df["close"].pct_change(10) * 100

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(14, min_periods=5).mean()
    avg_loss = loss.rolling(14, min_periods=5).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    df["rsi_14"] = 100 - (100 / (1 + rs))

    high_low = df["high"] - df["low"]
    high_pc = abs(df["high"] - df["close"].shift(1))
    low_pc = abs(df["low"] - df["close"].shift(1))
    tr = pd.concat([high_low, high_pc, low_pc], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(14, min_periods=5).mean()
    df["atr_pct"] = (df["atr_14"] / df["close"]) * 100

    df["bb_mid"] = df["close"].rolling(20, min_periods=5).mean()
    bb_std = df["close"].rolling(20, min_periods=5).std()
    df["bb_upper"] = df["bb_mid"] + 2 * bb_std
    df["bb_lower"] = df["bb_mid"] - 2 * bb_std
    df["bb_pct"] = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"] + 1e-10)
    df["bb_width"] = ((df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]) * 100

    df["cum_vol"] = df["volume"].cumsum()
    df["cum_vwap"] = (df["close"] * df["volume"]).cumsum()
    df["vwap"] = df["cum_vwap"] / (df["cum_vol"] + 1)
    df["close_vs_vwap"] = ((df["close"] - df["vwap"]) / df["vwap"]) * 100

    df["vol_sma_10"] = df["volume"].rolling(10, min_periods=3).mean()
    df["vol_ratio"] = df["volume"] / (df["vol_sma_10"] + 1)
    df["vol_surge"] = (df["volume"] > df["vol_sma_10"] * 1.5).astype(int)

    df["is_red"] = (df["close"] < df["open"]).astype(int)
    df["consec_red"] = df["is_red"].groupby((df["is_red"] != df["is_red"].shift()).cumsum()).cumcount() + 1
    df.loc[df["is_red"] == 0, "consec_red"] = 0
    df["is_green"] = (df["close"] > df["open"]).astype(int)
    df["consec_green"] = df["is_green"].groupby((df["is_green"] != df["is_green"].shift()).cumsum()).cumcount() + 1
    df.loc[df["is_green"] == 0, "consec_green"] = 0

    df["day_high_so_far"] = df["high"].cummax()
    df["day_low_so_far"] = df["low"].cummin()
    df["pct_from_day_high"] = ((df["close"] - df["day_high_so_far"]) / df["day_high_so_far"]) * 100
    df["pct_from_day_low"] = ((df["close"] - df["day_low_so_far"]) / df["day_low_so_far"]) * 100
    df["day_range_pct"] = ((df["day_high_so_far"] - df["day_low_so_far"]) / spot_open) * 100

    df["minutes_since_open"] = (df["hour"] - 9) * 60 + df["minute"] - 15
    df["is_first_hour"] = (df["minutes_since_open"] <= 60).astype(int)
    df["is_last_hour"] = (df["hour"] >= 15).astype(int)

    df["prev_day_change_pct"] = 0.0
    df["prev_day_range_pct"] = 0.0

    df.drop(columns=["cum_vol", "cum_vwap", "is_red", "is_green"], inplace=True, errors="ignore")
    return df


# ─── Training ────────────────────────────────────────────────────────────

def _fetch_spot(uclient, index_sym, ref_date):
    spot_key = SPOT_KEYS.get(index_sym)
    if not spot_key:
        return None
    cache = os.path.join(CACHE_DIR, f"spot_{index_sym}_{ref_date}.json")
    if os.path.exists(cache):
        with open(cache) as f:
            return json.load(f)
    y, m, d = ref_date.year, ref_date.month, ref_date.day
    from_dt = datetime(y, m, d, 9, 15, 0, tzinfo=IST)
    to_dt = datetime(y, m, d, 15, 30, 0, tzinfo=IST)
    try:
        candles = uclient.historical_data(spot_key, from_dt, to_dt, "5minute")
        time.sleep(0.25)
        if candles:
            with open(cache, "w") as f:
                json.dump(candles, f)
        return candles
    except Exception:
        time.sleep(0.5)
        return None


def _label_spot(candles, idx, index_sym):
    from src.notify.channel_listener import calc_charges
    if idx >= len(candles) - 1:
        return None, 0
    spot_at_entry = candles[idx]["close"]
    lot_size = LOT_SIZES.get(index_sym, 75)
    qty = lot_size * LOTS
    itm_depth = (ITM_MIN + ITM_MAX) / 2
    est_opt_entry = itm_depth * 0.85
    entry = est_opt_entry * (1 + SLIPPAGE_PCT / 100)
    sl_price = entry * 0.50
    tgt_price = entry * 1.25
    peak_pnl = 0
    floor_armed = False

    for c in candles[idx + 1:]:
        opt_change_worst = -(c["high"] - spot_at_entry) * PE_DELTA
        opt_change_best = -(c["low"] - spot_at_entry) * PE_DELTA
        opt_price_worst = entry + opt_change_worst
        opt_price_best = entry + opt_change_best

        worst_pnl = (opt_price_worst - entry) * qty
        if MAX_LOSS > 0 and worst_pnl <= -MAX_LOSS:
            exit_p = entry - (MAX_LOSS / qty)
            gross = (exit_p - entry) * qty
            return (1 if gross - calc_charges(entry, exit_p, qty)["total"] > 0 else 0), gross - calc_charges(entry, exit_p, qty)["total"]
        if opt_price_worst <= sl_price:
            gross = (sl_price - entry) * qty
            return (1 if gross - calc_charges(entry, sl_price, qty)["total"] > 0 else 0), gross - calc_charges(entry, sl_price, qty)["total"]
        if opt_price_best >= tgt_price:
            gross = (tgt_price - entry) * qty
            return (1 if gross - calc_charges(entry, tgt_price, qty)["total"] > 0 else 0), gross - calc_charges(entry, tgt_price, qty)["total"]
        best_pnl = (opt_price_best - entry) * qty
        peak_pnl = max(peak_pnl, best_pnl)
        if peak_pnl >= FLOOR:
            floor_armed = True
        if floor_armed:
            cur_pnl = (opt_price_worst - entry) * qty
            if cur_pnl <= FLOOR:
                floor_p = entry + (FLOOR / qty)
                gross = (floor_p - entry) * qty
                return (1 if gross - calc_charges(entry, floor_p, qty)["total"] > 0 else 0), gross - calc_charges(entry, floor_p, qty)["total"]

    last_spot_move = -(candles[-1]["close"] - spot_at_entry) * PE_DELTA
    eod_p = entry + last_spot_move
    gross = (eod_p - entry) * qty
    return (1 if gross - calc_charges(entry, eod_p, qty)["total"] > 0 else 0), gross - calc_charges(entry, eod_p, qty)["total"]


def train_model(index_sym, end_dt=None):
    """Train XGBoost on last TRAIN_DAYS trading days. Returns fitted model."""
    import numpy as np
    import pandas as pd
    from xgboost import XGBClassifier
    from src.broker.upstox_data import UpstoxData
    from src.notify.channel_listener import calc_charges

    os.makedirs(MODEL_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    uclient = UpstoxData()
    end_dt = end_dt or datetime.now(IST).date()
    start_dt = end_dt - timedelta(days=int(TRAIN_DAYS * 1.6))

    log.info("Training %s model: %s → %s", index_sym, start_dt, end_dt)

    rows = []
    prev_change = 0.0
    prev_range = 0.0
    current = start_dt
    day_count = 0

    while current <= end_dt:
        if current.weekday() >= 5:
            current += timedelta(days=1)
            continue

        candles = _fetch_spot(uclient, index_sym, current)
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

            label, pnl = _label_spot(candles, int(row["candle_num"]), index_sym)
            if label is None:
                continue

            feat = {col: row[col] for col in FEATURE_COLS if col in row.index}
            feat["label"] = label
            rows.append(feat)

        current += timedelta(days=1)

    if len(rows) < 50:
        log.warning("Not enough training data (%d rows) for %s", len(rows), index_sym)
        return None

    train_df = pd.DataFrame(rows)
    X = train_df[FEATURE_COLS].replace([np.inf, -np.inf], np.nan).fillna(0)
    y = train_df["label"]

    model = XGBClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, min_child_weight=5,
        scale_pos_weight=len(y[y == 0]) / max(len(y[y == 1]), 1),
        eval_metric="logloss", verbosity=0, random_state=42,
    )
    model.fit(X, y)

    model_path = os.path.join(MODEL_DIR, f"model_{index_sym}.pkl")
    with open(model_path, "wb") as f:
        pickle.dump(model, f)

    wr = y.mean()
    log.info("Trained %s: %d samples, %.1f%% base WR → saved %s",
             index_sym, len(train_df), wr * 100, model_path)
    return model


def load_model(index_sym):
    model_path = os.path.join(MODEL_DIR, f"model_{index_sym}.pkl")
    if not os.path.exists(model_path):
        return None
    with open(model_path, "rb") as f:
        return pickle.load(f)


# ─── ML Signal DB ────────────────────────────────────────────────────────

ML_SIGNALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS ml_signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    date       TEXT NOT NULL,
    index_sym  TEXT NOT NULL,
    spot       REAL,
    confidence REAL,
    strike     INTEGER,
    entry      REAL,
    sl         REAL,
    tgt        REAL,
    qty        INTEGER,
    status     TEXT DEFAULT 'PENDING',
    exit_price REAL,
    pnl        REAL,
    exit_reason TEXT,
    peak_pnl   REAL DEFAULT 0,
    floor_armed INTEGER DEFAULT 0
);
"""


def init_ml_db():
    with db.get_conn() as conn:
        conn.executescript(ML_SIGNALS_SCHEMA)


def save_signal(sig: MLSignal):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO ml_signals (ts, date, index_sym, spot, confidence, strike, "
            "entry, sl, tgt, qty, status, exit_price, pnl, exit_reason, peak_pnl, floor_armed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sig.ts, sig.ts[:10], sig.index, sig.spot, sig.confidence, sig.strike,
             sig.entry, sig.sl, sig.tgt, sig.qty, sig.status, sig.exit_price,
             sig.pnl, sig.exit_reason, sig.peak_pnl, 1 if sig.floor_armed else 0))


def update_signal(sig_id, **kwargs):
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [sig_id]
    with db.get_conn() as conn:
        conn.execute(f"UPDATE ml_signals SET {sets} WHERE id=?", vals)


def get_today_signals():
    today = datetime.now(IST).date().isoformat()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ml_signals WHERE date=? ORDER BY id DESC", (today,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_signals(limit=50):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ml_signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Live Scanner ────────────────────────────────────────────────────────

class MLScanner:
    def __init__(self):
        from src.broker.upstox_data import UpstoxData
        self.models = {}
        self.uclient = UpstoxData()
        self.today_signals = []
        self.today_losses = {}  # per-index loss count
        self.today_trades = 0
        self.prev_day_data = {}  # {index: {change_pct, range_pct}}
        self.last_candle_time = {}  # {index: "HH:MM"} to avoid duplicate signals

    def load_or_train_models(self):
        for idx in INDEXES:
            model = load_model(idx)
            if model is None:
                log.info("No saved model for %s, training...", idx)
                model = train_model(idx)
            if model:
                self.models[idx] = model
                log.info("Model loaded for %s", idx)
            else:
                log.warning("Could not load/train model for %s", idx)

    def _fetch_live_candles(self, index_sym):
        spot_key = SPOT_KEYS.get(index_sym)
        if not spot_key:
            return None
        now = datetime.now(IST)
        from_dt = datetime(now.year, now.month, now.day, 9, 15, 0, tzinfo=IST)
        to_dt = now
        try:
            candles = self.uclient.historical_data(spot_key, from_dt, to_dt, "5minute")
            return candles
        except Exception as e:
            log.warning("Failed to fetch live candles for %s: %s", index_sym, e)
            return None

    def scan_once(self):
        """Scan all indexes for signals on the latest candle."""
        import numpy as np
        now = datetime.now(IST)
        signals_generated = []

        for index_sym in INDEXES:
            if index_sym not in self.models:
                continue

            # Daily loss cap check
            if self.today_losses.get(index_sym, 0) >= DAILY_LOSS_CAP:
                continue

            # Total trade cap
            if self.today_trades >= MAX_TRADES_PER_DAY:
                continue

            candles = self._fetch_live_candles(index_sym)
            if not candles or len(candles) < 6:
                continue

            # Check if we already processed this candle
            latest_time = candles[-1]["date"][11:16]
            if self.last_candle_time.get(index_sym) == latest_time:
                continue
            self.last_candle_time[index_sym] = latest_time

            # Compute features
            df = compute_features(candles)
            if df.empty:
                continue

            # Fill prev day data
            prev = self.prev_day_data.get(index_sym, {})
            df["prev_day_change_pct"] = prev.get("change_pct", 0)
            df["prev_day_range_pct"] = prev.get("range_pct", 0)

            # Get the latest candle row
            last_row = df.iloc[-1]
            hour = int(last_row["hour"])
            minute = int(last_row["minute"])

            # Time filter
            if hour < SCAN_START_HOUR or (hour == SCAN_START_HOUR and minute < SCAN_START_MIN):
                continue
            if hour > SCAN_END_HOUR or (hour == SCAN_END_HOUR and minute > SCAN_END_MIN):
                continue
            if hour in SKIP_HOURS:
                continue
            if last_row["candle_num"] < 5:
                continue

            # Run model
            features = last_row[FEATURE_COLS].to_frame().T
            features = features.replace([np.inf, -np.inf], np.nan).fillna(0)
            model = self.models[index_sym]
            prob = model.predict_proba(features)[0][1]

            if prob < MIN_CONFIDENCE:
                continue

            # Generate signal!
            spot = last_row["close"]
            step = STRIKE_STEPS.get(index_sym, 50)
            itm_depth = (ITM_MIN + ITM_MAX) // 2
            strike = round((spot + itm_depth) / step) * step
            lot_size = LOT_SIZES.get(index_sym, 75)
            qty = lot_size * LOTS

            est_entry = itm_depth * 0.85 * (1 + SLIPPAGE_PCT / 100)

            sig = MLSignal(
                ts=now.strftime("%Y-%m-%d %H:%M:%S"),
                index=index_sym,
                spot=round(spot, 2),
                confidence=round(prob, 3),
                strike=strike,
                entry=round(est_entry, 2),
                sl=round(est_entry * 0.50, 2),
                tgt=round(est_entry * 1.25, 2),
                qty=qty,
                status="OPEN",
            )

            save_signal(sig)
            self.today_trades += 1
            self.today_signals.append(sig)
            signals_generated.append(sig)

            log.info("ML SIGNAL: %s PE %d @ ₹%.0f | conf=%.1f%% | spot=%.0f",
                     index_sym, strike, est_entry, prob * 100, spot)

        return signals_generated

    def check_exits(self):
        """Check open signals for exit conditions using latest candle data."""
        from src.notify.channel_listener import calc_charges
        today = datetime.now(IST).date().isoformat()
        with db.get_conn() as conn:
            open_sigs = conn.execute(
                "SELECT * FROM ml_signals WHERE date=? AND status='OPEN'", (today,)
            ).fetchall()

        for row in open_sigs:
            sig = dict(row)
            index_sym = sig["index_sym"]
            candles = self._fetch_live_candles(index_sym)
            if not candles:
                continue

            entry = sig["entry"]
            spot_at_entry = sig["spot"]
            qty = sig["qty"]
            peak_pnl = sig["peak_pnl"] or 0
            floor_armed = bool(sig["floor_armed"])

            latest = candles[-1]
            spot_move_low = -(latest["high"] - spot_at_entry) * PE_DELTA
            spot_move_high = -(latest["low"] - spot_at_entry) * PE_DELTA

            opt_worst = entry + spot_move_low
            opt_best = entry + spot_move_high

            exit_price = None
            exit_reason = None

            # MAX_SL
            worst_pnl = (opt_worst - entry) * qty
            if MAX_LOSS > 0 and worst_pnl <= -MAX_LOSS:
                exit_price = entry - (MAX_LOSS / qty)
                exit_reason = "MAX_SL"
            # SL hit
            elif opt_worst <= sig["sl"]:
                exit_price = sig["sl"]
                exit_reason = "SL"
            # TGT hit
            elif opt_best >= sig["tgt"]:
                exit_price = sig["tgt"]
                exit_reason = "TGT"
            else:
                # Floor logic
                best_pnl = (opt_best - entry) * qty
                peak_pnl = max(peak_pnl, best_pnl)
                if peak_pnl >= FLOOR:
                    floor_armed = True
                if floor_armed:
                    cur_pnl = (opt_worst - entry) * qty
                    if cur_pnl <= FLOOR:
                        exit_price = entry + (FLOOR / qty)
                        exit_reason = "FLOOR"

                # EOD check
                now = datetime.now(IST)
                if now.hour >= 15 and now.minute >= 20 and exit_price is None:
                    spot_move = -(latest["close"] - spot_at_entry) * PE_DELTA
                    exit_price = entry + spot_move
                    exit_reason = "EOD"

            if exit_price is not None:
                gross = (exit_price - entry) * qty
                charges = calc_charges(entry, exit_price, qty)["total"]
                net_pnl = gross - charges

                update_signal(sig["id"],
                              status="CLOSED",
                              exit_price=round(exit_price, 2),
                              pnl=round(net_pnl, 2),
                              exit_reason=exit_reason,
                              peak_pnl=round(peak_pnl, 2),
                              floor_armed=1 if floor_armed else 0)

                if net_pnl < 0:
                    self.today_losses[index_sym] = self.today_losses.get(index_sym, 0) + 1

                log.info("ML EXIT: %s %s PE %d → %s | P&L=₹%.0f",
                         index_sym, sig["ts"][11:16], sig["strike"], exit_reason, net_pnl)
            else:
                update_signal(sig["id"],
                              peak_pnl=round(peak_pnl, 2),
                              floor_armed=1 if floor_armed else 0)

    def load_prev_day(self):
        """Load previous trading day's data for prev_day features."""
        yesterday = datetime.now(IST).date() - timedelta(days=1)
        while yesterday.weekday() >= 5:
            yesterday -= timedelta(days=1)

        for idx in INDEXES:
            candles = _fetch_spot(self.uclient, idx, yesterday)
            if candles and len(candles) > 1:
                day_open = candles[0]["open"]
                day_close = candles[-1]["close"]
                day_high = max(c["high"] for c in candles)
                day_low = min(c["low"] for c in candles)
                self.prev_day_data[idx] = {
                    "change_pct": ((day_close - day_open) / day_open) * 100,
                    "range_pct": ((day_high - day_low) / day_open) * 100,
                }

    def run(self):
        """Main loop — scan every 5 minutes during trading window."""
        init_ml_db()
        self.load_or_train_models()
        self.load_prev_day()

        log.info("ML Scanner started — indexes=%s, confidence=%.0f%%, window=%02d:%02d-%02d:%02d",
                 INDEXES, MIN_CONFIDENCE * 100,
                 SCAN_START_HOUR, SCAN_START_MIN, SCAN_END_HOUR, SCAN_END_MIN)

        while True:
            now = datetime.now(IST)

            # Only scan during trading window
            current_mins = now.hour * 60 + now.minute
            start_mins = SCAN_START_HOUR * 60 + SCAN_START_MIN
            end_mins = SCAN_END_HOUR * 60 + SCAN_END_MIN

            if start_mins <= current_mins <= end_mins and now.weekday() < 5:
                self.scan_once()
                self.check_exits()
            elif current_mins > end_mins:
                # After scan window, just check exits until EOD
                if now.hour < 16:
                    self.check_exits()

            time.sleep(POLL_INTERVAL)


# ─── Dashboard API helpers ───────────────────────────────────────────────

def ml_status():
    """Return ML scanner status for dashboard."""
    today = datetime.now(IST).date().isoformat()
    try:
        with db.get_conn() as conn:
            conn.execute("SELECT 1 FROM ml_signals LIMIT 1")
    except Exception:
        return {"enabled": False, "signals": [], "summary": {}}

    signals = get_today_signals()
    closed = [s for s in signals if s["status"] == "CLOSED"]
    open_sigs = [s for s in signals if s["status"] == "OPEN"]

    total_pnl = sum(s["pnl"] or 0 for s in closed)
    wins = sum(1 for s in closed if (s["pnl"] or 0) > 0)
    losses = len(closed) - wins

    return {
        "enabled": True,
        "date": today,
        "signals": signals,
        "open_count": len(open_sigs),
        "closed_count": len(closed),
        "summary": {
            "total_trades": len(closed),
            "wins": wins,
            "losses": losses,
            "win_rate": f"{wins / len(closed) * 100:.0f}%" if closed else "—",
            "net_pnl": round(total_pnl, 2),
            "open_trades": len(open_sigs),
        },
    }


# ─── CLI ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s IST | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    p = argparse.ArgumentParser()
    p.add_argument("--retrain-only", action="store_true")
    p.add_argument("--index", default=None, help="Train specific index only")
    a = p.parse_args()

    if a.retrain_only:
        indexes = [a.index] if a.index else INDEXES
        for idx in indexes:
            train_model(idx)
        print("Retraining complete.")
    else:
        scanner = MLScanner()
        scanner.run()

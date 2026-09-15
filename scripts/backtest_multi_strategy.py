"""Multi-strategy signal backtester using real 1-min option candles.

Tests 5 strategies and tracks each signal to exit with real premiums:
  1. VWAP Bounce    — buy ATM option on VWAP touch + bounce (5-min)
  2. ORB Breakout   — buy ATM option on 30-min range breakout
  3. EMA Pullback   — buy ATM option on pullback to 20 EMA in trend
  4. Short Straddle — sell ATM CE+PE at 10:00, SL 25%, TGT 30% decay
  5. Expiry Theta   — sell OTM CE+PE on expiry day at 10:30

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/backtest_multi_strategy.py \
        --from 2026-09-01 --to 2026-09-15 --index NIFTY
"""
from __future__ import annotations

import argparse
import sys
import time as _time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.broker.upstox_data import UpstoxData
from src.utils.logging import get_logger

log = get_logger("backtest")
IST = ZoneInfo("Asia/Kolkata")

# ── Index config ────────────────────────────────────────────────────

INDEXES = {
    "NIFTY": {
        "key": "NSE_INDEX|Nifty 50",
        "step": 50,
        "lot_size": 75,
        "expiry_weekday": 1,   # Tuesday
    },
    "BANKNIFTY": {
        "key": "NSE_INDEX|Nifty Bank",
        "step": 100,
        "lot_size": 30,
        "expiry_weekday": 2,   # Wednesday
    },
}

# ── Strategy parameters ─────────────────────────────────────────────

STRATEGY_PARAMS = {
    "vwap_bounce": {
        "sl_pct": 0.30,
        "tgt_pct": 0.50,
        "max_hold_mins": 45,
        "active_from": "10:00",
        "active_to": "14:00",
        "vwap_touch_pct": 0.15,
        "bounce_min_pct": 0.08,
        "vol_mult": 1.2,
        "max_signals": 2,
        "cooldown_mins": 30,
    },
    "orb_breakout": {
        "sl_pct": 0.30,
        "tgt_pct": 0.60,
        "range_end": "09:44",
        "active_from": "09:45",
        "active_to": "11:30",
        "max_hold_mins": 90,
        "min_range_pct": 0.20,
        "max_range_pct": 1.00,
        "max_signals": 1,
    },
    "ema_pullback": {
        "sl_pct": 0.25,
        "tgt_pct": 0.50,
        "max_hold_mins": 45,
        "active_from": "10:15",
        "active_to": "14:30",
        "ema_period": 20,
        "trend_bars": 3,
        "touch_pct": 0.12,
        "max_signals": 2,
        "cooldown_mins": 30,
    },
    "short_straddle": {
        "entry_time": "10:00",
        "sl_pct": 0.25,
        "tgt_pct": 0.30,
        "time_exit": "15:15",
        "max_signals": 1,
    },
    "expiry_theta": {
        "entry_time": "10:30",
        "otm_steps": 3,
        "sl_pct": 1.00,
        "tgt_pct": 0.50,
        "time_exit": "15:15",
        "max_signals": 1,
    },
}

IMPACT_COST = 3.0


# ── Data classes ────────────────────────────────────────────────────

@dataclass
class Signal:
    time: str
    strategy: str
    action: str
    index: str
    strike: float
    option_type: str
    entry_premium: float
    sl_premium: float
    tgt_premium: float
    spot_at_entry: float
    instrument_key: str = ""
    paired_strike: float = 0
    paired_type: str = ""
    paired_premium: float = 0
    paired_instrument_key: str = ""
    ref_date: date = None


@dataclass
class TradeResult:
    signal: Signal
    exit_time: str
    exit_premium: float
    exit_reason: str
    pnl_per_lot: float
    hold_mins: int
    won: bool
    paired_exit_premium: float = 0


# ── Helpers ─────────────────────────────────────────────────────────

def round_strike(price, step):
    return round(price / step) * step


def time_diff_mins(t1: str, t2: str) -> int:
    h1, m1 = int(t1[:2]), int(t1[3:5])
    h2, m2 = int(t2[:2]), int(t2[3:5])
    return (h2 * 60 + m2) - (h1 * 60 + m1)


def resample_5min(candles_1min: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for c in candles_1min:
        t = c["time"]
        h, m = int(t[:2]), int(t[3:5])
        bar_m = (m // 5) * 5
        groups[f"{h:02d}:{bar_m:02d}"].append(c)

    bars = []
    for key in sorted(groups):
        grp = groups[key]
        bars.append({
            "time": key,
            "open": grp[0]["open"],
            "high": max(c["high"] for c in grp),
            "low": min(c["low"] for c in grp),
            "close": grp[-1]["close"],
            "volume": sum(c["volume"] for c in grp),
        })
    return bars


def compute_vwap(candles: list[dict]) -> list[float]:
    cum_pv, cum_vol = 0.0, 0.0
    out = []
    for c in candles:
        typical = (c["high"] + c["low"] + c["close"]) / 3
        cum_pv += typical * c["volume"]
        cum_vol += c["volume"]
        out.append(cum_pv / cum_vol if cum_vol > 0 else c["close"])
    return out


def compute_ema(values: list[float], period: int) -> list[float | None]:
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    result: list[float | None] = [None] * (period - 1)
    result.append(sum(values[:period]) / period)
    for i in range(period, len(values)):
        result.append(values[i] * k + result[-1] * (1 - k))
    return result


def compute_rsi(values: list[float], period: int = 14) -> list[float | None]:
    if len(values) < period + 1:
        return [None] * len(values)
    result: list[float | None] = [None] * period
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    ag = gains / period
    al = losses / period
    result.append(100 - 100 / (1 + ag / al) if al > 0 else 100)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        g = d if d > 0 else 0
        l = -d if d < 0 else 0
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
        result.append(100 - 100 / (1 + ag / al) if al > 0 else 100)
    return result


def next_expiry(ref_date: date, weekday: int) -> date:
    d = ref_date
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


# ── Data fetching ───────────────────────────────────────────────────

def fetch_spot_1min(udata: UpstoxData, index_name: str, ref_date: date) -> list[dict]:
    key = INDEXES[index_name]["key"]
    candles = []
    try:
        d = udata._get(
            f"/v3/historical-candle/{key}/minutes/1"
            f"/{ref_date.isoformat()}/{ref_date.isoformat()}")
        candles += d.get("data", {}).get("candles", [])
    except Exception:
        pass
    if ref_date == datetime.now(IST).date():
        try:
            d = udata._get(f"/v3/historical-candle/intraday/{key}/minutes/1")
            candles += d.get("data", {}).get("candles", [])
        except Exception:
            pass

    seen, rows = set(), []
    for c in candles:
        dt, tm = c[0][:10], c[0][11:16]
        if dt != ref_date.isoformat() or tm in seen:
            continue
        seen.add(tm)
        rows.append({"time": tm, "open": c[1], "high": c[2],
                      "low": c[3], "close": c[4], "volume": c[5]})
    rows.sort(key=lambda r: r["time"])
    return rows


def fetch_option_1min(udata: UpstoxData, inst_key: str, ref_date: date) -> list[dict]:
    candles = []
    try:
        d = udata._get(
            f"/v3/historical-candle/{inst_key}/minutes/1"
            f"/{ref_date.isoformat()}/{ref_date.isoformat()}")
        candles += d.get("data", {}).get("candles", [])
    except Exception:
        pass
    if ref_date == datetime.now(IST).date():
        try:
            d = udata._get(
                f"/v3/historical-candle/intraday/{inst_key}/minutes/1")
            candles += d.get("data", {}).get("candles", [])
        except Exception:
            pass

    seen, rows = set(), []
    for c in candles:
        dt, tm = c[0][:10], c[0][11:16]
        if dt != ref_date.isoformat() or tm in seen:
            continue
        seen.add(tm)
        rows.append({"time": tm, "open": c[1], "high": c[2],
                      "low": c[3], "close": c[4], "volume": c[5]})
    rows.sort(key=lambda r: r["time"])
    return rows


# ── Strategy 1: VWAP Bounce ────────────────────────────────────────

def detect_vwap_bounce(candles_5min: list[dict], index_name: str,
                       opt_candles: dict, master: dict, expiry: date,
                       ref_date: date) -> list[Signal]:
    p = STRATEGY_PARAMS["vwap_bounce"]
    step = INDEXES[index_name]["step"]
    signals: list[Signal] = []
    last_signal_time = None

    vwaps = compute_vwap(candles_5min)
    closes = [c["close"] for c in candles_5min]
    rsis = compute_rsi(closes, 14)
    vol_sum, vol_count = 0.0, 0

    for i, c in enumerate(candles_5min):
        vol_sum += c["volume"]
        vol_count += 1
        avg_vol = vol_sum / vol_count

        if c["time"] < p["active_from"] or c["time"] > p["active_to"]:
            continue
        if i < 3 or len(signals) >= p["max_signals"]:
            continue
        if last_signal_time and time_diff_mins(last_signal_time, c["time"]) < p["cooldown_mins"]:
            continue

        vwap = vwaps[i]
        prev = candles_5min[i - 1]
        prev_vwap = vwaps[i - 1]

        touch_band = vwap * p["vwap_touch_pct"] / 100
        prev_near = abs(prev["close"] - prev_vwap) <= touch_band

        if not prev_near:
            # also check if the bar's low/high pierced VWAP
            bar_touched = (c["low"] <= vwap + touch_band and
                           c["high"] >= vwap - touch_band)
            if not bar_touched:
                continue

        move = (c["close"] - vwap) / vwap * 100
        if abs(move) < p["bounce_min_pct"]:
            continue

        if c["volume"] < avg_vol * p["vol_mult"]:
            continue

        rsi = rsis[i] if i < len(rsis) else None

        direction = None
        if move > 0 and c["close"] > c["open"]:
            if rsi is None or (35 < rsi < 75):
                direction = "bullish"
        elif move < 0 and c["close"] < c["open"]:
            if rsi is None or (25 < rsi < 65):
                direction = "bearish"

        if not direction:
            continue

        strike = round_strike(c["close"], step)
        opt_type = "CE" if direction == "bullish" else "PE"
        opt_key = master.get((index_name, expiry, strike, opt_type))
        if not opt_key or opt_key not in opt_candles:
            continue

        oc = _opt_at_time(opt_candles[opt_key], c["time"])
        if not oc or oc["close"] <= 5:
            continue

        entry = oc["close"]
        signals.append(Signal(
            time=c["time"], strategy="vwap_bounce", action="BUY",
            index=index_name, strike=strike, option_type=opt_type,
            entry_premium=entry,
            sl_premium=entry * (1 - p["sl_pct"]),
            tgt_premium=entry * (1 + p["tgt_pct"]),
            spot_at_entry=c["close"], instrument_key=opt_key,
            ref_date=ref_date,
        ))
        last_signal_time = c["time"]

    return signals


# ── Strategy 2: ORB Breakout ───────────────────────────────────────

def detect_orb(candles_5min: list[dict], index_name: str,
               opt_candles: dict, master: dict, expiry: date,
               ref_date: date) -> list[Signal]:
    p = STRATEGY_PARAMS["orb_breakout"]
    step = INDEXES[index_name]["step"]

    range_bars = [c for c in candles_5min if c["time"] <= p["range_end"]]
    if not range_bars:
        return []

    rng_high = max(c["high"] for c in range_bars)
    rng_low = min(c["low"] for c in range_bars)
    rng_pct = (rng_high - rng_low) / rng_low * 100

    if rng_pct < p["min_range_pct"] or rng_pct > p["max_range_pct"]:
        return []

    signals: list[Signal] = []
    for c in candles_5min:
        if c["time"] < p["active_from"] or c["time"] > p["active_to"]:
            continue
        if signals:
            break

        direction = None
        if c["close"] > rng_high and c["close"] > c["open"]:
            direction = "bullish"
        elif c["close"] < rng_low and c["close"] < c["open"]:
            direction = "bearish"

        if not direction:
            continue

        strike = round_strike(c["close"], step)
        opt_type = "CE" if direction == "bullish" else "PE"
        opt_key = master.get((index_name, expiry, strike, opt_type))
        if not opt_key or opt_key not in opt_candles:
            continue

        oc = _opt_at_time(opt_candles[opt_key], c["time"])
        if not oc or oc["close"] <= 5:
            continue

        entry = oc["close"]
        signals.append(Signal(
            time=c["time"], strategy="orb_breakout", action="BUY",
            index=index_name, strike=strike, option_type=opt_type,
            entry_premium=entry,
            sl_premium=entry * (1 - p["sl_pct"]),
            tgt_premium=entry * (1 + p["tgt_pct"]),
            spot_at_entry=c["close"], instrument_key=opt_key,
            ref_date=ref_date,
        ))

    return signals


# ── Strategy 3: EMA Pullback ──────────────────────────────────────

def detect_ema_pullback(candles_5min: list[dict], index_name: str,
                        opt_candles: dict, master: dict, expiry: date,
                        ref_date: date) -> list[Signal]:
    p = STRATEGY_PARAMS["ema_pullback"]
    step = INDEXES[index_name]["step"]

    if len(candles_5min) < p["ema_period"] + p["trend_bars"] + 2:
        return []

    closes = [c["close"] for c in candles_5min]
    emas = compute_ema(closes, p["ema_period"])
    rsis = compute_rsi(closes, 14)

    signals: list[Signal] = []
    last_signal_time = None

    for i in range(p["ema_period"] + p["trend_bars"], len(candles_5min)):
        c = candles_5min[i]
        if c["time"] < p["active_from"] or c["time"] > p["active_to"]:
            continue
        if len(signals) >= p["max_signals"]:
            break
        if last_signal_time and time_diff_mins(last_signal_time, c["time"]) < p["cooldown_mins"]:
            continue

        ema = emas[i]
        if ema is None:
            continue

        above = sum(1 for j in range(i - p["trend_bars"], i)
                    if emas[j] is not None and candles_5min[j]["close"] > emas[j])
        below = sum(1 for j in range(i - p["trend_bars"], i)
                    if emas[j] is not None and candles_5min[j]["close"] < emas[j])

        touch_band = ema * p["touch_pct"] / 100
        rsi = rsis[i] if i < len(rsis) else None

        direction = None
        if above >= p["trend_bars"]:
            if c["low"] <= ema + touch_band and c["close"] > ema:
                if rsi is None or rsi > 45:
                    direction = "bullish"
        elif below >= p["trend_bars"]:
            if c["high"] >= ema - touch_band and c["close"] < ema:
                if rsi is None or rsi < 55:
                    direction = "bearish"

        if not direction:
            continue

        strike = round_strike(c["close"], step)
        opt_type = "CE" if direction == "bullish" else "PE"
        opt_key = master.get((index_name, expiry, strike, opt_type))
        if not opt_key or opt_key not in opt_candles:
            continue

        oc = _opt_at_time(opt_candles[opt_key], c["time"])
        if not oc or oc["close"] <= 5:
            continue

        entry = oc["close"]
        signals.append(Signal(
            time=c["time"], strategy="ema_pullback", action="BUY",
            index=index_name, strike=strike, option_type=opt_type,
            entry_premium=entry,
            sl_premium=entry * (1 - p["sl_pct"]),
            tgt_premium=entry * (1 + p["tgt_pct"]),
            spot_at_entry=c["close"], instrument_key=opt_key,
            ref_date=ref_date,
        ))
        last_signal_time = c["time"]

    return signals


# ── Strategy 4: Short Straddle ─────────────────────────────────────

def detect_short_straddle(candles_5min: list[dict], index_name: str,
                          opt_candles: dict, master: dict, expiry: date,
                          ref_date: date) -> list[Signal]:
    p = STRATEGY_PARAMS["short_straddle"]
    step = INDEXES[index_name]["step"]

    entry_bar = next((c for c in candles_5min if c["time"] == p["entry_time"]), None)
    if not entry_bar:
        return []

    spot = entry_bar["close"]
    strike = round_strike(spot, step)

    ce_key = master.get((index_name, expiry, strike, "CE"))
    pe_key = master.get((index_name, expiry, strike, "PE"))
    if not ce_key or not pe_key:
        return []
    if ce_key not in opt_candles or pe_key not in opt_candles:
        return []

    ce_oc = _opt_at_time(opt_candles[ce_key], p["entry_time"])
    pe_oc = _opt_at_time(opt_candles[pe_key], p["entry_time"])
    if not ce_oc or not pe_oc:
        return []

    ce_prem = ce_oc["close"]
    pe_prem = pe_oc["close"]
    combined = ce_prem + pe_prem
    if combined < 20:
        return []

    return [Signal(
        time=p["entry_time"], strategy="short_straddle", action="SELL",
        index=index_name, strike=strike, option_type="CE",
        entry_premium=ce_prem,
        sl_premium=combined * (1 + p["sl_pct"]),
        tgt_premium=combined * (1 - p["tgt_pct"]),
        spot_at_entry=spot, instrument_key=ce_key,
        paired_strike=strike, paired_type="PE",
        paired_premium=pe_prem, paired_instrument_key=pe_key,
        ref_date=ref_date,
    )]


# ── Strategy 5: Expiry Theta ──────────────────────────────────────

def detect_expiry_theta(candles_5min: list[dict], index_name: str,
                        opt_candles: dict, master: dict, expiry: date,
                        ref_date: date) -> list[Signal]:
    if ref_date != expiry:
        return []

    p = STRATEGY_PARAMS["expiry_theta"]
    step = INDEXES[index_name]["step"]

    entry_bar = next((c for c in candles_5min if c["time"] == p["entry_time"]), None)
    if not entry_bar:
        return []

    spot = entry_bar["close"]
    atm = round_strike(spot, step)
    ce_strike = atm + p["otm_steps"] * step
    pe_strike = atm - p["otm_steps"] * step

    ce_key = master.get((index_name, expiry, ce_strike, "CE"))
    pe_key = master.get((index_name, expiry, pe_strike, "PE"))
    if not ce_key or not pe_key:
        return []
    if ce_key not in opt_candles or pe_key not in opt_candles:
        return []

    ce_oc = _opt_at_time(opt_candles[ce_key], p["entry_time"])
    pe_oc = _opt_at_time(opt_candles[pe_key], p["entry_time"])
    if not ce_oc or not pe_oc:
        return []

    ce_prem = ce_oc["close"]
    pe_prem = pe_oc["close"]
    combined = ce_prem + pe_prem
    if combined < 5:
        return []

    return [Signal(
        time=p["entry_time"], strategy="expiry_theta", action="SELL",
        index=index_name, strike=ce_strike, option_type="CE",
        entry_premium=ce_prem,
        sl_premium=combined * (1 + p["sl_pct"]),
        tgt_premium=combined * (1 - p["tgt_pct"]),
        spot_at_entry=spot, instrument_key=ce_key,
        paired_strike=pe_strike, paired_type="PE",
        paired_premium=pe_prem, paired_instrument_key=pe_key,
        ref_date=ref_date,
    )]


# ── Option candle lookup helper ────────────────────────────────────

def _opt_at_time(opt_data: list[dict], time_str: str) -> dict | None:
    exact = next((c for c in opt_data if c["time"] == time_str), None)
    if exact:
        return exact
    # find closest candle within 5 min window
    for c in opt_data:
        if c["time"] >= time_str:
            if time_diff_mins(time_str, c["time"]) <= 5:
                return c
            break
    return None


# ── Trade trackers ──────────────────────────────────────────────────

def track_buy_trade(sig: Signal, opt_candles: dict) -> TradeResult | None:
    data = opt_candles.get(sig.instrument_key, [])
    p = STRATEGY_PARAMS[sig.strategy]
    max_hold = p.get("max_hold_mins", 60)
    entry = sig.entry_premium

    start_idx = None
    for i, c in enumerate(data):
        if c["time"] >= sig.time:
            start_idx = i
            break
    if start_idx is None:
        return None

    for i in range(start_idx + 1, len(data)):
        c = data[i]
        hold = time_diff_mins(sig.time, c["time"])

        if c["low"] <= sig.sl_premium:
            return TradeResult(
                signal=sig, exit_time=c["time"], exit_premium=sig.sl_premium,
                exit_reason="sl",
                pnl_per_lot=sig.sl_premium - entry - 2 * IMPACT_COST,
                hold_mins=hold, won=False)

        if c["high"] >= sig.tgt_premium:
            return TradeResult(
                signal=sig, exit_time=c["time"], exit_premium=sig.tgt_premium,
                exit_reason="tgt",
                pnl_per_lot=sig.tgt_premium - entry - 2 * IMPACT_COST,
                hold_mins=hold, won=True)

        if hold >= max_hold or c["time"] >= "15:15":
            return TradeResult(
                signal=sig, exit_time=c["time"], exit_premium=c["close"],
                exit_reason="time_exit",
                pnl_per_lot=c["close"] - entry - 2 * IMPACT_COST,
                hold_mins=hold, won=c["close"] > entry)

    if data:
        last = data[-1]
        return TradeResult(
            signal=sig, exit_time=last["time"], exit_premium=last["close"],
            exit_reason="day_end",
            pnl_per_lot=last["close"] - entry - 2 * IMPACT_COST,
            hold_mins=time_diff_mins(sig.time, last["time"]),
            won=last["close"] > entry)
    return None


def track_sell_trade(sig: Signal, opt_candles: dict) -> TradeResult | None:
    ce_data = opt_candles.get(sig.instrument_key, [])
    pe_data = opt_candles.get(sig.paired_instrument_key, [])
    if not ce_data or not pe_data:
        return None

    ce_by_t = {c["time"]: c for c in ce_data}
    pe_by_t = {c["time"]: c for c in pe_data}
    combined_entry = sig.entry_premium + sig.paired_premium
    time_exit = STRATEGY_PARAMS[sig.strategy]["time_exit"]
    sl_pct = STRATEGY_PARAMS[sig.strategy]["sl_pct"]

    all_times = sorted(set(list(ce_by_t) + list(pe_by_t)))
    started = False

    for t in all_times:
        if t <= sig.time:
            if t == sig.time:
                started = True
            continue
        if not started:
            continue

        ce_c = ce_by_t.get(t)
        pe_c = pe_by_t.get(t)
        if not ce_c or not pe_c:
            continue

        cur = ce_c["close"] + pe_c["close"]
        hold = time_diff_mins(sig.time, t)

        # Per-leg SL for theta (either leg doubles)
        if sig.strategy == "expiry_theta":
            ce_doubled = ce_c["high"] >= sig.entry_premium * 2
            pe_doubled = pe_c["high"] >= sig.paired_premium * 2
            if ce_doubled or pe_doubled:
                pnl = combined_entry - cur - 4 * IMPACT_COST
                return TradeResult(
                    signal=sig, exit_time=t, exit_premium=ce_c["close"],
                    paired_exit_premium=pe_c["close"], exit_reason="sl",
                    pnl_per_lot=pnl, hold_mins=hold, won=False)

        if cur >= sig.sl_premium:
            pnl = combined_entry - cur - 4 * IMPACT_COST
            return TradeResult(
                signal=sig, exit_time=t, exit_premium=ce_c["close"],
                paired_exit_premium=pe_c["close"], exit_reason="sl",
                pnl_per_lot=pnl, hold_mins=hold, won=False)

        if cur <= sig.tgt_premium:
            pnl = combined_entry - cur - 4 * IMPACT_COST
            return TradeResult(
                signal=sig, exit_time=t, exit_premium=ce_c["close"],
                paired_exit_premium=pe_c["close"], exit_reason="tgt",
                pnl_per_lot=pnl, hold_mins=hold, won=True)

        if t >= time_exit:
            pnl = combined_entry - cur - 4 * IMPACT_COST
            return TradeResult(
                signal=sig, exit_time=t, exit_premium=ce_c["close"],
                paired_exit_premium=pe_c["close"], exit_reason="time_exit",
                pnl_per_lot=pnl, hold_mins=hold, won=pnl > 0)

    # Day end
    last_ce = ce_data[-1] if ce_data else None
    last_pe = pe_data[-1] if pe_data else None
    if last_ce and last_pe:
        cur = last_ce["close"] + last_pe["close"]
        pnl = combined_entry - cur - 4 * IMPACT_COST
        return TradeResult(
            signal=sig, exit_time=last_ce["time"],
            exit_premium=last_ce["close"],
            paired_exit_premium=last_pe["close"],
            exit_reason="day_end", pnl_per_lot=pnl,
            hold_mins=time_diff_mins(sig.time, last_ce["time"]),
            won=pnl > 0)
    return None


# ── Day runner ──────────────────────────────────────────────────────

def run_day(udata: UpstoxData, index_name: str, ref_date: date,
            master: dict, strategies: set[str]) -> tuple[list[TradeResult], str]:
    idx = INDEXES[index_name]
    step = idx["step"]

    spot = fetch_spot_1min(udata, index_name, ref_date)
    if not spot or len(spot) < 30:
        return [], f"spot candles={len(spot) if spot else 0}"

    expiry = next_expiry(ref_date, idx["expiry_weekday"])
    opening = spot[0]["close"]
    atm = round_strike(opening, step)

    # Determine all strikes we need
    needed: set[tuple[float, str]] = set()
    for offset in range(-4, 5):
        s = atm + offset * step
        if s > 0:
            needed.add((s, "CE"))
            needed.add((s, "PE"))
    # OTM for theta
    otm_steps = STRATEGY_PARAMS["expiry_theta"]["otm_steps"]
    needed.add((atm + otm_steps * step, "CE"))
    needed.add((atm - otm_steps * step, "PE"))

    # Fetch option 1-min candles
    opt_candles: dict[str, list[dict]] = {}
    for strike, otype in needed:
        key = master.get((index_name, expiry, strike, otype))
        if not key:
            continue
        data = fetch_option_1min(udata, key, ref_date)
        if data:
            opt_candles[key] = data
        _time.sleep(0.15)  # rate limit

    if not opt_candles:
        return [], f"no option data (expiry={expiry})"

    candles_5min = resample_5min(spot)

    all_signals: list[Signal] = []
    if "vwap_bounce" in strategies:
        all_signals += detect_vwap_bounce(candles_5min, index_name, opt_candles, master, expiry, ref_date)
    if "orb_breakout" in strategies:
        all_signals += detect_orb(candles_5min, index_name, opt_candles, master, expiry, ref_date)
    if "ema_pullback" in strategies:
        all_signals += detect_ema_pullback(candles_5min, index_name, opt_candles, master, expiry, ref_date)
    if "short_straddle" in strategies:
        all_signals += detect_short_straddle(candles_5min, index_name, opt_candles, master, expiry, ref_date)
    if "expiry_theta" in strategies:
        all_signals += detect_expiry_theta(candles_5min, index_name, opt_candles, master, expiry, ref_date)

    results = []
    for sig in all_signals:
        if sig.action == "BUY":
            r = track_buy_trade(sig, opt_candles)
        else:
            r = track_sell_trade(sig, opt_candles)
        if r:
            results.append(r)

    return results, "ok"


# ── Main ────────────────────────────────────────────────────────────

def get_trading_days(from_date: date, to_date: date) -> list[date]:
    days = []
    d = from_date
    while d <= to_date:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def main():
    parser = argparse.ArgumentParser(description="Multi-strategy signal backtester")
    parser.add_argument("--from", dest="from_date", required=True)
    parser.add_argument("--to", dest="to_date", required=True)
    parser.add_argument("--index", default="NIFTY")
    parser.add_argument("--strategies", default="all",
                        help="Comma-separated: vwap_bounce,orb_breakout,ema_pullback,short_straddle,expiry_theta")
    args = parser.parse_args()

    from_date = date.fromisoformat(args.from_date)
    to_date = date.fromisoformat(args.to_date)
    index_name = args.index.upper()

    if args.strategies == "all":
        strategies = set(STRATEGY_PARAMS.keys())
    else:
        strategies = set(s.strip() for s in args.strategies.split(","))

    idx = INDEXES[index_name]
    lot = idx["lot_size"]

    print(f"\n{'='*75}")
    print(f"  MULTI-STRATEGY SIGNAL BACKTEST")
    print(f"  {index_name} (lot={lot}) | {from_date} → {to_date}")
    print(f"  Strategies: {', '.join(sorted(strategies))}")
    print(f"{'='*75}")

    print("\nLoading option master...")
    from src.strategy.live_runner import _build_option_master
    master = _build_option_master(None)
    print(f"  Master: {len(master)} entries")

    udata = UpstoxData()
    trading_days = get_trading_days(from_date, to_date)
    print(f"  Trading days: {len(trading_days)}\n")

    all_results: list[TradeResult] = []
    by_strategy: dict[str, list[TradeResult]] = defaultdict(list)
    by_date: dict[date, list[TradeResult]] = defaultdict(list)
    skipped: list[tuple[date, str]] = []

    for day in trading_days:
        print(f"  {day} ...", end=" ", flush=True)
        results, status = run_day(udata, index_name, day, master, strategies)

        if status != "ok":
            print(f"SKIP ({status})")
            skipped.append((day, status))
            continue

        print(f"{len(results)} trades")
        for r in results:
            all_results.append(r)
            by_strategy[r.signal.strategy].append(r)
            by_date[day].append(r)

            s = r.signal
            pnl = r.pnl_per_lot * lot
            w = "✓" if r.won else "✗"
            if s.action == "BUY":
                print(f"    {w} {s.strategy:<16s} BUY {s.strike:>7.0f} {s.option_type} "
                      f"@{s.entry_premium:>7.1f} → {r.exit_premium:>7.1f} "
                      f"({r.exit_reason:<10s}) ₹{pnl:>+8,.0f}  "
                      f"[{s.time}→{r.exit_time} {r.hold_mins}m]")
            else:
                comb_e = s.entry_premium + s.paired_premium
                comb_x = r.exit_premium + r.paired_exit_premium
                print(f"    {w} {s.strategy:<16s} SELL straddle "
                      f"@{comb_e:>7.1f} → {comb_x:>7.1f} "
                      f"({r.exit_reason:<10s}) ₹{pnl:>+8,.0f}  "
                      f"[{s.time}→{r.exit_time} {r.hold_mins}m]")

    # ── Per-strategy summary ────────────────────────────────────────
    print(f"\n{'='*75}")
    print(f"  STRATEGY BREAKDOWN")
    print(f"{'='*75}")

    strat_order = ["vwap_bounce", "orb_breakout", "ema_pullback",
                   "short_straddle", "expiry_theta"]
    for sn in strat_order:
        trades = by_strategy.get(sn, [])
        if not trades and sn not in strategies:
            continue

        if not trades:
            print(f"\n  {sn}: 0 trades")
            continue

        wins = sum(1 for t in trades if t.won)
        total = len(trades)
        wr = wins / total * 100
        total_pnl = sum(t.pnl_per_lot * lot for t in trades)
        avg_pnl = total_pnl / total
        avg_hold = sum(t.hold_mins for t in trades) / total

        exits = defaultdict(int)
        for t in trades:
            exits[t.exit_reason] += 1

        winning_pnl = [t.pnl_per_lot * lot for t in trades if t.won]
        losing_pnl = [t.pnl_per_lot * lot for t in trades if not t.won]
        avg_win = sum(winning_pnl) / len(winning_pnl) if winning_pnl else 0
        avg_loss = sum(losing_pnl) / len(losing_pnl) if losing_pnl else 0

        print(f"\n  ┌─ {sn.upper()} {'─' * (55 - len(sn))}")
        print(f"  │ Trades: {total}  |  Wins: {wins}  |  Win Rate: {wr:.1f}%")
        print(f"  │ Total P&L: ₹{total_pnl:>+10,.0f}  |  Avg: ₹{avg_pnl:>+8,.0f}/trade")
        print(f"  │ Avg Win: ₹{avg_win:>+8,.0f}  |  Avg Loss: ₹{avg_loss:>+8,.0f}")
        print(f"  │ Avg hold: {avg_hold:.0f} min  |  Exits: {dict(exits)}")
        print(f"  └{'─' * 60}")

    # ── Combined ────────────────────────────────────────────────────
    if not all_results:
        print("\n  No trades generated across all days.")
        return

    wins = sum(1 for t in all_results if t.won)
    total = len(all_results)
    wr = wins / total * 100
    total_pnl = sum(t.pnl_per_lot * lot for t in all_results)
    avg_pnl = total_pnl / total
    traded_days = len(trading_days) - len(skipped)

    print(f"\n  {'═' * 60}")
    print(f"  COMBINED: {total} trades | {wins} wins | {wr:.1f}% WIN RATE")
    print(f"  Total P&L: ₹{total_pnl:>+10,.0f}  |  Avg: ₹{avg_pnl:>+8,.0f}/trade")
    print(f"  Avg signals/day: {total / traded_days:.1f}  |  Days: {traded_days}/{len(trading_days)}")
    print(f"  {'═' * 60}")

    # ── Daily P&L ───────────────────────────────────────────────────
    print(f"\n  DAILY P&L:")
    print(f"  {'Date':<12s} {'Trades':>6s} {'Wins':>5s} {'WR':>6s} {'P&L':>10s}")
    print(f"  {'─'*12} {'─'*6} {'─'*5} {'─'*6} {'─'*10}")
    cum = 0
    for day in sorted(by_date):
        trades = by_date[day]
        d_wins = sum(1 for t in trades if t.won)
        d_pnl = sum(t.pnl_per_lot * lot for t in trades)
        d_wr = d_wins / len(trades) * 100 if trades else 0
        cum += d_pnl
        bar = "█" * min(int(abs(d_pnl) / 500), 20)
        sign = "+" if d_pnl >= 0 else "-"
        print(f"  {day!s:<12s} {len(trades):>6d} {d_wins:>5d} {d_wr:>5.0f}% "
              f"₹{d_pnl:>+9,.0f}  {bar}")
    print(f"  {'─'*12} {'─'*6} {'─'*5} {'─'*6} {'─'*10}")
    print(f"  {'TOTAL':<12s} {total:>6d} {wins:>5d} {wr:>5.1f}% ₹{cum:>+9,.0f}")

    if skipped:
        print(f"\n  Skipped days: {', '.join(str(d) for d, _ in skipped)}")

    # ── Channel signal format preview ───────────────────────────────
    print(f"\n\n  SAMPLE SIGNAL FORMAT (for Telegram channel):")
    print(f"  {'─'*50}")
    for r in all_results[:3]:
        s = r.signal
        if s.action == "BUY":
            print(f"  🟢 {s.strategy.upper().replace('_',' ')}")
            print(f"  BUY {s.index} {s.strike:.0f} {s.option_type} @ ₹{s.entry_premium:.1f}")
            print(f"  SL: ₹{s.sl_premium:.1f} | TGT: ₹{s.tgt_premium:.1f}")
        else:
            print(f"  🔴 {s.strategy.upper().replace('_',' ')}")
            print(f"  SELL {s.index} {s.strike:.0f} {s.option_type} @ ₹{s.entry_premium:.1f}")
            print(f"  SELL {s.index} {s.paired_strike:.0f} {s.paired_type} @ ₹{s.paired_premium:.1f}")
            comb = s.entry_premium + s.paired_premium
            print(f"  Combined: ₹{comb:.1f} | SL: ₹{s.sl_premium:.1f} | TGT: ₹{s.tgt_premium:.1f}")
        print(f"  Result: {'✅ WIN' if r.won else '❌ LOSS'} ₹{r.pnl_per_lot * lot:+,.0f}")
        print()

    print()


if __name__ == "__main__":
    main()

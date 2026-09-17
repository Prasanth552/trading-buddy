"""Strategy Scanner — find parameter combos that achieve 80%+ win rate.

Sweeps multiple parameter variations for each strategy over N trading days
using REAL option 1-min candles (same infrastructure as backtest_multi_strategy.py).

Also tests new strategy ideas:
  - Trend-confirmed momentum (EMA20 + VWAP alignment required)
  - Conservative ORB (wider SL, stricter retest, 2:1 RR)
  - VWAP bounce (price touches VWAP, bounces with trend)

Results are persisted to data/scanner_results.json and accumulate across runs.
Run daily after market close (15:30+) to build up multi-day statistics.

Usage (run on VM where Upstox data is available):
    # Scan today (run after market close):
    PYTHONPATH=. .venv/bin/python3 scripts/strategy_scanner.py --index NIFTY

    # Scan specific date range:
    PYTHONPATH=. .venv/bin/python3 scripts/strategy_scanner.py \
        --from 2026-09-17 --to 2026-09-19 --index NIFTY

    # Show accumulated results only (no new scan):
    PYTHONPATH=. .venv/bin/python3 scripts/strategy_scanner.py --report
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time as _time
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.broker.upstox_data import UpstoxData
from src.utils.logging import get_logger

log = get_logger("scanner")
IST = ZoneInfo("Asia/Kolkata")

# Reuse core infra from backtest
from scripts.backtest_multi_strategy import (
    INDEXES, IMPACT_COST, Signal, TradeResult,
    round_strike, time_diff_mins, resample_5min, compute_vwap,
    next_expiry, fetch_spot_1min, fetch_option_1min,
    track_buy_trade, track_single_sell_trade, track_paired_sell_trade,
    _opt_at_time,
)

# ── Parameter grid ─────────────────────────────────────────────────

PARAM_GRID = {
    "momentum_scalp": [
        # Original
        {"body_pct": 0.10, "vol_mult": 1.3, "sl_pct": 0.12, "tgt_pct": 0.18,
         "max_hold_mins": 15, "active_from": "09:25", "active_to": "14:00",
         "max_signals": 3, "cooldown_mins": 10, "close_position_min": 0.55,
         "label": "orig"},
        # Tighter body + higher RR
        {"body_pct": 0.15, "vol_mult": 1.5, "sl_pct": 0.10, "tgt_pct": 0.15,
         "max_hold_mins": 12, "active_from": "09:25", "active_to": "13:00",
         "max_signals": 2, "cooldown_mins": 15, "close_position_min": 0.60,
         "label": "tight_body"},
        # Big body only (very selective)
        {"body_pct": 0.20, "vol_mult": 1.5, "sl_pct": 0.08, "tgt_pct": 0.12,
         "max_hold_mins": 10, "active_from": "09:30", "active_to": "12:00",
         "max_signals": 2, "cooldown_mins": 20, "close_position_min": 0.65,
         "label": "big_body"},
        # Quick scalp low SL
        {"body_pct": 0.12, "vol_mult": 1.3, "sl_pct": 0.08, "tgt_pct": 0.10,
         "max_hold_mins": 8, "active_from": "09:25", "active_to": "11:30",
         "max_signals": 3, "cooldown_mins": 10, "close_position_min": 0.55,
         "label": "quick_scalp"},
        # Wide target
        {"body_pct": 0.12, "vol_mult": 1.3, "sl_pct": 0.15, "tgt_pct": 0.30,
         "max_hold_mins": 30, "active_from": "09:25", "active_to": "14:00",
         "max_signals": 2, "cooldown_mins": 15, "close_position_min": 0.55,
         "label": "wide_tgt"},
    ],
    # orb_retest removed — 0% WR across all variants on Sep 17
    "day_end_sell": [
        # Original
        {"entry_time": "14:00", "otm_steps": 2, "sl_mult": 2.0,
         "time_exit": "15:10", "min_trend_pct": 0.20, "max_signals": 1,
         "label": "orig"},
        # Later entry (more theta crush)
        {"entry_time": "14:15", "otm_steps": 2, "sl_mult": 1.8,
         "time_exit": "15:10", "min_trend_pct": 0.25, "max_signals": 1,
         "label": "late_entry"},
        # Wider OTM (safer)
        {"entry_time": "14:00", "otm_steps": 3, "sl_mult": 2.5,
         "time_exit": "15:10", "min_trend_pct": 0.30, "max_signals": 1,
         "label": "wide_otm"},
        # Very late + strong trend only
        {"entry_time": "14:30", "otm_steps": 2, "sl_mult": 1.5,
         "time_exit": "15:10", "min_trend_pct": 0.35, "max_signals": 1,
         "label": "very_late"},
    ],
}

# ── New strategies ─────────────────────────────────────────────────

def _compute_ema(candles: list[dict], period: int) -> list[float]:
    k = 2 / (period + 1)
    ema = [candles[0]["close"]]
    for i in range(1, len(candles)):
        ema.append(candles[i]["close"] * k + ema[-1] * (1 - k))
    return ema


def get_trend(candles_5min: list[dict], bar_idx: int) -> str | None:
    if bar_idx < 20:
        return None
    ema20 = _compute_ema(candles_5min[:bar_idx + 1], 20)
    vwap = compute_vwap(candles_5min[:bar_idx + 1])
    price = candles_5min[bar_idx]["close"]

    above_ema = price > ema20[-1]
    above_vwap = price > vwap[-1]

    # Check momentum: 3 of last 5 bars confirm direction
    lookback = candles_5min[max(0, bar_idx - 4):bar_idx + 1]
    bullish_bars = sum(1 for c in lookback if c["close"] > c["open"])
    bearish_bars = len(lookback) - bullish_bars

    if above_ema and above_vwap and bullish_bars >= 3:
        return "bullish"
    if not above_ema and not above_vwap and bearish_bars >= 3:
        return "bearish"
    return None


# Strategy: Trend-confirmed momentum (new)
def detect_trend_momentum(candles_5min: list[dict], index_name: str,
                          opt_candles: dict, master: dict, expiry: date,
                          ref_date: date, params: dict) -> list[Signal]:
    step = INDEXES[index_name]["step"]
    signals = []
    last_sig_time = None

    vol_sum, vol_count = 0.0, 0
    has_volume = any(c["volume"] > 0 for c in candles_5min[:20])

    for i, c in enumerate(candles_5min):
        vol_sum += c["volume"]
        vol_count += 1
        avg_vol = vol_sum / vol_count if vol_count > 0 else 0

        if c["time"] < params["active_from"] or c["time"] > params["active_to"]:
            continue
        if len(signals) >= params["max_signals"]:
            break
        if last_sig_time and time_diff_mins(last_sig_time, c["time"]) < params["cooldown_mins"]:
            continue
        if i < 20:
            continue

        body = abs(c["close"] - c["open"])
        if c["open"] == 0:
            continue
        body_pct = body / c["open"] * 100
        if body_pct < params["body_pct"]:
            continue

        if has_volume and avg_vol > 0:
            if c["volume"] < avg_vol * params["vol_mult"]:
                continue

        rng = c["high"] - c["low"]
        if rng == 0:
            continue
        if c["close"] > c["open"]:
            close_pos = (c["close"] - c["low"]) / rng
            candle_dir = "bullish"
        else:
            close_pos = (c["high"] - c["close"]) / rng
            candle_dir = "bearish"
        if close_pos < params["close_position_min"]:
            continue

        # MUST have trend confirmation
        trend = get_trend(candles_5min, i)
        if trend != candle_dir:
            continue

        # Check consecutive bars in same direction
        min_consec = params.get("min_consecutive", 2)
        if i >= min_consec:
            consec_ok = True
            for j in range(1, min_consec + 1):
                prev = candles_5min[i - j]
                if candle_dir == "bullish" and prev["close"] <= prev["open"]:
                    consec_ok = False
                    break
                if candle_dir == "bearish" and prev["close"] >= prev["open"]:
                    consec_ok = False
                    break
            if not consec_ok:
                continue

        strike = round_strike(c["close"], step)
        opt_type = "CE" if candle_dir == "bullish" else "PE"
        opt_key = master.get((index_name, expiry, strike, opt_type))
        if not opt_key or opt_key not in opt_candles:
            continue

        oc = _opt_at_time(opt_candles[opt_key], c["time"])
        if not oc or oc["close"] <= 5:
            continue

        entry = oc["close"]
        signals.append(Signal(
            time=c["time"], strategy="trend_momentum", action="BUY",
            index=index_name, strike=strike, option_type=opt_type,
            entry_premium=entry,
            sl_premium=entry * (1 - params["sl_pct"]),
            tgt_premium=entry * (1 + params["tgt_pct"]),
            spot_at_entry=c["close"], instrument_key=opt_key,
            ref_date=ref_date,
        ))
        last_sig_time = c["time"]

    return signals


# Strategy: VWAP bounce (new)
def detect_vwap_bounce(candles_5min: list[dict], index_name: str,
                       opt_candles: dict, master: dict, expiry: date,
                       ref_date: date, params: dict) -> list[Signal]:
    step = INDEXES[index_name]["step"]
    signals = []
    last_sig_time = None
    vwap = compute_vwap(candles_5min)

    for i, c in enumerate(candles_5min):
        if c["time"] < params["active_from"] or c["time"] > params["active_to"]:
            continue
        if len(signals) >= params["max_signals"]:
            break
        if last_sig_time and time_diff_mins(last_sig_time, c["time"]) < params["cooldown_mins"]:
            continue
        if i < 20:
            continue

        v = vwap[i]
        touch_zone = v * params["touch_pct"] / 100

        trend = get_trend(candles_5min, i)
        if not trend:
            continue

        # Bullish bounce: low touches VWAP from above, closes above VWAP
        if trend == "bullish":
            touched = c["low"] <= v + touch_zone and c["low"] >= v - touch_zone
            bounced = c["close"] > v and c["close"] > c["open"]
            if not (touched and bounced):
                continue
            # Confirm bars
            confirm_ok = True
            for j in range(1, params["confirm_bars"] + 1):
                if i - j < 0:
                    confirm_ok = False
                    break
                prev = candles_5min[i - j]
                if prev["close"] <= prev["open"]:
                    confirm_ok = False
                    break
            if not confirm_ok:
                continue

            bounce_pct = (c["close"] - c["low"]) / c["low"] * 100
            if bounce_pct < params["min_bounce_pct"]:
                continue

            opt_type = "CE"

        elif trend == "bearish":
            touched = c["high"] >= v - touch_zone and c["high"] <= v + touch_zone
            bounced = c["close"] < v and c["close"] < c["open"]
            if not (touched and bounced):
                continue
            confirm_ok = True
            for j in range(1, params["confirm_bars"] + 1):
                if i - j < 0:
                    confirm_ok = False
                    break
                prev = candles_5min[i - j]
                if prev["close"] >= prev["open"]:
                    confirm_ok = False
                    break
            if not confirm_ok:
                continue

            bounce_pct = (c["high"] - c["close"]) / c["close"] * 100
            if bounce_pct < params["min_bounce_pct"]:
                continue

            opt_type = "PE"
        else:
            continue

        strike = round_strike(c["close"], step)
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
            sl_premium=entry * (1 - params["sl_pct"]),
            tgt_premium=entry * (1 + params["tgt_pct"]),
            spot_at_entry=c["close"], instrument_key=opt_key,
            ref_date=ref_date,
        ))
        last_sig_time = c["time"]

    return signals


# Strategy: EMA crossover with strict trend (new)
def detect_ema_cross(candles_5min: list[dict], index_name: str,
                     opt_candles: dict, master: dict, expiry: date,
                     ref_date: date, params: dict) -> list[Signal]:
    step = INDEXES[index_name]["step"]
    signals = []

    fast = params["fast_period"]
    slow = params["slow_period"]
    if len(candles_5min) < slow + 2:
        return []

    ema_fast = _compute_ema(candles_5min, fast)
    ema_slow = _compute_ema(candles_5min, slow)

    for i in range(slow + 1, len(candles_5min)):
        c = candles_5min[i]
        if c["time"] < params["active_from"] or c["time"] > params["active_to"]:
            continue
        if len(signals) >= params["max_signals"]:
            break

        prev_diff = ema_fast[i - 1] - ema_slow[i - 1]
        curr_diff = ema_fast[i] - ema_slow[i]
        spread = abs(curr_diff) / c["close"] * 100

        if spread < params["min_spread"]:
            continue

        trend = get_trend(candles_5min, i)

        # Bullish cross
        if prev_diff <= 0 and curr_diff > 0 and trend == "bullish":
            opt_type = "CE"
        elif prev_diff >= 0 and curr_diff < 0 and trend == "bearish":
            opt_type = "PE"
        else:
            continue

        strike = round_strike(c["close"], step)
        opt_key = master.get((index_name, expiry, strike, opt_type))
        if not opt_key or opt_key not in opt_candles:
            continue

        oc = _opt_at_time(opt_candles[opt_key], c["time"])
        if not oc or oc["close"] <= 5:
            continue

        entry = oc["close"]
        signals.append(Signal(
            time=c["time"], strategy="ema_cross", action="BUY",
            index=index_name, strike=strike, option_type=opt_type,
            entry_premium=entry,
            sl_premium=entry * (1 - params["sl_pct"]),
            tgt_premium=entry * (1 + params["tgt_pct"]),
            spot_at_entry=c["close"], instrument_key=opt_key,
            ref_date=ref_date,
        ))

    return signals


# Parameter grids for new strategies
NEW_STRATEGY_GRID = {
    "trend_momentum": [
        {"body_pct": 0.12, "vol_mult": 1.3, "sl_pct": 0.10, "tgt_pct": 0.15,
         "max_hold_mins": 12, "active_from": "09:30", "active_to": "13:00",
         "max_signals": 3, "cooldown_mins": 15, "close_position_min": 0.55,
         "min_consecutive": 2, "label": "base"},
        {"body_pct": 0.15, "vol_mult": 1.5, "sl_pct": 0.08, "tgt_pct": 0.12,
         "max_hold_mins": 10, "active_from": "09:30", "active_to": "12:00",
         "max_signals": 2, "cooldown_mins": 20, "close_position_min": 0.60,
         "min_consecutive": 2, "label": "selective"},
        {"body_pct": 0.20, "vol_mult": 1.3, "sl_pct": 0.06, "tgt_pct": 0.10,
         "max_hold_mins": 8, "active_from": "09:30", "active_to": "12:00",
         "max_signals": 2, "cooldown_mins": 20, "close_position_min": 0.65,
         "min_consecutive": 3, "label": "very_selective"},
        {"body_pct": 0.10, "vol_mult": 1.3, "sl_pct": 0.12, "tgt_pct": 0.20,
         "max_hold_mins": 20, "active_from": "09:25", "active_to": "14:00",
         "max_signals": 3, "cooldown_mins": 10, "close_position_min": 0.55,
         "min_consecutive": 1, "label": "loose"},
    ],
    "vwap_bounce": [
        {"touch_pct": 0.05, "confirm_bars": 1, "min_bounce_pct": 0.05,
         "sl_pct": 0.10, "tgt_pct": 0.15, "max_hold_mins": 15,
         "active_from": "09:45", "active_to": "14:00",
         "max_signals": 2, "cooldown_mins": 20, "label": "base"},
        {"touch_pct": 0.03, "confirm_bars": 2, "min_bounce_pct": 0.08,
         "sl_pct": 0.08, "tgt_pct": 0.12, "max_hold_mins": 12,
         "active_from": "09:45", "active_to": "13:00",
         "max_signals": 2, "cooldown_mins": 25, "label": "tight"},
        {"touch_pct": 0.04, "confirm_bars": 1, "min_bounce_pct": 0.06,
         "sl_pct": 0.12, "tgt_pct": 0.20, "max_hold_mins": 20,
         "active_from": "10:00", "active_to": "14:00",
         "max_signals": 2, "cooldown_mins": 15, "label": "wide_rr"},
    ],
    "ema_cross": [
        {"fast_period": 8, "slow_period": 21, "min_spread": 0.03,
         "sl_pct": 0.10, "tgt_pct": 0.15, "max_hold_mins": 20,
         "active_from": "09:45", "active_to": "13:30",
         "max_signals": 2, "label": "8_21"},
        {"fast_period": 5, "slow_period": 13, "min_spread": 0.04,
         "sl_pct": 0.08, "tgt_pct": 0.12, "max_hold_mins": 15,
         "active_from": "09:45", "active_to": "13:00",
         "max_signals": 2, "label": "5_13"},
        {"fast_period": 8, "slow_period": 21, "min_spread": 0.05,
         "sl_pct": 0.12, "tgt_pct": 0.25, "max_hold_mins": 30,
         "active_from": "09:45", "active_to": "14:00",
         "max_signals": 2, "label": "8_21_wide"},
    ],
}

# ── Existing strategy detectors (from backtest) ──────────────────

from scripts.backtest_multi_strategy import (
    detect_momentum_scalp, detect_orb_retest,
    detect_day_end_sell, detect_short_strangle,
    STRATEGY_PARAMS,
)


# ── Day runner (modified to accept custom params) ─────────────────

def run_day_custom(udata: UpstoxData, index_name: str, ref_date: date,
                   master: dict, strategy_name: str, params: dict,
                   prev_close: float | None = None) -> list[TradeResult]:
    idx = INDEXES[index_name]
    step = idx["step"]

    spot = fetch_spot_1min(udata, index_name, ref_date)
    if not spot or len(spot) < 30:
        return []

    expiry = next_expiry(ref_date, idx["expiry_weekday"])
    if ref_date.weekday() == idx["expiry_weekday"]:
        test_key = (index_name, ref_date, round_strike(spot[0]["close"], step), "CE")
        if test_key in master:
            expiry = ref_date

    atm = round_strike(spot[0]["close"], step)

    # Fetch option candles
    needed: set[tuple[float, str]] = set()
    for offset in range(-5, 6):
        s = atm + offset * step
        if s > 0:
            needed.add((s, "CE"))
            needed.add((s, "PE"))
    otm_steps = params.get("otm_steps", 2)
    for extra in range(otm_steps, otm_steps + 2):
        needed.add((atm + extra * step, "CE"))
        needed.add((atm - extra * step, "PE"))

    opt_candles: dict[str, list[dict]] = {}
    for strike, otype in sorted(needed):
        key = master.get((index_name, expiry, strike, otype))
        if not key:
            continue
        data = fetch_option_1min(udata, key, ref_date)
        if data:
            opt_candles[key] = data
        _time.sleep(0.12)

    if not opt_candles:
        return []

    candles_5min = resample_5min(spot)

    # Temporarily patch STRATEGY_PARAMS for existing detectors
    orig_params = None
    signals = []

    if strategy_name in ("momentum_scalp", "orb_retest", "day_end_sell", "short_strangle"):
        orig_params = STRATEGY_PARAMS.get(strategy_name)
        STRATEGY_PARAMS[strategy_name] = params
        if strategy_name == "momentum_scalp":
            signals = detect_momentum_scalp(candles_5min, index_name, opt_candles, master, expiry, ref_date)
        elif strategy_name == "orb_retest":
            signals = detect_orb_retest(candles_5min, index_name, opt_candles, master, expiry, ref_date)
        elif strategy_name == "day_end_sell":
            signals = detect_day_end_sell(candles_5min, index_name, opt_candles, master, expiry, ref_date)
        elif strategy_name == "short_strangle":
            signals = detect_short_strangle(candles_5min, index_name, opt_candles, master, expiry, ref_date, prev_close)
        if orig_params is not None:
            STRATEGY_PARAMS[strategy_name] = orig_params
    elif strategy_name == "trend_momentum":
        signals = detect_trend_momentum(candles_5min, index_name, opt_candles, master, expiry, ref_date, params)
    elif strategy_name == "vwap_bounce":
        signals = detect_vwap_bounce(candles_5min, index_name, opt_candles, master, expiry, ref_date, params)
    elif strategy_name == "ema_cross":
        signals = detect_ema_cross(candles_5min, index_name, opt_candles, master, expiry, ref_date, params)

    results = []
    for sig in signals:
        # Patch max_hold for new strategies
        if strategy_name in ("trend_momentum", "vwap_bounce", "ema_cross"):
            STRATEGY_PARAMS[strategy_name] = params
        if sig.action == "BUY":
            r = track_buy_trade(sig, opt_candles)
        elif sig.paired_instrument_key:
            r = track_paired_sell_trade(sig, opt_candles)
        else:
            r = track_single_sell_trade(sig, opt_candles)
        if strategy_name in ("trend_momentum", "vwap_bounce", "ema_cross"):
            if strategy_name in STRATEGY_PARAMS:
                del STRATEGY_PARAMS[strategy_name]
        if r:
            results.append(r)

    return results


def get_trading_days(from_date: date, to_date: date) -> list[date]:
    days = []
    d = from_date
    while d <= to_date:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


# ── Cache for option data ──────────────────────────────────────────

_spot_cache: dict[tuple[str, date], list[dict]] = {}
_opt_cache: dict[tuple[str, date], dict[str, list[dict]]] = {}


def fetch_day_data(udata: UpstoxData, index_name: str, ref_date: date,
                   master: dict) -> tuple[list[dict], dict[str, list[dict]], date]:
    """Fetch and cache spot + option data for a day."""
    idx = INDEXES[index_name]
    step = idx["step"]

    cache_key = (index_name, ref_date)
    if cache_key in _spot_cache:
        spot = _spot_cache[cache_key]
        opt_candles = _opt_cache[cache_key]
        expiry = next_expiry(ref_date, idx["expiry_weekday"])
        if ref_date.weekday() == idx["expiry_weekday"]:
            test_key = (index_name, ref_date, round_strike(spot[0]["close"], step), "CE")
            if test_key in master:
                expiry = ref_date
        return spot, opt_candles, expiry

    spot = fetch_spot_1min(udata, index_name, ref_date)
    if not spot or len(spot) < 30:
        _spot_cache[cache_key] = []
        _opt_cache[cache_key] = {}
        return [], {}, ref_date

    expiry = next_expiry(ref_date, idx["expiry_weekday"])
    if ref_date.weekday() == idx["expiry_weekday"]:
        test_key = (index_name, ref_date, round_strike(spot[0]["close"], step), "CE")
        if test_key in master:
            expiry = ref_date

    atm = round_strike(spot[0]["close"], step)

    needed: set[tuple[float, str]] = set()
    for offset in range(-6, 7):
        s = atm + offset * step
        if s > 0:
            needed.add((s, "CE"))
            needed.add((s, "PE"))

    opt_candles: dict[str, list[dict]] = {}
    for strike, otype in sorted(needed):
        key = master.get((index_name, expiry, strike, otype))
        if not key:
            continue
        data = fetch_option_1min(udata, key, ref_date)
        if data:
            opt_candles[key] = data
        _time.sleep(0.12)

    _spot_cache[cache_key] = spot
    _opt_cache[cache_key] = opt_candles
    return spot, opt_candles, expiry


def run_cached(spot: list[dict], opt_candles: dict[str, list[dict]],
               index_name: str, ref_date: date, expiry: date,
               master: dict, strategy_name: str, params: dict,
               prev_close: float | None = None) -> list[TradeResult]:
    """Run strategy on cached data — no API calls."""
    if not spot or not opt_candles:
        return []

    candles_5min = resample_5min(spot)

    orig_params = None
    signals = []

    if strategy_name in ("momentum_scalp", "orb_retest", "day_end_sell", "short_strangle"):
        orig_params = STRATEGY_PARAMS.get(strategy_name)
        STRATEGY_PARAMS[strategy_name] = params
        if strategy_name == "momentum_scalp":
            signals = detect_momentum_scalp(candles_5min, index_name, opt_candles, master, expiry, ref_date)
        elif strategy_name == "orb_retest":
            signals = detect_orb_retest(candles_5min, index_name, opt_candles, master, expiry, ref_date)
        elif strategy_name == "day_end_sell":
            signals = detect_day_end_sell(candles_5min, index_name, opt_candles, master, expiry, ref_date)
        elif strategy_name == "short_strangle":
            signals = detect_short_strangle(candles_5min, index_name, opt_candles, master, expiry, ref_date, prev_close)
        if orig_params is not None:
            STRATEGY_PARAMS[strategy_name] = orig_params
        elif strategy_name in STRATEGY_PARAMS:
            del STRATEGY_PARAMS[strategy_name]
    elif strategy_name == "trend_momentum":
        signals = detect_trend_momentum(candles_5min, index_name, opt_candles, master, expiry, ref_date, params)
    elif strategy_name == "vwap_bounce":
        signals = detect_vwap_bounce(candles_5min, index_name, opt_candles, master, expiry, ref_date, params)
    elif strategy_name == "ema_cross":
        signals = detect_ema_cross(candles_5min, index_name, opt_candles, master, expiry, ref_date, params)

    results = []
    for sig in signals:
        if strategy_name in ("trend_momentum", "vwap_bounce", "ema_cross"):
            STRATEGY_PARAMS[strategy_name] = params
        if sig.action == "BUY":
            r = track_buy_trade(sig, opt_candles)
        elif sig.paired_instrument_key:
            r = track_paired_sell_trade(sig, opt_candles)
        else:
            r = track_single_sell_trade(sig, opt_candles)
        if strategy_name in ("trend_momentum", "vwap_bounce", "ema_cross"):
            if strategy_name in STRATEGY_PARAMS:
                del STRATEGY_PARAMS[strategy_name]
        if r:
            results.append(r)

    return results


# ── Persistence ────────────────────────────────────────────────────

RESULTS_FILE = Path(__file__).resolve().parent.parent / "data" / "scanner_results.json"


def _trade_to_dict(t: TradeResult) -> dict:
    s = t.signal
    return {
        "date": str(s.ref_date), "time": s.time, "exit_time": t.exit_time,
        "strategy": s.strategy, "action": s.action,
        "index": s.index, "strike": s.strike, "option_type": s.option_type,
        "entry": s.entry_premium, "exit": t.exit_premium,
        "sl": s.sl_premium, "tgt": s.tgt_premium,
        "exit_reason": t.exit_reason, "pnl_per_lot": t.pnl_per_lot,
        "hold_mins": t.hold_mins, "won": t.won,
    }


def load_saved_results() -> dict:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text())
    return {"days_scanned": {}, "combo_trades": {}}


def save_results(saved: dict):
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(saved, indent=2, default=str))


def combo_key(strategy: str, label: str) -> str:
    return f"{strategy}:{label}"


def print_report(saved: dict, index_name: str):
    """Print accumulated results from saved data."""
    days = sorted(saved["days_scanned"].get(index_name, []))
    if not days:
        print(f"\n  No saved results for {index_name}. Run a scan first.")
        return

    print(f"\n{'='*75}")
    print(f"  ACCUMULATED RESULTS — {index_name}")
    print(f"  Days scanned: {len(days)} ({days[0]} → {days[-1]})")
    print(f"{'='*75}")

    lot = INDEXES[index_name]["lot_size"]

    @dataclass
    class AccResult:
        strategy: str
        label: str
        trades: int
        wins: int
        wr: float
        total_pnl: float
        avg_pnl: float
        trade_list: list[dict]

    results: list[AccResult] = []

    for ck, trades in saved["combo_trades"].items():
        idx_trades = [t for t in trades if t["index"] == index_name]
        if not idx_trades:
            continue
        wins = sum(1 for t in idx_trades if t["won"])
        total = len(idx_trades)
        wr = wins / total * 100 if total > 0 else 0
        total_pnl = sum(t["pnl_per_lot"] * lot for t in idx_trades)
        avg_pnl = total_pnl / total if total > 0 else 0
        strategy, label = ck.split(":", 1)
        results.append(AccResult(
            strategy=strategy, label=label,
            trades=total, wins=wins, wr=wr,
            total_pnl=total_pnl, avg_pnl=avg_pnl,
            trade_list=idx_trades,
        ))

    qualified = [r for r in results if r.trades >= 3]
    qualified.sort(key=lambda r: (-r.wr, -r.total_pnl))

    print(f"\n  {'Rank':>4s}  {'Strategy':<18s} {'Label':<15s} "
          f"{'Trades':>6s} {'Wins':>5s} {'WR':>6s} {'P&L':>10s} {'Avg':>8s}")
    print(f"  {'─'*4}  {'─'*18} {'─'*15} {'─'*6} {'─'*5} {'─'*6} {'─'*10} {'─'*8}")

    for i, c in enumerate(qualified[:20]):
        marker = "🏆" if c.wr >= 80 else "★" if c.wr >= 70 else " "
        print(f"  {marker}{i+1:>3d}  {c.strategy:<18s} {c.label:<15s} "
              f"{c.trades:>6d} {c.wins:>5d} {c.wr:>5.1f}% "
              f"₹{c.total_pnl:>+9,.0f} ₹{c.avg_pnl:>+7,.0f}")

    # Detail top 5
    for i, c in enumerate(qualified[:5]):
        print(f"\n  ┌─ #{i+1} {c.strategy} [{c.label}] {'─' * 40}")
        print(f"  │ WR: {c.wr:.1f}% ({c.wins}/{c.trades})  P&L: ₹{c.total_pnl:>+,.0f}")

        exits = defaultdict(int)
        for t in c.trade_list:
            exits[t["exit_reason"]] += 1
        print(f"  │ Exits: {dict(exits)}")

        winning_pnl = [t["pnl_per_lot"] * lot for t in c.trade_list if t["won"]]
        losing_pnl = [t["pnl_per_lot"] * lot for t in c.trade_list if not t["won"]]
        avg_win = sum(winning_pnl) / len(winning_pnl) if winning_pnl else 0
        avg_loss = sum(losing_pnl) / len(losing_pnl) if losing_pnl else 0
        print(f"  │ Avg Win: ₹{avg_win:>+,.0f}  Avg Loss: ₹{avg_loss:>+,.0f}")
        print(f"  │ Trades:")
        for t in c.trade_list:
            w = "✓" if t["won"] else "✗"
            print(f"  │   {w} {t['date']} {t['time']}→{t['exit_time']} "
                  f"{t['strike']:.0f}{t['option_type']} "
                  f"@{t['entry']:.1f}→{t['exit']:.1f} "
                  f"({t['exit_reason']}) ₹{t['pnl_per_lot']*lot:+,.0f}")
        print(f"  └{'─' * 65}")

    winners = [c for c in qualified if c.wr >= 80]
    if winners:
        print(f"\n  🏆 FOUND {len(winners)} COMBO(S) WITH 80%+ WIN RATE!")
        for c in winners:
            print(f"     → {c.strategy} [{c.label}]: {c.wr:.1f}% ({c.wins}/{c.trades}) ₹{c.total_pnl:+,.0f}")
    elif qualified:
        best = qualified[0]
        print(f"\n  Best so far: {best.strategy} [{best.label}] "
              f"{best.wr:.1f}% ({best.wins}/{best.trades})")
        print(f"  Run more days to build up statistics.")
    print()


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Strategy Scanner — find 80%+ WR combos")
    parser.add_argument("--from", dest="from_date", default=None)
    parser.add_argument("--to", dest="to_date", default=None)
    parser.add_argument("--index", default="NIFTY")
    parser.add_argument("--report", action="store_true", help="Show accumulated results only")
    args = parser.parse_args()

    index_name = args.index.upper()
    if index_name not in INDEXES:
        print(f"Unknown index: {index_name}")
        sys.exit(1)

    if args.report:
        saved = load_saved_results()
        print_report(saved, index_name)
        return

    today = datetime.now(IST).date()
    from_date = date.fromisoformat(args.from_date) if args.from_date else today
    to_date = date.fromisoformat(args.to_date) if args.to_date else today

    print(f"\n{'='*75}")
    print(f"  STRATEGY SCANNER — Finding 80%+ Win Rate Combos")
    print(f"  {index_name} | {from_date} → {to_date}")
    print(f"{'='*75}")

    print("\nLoading option master...")
    from src.strategy.live_runner import _build_option_master
    master = _build_option_master(None)
    print(f"  Master: {len(master)} entries")

    udata = UpstoxData()
    trading_days = get_trading_days(from_date, to_date)
    print(f"  Trading days: {len(trading_days)}")

    # Load saved results
    saved = load_saved_results()

    # Check which days are already scanned
    scanned = set(saved["days_scanned"].get(index_name, []))
    new_days = [d for d in trading_days if d.isoformat() not in scanned]
    if not new_days:
        print(f"\n  All {len(trading_days)} days already scanned. Showing report.")
        print_report(saved, index_name)
        return

    print(f"  New days to scan: {len(new_days)} (already scanned: {len(trading_days) - len(new_days)})")

    # Phase 1: Fetch and cache all day data
    print(f"\nPhase 1: Fetching market data...")
    valid_days = []
    for day in new_days:
        print(f"  {day} ...", end=" ", flush=True)
        spot, opt, expiry = fetch_day_data(udata, index_name, day, master)
        if spot and opt:
            print(f"✓ spot={len(spot)} candles, {len(opt)} option chains")
            valid_days.append(day)
        elif spot:
            print(f"⚠ spot OK but 0 option chains (expiry not in master?)")
        else:
            print("SKIP (no data)")

    if not valid_days:
        print(f"\n  No valid days with option data. "
              f"The instrument master only has expiries >= {min(k[1] for k in master if k[0]==index_name) if any(k[0]==index_name for k in master) else '?'}.")
        print(f"  Run this after market close on a day within those expiry weeks.")
        if scanned:
            print_report(saved, index_name)
        return

    # Phase 2: Sweep all strategy + param combinations
    print(f"\nPhase 2: Scanning {len(valid_days)} day(s)...")

    all_combos = []
    for strat_name, param_list in PARAM_GRID.items():
        for params in param_list:
            all_combos.append((strat_name, params))
    for strat_name, param_list in NEW_STRATEGY_GRID.items():
        for params in param_list:
            all_combos.append((strat_name, params))

    print(f"  Total combos to test: {len(all_combos)}")

    for combo_idx, (strat_name, params) in enumerate(all_combos):
        label = params.get("label", "?")
        ck = combo_key(strat_name, label)
        new_trades: list[TradeResult] = []
        daily_counts = []

        for day in valid_days:
            cache_key = (index_name, day)
            spot = _spot_cache.get(cache_key, [])
            opt = _opt_cache.get(cache_key, {})
            if not spot or not opt:
                daily_counts.append(0)
                continue

            idx = INDEXES[index_name]
            expiry = next_expiry(day, idx["expiry_weekday"])
            if day.weekday() == idx["expiry_weekday"]:
                test_key = (index_name, day, round_strike(spot[0]["close"], idx["step"]), "CE")
                if test_key in master:
                    expiry = day

            results = run_cached(spot, opt, index_name, day, expiry, master,
                                 strat_name, params)
            new_trades.extend(results)
            daily_counts.append(len(results))

        # Save new trades
        if ck not in saved["combo_trades"]:
            saved["combo_trades"][ck] = []
        for t in new_trades:
            saved["combo_trades"][ck].append(_trade_to_dict(t))

        # Show progress
        all_saved = saved["combo_trades"][ck]
        idx_trades = [t for t in all_saved if t["index"] == index_name]
        total = len(idx_trades)
        wins = sum(1 for t in idx_trades if t["won"])
        wr = wins / total * 100 if total > 0 else 0
        lot = INDEXES[index_name]["lot_size"]
        total_pnl = sum(t["pnl_per_lot"] * lot for t in idx_trades)

        tag = "★" if wr >= 70 and total >= 3 else " "
        new_tag = f"+{len(new_trades)}" if new_trades else "0"
        print(f"  {tag} {combo_idx+1:>2}/{len(all_combos)}  "
              f"{strat_name:<18s} [{label:<15s}]  "
              f"{total:>3d} trades ({new_tag} new)  {wins:>3d} wins  {wr:>5.1f}% WR  "
              f"₹{total_pnl:>+9,.0f}")

    # Mark days as scanned
    if index_name not in saved["days_scanned"]:
        saved["days_scanned"][index_name] = []
    for d in valid_days:
        if d.isoformat() not in saved["days_scanned"][index_name]:
            saved["days_scanned"][index_name].append(d.isoformat())
    saved["days_scanned"][index_name].sort()

    save_results(saved)
    print(f"\n  Results saved to {RESULTS_FILE}")

    # Show full report
    print_report(saved, index_name)


if __name__ == "__main__":
    main()

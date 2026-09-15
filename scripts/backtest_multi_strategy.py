"""Multi-strategy signal backtester v2 — redesigned for higher win rate.

Strategies (redesigned based on v1 failure analysis):
  1. Momentum Scalp  — buy ATM on strong 5-min candle + volume spike, quick exit
  2. ORB Retest      — buy on breakout + retest confirmation (not raw breakout)
  3. Short Strangle   — sell OTM CE+PE at 10:00, wider 40% SL, skip 0DTE/volatile
  4. Day-End Sell     — sell OTM WITH the day's trend at 14:00, theta crush to close

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/backtest_multi_strategy.py \
        --from 2026-09-09 --to 2026-09-15 --index NIFTY -v
"""
from __future__ import annotations

import argparse
import sys
import time as _time
from collections import defaultdict
from dataclasses import dataclass
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
        "expiry_weekday": 1,
    },
    "BANKNIFTY": {
        "key": "NSE_INDEX|Nifty Bank",
        "step": 100,
        "lot_size": 30,
        "expiry_weekday": 2,
    },
}

# ── Strategy parameters ─────────────────────────────────────────────

STRATEGY_PARAMS = {
    "momentum_scalp": {
        "body_pct": 0.15,
        "vol_mult": 1.5,
        "sl_pct": 0.12,
        "tgt_pct": 0.18,
        "max_hold_mins": 15,
        "active_from": "09:30",
        "active_to": "14:00",
        "max_signals": 3,
        "cooldown_mins": 15,
    },
    "orb_retest": {
        "range_end": "09:44",
        "active_from": "09:50",
        "active_to": "12:00",
        "sl_pct": 0.25,
        "tgt_pct": 0.45,
        "max_hold_mins": 60,
        "min_range_pct": 0.15,
        "max_range_pct": 0.80,
        "retest_pct": 0.10,
        "max_signals": 1,
    },
    "short_strangle": {
        "entry_time": "10:00",
        "otm_steps": 1,
        "sl_pct": 0.40,
        "tgt_pct": 0.35,
        "time_exit": "15:15",
        "min_dte": 1,
        "max_gap_pct": 0.80,
        "max_signals": 1,
    },
    "day_end_sell": {
        "entry_time": "14:00",
        "otm_steps": 2,
        "sl_mult": 2.0,
        "time_exit": "15:10",
        "min_trend_pct": 0.20,
        "max_signals": 1,
    },
}

IMPACT_COST = 1.0


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
    note: str = ""


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


def next_expiry(ref_date: date, weekday: int) -> date:
    d = ref_date
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


def _opt_at_time(opt_data: list[dict], time_str: str) -> dict | None:
    exact = next((c for c in opt_data if c["time"] == time_str), None)
    if exact:
        return exact
    for c in opt_data:
        if c["time"] >= time_str:
            if time_diff_mins(time_str, c["time"]) <= 5:
                return c
            break
    return None


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


# ── Strategy 1: Momentum Scalp ─────────────────────────────────────

def detect_momentum_scalp(candles_5min: list[dict], index_name: str,
                          opt_candles: dict, master: dict, expiry: date,
                          ref_date: date) -> list[Signal]:
    p = STRATEGY_PARAMS["momentum_scalp"]
    step = INDEXES[index_name]["step"]
    signals: list[Signal] = []
    last_sig_time = None

    vol_sum, vol_count = 0.0, 0

    for i, c in enumerate(candles_5min):
        vol_sum += c["volume"]
        vol_count += 1
        avg_vol = vol_sum / vol_count if vol_count > 0 else 0

        if c["time"] < p["active_from"] or c["time"] > p["active_to"]:
            continue
        if len(signals) >= p["max_signals"]:
            break
        if last_sig_time and time_diff_mins(last_sig_time, c["time"]) < p["cooldown_mins"]:
            continue
        if i < 2 or avg_vol <= 0:
            continue

        body = abs(c["close"] - c["open"])
        if c["open"] == 0:
            continue
        body_pct = body / c["open"] * 100

        if body_pct < p["body_pct"]:
            continue
        if c["volume"] < avg_vol * p["vol_mult"]:
            continue

        # Confirm: candle body is in the direction of the wick
        # (close near high for bullish, close near low for bearish)
        rng = c["high"] - c["low"]
        if rng == 0:
            continue

        direction = None
        if c["close"] > c["open"]:
            close_position = (c["close"] - c["low"]) / rng
            if close_position > 0.6:
                direction = "bullish"
        else:
            close_position = (c["high"] - c["close"]) / rng
            if close_position > 0.6:
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
            time=c["time"], strategy="momentum_scalp", action="BUY",
            index=index_name, strike=strike, option_type=opt_type,
            entry_premium=entry,
            sl_premium=entry * (1 - p["sl_pct"]),
            tgt_premium=entry * (1 + p["tgt_pct"]),
            spot_at_entry=c["close"], instrument_key=opt_key,
            ref_date=ref_date,
            note=f"body={body_pct:.2f}% vol={c['volume']/avg_vol:.1f}x" if avg_vol > 0 else "",
        ))
        last_sig_time = c["time"]

    return signals


# ── Strategy 2: ORB with Retest ────────────────────────────────────

def detect_orb_retest(candles_5min: list[dict], index_name: str,
                      opt_candles: dict, master: dict, expiry: date,
                      ref_date: date) -> list[Signal]:
    p = STRATEGY_PARAMS["orb_retest"]
    step = INDEXES[index_name]["step"]

    range_bars = [c for c in candles_5min if c["time"] <= p["range_end"]]
    if not range_bars:
        return []

    rng_high = max(c["high"] for c in range_bars)
    rng_low = min(c["low"] for c in range_bars)
    rng_pct = (rng_high - rng_low) / rng_low * 100

    if rng_pct < p["min_range_pct"] or rng_pct > p["max_range_pct"]:
        return []

    breakout_dir = None
    breakout_level = None
    retest_done = False
    signals: list[Signal] = []

    for c in candles_5min:
        if c["time"] < p["active_from"] or c["time"] > p["active_to"]:
            continue
        if signals:
            break

        # Phase 1: detect initial breakout
        if breakout_dir is None:
            if c["close"] > rng_high and c["close"] > c["open"]:
                breakout_dir = "bullish"
                breakout_level = rng_high
            elif c["close"] < rng_low and c["close"] < c["open"]:
                breakout_dir = "bearish"
                breakout_level = rng_low
            continue

        tolerance = breakout_level * p["retest_pct"] / 100

        # Phase 2: wait for retest + bounce
        if breakout_dir == "bullish":
            # Retest: low pulls back near breakout level (range high)
            pulled_back = c["low"] <= breakout_level + tolerance
            bounced = c["close"] > breakout_level and c["close"] > c["open"]
            still_above = c["close"] > rng_high

            if pulled_back and bounced and still_above:
                strike = round_strike(c["close"], step)
                opt_key = master.get((index_name, expiry, strike, "CE"))
                if not opt_key or opt_key not in opt_candles:
                    continue
                oc = _opt_at_time(opt_candles[opt_key], c["time"])
                if not oc or oc["close"] <= 5:
                    continue
                entry = oc["close"]
                signals.append(Signal(
                    time=c["time"], strategy="orb_retest", action="BUY",
                    index=index_name, strike=strike, option_type="CE",
                    entry_premium=entry,
                    sl_premium=entry * (1 - p["sl_pct"]),
                    tgt_premium=entry * (1 + p["tgt_pct"]),
                    spot_at_entry=c["close"], instrument_key=opt_key,
                    ref_date=ref_date,
                    note=f"range={rng_low:.0f}-{rng_high:.0f} retest@{c['time']}",
                ))
            # If price goes back inside range, breakout failed
            elif c["close"] < rng_low:
                breakout_dir = None

        elif breakout_dir == "bearish":
            pulled_back = c["high"] >= breakout_level - tolerance
            bounced = c["close"] < breakout_level and c["close"] < c["open"]
            still_below = c["close"] < rng_low

            if pulled_back and bounced and still_below:
                strike = round_strike(c["close"], step)
                opt_key = master.get((index_name, expiry, strike, "PE"))
                if not opt_key or opt_key not in opt_candles:
                    continue
                oc = _opt_at_time(opt_candles[opt_key], c["time"])
                if not oc or oc["close"] <= 5:
                    continue
                entry = oc["close"]
                signals.append(Signal(
                    time=c["time"], strategy="orb_retest", action="BUY",
                    index=index_name, strike=strike, option_type="PE",
                    entry_premium=entry,
                    sl_premium=entry * (1 - p["sl_pct"]),
                    tgt_premium=entry * (1 + p["tgt_pct"]),
                    spot_at_entry=c["close"], instrument_key=opt_key,
                    ref_date=ref_date,
                    note=f"range={rng_low:.0f}-{rng_high:.0f} retest@{c['time']}",
                ))
            elif c["close"] > rng_high:
                breakout_dir = None

    return signals


# ── Strategy 3: Short Strangle ─────────────────────────────────────

def detect_short_strangle(candles_5min: list[dict], index_name: str,
                          opt_candles: dict, master: dict, expiry: date,
                          ref_date: date, prev_close: float | None = None) -> list[Signal]:
    p = STRATEGY_PARAMS["short_strangle"]
    step = INDEXES[index_name]["step"]

    dte = (expiry - ref_date).days
    if dte < p["min_dte"]:
        return []

    entry_bar = next((c for c in candles_5min if c["time"] == p["entry_time"]), None)
    if not entry_bar:
        return []

    spot = entry_bar["close"]

    # Skip volatile gap days
    if prev_close and prev_close > 0:
        gap_pct = abs(spot - prev_close) / prev_close * 100
        if gap_pct > p["max_gap_pct"]:
            return []

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
    if combined < 15:
        return []

    return [Signal(
        time=p["entry_time"], strategy="short_strangle", action="SELL",
        index=index_name, strike=ce_strike, option_type="CE",
        entry_premium=ce_prem,
        sl_premium=combined * (1 + p["sl_pct"]),
        tgt_premium=combined * (1 - p["tgt_pct"]),
        spot_at_entry=spot, instrument_key=ce_key,
        paired_strike=pe_strike, paired_type="PE",
        paired_premium=pe_prem, paired_instrument_key=pe_key,
        ref_date=ref_date,
        note=f"CE={ce_strike} PE={pe_strike} DTE={dte} comb={combined:.0f}",
    )]


# ── Strategy 4: Day-End Sell ───────────────────────────────────────

def detect_day_end_sell(candles_5min: list[dict], index_name: str,
                        opt_candles: dict, master: dict, expiry: date,
                        ref_date: date) -> list[Signal]:
    p = STRATEGY_PARAMS["day_end_sell"]
    step = INDEXES[index_name]["step"]

    entry_bar = next((c for c in candles_5min if c["time"] == p["entry_time"]), None)
    if not entry_bar:
        return []

    day_open = candles_5min[0]["open"]
    current = entry_bar["close"]
    trend_pct = (current - day_open) / day_open * 100

    if abs(trend_pct) < p["min_trend_pct"]:
        return []

    spot = entry_bar["close"]
    atm = round_strike(spot, step)

    # Sell WITH the trend (trend continuation protects us)
    if trend_pct > 0:
        # Bullish day → sell OTM PE (unlikely to go down further)
        sell_strike = atm - p["otm_steps"] * step
        sell_type = "PE"
    else:
        # Bearish day → sell OTM CE (unlikely to reverse up)
        sell_strike = atm + p["otm_steps"] * step
        sell_type = "CE"

    sell_key = master.get((index_name, expiry, sell_strike, sell_type))
    if not sell_key or sell_key not in opt_candles:
        return []

    oc = _opt_at_time(opt_candles[sell_key], p["entry_time"])
    if not oc or oc["close"] <= 2:
        return []

    entry = oc["close"]
    sl = entry * p["sl_mult"]

    tag = "sell PE (bullish day)" if trend_pct > 0 else "sell CE (bearish day)"

    return [Signal(
        time=p["entry_time"], strategy="day_end_sell", action="SELL",
        index=index_name, strike=sell_strike, option_type=sell_type,
        entry_premium=entry,
        sl_premium=sl,
        tgt_premium=0,
        spot_at_entry=spot, instrument_key=sell_key,
        ref_date=ref_date,
        note=f"{tag} trend={trend_pct:+.2f}%",
    )]


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
            pnl = sig.sl_premium - entry - 2 * IMPACT_COST
            return TradeResult(
                signal=sig, exit_time=c["time"], exit_premium=sig.sl_premium,
                exit_reason="sl", pnl_per_lot=pnl, hold_mins=hold, won=False)

        if c["high"] >= sig.tgt_premium:
            pnl = sig.tgt_premium - entry - 2 * IMPACT_COST
            return TradeResult(
                signal=sig, exit_time=c["time"], exit_premium=sig.tgt_premium,
                exit_reason="tgt", pnl_per_lot=pnl, hold_mins=hold, won=True)

        if hold >= max_hold or c["time"] >= "15:15":
            pnl = c["close"] - entry - 2 * IMPACT_COST
            return TradeResult(
                signal=sig, exit_time=c["time"], exit_premium=c["close"],
                exit_reason="time_exit", pnl_per_lot=pnl,
                hold_mins=hold, won=pnl > 0)

    if data:
        last = data[-1]
        pnl = last["close"] - entry - 2 * IMPACT_COST
        return TradeResult(
            signal=sig, exit_time=last["time"], exit_premium=last["close"],
            exit_reason="day_end", pnl_per_lot=pnl,
            hold_mins=time_diff_mins(sig.time, last["time"]), won=pnl > 0)
    return None


def track_paired_sell_trade(sig: Signal, opt_candles: dict) -> TradeResult | None:
    """Track strangle (paired sell) — SL/TGT on combined premium."""
    ce_data = opt_candles.get(sig.instrument_key, [])
    pe_data = opt_candles.get(sig.paired_instrument_key, [])
    if not ce_data or not pe_data:
        return None

    ce_by_t = {c["time"]: c for c in ce_data}
    pe_by_t = {c["time"]: c for c in pe_data}
    combined_entry = sig.entry_premium + sig.paired_premium
    time_exit = STRATEGY_PARAMS[sig.strategy]["time_exit"]

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


def track_single_sell_trade(sig: Signal, opt_candles: dict) -> TradeResult | None:
    """Track single-leg sell (day_end_sell) — SL when premium doubles."""
    data = opt_candles.get(sig.instrument_key, [])
    if not data:
        return None

    entry = sig.entry_premium
    time_exit = STRATEGY_PARAMS[sig.strategy]["time_exit"]

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

        # SL: premium rises to sl level
        if c["high"] >= sig.sl_premium:
            pnl = entry - sig.sl_premium - 2 * IMPACT_COST
            return TradeResult(
                signal=sig, exit_time=c["time"], exit_premium=sig.sl_premium,
                exit_reason="sl", pnl_per_lot=pnl, hold_mins=hold, won=False)

        # Time exit
        if c["time"] >= time_exit:
            pnl = entry - c["close"] - 2 * IMPACT_COST
            return TradeResult(
                signal=sig, exit_time=c["time"], exit_premium=c["close"],
                exit_reason="time_exit", pnl_per_lot=pnl,
                hold_mins=hold, won=pnl > 0)

    if data:
        last = data[-1]
        pnl = entry - last["close"] - 2 * IMPACT_COST
        return TradeResult(
            signal=sig, exit_time=last["time"], exit_premium=last["close"],
            exit_reason="day_end", pnl_per_lot=pnl,
            hold_mins=time_diff_mins(sig.time, last["time"]),
            won=pnl > 0)
    return None


# ── Day runner ──────────────────────────────────────────────────────

def run_day(udata: UpstoxData, index_name: str, ref_date: date,
            master: dict, strategies: set[str],
            verbose: bool = False,
            prev_close: float | None = None) -> tuple[list[TradeResult], str, float]:
    """Returns (results, status, closing_price)."""
    idx = INDEXES[index_name]
    step = idx["step"]

    spot = fetch_spot_1min(udata, index_name, ref_date)
    if not spot or len(spot) < 30:
        return [], f"spot candles={len(spot) if spot else 0}", 0

    expiry = next_expiry(ref_date, idx["expiry_weekday"])

    # Check if ref_date itself has options in master (0DTE)
    if ref_date.weekday() == idx["expiry_weekday"]:
        test_key = (index_name, ref_date, round_strike(spot[0]["close"], step), "CE")
        if test_key in master:
            expiry = ref_date

    opening = spot[0]["close"]
    closing = spot[-1]["close"]
    atm = round_strike(opening, step)
    dte = (expiry - ref_date).days

    if verbose:
        day_chg = (closing - opening) / opening * 100
        print(f"    open={opening:.0f} close={closing:.0f} ({day_chg:+.2f}%) "
              f"atm={atm} expiry={expiry} DTE={dte} candles={len(spot)}")

    # Determine all strikes we need
    needed: set[tuple[float, str]] = set()
    for offset in range(-5, 6):
        s = atm + offset * step
        if s > 0:
            needed.add((s, "CE"))
            needed.add((s, "PE"))
    # Wider OTM for day_end_sell
    otm_steps = STRATEGY_PARAMS["day_end_sell"]["otm_steps"]
    for extra in range(otm_steps, otm_steps + 2):
        needed.add((atm + extra * step, "CE"))
        needed.add((atm - extra * step, "PE"))

    # Fetch option 1-min candles
    opt_candles: dict[str, list[dict]] = {}
    missing = 0
    for strike, otype in sorted(needed):
        key = master.get((index_name, expiry, strike, otype))
        if not key:
            missing += 1
            continue
        data = fetch_option_1min(udata, key, ref_date)
        if data:
            opt_candles[key] = data
        _time.sleep(0.12)

    if not opt_candles:
        return [], f"no option data (expiry={expiry}, missing={missing})", 0

    if verbose:
        print(f"    options: {len(opt_candles)} fetched, {missing} missing from master")

    candles_5min = resample_5min(spot)

    all_signals: list[Signal] = []
    if "momentum_scalp" in strategies:
        all_signals += detect_momentum_scalp(candles_5min, index_name, opt_candles, master, expiry, ref_date)
    if "orb_retest" in strategies:
        all_signals += detect_orb_retest(candles_5min, index_name, opt_candles, master, expiry, ref_date)
    if "short_strangle" in strategies:
        all_signals += detect_short_strangle(candles_5min, index_name, opt_candles, master, expiry, ref_date, prev_close)
    if "day_end_sell" in strategies:
        all_signals += detect_day_end_sell(candles_5min, index_name, opt_candles, master, expiry, ref_date)

    results = []
    for sig in all_signals:
        if sig.action == "BUY":
            r = track_buy_trade(sig, opt_candles)
        elif sig.paired_instrument_key:
            r = track_paired_sell_trade(sig, opt_candles)
        else:
            r = track_single_sell_trade(sig, opt_candles)
        if r:
            results.append(r)

    return results, "ok", closing


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
    parser = argparse.ArgumentParser(description="Multi-strategy signal backtester v2")
    parser.add_argument("--from", dest="from_date", required=True)
    parser.add_argument("--to", dest="to_date", required=True)
    parser.add_argument("--index", default="NIFTY")
    parser.add_argument("--strategies", default="all",
                        help="Comma-separated: momentum_scalp,orb_retest,short_strangle,day_end_sell")
    parser.add_argument("--verbose", "-v", action="store_true")
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
    print(f"  MULTI-STRATEGY SIGNAL BACKTEST v2")
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
    prev_close = None

    for day in trading_days:
        print(f"  {day} ...", end=" " if not args.verbose else "\n", flush=True)
        results, status, closing = run_day(
            udata, index_name, day, master, strategies,
            verbose=args.verbose, prev_close=prev_close)
        prev_close = closing if closing > 0 else prev_close

        if status != "ok":
            print(f"SKIP ({status})" if not args.verbose else f"    SKIP ({status})")
            skipped.append((day, status))
            continue

        if not args.verbose:
            print(f"{len(results)} trades")
        else:
            print(f"    → {len(results)} trades")

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
                      f"[{s.time}→{r.exit_time} {r.hold_mins}m]"
                      f"  {s.note}" if s.note else
                      f"    {w} {s.strategy:<16s} BUY {s.strike:>7.0f} {s.option_type} "
                      f"@{s.entry_premium:>7.1f} → {r.exit_premium:>7.1f} "
                      f"({r.exit_reason:<10s}) ₹{pnl:>+8,.0f}  "
                      f"[{s.time}→{r.exit_time} {r.hold_mins}m]")
            elif s.paired_instrument_key:
                comb_e = s.entry_premium + s.paired_premium
                comb_x = r.exit_premium + r.paired_exit_premium
                print(f"    {w} {s.strategy:<16s} SELL strangle "
                      f"@{comb_e:>7.1f} → {comb_x:>7.1f} "
                      f"({r.exit_reason:<10s}) ₹{pnl:>+8,.0f}  "
                      f"[{s.time}→{r.exit_time} {r.hold_mins}m]")
            else:
                print(f"    {w} {s.strategy:<16s} SELL {s.strike:>7.0f} {s.option_type} "
                      f"@{s.entry_premium:>7.1f} → {r.exit_premium:>7.1f} "
                      f"({r.exit_reason:<10s}) ₹{pnl:>+8,.0f}  "
                      f"[{s.time}→{r.exit_time} {r.hold_mins}m]"
                      f"  {s.note}" if s.note else
                      f"    {w} {s.strategy:<16s} SELL {s.strike:>7.0f} {s.option_type} "
                      f"@{s.entry_premium:>7.1f} → {r.exit_premium:>7.1f} "
                      f"({r.exit_reason:<10s}) ₹{pnl:>+8,.0f}  "
                      f"[{s.time}→{r.exit_time} {r.hold_mins}m]")

    # ── Per-strategy summary ────────────────────────────────────────
    print(f"\n{'='*75}")
    print(f"  STRATEGY BREAKDOWN — {index_name}")
    print(f"{'='*75}")

    strat_order = ["momentum_scalp", "orb_retest", "short_strangle", "day_end_sell"]
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
        expectancy = (wr / 100 * avg_win) + ((100 - wr) / 100 * avg_loss)

        print(f"\n  ┌─ {sn.upper()} {'─' * (55 - len(sn))}")
        print(f"  │ Trades: {total}  |  Wins: {wins}  |  Win Rate: {wr:.1f}%")
        print(f"  │ Total P&L: ₹{total_pnl:>+10,.0f}  |  Avg: ₹{avg_pnl:>+8,.0f}/trade")
        print(f"  │ Avg Win: ₹{avg_win:>+8,.0f}  |  Avg Loss: ₹{avg_loss:>+8,.0f}")
        print(f"  │ Expectancy: ₹{expectancy:>+8,.0f}/trade")
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

    winning_pnl = [t.pnl_per_lot * lot for t in all_results if t.won]
    losing_pnl = [t.pnl_per_lot * lot for t in all_results if not t.won]
    avg_win = sum(winning_pnl) / len(winning_pnl) if winning_pnl else 0
    avg_loss = sum(losing_pnl) / len(losing_pnl) if losing_pnl else 0

    print(f"\n  {'═' * 60}")
    print(f"  COMBINED: {total} trades | {wins} wins | {wr:.1f}% WIN RATE")
    print(f"  Total P&L: ₹{total_pnl:>+10,.0f}  |  Avg: ₹{avg_pnl:>+8,.0f}/trade")
    print(f"  Avg Win: ₹{avg_win:>+8,.0f}  |  Avg Loss: ₹{avg_loss:>+8,.0f}")
    print(f"  Avg signals/day: {total / traded_days:.1f}  |  Days: {traded_days}/{len(trading_days)}")
    print(f"  {'═' * 60}")

    # ── Daily P&L ───────────────────────────────────────────────────
    print(f"\n  DAILY P&L:")
    print(f"  {'Date':<12s} {'Trades':>6s} {'Wins':>5s} {'WR':>6s} {'P&L':>10s}  {'Cum P&L':>10s}")
    print(f"  {'─'*12} {'─'*6} {'─'*5} {'─'*6} {'─'*10}  {'─'*10}")
    cum = 0
    winning_days = 0
    for day in sorted(by_date):
        trades = by_date[day]
        d_wins = sum(1 for t in trades if t.won)
        d_pnl = sum(t.pnl_per_lot * lot for t in trades)
        d_wr = d_wins / len(trades) * 100 if trades else 0
        cum += d_pnl
        if d_pnl > 0:
            winning_days += 1
        bar_len = min(int(abs(d_pnl) / 300), 20)
        bar = ("█" if d_pnl >= 0 else "░") * bar_len
        print(f"  {day!s:<12s} {len(trades):>6d} {d_wins:>5d} {d_wr:>5.0f}% "
              f"₹{d_pnl:>+9,.0f}  ₹{cum:>+9,.0f}  {bar}")
    print(f"  {'─'*12} {'─'*6} {'─'*5} {'─'*6} {'─'*10}  {'─'*10}")
    print(f"  {'TOTAL':<12s} {total:>6d} {wins:>5d} {wr:>5.1f}% ₹{cum:>+9,.0f}")
    print(f"  Winning days: {winning_days}/{len(by_date)}")

    if skipped:
        print(f"\n  Skipped: {', '.join(str(d) for d, _ in skipped)}")

    # ── Sample signals ──────────────────────────────────────────────
    print(f"\n\n  SAMPLE SIGNALS (Telegram channel format):")
    print(f"  {'─'*50}")
    shown = 0
    for r in all_results:
        if shown >= 4:
            break
        s = r.signal
        pnl = r.pnl_per_lot * lot
        label = s.strategy.upper().replace("_", " ")
        if s.action == "BUY":
            print(f"  🟢 {label}")
            print(f"  BUY {s.index} {s.strike:.0f} {s.option_type} @ ₹{s.entry_premium:.1f}")
            print(f"  SL: ₹{s.sl_premium:.1f} | TGT: ₹{s.tgt_premium:.1f}")
        elif s.paired_instrument_key:
            print(f"  🔴 {label}")
            print(f"  SELL {s.index} {s.strike:.0f} {s.option_type} @ ₹{s.entry_premium:.1f}")
            print(f"  SELL {s.index} {s.paired_strike:.0f} {s.paired_type} @ ₹{s.paired_premium:.1f}")
            comb = s.entry_premium + s.paired_premium
            print(f"  Combined: ₹{comb:.0f} | SL: ₹{s.sl_premium:.0f} | TGT: ₹{s.tgt_premium:.0f}")
        else:
            print(f"  🔴 {label}")
            print(f"  SELL {s.index} {s.strike:.0f} {s.option_type} @ ₹{s.entry_premium:.1f}")
            print(f"  SL: ₹{s.sl_premium:.1f} | TGT: hold to close")
        print(f"  → {'✅ WIN' if r.won else '❌ LOSS'} ₹{pnl:+,.0f} ({r.hold_mins}m)")
        print()
        shown += 1

    print()


if __name__ == "__main__":
    main()

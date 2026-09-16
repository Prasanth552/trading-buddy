"""Live signal generator — runs during market hours, detects signals, posts to Telegram.

Strategies (indexes + stocks):
  1. Momentum Scalp — buy ATM on strong 5-min candle
  2. ORB Retest — buy on breakout + retest confirmation
  3. VWAP Reversal — buy when price bounces off VWAP with confirmation
  4. EMA Crossover — buy when 8-EMA crosses 21-EMA with momentum
  5. Short Strangle — sell OTM CE+PE at 10:00 (indexes only)
  6. Day-End Sell — sell OTM with trend at 14:00 (indexes only)

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/live_signal_poster.py [--dry-run]
"""
from __future__ import annotations

import os
import sys
import time
import requests
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.broker.upstox_data import UpstoxData
from src.strategy.live_runner import _build_option_master
from src.utils.logging import get_logger

log = get_logger("signal_poster")
IST = ZoneInfo("Asia/Kolkata")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_SIGNAL_CHANNEL", "-1004385130897")

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

STOCKS = {
    "RELIANCE": {"key": "NSE_EQ|INE002A01018", "step": 10, "lot_size": 250, "expiry_weekday": 3},
    "HDFCBANK": {"key": "NSE_EQ|INE040A01034", "step": 10, "lot_size": 550, "expiry_weekday": 3},
    "SBIN": {"key": "NSE_EQ|INE062A01020", "step": 10, "lot_size": 750, "expiry_weekday": 3},
    "TATAMOTORS": {"key": "NSE_EQ|INE155A01022", "step": 10, "lot_size": 1400, "expiry_weekday": 3},
}

ALL_INSTRUMENTS = {}
ALL_INSTRUMENTS.update(INDEXES)
ALL_INSTRUMENTS.update(STOCKS)

STRATEGY_PARAMS = {
    "momentum_scalp": {
        "body_pct": 0.12,
        "vol_mult": 1.3,
        "sl_pct": 0.12,
        "tgt_pct": 0.18,
        "max_hold_mins": 15,
        "active_from": "09:20",
        "active_to": "14:30",
        "max_signals": 2,
        "cooldown_mins": 15,
        "close_position_min": 0.60,
    },
    "orb_retest": {
        "range_end": "09:44",
        "active_from": "09:50",
        "active_to": "12:00",
        "sl_pct": 0.20,
        "tgt_pct": 0.40,
        "max_hold_mins": 45,
        "min_range_pct": 0.15,
        "max_range_pct": 0.80,
        "retest_pct": 0.10,
        "max_signals": 1,
    },
    "vwap_reversal": {
        "sl_pct": 0.15,
        "tgt_pct": 0.25,
        "max_hold_mins": 25,
        "active_from": "10:00",
        "active_to": "13:30",
        "max_signals": 1,
        "cooldown_mins": 20,
        "vwap_touch_pct": 0.05,
        "min_bounce_pct": 0.08,
        "confirm_bars": 2,
    },
    "ema_crossover": {
        "fast_period": 8,
        "slow_period": 21,
        "sl_pct": 0.15,
        "tgt_pct": 0.22,
        "max_hold_mins": 25,
        "active_from": "10:00",
        "active_to": "14:00",
        "max_signals": 2,
        "cooldown_mins": 20,
        "min_spread_pct": 0.03,
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
    note: str = ""


@dataclass
class ActiveTrade:
    signal: Signal
    entry_time: str
    max_hold_until: str
    posted_msg_id: int = 0


@dataclass
class ClosedTrade:
    signal: Signal
    exit_premium: float
    exit_time: str
    exit_reason: str
    pnl: float
    won: bool


# ── Telegram ────────────────────────────────────────────────────────

def send_telegram(text: str, dry_run: bool = False) -> int:
    if dry_run:
        print(f"[DRY-RUN] Would post:\n{text}\n")
        return 0
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHANNEL,
        "text": text,
        "parse_mode": "HTML",
    }, timeout=10)
    if resp.ok:
        msg_id = resp.json().get("result", {}).get("message_id", 0)
        log.info(f"Posted signal (msg_id={msg_id})")
        return msg_id
    else:
        log.error(f"Telegram send failed: {resp.text}")
        return 0


def send_update(text: str, reply_to: int = 0, dry_run: bool = False):
    if dry_run:
        print(f"[DRY-RUN] Update: {text}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHANNEL, "text": text, "parse_mode": "HTML"}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    requests.post(url, data=data, timeout=10)


# ── Helpers ─────────────────────────────────────────────────────────

def round_strike(price, step):
    return round(price / step) * step


def now_ist():
    return datetime.now(IST)


def now_time_str():
    return now_ist().strftime("%H:%M")


def time_to_mins(t: str) -> int:
    h, m = int(t[:2]), int(t[3:5])
    return h * 60 + m


def next_expiry(ref_date: date, weekday: int) -> date:
    days_ahead = (weekday - ref_date.weekday()) % 7
    if days_ahead == 0 and ref_date.weekday() == weekday:
        return ref_date
    return ref_date + timedelta(days=days_ahead if days_ahead > 0 else 7)


def fetch_spot_candles(udata: UpstoxData, index_key: str) -> list[dict]:
    today = now_ist().date()
    candles = []
    try:
        d = udata._get(f"/v3/historical-candle/intraday/{index_key}/minutes/1")
        candles += d.get("data", {}).get("candles", [])
    except Exception as e:
        log.error(f"Spot candle fetch failed: {e}")

    rows = []
    seen = set()
    for c in candles:
        dt, tm = c[0][:10], c[0][11:16]
        if dt != today.isoformat() or tm in seen:
            continue
        seen.add(tm)
        rows.append({"time": tm, "open": c[1], "high": c[2],
                      "low": c[3], "close": c[4], "volume": c[5]})
    rows.sort(key=lambda r: r["time"])
    return rows


def fetch_option_ltp(udata: UpstoxData, inst_key: str) -> float:
    try:
        d = udata._get("/v2/market-quote/ltp", params={"instrument_key": inst_key})
        data = d.get("data", {})
        for v in data.values():
            return v.get("last_price", 0)
    except Exception as e:
        log.error(f"LTP fetch failed for {inst_key}: {e}")
    return 0


def resample_5min(candles_1min: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for c in candles_1min:
        t = c["time"]
        h, m = int(t[:2]), int(t[3:5])
        bar_m = (m // 5) * 5
        groups[f"{h:02d}:{bar_m:02d}"].append(c)
    result = []
    for bar_time in sorted(groups):
        grp = groups[bar_time]
        result.append({
            "time": bar_time,
            "open": grp[0]["open"],
            "high": max(c["high"] for c in grp),
            "low": min(c["low"] for c in grp),
            "close": grp[-1]["close"],
            "volume": sum(c["volume"] for c in grp),
        })
    return result


def calc_ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    mult = 2 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * mult + ema[-1] * (1 - mult))
    return ema


def calc_vwap(candles: list[dict]) -> list[float]:
    cum_vol = 0.0
    cum_tp_vol = 0.0
    vwap = []
    for c in candles:
        tp = (c["high"] + c["low"] + c["close"]) / 3
        vol = max(c["volume"], 1)
        cum_tp_vol += tp * vol
        cum_vol += vol
        vwap.append(cum_tp_vol / cum_vol)
    return vwap


# ── Signal detection ──────────────────────────────────────────────

def detect_momentum_scalp(candles_5min: list[dict], sym: str,
                          master: dict, expiry: date, udata: UpstoxData,
                          already_fired: int) -> list[Signal]:
    p = STRATEGY_PARAMS["momentum_scalp"]
    inst = ALL_INSTRUMENTS[sym]
    step = inst["step"]

    remaining = p["max_signals"] - already_fired
    if remaining <= 0:
        return []

    now_t = now_time_str()
    if now_t < p["active_from"] or now_t > p["active_to"]:
        return []

    has_volume = any(c["volume"] > 0 for c in candles_5min[:20])
    avg_vol = sum(c["volume"] for c in candles_5min) / len(candles_5min) if candles_5min else 0

    c = candles_5min[-1] if candles_5min else None
    if not c or c["open"] == 0:
        return []

    body = abs(c["close"] - c["open"])
    body_pct = body / c["open"] * 100
    if body_pct < p["body_pct"]:
        return []

    if has_volume and avg_vol > 0 and c["volume"] < avg_vol * p["vol_mult"]:
        return []

    rng = c["high"] - c["low"]
    if rng == 0:
        return []

    cp_min = p["close_position_min"]
    direction = None
    if c["close"] > c["open"]:
        if (c["close"] - c["low"]) / rng > cp_min:
            direction = "bullish"
    else:
        if (c["high"] - c["close"]) / rng > cp_min:
            direction = "bearish"

    if not direction:
        return []

    strike = round_strike(c["close"], step)
    opt_type = "CE" if direction == "bullish" else "PE"
    opt_key = master.get((sym, expiry, strike, opt_type))
    if not opt_key:
        return []

    entry = fetch_option_ltp(udata, opt_key)
    if entry <= 5:
        return []

    return [Signal(
        time=now_time_str(), strategy="momentum_scalp", action="BUY",
        index=sym, strike=strike, option_type=opt_type,
        entry_premium=entry,
        sl_premium=round(entry * (1 - p["sl_pct"]), 1),
        tgt_premium=round(entry * (1 + p["tgt_pct"]), 1),
        spot_at_entry=c["close"], instrument_key=opt_key,
        note=f"body={body_pct:.2f}%",
    )]


def detect_orb_retest(candles_5min: list[dict], sym: str,
                      master: dict, expiry: date, udata: UpstoxData,
                      already_fired: int) -> list[Signal]:
    p = STRATEGY_PARAMS["orb_retest"]
    inst = ALL_INSTRUMENTS[sym]
    step = inst["step"]

    if already_fired >= p["max_signals"]:
        return []

    now_t = now_time_str()
    if now_t < p["active_from"] or now_t > p["active_to"]:
        return []

    range_bars = [c for c in candles_5min if c["time"] <= p["range_end"]]
    if not range_bars:
        return []

    orb_high = max(c["high"] for c in range_bars)
    orb_low = min(c["low"] for c in range_bars)
    orb_range = orb_high - orb_low
    if orb_range <= 0:
        return []

    range_pct = orb_range / orb_low * 100
    if range_pct < p["min_range_pct"] or range_pct > p["max_range_pct"]:
        return []

    recent = [c for c in candles_5min if c["time"] >= p["active_from"]]
    if not recent:
        return []

    latest = recent[-1]
    retest_zone = orb_range * p["retest_pct"] / 100 if p["retest_pct"] > 1 else orb_range * p["retest_pct"]

    broke_above = any(c["high"] > orb_high for c in recent[:-1])
    broke_below = any(c["low"] < orb_low for c in recent[:-1])

    direction = None
    if broke_above and abs(latest["low"] - orb_high) <= retest_zone and latest["close"] > orb_high:
        direction = "bullish"
    elif broke_below and abs(latest["high"] - orb_low) <= retest_zone and latest["close"] < orb_low:
        direction = "bearish"

    if not direction:
        return []

    strike = round_strike(latest["close"], step)
    opt_type = "CE" if direction == "bullish" else "PE"
    opt_key = master.get((sym, expiry, strike, opt_type))
    if not opt_key:
        return []

    entry = fetch_option_ltp(udata, opt_key)
    if entry <= 5:
        return []

    return [Signal(
        time=now_time_str(), strategy="orb_retest", action="BUY",
        index=sym, strike=strike, option_type=opt_type,
        entry_premium=entry,
        sl_premium=round(entry * (1 - p["sl_pct"]), 1),
        tgt_premium=round(entry * (1 + p["tgt_pct"]), 1),
        spot_at_entry=latest["close"], instrument_key=opt_key,
        note=f"range={orb_low:.0f}-{orb_high:.0f}",
    )]


def detect_vwap_reversal(candles_5min: list[dict], sym: str,
                         master: dict, expiry: date, udata: UpstoxData,
                         already_fired: int) -> list[Signal]:
    p = STRATEGY_PARAMS["vwap_reversal"]
    inst = ALL_INSTRUMENTS[sym]
    step = inst["step"]

    if already_fired >= p["max_signals"]:
        return []

    now_t = now_time_str()
    if now_t < p["active_from"] or now_t > p["active_to"]:
        return []

    confirm = p.get("confirm_bars", 2)
    if len(candles_5min) < 6 + confirm:
        return []

    vwap = calc_vwap(candles_5min)
    cur = candles_5min[-1]
    cur_vwap = vwap[-1]
    touch_zone = cur_vwap * p["vwap_touch_pct"] / 100
    min_bounce = cur_vwap * p.get("min_bounce_pct", 0.08) / 100

    touch_bar = candles_5min[-1 - confirm]
    touched_below = touch_bar["low"] <= cur_vwap + touch_zone and touch_bar["low"] >= cur_vwap - touch_zone
    touched_above = touch_bar["high"] >= cur_vwap - touch_zone and touch_bar["high"] <= cur_vwap + touch_zone

    confirm_ok_bull = all(
        candles_5min[-1 - confirm + j]["close"] > candles_5min[-1 - confirm + j]["open"]
        for j in range(1, confirm + 1)
    )
    confirm_ok_bear = all(
        candles_5min[-1 - confirm + j]["close"] < candles_5min[-1 - confirm + j]["open"]
        for j in range(1, confirm + 1)
    )

    direction = None
    bounce_size = abs(cur["close"] - cur_vwap)
    if touched_below and confirm_ok_bull and cur["close"] > cur_vwap and bounce_size >= min_bounce:
        direction = "bullish"
    elif touched_above and confirm_ok_bear and cur["close"] < cur_vwap and bounce_size >= min_bounce:
        direction = "bearish"

    if not direction:
        return []

    strike = round_strike(cur["close"], step)
    opt_type = "CE" if direction == "bullish" else "PE"
    opt_key = master.get((sym, expiry, strike, opt_type))
    if not opt_key:
        return []

    entry = fetch_option_ltp(udata, opt_key)
    if entry <= 5:
        return []

    return [Signal(
        time=now_time_str(), strategy="vwap_reversal", action="BUY",
        index=sym, strike=strike, option_type=opt_type,
        entry_premium=entry,
        sl_premium=round(entry * (1 - p["sl_pct"]), 1),
        tgt_premium=round(entry * (1 + p["tgt_pct"]), 1),
        spot_at_entry=cur["close"], instrument_key=opt_key,
        note=f"VWAP={cur_vwap:.1f}",
    )]


def detect_ema_crossover(candles_5min: list[dict], sym: str,
                         master: dict, expiry: date, udata: UpstoxData,
                         already_fired: int) -> list[Signal]:
    p = STRATEGY_PARAMS["ema_crossover"]
    inst = ALL_INSTRUMENTS[sym]
    step = inst["step"]

    if already_fired >= p["max_signals"]:
        return []

    now_t = now_time_str()
    if now_t < p["active_from"] or now_t > p["active_to"]:
        return []

    if len(candles_5min) < p["slow_period"] + 2:
        return []

    closes = [c["close"] for c in candles_5min]
    fast_ema = calc_ema(closes, p["fast_period"])
    slow_ema = calc_ema(closes, p["slow_period"])

    cur_fast, cur_slow = fast_ema[-1], slow_ema[-1]
    prev_fast, prev_slow = fast_ema[-2], slow_ema[-2]

    spread_pct = abs(cur_fast - cur_slow) / cur_slow * 100
    if spread_pct < p["min_spread_pct"]:
        return []

    direction = None
    if prev_fast <= prev_slow and cur_fast > cur_slow:
        direction = "bullish"
    elif prev_fast >= prev_slow and cur_fast < cur_slow:
        direction = "bearish"

    if not direction:
        return []

    cur = candles_5min[-1]
    strike = round_strike(cur["close"], step)
    opt_type = "CE" if direction == "bullish" else "PE"
    opt_key = master.get((sym, expiry, strike, opt_type))
    if not opt_key:
        return []

    entry = fetch_option_ltp(udata, opt_key)
    if entry <= 5:
        return []

    return [Signal(
        time=now_time_str(), strategy="ema_crossover", action="BUY",
        index=sym, strike=strike, option_type=opt_type,
        entry_premium=entry,
        sl_premium=round(entry * (1 - p["sl_pct"]), 1),
        tgt_premium=round(entry * (1 + p["tgt_pct"]), 1),
        spot_at_entry=cur["close"], instrument_key=opt_key,
        note=f"EMA8={cur_fast:.1f} x EMA21={cur_slow:.1f}",
    )]


def detect_short_strangle(candles_5min: list[dict], sym: str,
                          master: dict, expiry: date, udata: UpstoxData,
                          prev_close: float | None, already_fired: int) -> list[Signal]:
    p = STRATEGY_PARAMS["short_strangle"]
    inst = ALL_INSTRUMENTS[sym]
    step = inst["step"]

    if already_fired >= p["max_signals"]:
        return []

    now_t = now_time_str()
    if now_t != p["entry_time"]:
        return []

    today = now_ist().date()
    dte = (expiry - today).days
    if dte < p["min_dte"]:
        return []

    if not candles_5min:
        return []

    spot = candles_5min[-1]["close"]
    if prev_close and prev_close > 0:
        gap_pct = abs(spot - prev_close) / prev_close * 100
        if gap_pct > p["max_gap_pct"]:
            return []

    atm = round_strike(spot, step)
    ce_strike = atm + step * p["otm_steps"]
    pe_strike = atm - step * p["otm_steps"]

    ce_key = master.get((sym, expiry, ce_strike, "CE"))
    pe_key = master.get((sym, expiry, pe_strike, "PE"))
    if not ce_key or not pe_key:
        return []

    ce_premium = fetch_option_ltp(udata, ce_key)
    pe_premium = fetch_option_ltp(udata, pe_key)
    if ce_premium <= 5 or pe_premium <= 5:
        return []

    combined = ce_premium + pe_premium
    return [Signal(
        time=now_time_str(), strategy="short_strangle", action="SELL",
        index=sym, strike=ce_strike, option_type="CE",
        entry_premium=ce_premium,
        sl_premium=round(combined * (1 + p["sl_pct"]), 1),
        tgt_premium=round(combined * (1 - p["tgt_pct"]), 1),
        spot_at_entry=spot, instrument_key=ce_key,
        paired_strike=pe_strike, paired_type="PE",
        paired_premium=pe_premium, paired_instrument_key=pe_key,
        note=f"DTE={dte}",
    )]


def detect_day_end_sell(candles_5min: list[dict], sym: str,
                        master: dict, expiry: date, udata: UpstoxData,
                        already_fired: int) -> list[Signal]:
    p = STRATEGY_PARAMS["day_end_sell"]
    inst = ALL_INSTRUMENTS[sym]
    step = inst["step"]

    if already_fired >= p["max_signals"]:
        return []

    now_t = now_time_str()
    if now_t != p["entry_time"]:
        return []

    if not candles_5min:
        return []

    day_open = candles_5min[0]["open"]
    current = candles_5min[-1]["close"]
    if day_open == 0:
        return []

    trend_pct = (current - day_open) / day_open * 100
    if abs(trend_pct) < p["min_trend_pct"]:
        return []

    atm = round_strike(current, step)
    if trend_pct > 0:
        opt_type = "PE"
        sell_strike = atm - step * p["otm_steps"]
    else:
        opt_type = "CE"
        sell_strike = atm + step * p["otm_steps"]

    opt_key = master.get((sym, expiry, sell_strike, opt_type))
    if not opt_key:
        return []

    entry = fetch_option_ltp(udata, opt_key)
    if entry <= 3:
        return []

    sl_premium = round(entry * p["sl_mult"], 1)
    return [Signal(
        time=now_time_str(), strategy="day_end_sell", action="SELL",
        index=sym, strike=sell_strike, option_type=opt_type,
        entry_premium=entry,
        sl_premium=sl_premium,
        tgt_premium=0,
        spot_at_entry=current, instrument_key=opt_key,
        note=f"trend={trend_pct:+.2f}%",
    )]


# ── Signal formatting (human channel style) ────────────────────────

def format_signal(s: Signal) -> str:
    if s.action == "BUY":
        return (
            f"BUY {s.index} {s.strike:.0f} {s.option_type} @ ₹{s.entry_premium:.1f}\n"
            f"SL ₹{s.sl_premium:.1f} | TGT ₹{s.tgt_premium:.1f}"
        )
    elif s.paired_instrument_key:
        combined = s.entry_premium + s.paired_premium
        return (
            f"SELL {s.index} {s.strike:.0f} {s.option_type} @ ₹{s.entry_premium:.1f}\n"
            f"SELL {s.index} {s.paired_strike:.0f} {s.paired_type} @ ₹{s.paired_premium:.1f}\n"
            f"Combined ₹{combined:.0f} | SL ₹{s.sl_premium:.0f} | TGT ₹{s.tgt_premium:.0f}"
        )
    else:
        return (
            f"SELL {s.index} {s.strike:.0f} {s.option_type} @ ₹{s.entry_premium:.1f}\n"
            f"SL ₹{s.sl_premium:.1f} | TGT hold to expiry"
        )


def format_exit(s: Signal, exit_premium: float, reason: str, pnl: float, won: bool) -> str:
    icon = "✅ TARGET HIT" if reason == "TGT hit" else ("❌ SL HIT" if reason == "SL hit" else "🔔 TIME EXIT")
    result = "PROFIT" if won else "LOSS"

    if s.paired_instrument_key:
        return (
            f"{icon}\n"
            f"{s.index} {s.strike:.0f} {s.option_type} + {s.paired_strike:.0f} {s.paired_type}\n"
            f"Entry ₹{s.entry_premium + s.paired_premium:.0f} → Exit ₹{exit_premium:.1f}\n"
            f"{result}: ₹{pnl:+,.0f}"
        )
    else:
        return (
            f"{icon}\n"
            f"{s.index} {s.strike:.0f} {s.option_type}\n"
            f"Entry ₹{s.entry_premium:.1f} → Exit ₹{exit_premium:.1f}\n"
            f"{result}: ₹{pnl:+,.0f}"
        )


def format_day_summary(closed_trades: list[ClosedTrade], today: date) -> str:
    if not closed_trades:
        return f"📋 <b>Day Summary — {today}</b>\nNo signals today."

    total = len(closed_trades)
    wins = sum(1 for t in closed_trades if t.won)
    losses = total - wins
    wr = wins / total * 100 if total else 0
    total_pnl = sum(t.pnl for t in closed_trades)

    lines = [
        f"📋 <b>Day Summary — {today}</b>",
        f"",
        f"Signals: {total} | Wins: {wins} | Losses: {losses}",
        f"Win Rate: {wr:.0f}%",
        f"Total P&L: ₹{total_pnl:+,.0f}",
        f"",
        f"<b>All Trades:</b>",
    ]

    for i, t in enumerate(closed_trades, 1):
        s = t.signal
        icon = "✅" if t.won else "❌"
        if s.paired_instrument_key:
            desc = f"{s.action} {s.index} {s.strike:.0f}{s.option_type}+{s.paired_strike:.0f}{s.paired_type}"
        else:
            desc = f"{s.action} {s.index} {s.strike:.0f} {s.option_type}"
        lines.append(
            f"{icon} {desc} @ ₹{s.entry_premium:.0f} → ₹{t.exit_premium:.0f} "
            f"| ₹{t.pnl:+,.0f} ({t.exit_reason})"
        )

    return "\n".join(lines)


# ── Trade tracking ─────────────────────────────────────────────────

def check_exits(active_trades: list[ActiveTrade], udata: UpstoxData,
                dry_run: bool, closed_trades: list[ClosedTrade]) -> list[ActiveTrade]:
    still_active = []
    now_t = now_time_str()

    for trade in active_trades:
        s = trade.signal
        inst = ALL_INSTRUMENTS.get(s.index, {})
        lot = inst.get("lot_size", 75)
        exited = False
        reason = ""
        pnl = 0.0
        current = 0.0

        if s.paired_instrument_key:
            ce_ltp = fetch_option_ltp(udata, s.instrument_key)
            pe_ltp = fetch_option_ltp(udata, s.paired_instrument_key)
            current = ce_ltp + pe_ltp
            combined_entry = s.entry_premium + s.paired_premium
            pnl_per_lot = (combined_entry - current) - 2 * IMPACT_COST

            time_exit = STRATEGY_PARAMS[s.strategy].get("time_exit", "15:15")
            if current >= s.sl_premium:
                reason, pnl, exited = "SL hit", pnl_per_lot * lot, True
            elif current <= s.tgt_premium:
                reason, pnl, exited = "TGT hit", pnl_per_lot * lot, True
            elif now_t >= time_exit:
                reason, pnl, exited = "time exit", pnl_per_lot * lot, True
        elif s.action == "BUY":
            current = fetch_option_ltp(udata, s.instrument_key)
            pnl_per_lot = (current - s.entry_premium) - 2 * IMPACT_COST
            max_hold = STRATEGY_PARAMS[s.strategy].get("max_hold_mins", 15)
            entry_mins = time_to_mins(trade.entry_time)
            now_mins = time_to_mins(now_t)

            if current <= s.sl_premium:
                reason, pnl, exited = "SL hit", pnl_per_lot * lot, True
            elif current >= s.tgt_premium:
                reason, pnl, exited = "TGT hit", pnl_per_lot * lot, True
            elif now_mins - entry_mins >= max_hold:
                reason, pnl, exited = "time exit", pnl_per_lot * lot, True
        else:
            current = fetch_option_ltp(udata, s.instrument_key)
            pnl_per_lot = (s.entry_premium - current) - 2 * IMPACT_COST
            time_exit = STRATEGY_PARAMS[s.strategy].get("time_exit", "15:10")

            if current >= s.sl_premium:
                reason, pnl, exited = "SL hit", pnl_per_lot * lot, True
            elif now_t >= time_exit:
                reason, pnl, exited = "time exit", pnl_per_lot * lot, True

        if exited:
            won = pnl > 0
            msg = format_exit(s, current, reason, pnl, won)
            send_update(msg, trade.posted_msg_id, dry_run)
            closed_trades.append(ClosedTrade(
                signal=s, exit_premium=current, exit_time=now_t,
                exit_reason=reason, pnl=pnl, won=won,
            ))
            print(f"  [{now_t}] EXIT {s.index} {s.strike:.0f} {s.option_type} "
                  f"₹{pnl:+,.0f} ({reason})")
        else:
            still_active.append(trade)

    return still_active


# ── Main loop ──────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Live signal poster to Telegram")
    parser.add_argument("--dry-run", action="store_true", help="Print signals without posting")
    parser.add_argument("--no-stocks", action="store_true", help="Skip stock scanning")
    args = parser.parse_args()

    index_list = list(INDEXES.keys())
    stock_list = [] if args.no_stocks else list(STOCKS.keys())
    all_syms = index_list + stock_list

    if not args.dry_run and not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in env")
        sys.exit(1)

    print(f"Loading option master...")
    master = _build_option_master(None)
    print(f"  Master: {len(master)} entries")

    today = now_ist().date()
    expiries = {}
    for sym in all_syms:
        expiries[sym] = next_expiry(today, ALL_INSTRUMENTS[sym]["expiry_weekday"])

    print(f"\n{'='*60}")
    print(f"  LIVE SIGNAL POSTER")
    print(f"  Date: {today}")
    print(f"  Indexes: {', '.join(index_list)}")
    if stock_list:
        print(f"  Stocks: {', '.join(stock_list)}")
    print(f"  Strategies: {', '.join(STRATEGY_PARAMS.keys())}")
    print(f"  Mode: {'DRY RUN' if args.dry_run else 'LIVE POSTING'}")
    print(f"  Channel: {TELEGRAM_CHANNEL}")
    print(f"{'='*60}")
    print(f"  Waiting for Upstox token + market open...\n")

    udata = None
    prev_closes: dict[str, float] = {}
    active_trades: list[ActiveTrade] = []
    closed_trades: list[ClosedTrade] = []
    fired_counts: dict[str, dict[str, int]] = {sym: defaultdict(int) for sym in all_syms}
    last_5min_bar: dict[str, str] = {}
    bot_started = False
    scan_interval = 60

    while True:
        now = now_ist()
        t = now.strftime("%H:%M")

        if now.weekday() >= 5:
            print(f"  [{t}] Weekend — sleeping until Monday 08:50...")
            days_until_monday = 7 - now.weekday()
            next_monday = now.replace(hour=8, minute=50, second=0, microsecond=0) + timedelta(days=days_until_monday)
            time.sleep((next_monday - now).total_seconds())
            today = now_ist().date()
            for sym in all_syms:
                expiries[sym] = next_expiry(today, ALL_INSTRUMENTS[sym]["expiry_weekday"])
            fired_counts = {sym: defaultdict(int) for sym in all_syms}
            active_trades, closed_trades = [], []
            udata, bot_started = None, False
            continue

        if t < "09:15" or t > "15:30":
            if t > "15:30":
                summary = format_day_summary(closed_trades, today)
                send_telegram(summary, args.dry_run)
                print("\nMarket closed. Sleeping until tomorrow 08:50...")
                print(summary.replace("<b>", "").replace("</b>", ""))
                tomorrow = now.replace(hour=8, minute=50, second=0, microsecond=0) + timedelta(days=1)
                time.sleep((tomorrow - now).total_seconds())
                today = now_ist().date()
                for sym in all_syms:
                    expiries[sym] = next_expiry(today, ALL_INSTRUMENTS[sym]["expiry_weekday"])
                fired_counts = {sym: defaultdict(int) for sym in all_syms}
                active_trades, closed_trades = [], []
                udata, bot_started = None, False
                master = _build_option_master(None)
                continue
            time.sleep(30)
            continue

        if udata is None:
            try:
                udata = UpstoxData()
                print(f"  [{t}] Upstox token loaded (cached).")
            except Exception:
                try:
                    from src.broker.upstox_data import automated_login
                    print(f"  [{t}] Running automated Upstox login...")
                    udata = automated_login()
                    print(f"  [{t}] Upstox auto-login OK.")
                except Exception as e:
                    print(f"  [{t}] Upstox login failed: {e}")
                    time.sleep(60)
                    continue

            for sym in all_syms:
                inst = ALL_INSTRUMENTS[sym]
                try:
                    yesterday = today - timedelta(days=1)
                    d = udata._get(
                        f"/v3/historical-candle/{inst['key']}/minutes/1"
                        f"/{yesterday.isoformat()}/{yesterday.isoformat()}")
                    candles = d.get("data", {}).get("candles", [])
                    if candles:
                        prev_closes[sym] = candles[0][4]
                except Exception:
                    pass

        if not bot_started:
            bot_started = True
            syms_str = ", ".join(index_list)
            if stock_list:
                syms_str += f" + {len(stock_list)} stocks"
            header = f"📊 <b>Signal Bot Started</b>\n{syms_str} | {today}"
            header += f"\n6 strategies active"
            send_telegram(header, args.dry_run)

        active_trades = check_exits(active_trades, udata, args.dry_run, closed_trades)

        for sym in all_syms:
            inst = ALL_INSTRUMENTS[sym]
            expiry = expiries[sym]
            is_index = sym in INDEXES

            spot_candles = fetch_spot_candles(udata, inst["key"])
            if not spot_candles:
                continue

            candles_5min = resample_5min(spot_candles)
            if not candles_5min:
                continue

            current_bar = candles_5min[-1]["time"]
            bar_key = f"{sym}:{current_bar}"

            new_signals = []

            if bar_key != last_5min_bar.get(sym):
                last_5min_bar[sym] = bar_key

                new_signals += detect_momentum_scalp(
                    candles_5min, sym, master, expiry, udata,
                    fired_counts[sym]["momentum_scalp"])

                new_signals += detect_orb_retest(
                    candles_5min, sym, master, expiry, udata,
                    fired_counts[sym]["orb_retest"])

                new_signals += detect_vwap_reversal(
                    candles_5min, sym, master, expiry, udata,
                    fired_counts[sym]["vwap_reversal"])

                new_signals += detect_ema_crossover(
                    candles_5min, sym, master, expiry, udata,
                    fired_counts[sym]["ema_crossover"])

            if is_index:
                new_signals += detect_short_strangle(
                    candles_5min, sym, master, expiry, udata,
                    prev_closes.get(sym), fired_counts[sym]["short_strangle"])

                new_signals += detect_day_end_sell(
                    candles_5min, sym, master, expiry, udata,
                    fired_counts[sym]["day_end_sell"])

            for sig in new_signals:
                msg_text = format_signal(sig)
                msg_id = send_telegram(msg_text, args.dry_run)
                fired_counts[sym][sig.strategy] += 1

                active_trades.append(ActiveTrade(
                    signal=sig,
                    entry_time=sig.time,
                    max_hold_until="",
                    posted_msg_id=msg_id,
                ))

                print(f"  [{t}] {sym} {sig.strategy}: "
                      f"{sig.action} {sig.strike:.0f} {sig.option_type} @ ₹{sig.entry_premium:.1f}")

        time.sleep(scan_interval)


if __name__ == "__main__":
    main()

"""Live signal generator — runs during market hours, detects signals, posts to Telegram.

Uses the same 4 strategies from backtest_multi_strategy.py:
  1. Momentum Scalp — buy ATM on strong 5-min candle, quick exit
  2. ORB Retest — buy on breakout + retest confirmation
  3. Short Strangle — sell OTM CE+PE at 10:00
  4. Day-End Sell — sell OTM with trend at 14:00

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/live_signal_poster.py [--index all] [--dry-run]
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
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_SIGNAL_CHANNEL", "@nifty_signals_test")

INDEXES = {
    "NIFTY": {
        "key": "NSE_INDEX|Nifty 50",
        "step": 50,
        "lot_size": 75,
        "expiry_weekday": 1,  # Tuesday
    },
    "BANKNIFTY": {
        "key": "NSE_INDEX|Nifty Bank",
        "step": 100,
        "lot_size": 30,
        "expiry_weekday": 2,  # Wednesday
    },
}

STRATEGY_PARAMS = {
    "momentum_scalp": {
        "body_pct": 0.10,
        "vol_mult": 1.3,
        "sl_pct": 0.12,
        "tgt_pct": 0.18,
        "max_hold_mins": 15,
        "active_from": "09:25",
        "active_to": "14:00",
        "max_signals": 3,
        "cooldown_mins": 10,
        "close_position_min": 0.55,
    },
    "orb_retest": {
        "range_end": "09:44",
        "active_from": "09:50",
        "active_to": "12:00",
        "sl_pct": 0.20,
        "tgt_pct": 0.40,
        "max_hold_mins": 45,
        "min_range_pct": 0.12,
        "max_range_pct": 0.85,
        "retest_pct": 0.12,
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
    note: str = ""


@dataclass
class ActiveTrade:
    signal: Signal
    entry_time: str
    max_hold_until: str
    posted_msg_id: int = 0


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
    """Fetch today's 1-min spot candles."""
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
    """Get current LTP for an option."""
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


# ── Signal detection (reused from backtester) ───────────────────────

def detect_momentum_scalp(candles_5min: list[dict], index_name: str,
                          master: dict, expiry: date, udata: UpstoxData,
                          already_fired: int) -> list[Signal]:
    p = STRATEGY_PARAMS["momentum_scalp"]
    step = INDEXES[index_name]["step"]
    signals = []
    has_volume = any(c["volume"] > 0 for c in candles_5min[:20])

    remaining = p["max_signals"] - already_fired
    if remaining <= 0:
        return []

    now_t = now_time_str()
    if now_t < p["active_from"] or now_t > p["active_to"]:
        return []

    vol_sum, vol_count = 0.0, 0
    for c in candles_5min:
        vol_sum += c["volume"]
        vol_count += 1

    avg_vol = vol_sum / vol_count if vol_count > 0 else 0

    last_candle = candles_5min[-1] if candles_5min else None
    if not last_candle:
        return []

    c = last_candle
    body = abs(c["close"] - c["open"])
    if c["open"] == 0:
        return []
    body_pct = body / c["open"] * 100

    if body_pct < p["body_pct"]:
        return []

    if has_volume and avg_vol > 0:
        if c["volume"] < avg_vol * p["vol_mult"]:
            return []

    rng = c["high"] - c["low"]
    if rng == 0:
        return []

    cp_min = p.get("close_position_min", 0.55)
    direction = None
    if c["close"] > c["open"]:
        close_position = (c["close"] - c["low"]) / rng
        if close_position > cp_min:
            direction = "bullish"
    else:
        close_position = (c["high"] - c["close"]) / rng
        if close_position > cp_min:
            direction = "bearish"

    if not direction:
        return []

    strike = round_strike(c["close"], step)
    opt_type = "CE" if direction == "bullish" else "PE"
    opt_key = master.get((index_name, expiry, strike, opt_type))
    if not opt_key:
        return []

    entry = fetch_option_ltp(udata, opt_key)
    if entry <= 5:
        return []

    signals.append(Signal(
        time=now_time_str(), strategy="momentum_scalp", action="BUY",
        index=index_name, strike=strike, option_type=opt_type,
        entry_premium=entry,
        sl_premium=round(entry * (1 - p["sl_pct"]), 1),
        tgt_premium=round(entry * (1 + p["tgt_pct"]), 1),
        spot_at_entry=c["close"], instrument_key=opt_key,
        note=f"body={body_pct:.2f}%",
    ))
    return signals


def detect_orb_retest(candles_5min: list[dict], index_name: str,
                      master: dict, expiry: date, udata: UpstoxData,
                      already_fired: int) -> list[Signal]:
    p = STRATEGY_PARAMS["orb_retest"]
    step = INDEXES[index_name]["step"]

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

    direction = None
    broke_above = any(c["high"] > orb_high for c in recent[:-1])
    broke_below = any(c["low"] < orb_low for c in recent[:-1])

    if broke_above and abs(latest["low"] - orb_high) <= retest_zone and latest["close"] > orb_high:
        direction = "bullish"
    elif broke_below and abs(latest["high"] - orb_low) <= retest_zone and latest["close"] < orb_low:
        direction = "bearish"

    if not direction:
        return []

    strike = round_strike(latest["close"], step)
    opt_type = "CE" if direction == "bullish" else "PE"
    opt_key = master.get((index_name, expiry, strike, opt_type))
    if not opt_key:
        return []

    entry = fetch_option_ltp(udata, opt_key)
    if entry <= 5:
        return []

    return [Signal(
        time=now_time_str(), strategy="orb_retest", action="BUY",
        index=index_name, strike=strike, option_type=opt_type,
        entry_premium=entry,
        sl_premium=round(entry * (1 - p["sl_pct"]), 1),
        tgt_premium=round(entry * (1 + p["tgt_pct"]), 1),
        spot_at_entry=latest["close"], instrument_key=opt_key,
        note=f"range={orb_low:.0f}-{orb_high:.0f} retest@{now_time_str()}",
    )]


def detect_short_strangle(candles_5min: list[dict], index_name: str,
                          master: dict, expiry: date, udata: UpstoxData,
                          prev_close: float | None, already_fired: int) -> list[Signal]:
    p = STRATEGY_PARAMS["short_strangle"]

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

    step = INDEXES[index_name]["step"]
    atm = round_strike(spot, step)
    ce_strike = atm + step * p["otm_steps"]
    pe_strike = atm - step * p["otm_steps"]

    ce_key = master.get((index_name, expiry, ce_strike, "CE"))
    pe_key = master.get((index_name, expiry, pe_strike, "PE"))
    if not ce_key or not pe_key:
        return []

    ce_premium = fetch_option_ltp(udata, ce_key)
    pe_premium = fetch_option_ltp(udata, pe_key)
    if ce_premium <= 5 or pe_premium <= 5:
        return []

    combined = ce_premium + pe_premium
    return [Signal(
        time=now_time_str(), strategy="short_strangle", action="SELL",
        index=index_name, strike=ce_strike, option_type="CE",
        entry_premium=ce_premium,
        sl_premium=round(combined * (1 + p["sl_pct"]), 1),
        tgt_premium=round(combined * (1 - p["tgt_pct"]), 1),
        spot_at_entry=spot, instrument_key=ce_key,
        paired_strike=pe_strike, paired_type="PE",
        paired_premium=pe_premium, paired_instrument_key=pe_key,
        note=f"DTE={dte}",
    )]


def detect_day_end_sell(candles_5min: list[dict], index_name: str,
                        master: dict, expiry: date, udata: UpstoxData,
                        already_fired: int) -> list[Signal]:
    p = STRATEGY_PARAMS["day_end_sell"]

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

    step = INDEXES[index_name]["step"]
    atm = round_strike(current, step)

    if trend_pct > 0:
        opt_type = "PE"
        sell_strike = atm - step * p["otm_steps"]
    else:
        opt_type = "CE"
        sell_strike = atm + step * p["otm_steps"]

    opt_key = master.get((index_name, expiry, sell_strike, opt_type))
    if not opt_key:
        return []

    entry = fetch_option_ltp(udata, opt_key)
    if entry <= 3:
        return []

    sl_premium = round(entry * p["sl_mult"], 1)
    return [Signal(
        time=now_time_str(), strategy="day_end_sell", action="SELL",
        index=index_name, strike=sell_strike, option_type=opt_type,
        entry_premium=entry,
        sl_premium=sl_premium,
        tgt_premium=0,
        spot_at_entry=current, instrument_key=opt_key,
        note=f"sell {opt_type} ({'bullish' if trend_pct > 0 else 'bearish'} day) trend={trend_pct:+.2f}%",
    )]


# ── Signal formatting ──────────────────────────────────────────────

def format_signal(s: Signal) -> str:
    lot = INDEXES[s.index]["lot_size"]
    label = s.strategy.upper().replace("_", " ")

    if s.action == "BUY":
        return (
            f"🟢 <b>{label}</b>\n"
            f"BUY {s.index} {s.strike:.0f} {s.option_type} @ ₹{s.entry_premium:.1f}\n"
            f"SL: ₹{s.sl_premium:.1f} | TGT: ₹{s.tgt_premium:.1f}\n"
            f"Lot: {lot} | Max Hold: {STRATEGY_PARAMS[s.strategy].get('max_hold_mins', 60)}m\n"
            f"⏰ {s.time}"
        )
    elif s.paired_instrument_key:
        combined = s.entry_premium + s.paired_premium
        return (
            f"🔴 <b>{label}</b>\n"
            f"SELL {s.index} {s.strike:.0f} {s.option_type} @ ₹{s.entry_premium:.1f}\n"
            f"SELL {s.index} {s.paired_strike:.0f} {s.paired_type} @ ₹{s.paired_premium:.1f}\n"
            f"Combined: ₹{combined:.0f} | SL: ₹{s.sl_premium:.0f} | TGT: ₹{s.tgt_premium:.0f}\n"
            f"Lot: {lot} | Exit: {STRATEGY_PARAMS[s.strategy].get('time_exit', '15:15')}\n"
            f"⏰ {s.time}"
        )
    else:
        return (
            f"🔴 <b>{label}</b>\n"
            f"SELL {s.index} {s.strike:.0f} {s.option_type} @ ₹{s.entry_premium:.1f}\n"
            f"SL: ₹{s.sl_premium:.1f} | Hold to close\n"
            f"Lot: {lot} | Exit: {STRATEGY_PARAMS[s.strategy].get('time_exit', '15:10')}\n"
            f"⏰ {s.time}"
        )


def format_exit(s: Signal, exit_premium: float, reason: str, pnl: float, won: bool) -> str:
    icon = "✅" if won else "❌"
    return (
        f"{icon} <b>EXIT — {s.strategy.upper().replace('_', ' ')}</b>\n"
        f"{s.index} {s.strike:.0f} {s.option_type}\n"
        f"Entry: ₹{s.entry_premium:.1f} → Exit: ₹{exit_premium:.1f}\n"
        f"P&L: ₹{pnl:+,.0f} | {reason}\n"
        f"⏰ {now_time_str()}"
    )


# ── Trade tracking ─────────────────────────────────────────────────

def check_exits(active_trades: list[ActiveTrade], udata: UpstoxData,
                dry_run: bool) -> list[ActiveTrade]:
    still_active = []
    now_t = now_time_str()

    for trade in active_trades:
        s = trade.signal
        lot = INDEXES[s.index]["lot_size"]

        if s.paired_instrument_key:
            ce_ltp = fetch_option_ltp(udata, s.instrument_key)
            pe_ltp = fetch_option_ltp(udata, s.paired_instrument_key)
            current = ce_ltp + pe_ltp
            combined_entry = s.entry_premium + s.paired_premium
            pnl_per_lot = (combined_entry - current) - 2 * IMPACT_COST

            time_exit = STRATEGY_PARAMS[s.strategy].get("time_exit", "15:15")
            if current >= s.sl_premium:
                pnl = pnl_per_lot * lot
                msg = format_exit(s, current, "SL hit", pnl, False)
                send_update(msg, trade.posted_msg_id, dry_run)
                continue
            elif current <= s.tgt_premium or now_t >= time_exit:
                reason = "TGT" if current <= s.tgt_premium else "time exit"
                pnl = pnl_per_lot * lot
                msg = format_exit(s, current, reason, pnl, pnl > 0)
                send_update(msg, trade.posted_msg_id, dry_run)
                continue
        elif s.action == "BUY":
            current = fetch_option_ltp(udata, s.instrument_key)
            pnl_per_lot = (current - s.entry_premium) - 2 * IMPACT_COST
            max_hold = STRATEGY_PARAMS[s.strategy].get("max_hold_mins", 15)
            entry_mins = time_to_mins(trade.entry_time)
            now_mins = time_to_mins(now_t)

            if current <= s.sl_premium:
                pnl = pnl_per_lot * lot
                msg = format_exit(s, current, "SL hit", pnl, False)
                send_update(msg, trade.posted_msg_id, dry_run)
                continue
            elif current >= s.tgt_premium:
                pnl = pnl_per_lot * lot
                msg = format_exit(s, current, "TGT hit", pnl, True)
                send_update(msg, trade.posted_msg_id, dry_run)
                continue
            elif now_mins - entry_mins >= max_hold:
                pnl = pnl_per_lot * lot
                msg = format_exit(s, current, "time exit", pnl, pnl > 0)
                send_update(msg, trade.posted_msg_id, dry_run)
                continue
        else:
            current = fetch_option_ltp(udata, s.instrument_key)
            pnl_per_lot = (s.entry_premium - current) - 2 * IMPACT_COST
            time_exit = STRATEGY_PARAMS[s.strategy].get("time_exit", "15:10")

            if current >= s.sl_premium:
                pnl = pnl_per_lot * lot
                msg = format_exit(s, current, "SL hit", pnl, False)
                send_update(msg, trade.posted_msg_id, dry_run)
                continue
            elif now_t >= time_exit:
                pnl = pnl_per_lot * lot
                msg = format_exit(s, current, "time exit", pnl, pnl > 0)
                send_update(msg, trade.posted_msg_id, dry_run)
                continue

        still_active.append(trade)

    return still_active


# ── Main loop ──────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Live signal poster to Telegram")
    parser.add_argument("--index", default="NIFTY")
    parser.add_argument("--dry-run", action="store_true", help="Print signals without posting")
    args = parser.parse_args()

    index_arg = args.index.upper()
    if index_arg == "ALL":
        index_list = ["NIFTY", "BANKNIFTY"]
    else:
        index_list = [index_arg]

    if not args.dry_run and not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in env")
        sys.exit(1)

    print(f"Loading option master...")
    master = _build_option_master(None)
    print(f"  Master: {len(master)} entries")

    udata = UpstoxData()
    today = now_ist().date()

    prev_closes: dict[str, float] = {}
    for ix_name in index_list:
        idx = INDEXES[ix_name]
        try:
            yesterday = today - timedelta(days=1)
            d = udata._get(
                f"/v3/historical-candle/{idx['key']}/minutes/1"
                f"/{yesterday.isoformat()}/{yesterday.isoformat()}")
            candles = d.get("data", {}).get("candles", [])
            if candles:
                prev_closes[ix_name] = candles[0][4]
        except Exception:
            pass

    expiries = {}
    for ix_name in index_list:
        expiries[ix_name] = next_expiry(today, INDEXES[ix_name]["expiry_weekday"])

    active_trades: list[ActiveTrade] = []
    fired_counts: dict[str, dict[str, int]] = {ix: defaultdict(int) for ix in index_list}
    last_5min_bar: dict[str, str] = {}

    header = f"📊 <b>Signal Bot Started</b>\n{' + '.join(index_list)} | {today}"
    for ix in index_list:
        header += f"\n{ix} expiry: {expiries[ix]}"
    send_telegram(header, args.dry_run)

    print(f"\n{'='*50}")
    print(f"  LIVE SIGNAL POSTER — {' + '.join(index_list)}")
    print(f"  Date: {today}")
    for ix in index_list:
        print(f"  {ix} expiry: {expiries[ix]}")
    print(f"  Mode: {'DRY RUN' if args.dry_run else 'LIVE POSTING'}")
    print(f"  Channel: {TELEGRAM_CHANNEL}")
    print(f"{'='*50}\n")

    scan_interval = 60

    while True:
        now = now_ist()
        t = now.strftime("%H:%M")

        if t < "09:15" or t > "15:30":
            if t > "15:30":
                summary = f"📋 <b>Day Summary — {today}</b>\n"
                total_signals = sum(sum(v.values()) for v in fired_counts.values())
                summary += f"Total signals: {total_signals}"
                send_telegram(summary, args.dry_run)
                print("\nMarket closed. Exiting.")
                break
            time.sleep(30)
            continue

        active_trades = check_exits(active_trades, udata, args.dry_run)

        for ix_name in index_list:
            idx = INDEXES[ix_name]
            expiry = expiries[ix_name]

            spot_candles = fetch_spot_candles(udata, idx["key"])
            if not spot_candles:
                continue

            candles_5min = resample_5min(spot_candles)
            if not candles_5min:
                continue

            current_bar = candles_5min[-1]["time"]
            bar_key = f"{ix_name}:{current_bar}"

            new_signals = []

            if bar_key != last_5min_bar.get(ix_name):
                last_5min_bar[ix_name] = bar_key

                sigs = detect_momentum_scalp(
                    candles_5min, ix_name, master, expiry, udata,
                    fired_counts[ix_name]["momentum_scalp"])
                new_signals += sigs

                sigs = detect_orb_retest(
                    candles_5min, ix_name, master, expiry, udata,
                    fired_counts[ix_name]["orb_retest"])
                new_signals += sigs

            sigs = detect_short_strangle(
                candles_5min, ix_name, master, expiry, udata,
                prev_closes.get(ix_name), fired_counts[ix_name]["short_strangle"])
            new_signals += sigs

            sigs = detect_day_end_sell(
                candles_5min, ix_name, master, expiry, udata,
                fired_counts[ix_name]["day_end_sell"])
            new_signals += sigs

            for sig in new_signals:
                msg_text = format_signal(sig)
                msg_id = send_telegram(msg_text, args.dry_run)
                fired_counts[ix_name][sig.strategy] += 1

                max_hold = STRATEGY_PARAMS[sig.strategy].get("max_hold_mins", 60)
                active_trades.append(ActiveTrade(
                    signal=sig,
                    entry_time=sig.time,
                    max_hold_until="",
                    posted_msg_id=msg_id,
                ))

                print(f"  [{t}] {ix_name} {sig.strategy}: "
                      f"{sig.action} {sig.strike:.0f} {sig.option_type} @ ₹{sig.entry_premium:.1f}")

        time.sleep(scan_interval)


if __name__ == "__main__":
    main()

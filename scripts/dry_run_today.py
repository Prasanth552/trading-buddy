"""Replay today's candles through signal poster strategies (offline dry run).

Fetches today's 1-min candles from Upstox, resamples to 5-min, and walks
through each bar simulating what the live poster would have detected.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/dry_run_today.py [--index NIFTY]
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from src.broker.upstox_data import UpstoxData
from src.strategy.live_runner import _build_option_master
from scripts.live_signal_poster import (
    INDEXES, STRATEGY_PARAMS, Signal,
    resample_5min, round_strike, next_expiry,
    format_signal, fetch_option_ltp,
)

IST = ZoneInfo("Asia/Kolkata")


def fetch_todays_candles(udata: UpstoxData, index_key: str) -> list[dict]:
    from datetime import datetime
    today = datetime.now(IST).date()
    candles = []

    # Try intraday first
    try:
        d = udata._get(f"/v3/historical-candle/intraday/{index_key}/minutes/1")
        candles += d.get("data", {}).get("candles", [])
    except Exception:
        pass

    # Also try historical for today
    try:
        d = udata._get(
            f"/v3/historical-candle/{index_key}/1minute/{today.isoformat()}/{today.isoformat()}")
        candles += d.get("data", {}).get("candles", [])
    except Exception:
        pass

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


def simulate_momentum_scalp(candles_5min: list[dict], index_name: str,
                            master: dict, expiry: date, udata: UpstoxData) -> list[dict]:
    p = STRATEGY_PARAMS["momentum_scalp"]
    step = INDEXES[index_name]["step"]
    has_volume = any(c["volume"] > 0 for c in candles_5min[:20])
    results = []
    fired = 0

    for i, bar in enumerate(candles_5min):
        t = bar["time"]
        if t < p["active_from"] or t > p["active_to"]:
            continue
        if fired >= p["max_signals"]:
            break

        bars_so_far = candles_5min[:i+1]
        avg_vol = sum(c["volume"] for c in bars_so_far) / len(bars_so_far) if bars_so_far else 0

        body = abs(bar["close"] - bar["open"])
        if bar["open"] == 0:
            continue
        body_pct = body / bar["open"] * 100
        if body_pct < p["body_pct"]:
            continue

        if has_volume and avg_vol > 0 and bar["volume"] < avg_vol * p["vol_mult"]:
            continue

        rng = bar["high"] - bar["low"]
        if rng == 0:
            continue

        cp_min = p.get("close_position_min", 0.55)
        direction = None
        if bar["close"] > bar["open"]:
            if (bar["close"] - bar["low"]) / rng > cp_min:
                direction = "bullish"
        else:
            if (bar["high"] - bar["close"]) / rng > cp_min:
                direction = "bearish"

        if not direction:
            continue

        strike = round_strike(bar["close"], step)
        opt_type = "CE" if direction == "bullish" else "PE"
        opt_key = master.get((index_name, expiry, strike, opt_type))
        if not opt_key:
            continue

        entry = fetch_option_ltp(udata, opt_key)
        if entry <= 5:
            entry = bar["close"] * 0.01  # estimate

        sl = round(entry * (1 - p["sl_pct"]), 1)
        tgt = round(entry * (1 + p["tgt_pct"]), 1)

        results.append({
            "time": t, "strategy": "momentum_scalp", "direction": direction,
            "strike": strike, "type": opt_type, "entry": entry, "sl": sl, "tgt": tgt,
            "spot": bar["close"], "body_pct": body_pct,
        })
        fired += 1

    return results


def simulate_orb_retest(candles_5min: list[dict], index_name: str,
                        master: dict, expiry: date, udata: UpstoxData) -> list[dict]:
    p = STRATEGY_PARAMS["orb_retest"]
    step = INDEXES[index_name]["step"]
    results = []

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

    fired = 0
    for i, bar in enumerate(candles_5min):
        if bar["time"] < p["active_from"] or bar["time"] > p["active_to"]:
            continue
        if fired >= p["max_signals"]:
            break

        recent_before = [c for c in candles_5min[:i] if c["time"] >= p["active_from"]]
        retest_zone = orb_range * p["retest_pct"] / 100 if p["retest_pct"] > 1 else orb_range * p["retest_pct"]

        broke_above = any(c["high"] > orb_high for c in recent_before)
        broke_below = any(c["low"] < orb_low for c in recent_before)

        direction = None
        if broke_above and abs(bar["low"] - orb_high) <= retest_zone and bar["close"] > orb_high:
            direction = "bullish"
        elif broke_below and abs(bar["high"] - orb_low) <= retest_zone and bar["close"] < orb_low:
            direction = "bearish"

        if not direction:
            continue

        strike = round_strike(bar["close"], step)
        opt_type = "CE" if direction == "bullish" else "PE"

        entry = bar["close"] * 0.01
        sl = round(entry * (1 - p["sl_pct"]), 1)
        tgt = round(entry * (1 + p["tgt_pct"]), 1)

        results.append({
            "time": bar["time"], "strategy": "orb_retest", "direction": direction,
            "strike": strike, "type": opt_type, "entry": entry, "sl": sl, "tgt": tgt,
            "spot": bar["close"], "body_pct": 0,
        })
        fired += 1

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default="NIFTY")
    args = parser.parse_args()

    index_name = args.index.upper()
    idx = INDEXES[index_name]

    print("Loading Upstox data...")
    try:
        udata = UpstoxData()
    except Exception:
        from src.broker.upstox_data import automated_login
        udata = automated_login()

    print("Loading option master...")
    master = _build_option_master(None)

    from datetime import datetime
    today = datetime.now(IST).date()
    expiry = next_expiry(today, idx["expiry_weekday"])

    print(f"Fetching today's 1-min candles for {index_name}...")
    candles_1min = fetch_todays_candles(udata, idx["key"])
    if not candles_1min:
        print("No candles found for today!")
        return

    candles_5min = resample_5min(candles_1min)
    print(f"  {len(candles_1min)} 1-min candles → {len(candles_5min)} 5-min bars")
    print(f"  Range: {candles_1min[0]['time']} to {candles_1min[-1]['time']}")
    print(f"  Expiry: {expiry}")
    print()

    all_signals = []

    # Momentum Scalp
    ms_signals = simulate_momentum_scalp(candles_5min, index_name, master, expiry, udata)
    all_signals += ms_signals

    # ORB Retest
    orb_signals = simulate_orb_retest(candles_5min, index_name, master, expiry, udata)
    all_signals += orb_signals

    # Short Strangle — check if 10:00 bar exists
    p_ss = STRATEGY_PARAMS["short_strangle"]
    ss_bar = next((c for c in candles_5min if c["time"] == p_ss["entry_time"]), None)
    if ss_bar:
        spot = ss_bar["close"]
        step = idx["step"]
        atm = round_strike(spot, step)
        ce_strike = atm + step * p_ss["otm_steps"]
        pe_strike = atm - step * p_ss["otm_steps"]
        ce_key = master.get((index_name, expiry, ce_strike, "CE"))
        pe_key = master.get((index_name, expiry, pe_strike, "PE"))
        if ce_key and pe_key:
            ce_ltp = fetch_option_ltp(udata, ce_key)
            pe_ltp = fetch_option_ltp(udata, pe_key)
            if ce_ltp > 5 and pe_ltp > 5:
                dte = (expiry - today).days
                all_signals.append({
                    "time": "10:00", "strategy": "short_strangle", "direction": "neutral",
                    "strike": ce_strike, "type": f"CE+PE({pe_strike})",
                    "entry": ce_ltp + pe_ltp, "sl": round((ce_ltp + pe_ltp) * (1 + p_ss["sl_pct"]), 1),
                    "tgt": round((ce_ltp + pe_ltp) * (1 - p_ss["tgt_pct"]), 1),
                    "spot": spot, "body_pct": 0,
                })

    # Day End Sell — check if 14:00 bar exists
    p_de = STRATEGY_PARAMS["day_end_sell"]
    de_bar = next((c for c in candles_5min if c["time"] == p_de["entry_time"]), None)
    if de_bar and candles_5min:
        day_open = candles_5min[0]["open"]
        current = de_bar["close"]
        if day_open > 0:
            trend_pct = (current - day_open) / day_open * 100
            if abs(trend_pct) >= p_de["min_trend_pct"]:
                step = idx["step"]
                atm = round_strike(current, step)
                if trend_pct > 0:
                    opt_type, sell_strike = "PE", atm - step * p_de["otm_steps"]
                else:
                    opt_type, sell_strike = "CE", atm + step * p_de["otm_steps"]
                opt_key = master.get((index_name, expiry, sell_strike, opt_type))
                if opt_key:
                    entry = fetch_option_ltp(udata, opt_key)
                    if entry > 3:
                        all_signals.append({
                            "time": "14:00", "strategy": "day_end_sell",
                            "direction": "bullish" if trend_pct > 0 else "bearish",
                            "strike": sell_strike, "type": opt_type,
                            "entry": entry, "sl": round(entry * p_de["sl_mult"], 1),
                            "tgt": 0, "spot": current, "body_pct": trend_pct,
                        })

    all_signals.sort(key=lambda s: s["time"])

    print(f"{'='*60}")
    print(f"  DRY RUN RESULTS — {index_name} — {today}")
    print(f"{'='*60}")

    if not all_signals:
        print("\n  No signals detected today.\n")
    else:
        print(f"\n  {len(all_signals)} signal(s) detected:\n")
        for s in all_signals:
            action = "SELL" if "strangle" in s["strategy"] or "day_end" in s["strategy"] else "BUY"
            print(f"  [{s['time']}] {s['strategy']}")
            print(f"    {action} {index_name} {s['strike']:.0f} {s['type']} @ ₹{s['entry']:.1f}")
            print(f"    SL ₹{s['sl']:.1f} | TGT ₹{s['tgt']:.1f}")
            print(f"    Spot: {s['spot']:.1f} | {s['direction']}")
            print()


if __name__ == "__main__":
    main()

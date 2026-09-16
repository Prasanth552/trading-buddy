"""Replay today's candles through all signal poster strategies (offline dry run).

Fetches today's candles from Upstox for all instruments, walks through
each 5-min bar simulating what the live poster would have detected.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/dry_run_today.py [--no-stocks]
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from src.broker.upstox_data import UpstoxData
from src.strategy.live_runner import _build_option_master
from scripts.live_signal_poster import (
    INDEXES, STOCKS, ALL_INSTRUMENTS, STRATEGY_PARAMS,
    resample_5min, round_strike, next_expiry, calc_vwap, calc_ema,
    fetch_option_ltp,
)

IST = ZoneInfo("Asia/Kolkata")


def fetch_todays_candles(udata: UpstoxData, inst_key: str) -> list[dict]:
    today = datetime.now(IST).date()
    candles = []
    try:
        d = udata._get(f"/v3/historical-candle/intraday/{inst_key}/minutes/1")
        candles += d.get("data", {}).get("candles", [])
    except Exception:
        pass
    try:
        d = udata._get(
            f"/v3/historical-candle/{inst_key}/1minute/{today.isoformat()}/{today.isoformat()}")
        candles += d.get("data", {}).get("candles", [])
    except Exception:
        pass

    rows, seen = [], set()
    for c in candles:
        dt, tm = c[0][:10], c[0][11:16]
        if dt != today.isoformat() or tm in seen:
            continue
        seen.add(tm)
        rows.append({"time": tm, "open": c[1], "high": c[2],
                      "low": c[3], "close": c[4], "volume": c[5]})
    rows.sort(key=lambda r: r["time"])
    return rows


def sim_momentum_scalp(candles_5min, sym, p):
    inst = ALL_INSTRUMENTS[sym]
    step = inst["step"]
    has_volume = any(c["volume"] > 0 for c in candles_5min[:20])
    results, fired = [], 0

    for i, bar in enumerate(candles_5min):
        if bar["time"] < p["active_from"] or bar["time"] > p["active_to"] or fired >= p["max_signals"]:
            continue
        bars_so_far = candles_5min[:i+1]
        avg_vol = sum(c["volume"] for c in bars_so_far) / len(bars_so_far)
        body = abs(bar["close"] - bar["open"])
        if bar["open"] == 0: continue
        body_pct = body / bar["open"] * 100
        if body_pct < p["body_pct"]: continue
        if has_volume and avg_vol > 0 and bar["volume"] < avg_vol * p["vol_mult"]: continue
        rng = bar["high"] - bar["low"]
        if rng == 0: continue
        d = None
        if bar["close"] > bar["open"] and (bar["close"] - bar["low"]) / rng > p["close_position_min"]:
            d = "bullish"
        elif bar["close"] < bar["open"] and (bar["high"] - bar["close"]) / rng > p["close_position_min"]:
            d = "bearish"
        if not d: continue
        strike = round_strike(bar["close"], step)
        opt = "CE" if d == "bullish" else "PE"
        results.append({"time": bar["time"], "strategy": "momentum_scalp", "sym": sym,
                        "strike": strike, "type": opt, "direction": d, "spot": bar["close"],
                        "body_pct": body_pct, "action": "BUY"})
        fired += 1
    return results


def sim_orb_retest(candles_5min, sym, p):
    inst = ALL_INSTRUMENTS[sym]
    step = inst["step"]
    range_bars = [c for c in candles_5min if c["time"] <= p["range_end"]]
    if not range_bars: return []
    orb_high = max(c["high"] for c in range_bars)
    orb_low = min(c["low"] for c in range_bars)
    orb_range = orb_high - orb_low
    if orb_range <= 0: return []
    range_pct = orb_range / orb_low * 100
    if range_pct < p["min_range_pct"] or range_pct > p["max_range_pct"]: return []

    results, fired = [], 0
    for i, bar in enumerate(candles_5min):
        if bar["time"] < p["active_from"] or bar["time"] > p["active_to"] or fired >= p["max_signals"]:
            continue
        recent_before = [c for c in candles_5min[:i] if c["time"] >= p["active_from"]]
        retest_zone = orb_range * p["retest_pct"] / 100 if p["retest_pct"] > 1 else orb_range * p["retest_pct"]
        broke_above = any(c["high"] > orb_high for c in recent_before)
        broke_below = any(c["low"] < orb_low for c in recent_before)
        d = None
        if broke_above and abs(bar["low"] - orb_high) <= retest_zone and bar["close"] > orb_high:
            d = "bullish"
        elif broke_below and abs(bar["high"] - orb_low) <= retest_zone and bar["close"] < orb_low:
            d = "bearish"
        if not d: continue
        strike = round_strike(bar["close"], step)
        opt = "CE" if d == "bullish" else "PE"
        results.append({"time": bar["time"], "strategy": "orb_retest", "sym": sym,
                        "strike": strike, "type": opt, "direction": d, "spot": bar["close"],
                        "action": "BUY"})
        fired += 1
    return results


def sim_vwap_reversal(candles_5min, sym, p):
    inst = ALL_INSTRUMENTS[sym]
    step = inst["step"]
    if len(candles_5min) < 6: return []
    vwap = calc_vwap(candles_5min)
    results, fired = [], 0

    for i in range(2, len(candles_5min)):
        bar = candles_5min[i]
        prev = candles_5min[i-1]
        if bar["time"] < p["active_from"] or bar["time"] > p["active_to"] or fired >= p["max_signals"]:
            continue
        cur_vwap = vwap[i]
        touch_zone = cur_vwap * p["vwap_touch_pct"] / 100
        d = None
        if prev["low"] <= cur_vwap + touch_zone and bar["close"] > cur_vwap and bar["close"] > bar["open"]:
            d = "bullish"
        elif prev["high"] >= cur_vwap - touch_zone and bar["close"] < cur_vwap and bar["close"] < bar["open"]:
            d = "bearish"
        if not d: continue
        strike = round_strike(bar["close"], step)
        opt = "CE" if d == "bullish" else "PE"
        results.append({"time": bar["time"], "strategy": "vwap_reversal", "sym": sym,
                        "strike": strike, "type": opt, "direction": d, "spot": bar["close"],
                        "vwap": cur_vwap, "action": "BUY"})
        fired += 1
    return results


def sim_ema_crossover(candles_5min, sym, p):
    inst = ALL_INSTRUMENTS[sym]
    step = inst["step"]
    if len(candles_5min) < p["slow_period"] + 2: return []
    closes = [c["close"] for c in candles_5min]
    fast = calc_ema(closes, p["fast_period"])
    slow = calc_ema(closes, p["slow_period"])
    results, fired = [], 0

    for i in range(p["slow_period"] + 1, len(candles_5min)):
        bar = candles_5min[i]
        if bar["time"] < p["active_from"] or bar["time"] > p["active_to"] or fired >= p["max_signals"]:
            continue
        spread_pct = abs(fast[i] - slow[i]) / slow[i] * 100
        if spread_pct < p["min_spread_pct"]: continue
        d = None
        if fast[i-1] <= slow[i-1] and fast[i] > slow[i]:
            d = "bullish"
        elif fast[i-1] >= slow[i-1] and fast[i] < slow[i]:
            d = "bearish"
        if not d: continue
        strike = round_strike(bar["close"], step)
        opt = "CE" if d == "bullish" else "PE"
        results.append({"time": bar["time"], "strategy": "ema_crossover", "sym": sym,
                        "strike": strike, "type": opt, "direction": d, "spot": bar["close"],
                        "ema_fast": fast[i], "ema_slow": slow[i], "action": "BUY"})
        fired += 1
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-stocks", action="store_true")
    args = parser.parse_args()

    index_list = list(INDEXES.keys())
    stock_list = [] if args.no_stocks else list(STOCKS.keys())
    all_syms = index_list + stock_list

    print("Loading Upstox data...")
    try:
        udata = UpstoxData()
    except Exception:
        from src.broker.upstox_data import automated_login
        udata = automated_login()

    print("Loading option master...")
    master = _build_option_master(None)

    today = datetime.now(IST).date()
    all_signals = []

    for sym in all_syms:
        inst = ALL_INSTRUMENTS[sym]
        expiry = next_expiry(today, inst["expiry_weekday"])

        print(f"  Fetching {sym}...")
        candles_1min = fetch_todays_candles(udata, inst["key"])
        if not candles_1min:
            print(f"    No candles for {sym}")
            continue

        candles_5min = resample_5min(candles_1min)
        print(f"    {len(candles_1min)} 1m → {len(candles_5min)} 5m bars "
              f"({candles_1min[0]['time']}-{candles_1min[-1]['time']})")

        all_signals += sim_momentum_scalp(candles_5min, sym, STRATEGY_PARAMS["momentum_scalp"])
        all_signals += sim_orb_retest(candles_5min, sym, STRATEGY_PARAMS["orb_retest"])
        all_signals += sim_vwap_reversal(candles_5min, sym, STRATEGY_PARAMS["vwap_reversal"])
        all_signals += sim_ema_crossover(candles_5min, sym, STRATEGY_PARAMS["ema_crossover"])

        is_index = sym in INDEXES
        if is_index:
            p_ss = STRATEGY_PARAMS["short_strangle"]
            ss_bar = next((c for c in candles_5min if c["time"] == p_ss["entry_time"]), None)
            if ss_bar:
                spot = ss_bar["close"]
                step = inst["step"]
                atm = round_strike(spot, step)
                ce_strike = atm + step * p_ss["otm_steps"]
                pe_strike = atm - step * p_ss["otm_steps"]
                ce_key = master.get((sym, expiry, ce_strike, "CE"))
                pe_key = master.get((sym, expiry, pe_strike, "PE"))
                if ce_key and pe_key:
                    ce_ltp = fetch_option_ltp(udata, ce_key)
                    pe_ltp = fetch_option_ltp(udata, pe_key)
                    if ce_ltp > 5 and pe_ltp > 5:
                        all_signals.append({
                            "time": "10:00", "strategy": "short_strangle", "sym": sym,
                            "strike": ce_strike, "type": f"CE+PE({pe_strike})",
                            "entry": ce_ltp + pe_ltp, "direction": "neutral",
                            "spot": spot, "action": "SELL",
                        })

            p_de = STRATEGY_PARAMS["day_end_sell"]
            de_bar = next((c for c in candles_5min if c["time"] == p_de["entry_time"]), None)
            if de_bar and candles_5min:
                day_open = candles_5min[0]["open"]
                current = de_bar["close"]
                if day_open > 0:
                    trend_pct = (current - day_open) / day_open * 100
                    if abs(trend_pct) >= p_de["min_trend_pct"]:
                        step = inst["step"]
                        atm = round_strike(current, step)
                        if trend_pct > 0:
                            opt_type, sell_strike = "PE", atm - step * p_de["otm_steps"]
                        else:
                            opt_type, sell_strike = "CE", atm + step * p_de["otm_steps"]
                        all_signals.append({
                            "time": "14:00", "strategy": "day_end_sell", "sym": sym,
                            "strike": sell_strike, "type": opt_type,
                            "direction": "bullish" if trend_pct > 0 else "bearish",
                            "spot": current, "trend_pct": trend_pct, "action": "SELL",
                        })

    all_signals.sort(key=lambda s: s["time"])

    print(f"\n{'='*60}")
    print(f"  DRY RUN RESULTS — {today}")
    print(f"  Instruments: {', '.join(all_syms)}")
    print(f"{'='*60}")

    if not all_signals:
        print("\n  No signals detected today.\n")
    else:
        print(f"\n  {len(all_signals)} signal(s) detected:\n")
        by_strategy = defaultdict(int)
        for s in all_signals:
            by_strategy[s["strategy"]] += 1
            print(f"  [{s['time']}] {s['sym']} — {s['strategy']}")
            print(f"    {s['action']} {s['sym']} {s['strike']:.0f} {s['type']}")
            print(f"    Spot: {s['spot']:.1f} | {s['direction']}")
            if "vwap" in s: print(f"    VWAP: {s['vwap']:.1f}")
            if "ema_fast" in s: print(f"    EMA8={s['ema_fast']:.1f} x EMA21={s['ema_slow']:.1f}")
            print()

        print(f"  --- By strategy ---")
        for strat, count in sorted(by_strategy.items(), key=lambda x: -x[1]):
            print(f"    {strat}: {count}")
        print()


if __name__ == "__main__":
    main()

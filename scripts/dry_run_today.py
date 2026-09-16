"""Replay today's candles through signal strategies with P&L simulation.

For each detected signal, simulates exit using subsequent 5-min bars:
- BUY options: tracks spot movement, estimates option premium change
- Checks SL, TGT, and max hold time exits

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
    fetch_option_ltp, time_to_mins,
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


def simulate_exit(signal: dict, candles_5min: list[dict], params: dict) -> dict:
    """Simulate trade exit using subsequent spot candles.

    For BUY CE: option gains when spot goes up, loses when spot goes down.
    For BUY PE: option gains when spot goes down, loses when spot goes up.
    We estimate option premium change as ~0.5 * spot_change (ATM delta ≈ 0.5).
    """
    entry_time = signal["time"]
    entry_spot = signal["spot"]
    strategy = signal["strategy"]
    p = params[strategy]

    sl_pct = p.get("sl_pct", 0.15)
    tgt_pct = p.get("tgt_pct", 0.20)
    max_hold = p.get("max_hold_mins", p.get("time_exit", 20))

    if isinstance(max_hold, str):
        max_hold_mins = time_to_mins(max_hold) - time_to_mins(entry_time)
    else:
        max_hold_mins = max_hold

    is_ce = "CE" in signal.get("type", "CE")
    is_sell = signal.get("action", "BUY") == "SELL"

    entry_idx = None
    for i, c in enumerate(candles_5min):
        if c["time"] >= entry_time:
            entry_idx = i
            break
    if entry_idx is None:
        return {"exit_reason": "no_data", "pnl_pct": 0, "exit_time": entry_time}

    for j in range(entry_idx + 1, len(candles_5min)):
        bar = candles_5min[j]
        elapsed = time_to_mins(bar["time"]) - time_to_mins(entry_time)

        spot_change_pct = (bar["close"] - entry_spot) / entry_spot * 100

        if is_sell:
            if is_ce:
                option_pnl_pct = -spot_change_pct * 3.0
            else:
                option_pnl_pct = spot_change_pct * 3.0
        else:
            if is_ce:
                option_pnl_pct = spot_change_pct * 3.0
            else:
                option_pnl_pct = -spot_change_pct * 3.0

        high_spot_pct = (bar["high"] - entry_spot) / entry_spot * 100
        low_spot_pct = (bar["low"] - entry_spot) / entry_spot * 100

        if not is_sell:
            if is_ce:
                best_pnl = high_spot_pct * 3.0
                worst_pnl = low_spot_pct * 3.0
            else:
                best_pnl = -low_spot_pct * 3.0
                worst_pnl = -high_spot_pct * 3.0
        else:
            if is_ce:
                best_pnl = -low_spot_pct * 3.0
                worst_pnl = -high_spot_pct * 3.0
            else:
                best_pnl = high_spot_pct * 3.0
                worst_pnl = low_spot_pct * 3.0

        if worst_pnl <= -sl_pct * 100:
            return {"exit_reason": "SL", "pnl_pct": -sl_pct * 100, "exit_time": bar["time"],
                    "hold_mins": elapsed}

        if best_pnl >= tgt_pct * 100:
            return {"exit_reason": "TGT", "pnl_pct": tgt_pct * 100, "exit_time": bar["time"],
                    "hold_mins": elapsed}

        if elapsed >= max_hold_mins:
            return {"exit_reason": "TIME", "pnl_pct": option_pnl_pct, "exit_time": bar["time"],
                    "hold_mins": elapsed}

    last = candles_5min[-1]
    spot_change_pct = (last["close"] - entry_spot) / entry_spot * 100
    if not is_sell:
        final_pnl = (spot_change_pct * 3.0) if is_ce else (-spot_change_pct * 3.0)
    else:
        final_pnl = (-spot_change_pct * 3.0) if is_ce else (spot_change_pct * 3.0)

    return {"exit_reason": "EOD", "pnl_pct": final_pnl, "exit_time": last["time"],
            "hold_mins": time_to_mins(last["time"]) - time_to_mins(entry_time)}


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
                        "action": "BUY"})
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
    confirm = p.get("confirm_bars", 2)
    if len(candles_5min) < 6 + confirm: return []
    vwap = calc_vwap(candles_5min)
    results, fired = [], 0

    for i in range(confirm + 2, len(candles_5min)):
        bar = candles_5min[i]
        if bar["time"] < p["active_from"] or bar["time"] > p["active_to"] or fired >= p["max_signals"]:
            continue
        cur_vwap = vwap[i]
        touch_zone = cur_vwap * p["vwap_touch_pct"] / 100
        min_bounce = cur_vwap * p.get("min_bounce_pct", 0.08) / 100

        touch_bar = candles_5min[i - confirm]
        touched_below = touch_bar["low"] <= cur_vwap + touch_zone and touch_bar["low"] >= cur_vwap - touch_zone
        touched_above = touch_bar["high"] >= cur_vwap - touch_zone and touch_bar["high"] <= cur_vwap + touch_zone

        confirm_ok_bull = all(
            candles_5min[i - confirm + j]["close"] > candles_5min[i - confirm + j]["open"]
            for j in range(1, confirm + 1)
        )
        confirm_ok_bear = all(
            candles_5min[i - confirm + j]["close"] < candles_5min[i - confirm + j]["open"]
            for j in range(1, confirm + 1)
        )

        bounce_size = abs(bar["close"] - cur_vwap)
        d = None
        if touched_below and confirm_ok_bull and bar["close"] > cur_vwap and bounce_size >= min_bounce:
            d = "bullish"
        elif touched_above and confirm_ok_bear and bar["close"] < cur_vwap and bounce_size >= min_bounce:
            d = "bearish"
        if not d: continue
        strike = round_strike(bar["close"], step)
        opt = "CE" if d == "bullish" else "PE"
        results.append({"time": bar["time"], "strategy": "vwap_reversal", "sym": sym,
                        "strike": strike, "type": opt, "direction": d, "spot": bar["close"],
                        "action": "BUY"})
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
                        "action": "BUY"})
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

    # Fetch all candles first
    candle_data: dict[str, list[dict]] = {}
    candle_5m_data: dict[str, list[dict]] = {}

    for sym in all_syms:
        inst = ALL_INSTRUMENTS[sym]
        print(f"  Fetching {sym}...")
        candles_1min = fetch_todays_candles(udata, inst["key"])
        if not candles_1min:
            print(f"    No candles")
            continue
        candles_5min = resample_5min(candles_1min)
        candle_data[sym] = candles_1min
        candle_5m_data[sym] = candles_5min
        print(f"    {len(candles_1min)} 1m → {len(candles_5min)} 5m "
              f"({candles_1min[0]['time']}-{candles_1min[-1]['time']})")

    # Detect signals
    all_signals = []
    for sym in all_syms:
        if sym not in candle_5m_data:
            continue
        candles_5min = candle_5m_data[sym]
        inst = ALL_INSTRUMENTS[sym]
        expiry = next_expiry(today, inst["expiry_weekday"])

        all_signals += sim_momentum_scalp(candles_5min, sym, STRATEGY_PARAMS["momentum_scalp"])
        all_signals += sim_orb_retest(candles_5min, sym, STRATEGY_PARAMS["orb_retest"])
        all_signals += sim_vwap_reversal(candles_5min, sym, STRATEGY_PARAMS["vwap_reversal"])
        all_signals += sim_ema_crossover(candles_5min, sym, STRATEGY_PARAMS["ema_crossover"])

        if sym in INDEXES:
            p_ss = STRATEGY_PARAMS["short_strangle"]
            ss_bar = next((c for c in candles_5min if c["time"] == p_ss["entry_time"]), None)
            if ss_bar:
                spot = ss_bar["close"]
                step = inst["step"]
                atm = round_strike(spot, step)
                ce_strike = atm + step * p_ss["otm_steps"]
                pe_strike = atm - step * p_ss["otm_steps"]
                all_signals.append({
                    "time": "10:00", "strategy": "short_strangle", "sym": sym,
                    "strike": ce_strike, "type": f"CE",
                    "direction": "neutral", "spot": spot, "action": "SELL",
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
                            "spot": current, "action": "SELL",
                        })

    # Simulate exits for each signal
    for sig in all_signals:
        sym = sig["sym"]
        if sym in candle_5m_data:
            exit_result = simulate_exit(sig, candle_5m_data[sym], STRATEGY_PARAMS)
            sig.update(exit_result)

    all_signals.sort(key=lambda s: s["time"])

    # Print results
    print(f"\n{'='*70}")
    print(f"  DRY RUN WITH P&L — {today}")
    print(f"  Instruments: {', '.join(all_syms)}")
    print(f"{'='*70}")

    if not all_signals:
        print("\n  No signals detected.\n")
        return

    wins = [s for s in all_signals if s.get("pnl_pct", 0) > 0]
    losses = [s for s in all_signals if s.get("pnl_pct", 0) <= 0]
    total = len(all_signals)
    wr = len(wins) / total * 100 if total else 0
    avg_win = sum(s["pnl_pct"] for s in wins) / len(wins) if wins else 0
    avg_loss = sum(s["pnl_pct"] for s in losses) / len(losses) if losses else 0

    print(f"\n  SUMMARY: {total} signals | {len(wins)} wins | {len(losses)} losses | WR: {wr:.0f}%")
    print(f"  Avg win: {avg_win:+.1f}% | Avg loss: {avg_loss:+.1f}%")
    print()

    # By strategy
    by_strat = defaultdict(lambda: {"total": 0, "wins": 0, "pnl_sum": 0.0})
    for s in all_signals:
        st = by_strat[s["strategy"]]
        st["total"] += 1
        if s.get("pnl_pct", 0) > 0: st["wins"] += 1
        st["pnl_sum"] += s.get("pnl_pct", 0)

    print(f"  {'Strategy':<20} {'Signals':>8} {'Wins':>6} {'WR':>6} {'Total P&L%':>10}")
    print(f"  {'-'*50}")
    for strat, st in sorted(by_strat.items(), key=lambda x: -x[1]["pnl_sum"]):
        wr_s = st["wins"] / st["total"] * 100 if st["total"] else 0
        print(f"  {strat:<20} {st['total']:>8} {st['wins']:>6} {wr_s:>5.0f}% {st['pnl_sum']:>+9.1f}%")
    print()

    # Detailed signals
    print(f"  {'Time':<7} {'Sym':<12} {'Strategy':<18} {'Action':<5} {'Strike':>8} {'Type':<4} "
          f"{'Exit':>5} {'P&L%':>7} {'Hold':>5}")
    print(f"  {'-'*75}")
    for s in all_signals:
        icon = "✅" if s.get("pnl_pct", 0) > 0 else "❌"
        exit_r = s.get("exit_reason", "?")[:4]
        pnl = s.get("pnl_pct", 0)
        hold = s.get("hold_mins", 0)
        print(f"  {icon} {s['time']:<5} {s['sym']:<12} {s['strategy']:<18} {s['action']:<5} "
              f"{s['strike']:>7.0f} {s['type']:<4} {exit_r:>5} {pnl:>+6.1f}% {hold:>4}m")

    print()


if __name__ == "__main__":
    main()

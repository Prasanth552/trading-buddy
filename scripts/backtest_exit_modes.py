#!/usr/bin/env python3
"""
Backtest OEH + OEL for 13 Oct 2026 — compare exit modes:
  A) Current floors (₹500→₹1000→...)
  B) Static ₹2K target (exit at +₹2000)
  C) Hybrid: floors + ₹2K hard ceiling
  D) Hybrid: floors + ₹3K hard ceiling

Reuses proven logic from backtest_oeh_oel_today.py.
"""

import asyncio
import sys
import os
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo(config.TIMEZONE)
TODAY = datetime(2026, 10, 13, tzinfo=IST).date()

OEH_CAPITAL = 300000
OEL_CAPITAL = 300000
MAX_SL = 5000
FLOOR_LEVELS = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
OEH_TOLERANCE = 0.05
OEH_MIN_DROP_PCT = 0.3
OEL_TOLERANCE = 0.05
OEL_MIN_RISE_PCT = 0.3
SL_PCT = 0.30

MAX_ENTRIES_PER_SYMBOL = 3
OEH_TIME_CUTOFF = dtime(13, 0)
OEL_TIME_CUTOFF = dtime(14, 0)
OEL_MAX_ENTRIES = 1
RESCAN_INTERVAL_MINS = 15


def get_stepped_floor(net_pnl):
    locked = 0
    for lvl in FLOOR_LEVELS:
        if net_pnl >= lvl:
            locked = lvl
        else:
            break
    return locked


def simulate_trade(sym, strike, otype, entry_time, lot_qty, option_candles,
                   exit_mode="floors", target_amount=2000):
    """
    exit_mode:
      "floors"  — current floor system
      "static"  — exit at target_amount profit, no floors
      "hybrid"  — floors + hard ceiling at target_amount
    """
    key = (sym, strike, otype)
    candles = option_candles.get(key, [])

    # Find entry price
    entry_price = None
    for c in candles:
        ts = c.get("ts") or c.get("date") or c.get("timestamp")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if hasattr(ts, "astimezone"):
            ts = ts.astimezone(IST)
        ct = ts.replace(tzinfo=None) if ts.tzinfo else ts
        et = datetime.combine(TODAY, entry_time)
        if ct <= et:
            entry_price = c["close"]
        else:
            break

    if not entry_price or entry_price <= 0:
        return None

    sl_price = entry_price * (1 - SL_PCT)
    sl_cap = entry_price - (MAX_SL / lot_qty)
    sl = max(sl_price, sl_cap)

    # Get candles after entry
    candles_after = []
    for c in candles:
        ts = c.get("ts") or c.get("date") or c.get("timestamp")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if hasattr(ts, "astimezone"):
            ts = ts.astimezone(IST)
        ct = ts.replace(tzinfo=None) if ts.tzinfo else ts
        et = datetime.combine(TODAY, entry_time)
        if ct >= et:
            candles_after.append({"time": ct, **{k: c[k] for k in ("open", "high", "low", "close")}})

    if not candles_after:
        return None

    peak_pnl = 0
    locked_floor = 0
    exit_price = None
    exit_time = None
    exit_reason = None
    is_entry_candle = True

    for c in candles_after:
        current_high_pnl = (c["high"] - entry_price) * lot_qty

        # 1) SL hit
        if c["low"] <= sl:
            exit_price = sl
            exit_time = c["time"]
            exit_reason = "max_loss_cap"
            break

        # 2) Static target — exit immediately when profit hits target
        if exit_mode == "static":
            target_price = entry_price + (target_amount / lot_qty)
            if c["high"] >= target_price:
                exit_price = target_price
                exit_time = c["time"]
                exit_reason = f"target_{target_amount}"
                break

        # 3) Hybrid ceiling — exit immediately when profit hits ceiling
        if exit_mode == "hybrid":
            ceiling_price = entry_price + (target_amount / lot_qty)
            if c["high"] >= ceiling_price:
                exit_price = ceiling_price
                exit_time = c["time"]
                exit_reason = f"ceiling_{target_amount}"
                break

        # 4) Floor exit (floors or hybrid, not on entry candle)
        if exit_mode in ("floors", "hybrid") and not is_entry_candle and locked_floor > 0:
            current_low_pnl = (c["low"] - entry_price) * lot_qty
            if current_low_pnl <= locked_floor:
                exit_price = entry_price + (locked_floor / lot_qty)
                exit_time = c["time"]
                exit_reason = f"floor_{locked_floor}"
                break

        # Update peak and floor
        peak_pnl = max(peak_pnl, current_high_pnl)
        if exit_mode in ("floors", "hybrid"):
            new_floor = get_stepped_floor(peak_pnl)
            if new_floor > locked_floor:
                locked_floor = new_floor

        is_entry_candle = False

    if exit_price is None:
        last = candles_after[-1] if candles_after else None
        if last:
            last_time = last["time"]
            if hasattr(last_time, "hour") and last_time.hour >= 15 and last_time.minute >= 20:
                exit_price = last["close"]
                exit_time = last["time"]
                exit_reason = "eod_close"
            else:
                exit_price = last["close"]
                exit_time = last["time"]
                exit_reason = "STILL_OPEN"

    if exit_price is None:
        return None

    gross_pnl = (exit_price - entry_price) * lot_qty
    charges = max(100, abs(gross_pnl) * 0.02) if exit_reason != "STILL_OPEN" else 0
    net_pnl = gross_pnl - charges

    return {
        "symbol": f"{sym} {int(strike)} {otype}",
        "entry_price": round(entry_price, 2),
        "exit_price": round(exit_price, 2),
        "qty": lot_qty,
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(net_pnl, 2),
        "peak_pnl": round(peak_pnl, 2),
        "locked_floor": locked_floor,
        "entry_time": entry_time.strftime("%H:%M"),
        "exit_time": exit_time.strftime("%H:%M") if hasattr(exit_time, "strftime") else str(exit_time),
        "exit_reason": exit_reason,
    }


async def main():
    token = load_cached_token()
    if not token:
        print("ERROR: No Upstox token.")
        return

    ud = UpstoxData(access_token=token)
    master = ud._load_master()
    print(f"Loaded {len(master)} instruments")

    import re as _re
    eq_keys = {}
    lot_sizes = {}
    fo_symbols = set()
    for inst in master:
        seg = inst.get("segment")
        if seg == "NSE_EQ":
            tsym = (inst.get("trading_symbol") or "").upper()
            if tsym:
                eq_keys[tsym] = inst.get("instrument_key")
        elif seg == "NSE_FO":
            _base = _re.match(r'^([A-Z&]+)', (inst.get("trading_symbol") or "").upper())
            if _base:
                fo_symbols.add(_base.group(1))
                _ls = int(inst.get("lot_size") or 0)
                if _ls > 0:
                    lot_sizes[_base.group(1)] = _ls

    universe = sorted(fo_symbols & set(eq_keys.keys()))
    print(f"F&O universe: {len(universe)} stocks")

    option_keys = defaultdict(lambda: defaultdict(dict))
    strike_steps = {}
    sym_strikes = defaultdict(set)
    for inst in master:
        if inst.get("segment") != "NSE_FO":
            continue
        tsym = (inst.get("trading_symbol") or "").upper()
        if not tsym:
            continue
        otype = inst.get("instrument_type", "").upper()
        if otype not in ("CE", "PE"):
            continue
        strike = float(inst.get("strike_price") or 0)
        if strike <= 0:
            continue
        asset_sym = (inst.get("asset_symbol") or inst.get("name") or "").upper()
        if not asset_sym:
            base = _re.match(r'^([A-Z&]+)', tsym)
            if base:
                asset_sym = base.group(1)
        if not asset_sym:
            continue
        ikey = inst.get("instrument_key")
        expiry = inst.get("expiry")
        sym_strikes[asset_sym].add(strike)
        existing = option_keys[asset_sym][strike].get(otype)
        if existing is None or (expiry and existing.get("expiry") and expiry < existing["expiry"]):
            option_keys[asset_sym][strike][otype] = {"key": ikey, "expiry": expiry}

    for sym, strikes in sym_strikes.items():
        ss = sorted(strikes)
        if len(ss) >= 2:
            step = min(ss[j+1] - ss[j] for j in range(min(20, len(ss)-1)))
            strike_steps[sym] = step
        else:
            strike_steps[sym] = 50

    # Fetch equity candles
    print("\n=== Fetching equity intraday data... ===")
    from_dt = datetime.combine(TODAY, dtime(9, 15))
    to_dt = datetime.combine(TODAY, dtime(15, 30))
    equity_candles = {}
    batch_size = 20
    syms_with_keys = [(sym, eq_keys[sym]) for sym in universe if sym in eq_keys]

    for i in range(0, len(syms_with_keys), batch_size):
        batch = syms_with_keys[i:i + batch_size]
        for sym, key in batch:
            try:
                candles = ud.historical_data(key, from_dt, to_dt, "1minute")
                if candles:
                    equity_candles[sym] = candles
            except Exception:
                pass
        pct = min(100, (i + batch_size) / len(syms_with_keys) * 100)
        print(f"  {min(i + batch_size, len(syms_with_keys))}/{len(syms_with_keys)} ({pct:.0f}%)", end="\r")
        await asyncio.sleep(0.3)
    print(f"\nGot candles for {len(equity_candles)} stocks")

    def get_open_price(sym):
        candles = equity_candles.get(sym, [])
        return candles[0]["open"] if candles else None

    def is_oeh_at_time(sym, scan_time):
        opn = get_open_price(sym)
        if not opn or opn <= 0:
            return None
        candles = equity_candles.get(sym, [])
        if not candles:
            return None
        max_high = opn
        ltp = opn
        for c in candles:
            ts = c.get("ts") or c.get("date") or c.get("timestamp")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if hasattr(ts, "astimezone"):
                ts = ts.astimezone(IST)
            ct = ts.replace(tzinfo=None) if ts.tzinfo else ts
            tt = datetime.combine(TODAY, scan_time)
            if ct > tt:
                break
            max_high = max(max_high, c["high"])
            ltp = c["close"]
        if max_high > opn + OEH_TOLERANCE:
            return None
        drop_pct = (opn - ltp) / opn * 100
        if drop_pct < OEH_MIN_DROP_PCT:
            return None
        return {"symbol": sym, "open": opn, "ltp": ltp, "drop_pct": drop_pct}

    def is_oel_at_time(sym, scan_time):
        opn = get_open_price(sym)
        if not opn or opn <= 0:
            return None
        candles = equity_candles.get(sym, [])
        if not candles:
            return None
        min_low = opn
        ltp = opn
        for c in candles:
            ts = c.get("ts") or c.get("date") or c.get("timestamp")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if hasattr(ts, "astimezone"):
                ts = ts.astimezone(IST)
            ct = ts.replace(tzinfo=None) if ts.tzinfo else ts
            tt = datetime.combine(TODAY, scan_time)
            if ct > tt:
                break
            min_low = min(min_low, c["low"])
            ltp = c["close"]
        if min_low < opn - OEL_TOLERANCE:
            return None
        rise_pct = (ltp - opn) / opn * 100
        if rise_pct < OEL_MIN_RISE_PCT:
            return None
        return {"symbol": sym, "open": opn, "ltp": ltp, "rise_pct": rise_pct}

    def find_atm_strike(sym, ltp, otype):
        step = strike_steps.get(sym, 50)
        atm = round(ltp / step) * step
        strikes = option_keys.get(sym, {})
        if not strikes:
            return None
        for offset in [0, step, -step, 2*step, -2*step]:
            s = atm + offset
            if s in strikes and otype in strikes[s]:
                return s
        valid = [s for s in strikes if otype in strikes[s]]
        if not valid:
            return None
        return min(valid, key=lambda s: abs(s - ltp))

    def get_option_price_at(sym, strike, otype, target_time, opt_candles):
        key = (sym, strike, otype)
        candles = opt_candles.get(key, [])
        best = None
        for c in candles:
            ts = c.get("ts") or c.get("date") or c.get("timestamp")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if hasattr(ts, "astimezone"):
                ts = ts.astimezone(IST)
            ct = ts.replace(tzinfo=None) if ts.tzinfo else ts
            tt = datetime.combine(TODAY, target_time)
            if ct <= tt:
                best = c["close"]
            else:
                break
        return best

    # Build scan times
    scan_times = []
    t = dtime(9, 20)
    while t <= dtime(15, 0):
        scan_times.append(t)
        mins = t.hour * 60 + t.minute + RESCAN_INTERVAL_MINS
        t = dtime(mins // 60, mins % 60)

    # Identify candidates
    print("\n=== Identifying candidates... ===")
    oeh_candidates_by_time = {}
    oel_candidates_by_time = {}
    all_option_syms = set()

    for st in scan_times:
        oeh_cands = []
        oel_cands = []
        for sym in universe:
            c = is_oeh_at_time(sym, st)
            if c:
                oeh_cands.append(c)
                s = find_atm_strike(sym, c["ltp"], "PE")
                if s is not None:
                    all_option_syms.add((sym, s, "PE"))
            c = is_oel_at_time(sym, st)
            if c:
                oel_cands.append(c)
                s = find_atm_strike(sym, c["ltp"], "CE")
                if s is not None:
                    all_option_syms.add((sym, s, "CE"))
        oeh_cands.sort(key=lambda x: x["drop_pct"], reverse=True)
        oel_cands.sort(key=lambda x: x["rise_pct"], reverse=True)
        oeh_candidates_by_time[st] = oeh_cands
        oel_candidates_by_time[st] = oel_cands

    # Fetch option candles
    print(f"=== Fetching option candles for {len(all_option_syms)} options... ===")
    opt_candles = {}
    opt_list = list(all_option_syms)
    for i in range(0, len(opt_list), batch_size):
        batch = opt_list[i:i + batch_size]
        for sym, strike, otype in batch:
            info = option_keys.get(sym, {}).get(strike, {}).get(otype)
            if not info:
                continue
            try:
                candles = ud.historical_data(info["key"], from_dt, to_dt, "1minute")
                if candles:
                    opt_candles[(sym, strike, otype)] = candles
            except Exception:
                pass
        pct = min(100, (i + batch_size) / len(opt_list) * 100)
        print(f"  {min(i + batch_size, len(opt_list))}/{len(opt_list)} ({pct:.0f}%)", end="\r")
        await asyncio.sleep(0.3)
    print(f"\nGot option candles for {len(opt_candles)} options")

    # ── Run backtest for each exit mode ──
    modes = [
        ("floors", "Current Floors", 0),
        ("static", "Static ₹2K Target", 2000),
        ("static", "Static ₹3K Target", 3000),
        ("hybrid", "Hybrid: Floors + ₹2K Ceiling", 2000),
        ("hybrid", "Hybrid: Floors + ₹3K Ceiling", 3000),
    ]

    results = []

    for mode_key, mode_name, target in modes:
        # OEH
        oeh_trades = []
        oeh_open_symbols = set()
        oeh_capital = OEH_CAPITAL
        oeh_sym_entry_count = defaultdict(int)
        oeh_sl_blocked = set()

        for st in scan_times:
            if st > OEH_TIME_CUTOFF:
                continue
            cands = oeh_candidates_by_time.get(st, [])
            if not cands:
                continue
            if not oeh_open_symbols:
                oeh_capital = OEH_CAPITAL
            for c in cands:
                sym = c["symbol"]
                if sym in oeh_open_symbols:
                    continue
                if oeh_sym_entry_count[sym] >= MAX_ENTRIES_PER_SYMBOL:
                    continue
                if sym in oeh_sl_blocked:
                    continue
                strike = find_atm_strike(sym, c["ltp"], "PE")
                if strike is None:
                    continue
                if (sym, strike, "PE") not in opt_candles:
                    continue
                lot = lot_sizes.get(sym, 1) * 2
                entry_premium = get_option_price_at(sym, strike, "PE", st, opt_candles)
                if not entry_premium or entry_premium < 2:
                    continue
                trade_cost = entry_premium * lot
                if trade_cost > oeh_capital:
                    continue
                trade = simulate_trade(sym, strike, "PE", st, lot, opt_candles,
                                       exit_mode=mode_key, target_amount=target)
                if trade is None:
                    continue
                trade["is_reentry"] = oeh_sym_entry_count[sym] > 0
                oeh_trades.append(trade)
                oeh_capital -= trade_cost
                oeh_open_symbols.add(sym)
                oeh_sym_entry_count[sym] += 1
                oeh_capital += trade["exit_price"] * lot
                oeh_open_symbols.discard(sym)
                if trade["exit_reason"] == "max_loss_cap":
                    oeh_sl_blocked.add(sym)

        # OEL
        oel_trades = []
        oel_open_symbols = set()
        oel_capital = OEL_CAPITAL
        oel_sym_entry_count = defaultdict(int)
        oel_sl_blocked = set()

        for st in scan_times:
            if st > OEL_TIME_CUTOFF:
                continue
            cands = oel_candidates_by_time.get(st, [])
            if not cands:
                continue
            if not oel_open_symbols:
                oel_capital = OEL_CAPITAL
            for c in cands:
                sym = c["symbol"]
                if sym in oel_open_symbols:
                    continue
                if oel_sym_entry_count[sym] >= OEL_MAX_ENTRIES:
                    continue
                if sym in oel_sl_blocked:
                    continue
                strike = find_atm_strike(sym, c["ltp"], "CE")
                if strike is None:
                    continue
                if (sym, strike, "CE") not in opt_candles:
                    continue
                lot = lot_sizes.get(sym, 1) * 2
                entry_premium = get_option_price_at(sym, strike, "CE", st, opt_candles)
                if not entry_premium or entry_premium < 2:
                    continue
                trade_cost = entry_premium * lot
                if trade_cost > oel_capital:
                    continue
                trade = simulate_trade(sym, strike, "CE", st, lot, opt_candles,
                                       exit_mode=mode_key, target_amount=target)
                if trade is None:
                    continue
                trade["is_reentry"] = oel_sym_entry_count[sym] > 0
                oel_trades.append(trade)
                oel_capital -= trade_cost
                oel_open_symbols.add(sym)
                oel_sym_entry_count[sym] += 1
                oel_capital += trade["exit_price"] * lot
                oel_open_symbols.discard(sym)
                if trade["exit_reason"] == "max_loss_cap":
                    oel_sl_blocked.add(sym)

        # Summarize
        def summarize(trades, label):
            closed = [t for t in trades if t["exit_reason"] != "STILL_OPEN"]
            still_open = [t for t in trades if t["exit_reason"] == "STILL_OPEN"]
            net = sum(t["net_pnl"] for t in closed)
            wins = sum(1 for t in closed if t["net_pnl"] > 0)
            losses = sum(1 for t in closed if t["net_pnl"] <= 0)
            sl_count = sum(1 for t in closed if t["exit_reason"] == "max_loss_cap")
            open_pnl = sum(t["net_pnl"] for t in still_open)
            return {
                "label": label, "trades": len(closed), "open": len(still_open),
                "net": net, "wins": wins, "losses": losses, "sl": sl_count,
                "open_pnl": open_pnl,
            }

        oeh_s = summarize(oeh_trades, "OEH")
        oel_s = summarize(oel_trades, "OEL")
        combined_net = oeh_s["net"] + oel_s["net"]
        combined_trades = oeh_s["trades"] + oel_s["trades"]

        results.append({
            "mode": mode_name,
            "oeh": oeh_s,
            "oel": oel_s,
            "total_net": combined_net,
            "total_trades": combined_trades,
        })

        # Print detail for this mode
        print(f"\n{'='*80}")
        print(f"  {mode_name}")
        print(f"{'='*80}")

        for label, trades in [("OEH", oeh_trades), ("OEL", oel_trades)]:
            closed = [t for t in trades if t["exit_reason"] != "STILL_OPEN"]
            still_open = [t for t in trades if t["exit_reason"] == "STILL_OPEN"]
            net = sum(t["net_pnl"] for t in closed)
            wins = sum(1 for t in closed if t["net_pnl"] > 0)
            losses = len(closed) - wins
            print(f"\n  {label}: {len(closed)} closed | Net: ₹{net:+,.0f} | W/L: {wins}/{losses}", end="")
            if still_open:
                op = sum(t["net_pnl"] for t in still_open)
                print(f" | {len(still_open)} open (₹{op:+,.0f})", end="")
            print()

            for t in trades:
                tag = "***" if t["exit_reason"] == "STILL_OPEN" else ""
                re = "RE" if t.get("is_reentry") else "  "
                print(f"    {t['symbol']:<25} {t['entry_price']:>7.2f}→{t['exit_price']:>7.2f} "
                      f"x{t['qty']:<5} {t['net_pnl']:>+8,.0f} peak={t['peak_pnl']:>+6,.0f} "
                      f"{t['entry_time']}→{t['exit_time']} {t['exit_reason']:<18} {re} {tag}")

    # ── COMPARISON TABLE ──
    print(f"\n{'='*80}")
    print(f"  COMPARISON — 13 Oct 2026")
    print(f"{'='*80}")
    print(f"\n{'Mode':<35} {'Trades':>6} {'OEH':>10} {'OEL':>10} {'TOTAL':>10}")
    print("-" * 75)
    for r in results:
        print(f"{r['mode']:<35} {r['total_trades']:>6} "
              f"₹{r['oeh']['net']:>+9,.0f} ₹{r['oel']['net']:>+9,.0f} "
              f"₹{r['total_net']:>+9,.0f}")
    print("-" * 75)

    best = max(results, key=lambda r: r["total_net"])
    print(f"\n✅ Best mode: {best['mode']} → ₹{best['total_net']:+,.0f}")


if __name__ == "__main__":
    asyncio.run(main())

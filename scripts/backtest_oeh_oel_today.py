#!/usr/bin/env python3
"""
Backtest OEH + OEL strategies for today (2026-10-12) with RE-ENTRY support.

Uses real intraday candle data to simulate:
- OEH: Open=High stocks → buy PEs, rescan every 60s with re-entries
- OEL: Open=Low stocks → buy CEs, rescan every 60s with re-entries

Mirrors live bot logic exactly: ATM strike, SL, floor levels.

GUARDRAILS (new):
1. Per-stock daily trade limit: max 3 entries per symbol per day
2. Time cutoff: no new OEH entries after 13:00 (theta trap)
3. Cooldown after SL: if a stock hits SL, block it for the rest of the day
"""

import asyncio
import json
import gzip
import sys
import os
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo(config.TIMEZONE)
TODAY = datetime(2026, 10, 12, tzinfo=IST).date()

# Strategy params (mirror channel_listener.py exactly)
OEH_CAPITAL = 300000
OEL_CAPITAL = 300000
MAX_SL = 5000
FLOOR_LEVELS = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
OEH_TOLERANCE = 0.05
OEH_MIN_DROP_PCT = 0.3
OEL_TOLERANCE = 0.05
OEL_MIN_RISE_PCT = 0.3
SL_PCT = 0.30  # 30% SL on premium

# GUARDRAIL PARAMS
MAX_ENTRIES_PER_SYMBOL = 3       # max trades per stock per day
OEH_TIME_CUTOFF = dtime(13, 0)  # no new OEH entries after 13:00
OEL_TIME_CUTOFF = dtime(14, 0)  # no new OEL entries after 14:00

RESCAN_INTERVAL_MINS = 15  # check every 15 min for re-entry test


def get_stepped_floor(net_pnl):
    """Return the highest locked floor level, or 0."""
    locked = 0
    for lvl in FLOOR_LEVELS:
        if net_pnl >= lvl:
            locked = lvl
        else:
            break
    return locked


async def main():
    token = load_cached_token()
    if not token:
        print("ERROR: No Upstox token. Run auto-login first.")
        return

    ud = UpstoxData(access_token=token)
    master = ud._load_master()
    print(f"Loaded {len(master)} instruments")

    # Build F&O universe + equity keys + lot sizes
    eq_keys = {}
    lot_sizes = {}
    fo_symbols = set()
    import re as _re
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

    # Build option key lookup: sym → {strike → {CE: key, PE: key}}
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
        expiry = inst.get("expiry")
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
        sym_strikes[asset_sym].add(strike)

        existing = option_keys[asset_sym][strike].get(otype)
        if existing is None or (expiry and existing.get("expiry") and expiry < existing["expiry"]):
            option_keys[asset_sym][strike][otype] = {"key": ikey, "expiry": expiry}

    # Compute strike steps
    for sym, strikes in sym_strikes.items():
        ss = sorted(strikes)
        if len(ss) >= 2:
            step = min(ss[j+1] - ss[j] for j in range(min(20, len(ss)-1)))
            strike_steps[sym] = step
        else:
            strike_steps[sym] = 50

    print(f"Option keys for {len(option_keys)} symbols, e.g. strike steps: "
          f"{dict(list(strike_steps.items())[:5])}")

    # ── Step 1: Fetch equity 1-min candles for all universe stocks (9:15–15:30) ──
    print("\n=== Fetching equity intraday data (this takes a minute)... ===")
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
        print(f"  Fetched {min(i + batch_size, len(syms_with_keys))}/{len(syms_with_keys)} ({pct:.0f}%)", end="\r")
        await asyncio.sleep(0.3)

    print(f"\nGot candles for {len(equity_candles)} stocks")

    # ── Helper: get equity price at a given time ──
    def get_price_at(sym, target_time):
        candles = equity_candles.get(sym, [])
        if not candles:
            return None
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
                best = c
            else:
                break
        return best

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

    # ── Build scan times ──
    scan_times = []
    t = dtime(9, 20)
    while t <= dtime(15, 0):
        scan_times.append(t)
        mins = t.hour * 60 + t.minute + RESCAN_INTERVAL_MINS
        t = dtime(mins // 60, mins % 60)

    # ── Helper: find ATM strike ──
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

    # ── Identify candidates at each scan time ──
    print("\n=== Identifying candidates at each scan time... ===")

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
        if oeh_cands or oel_cands:
            print(f"  {st.strftime('%H:%M')} — OEH: {len(oeh_cands)} candidates, OEL: {len(oel_cands)} candidates")

    # ── Fetch option 1-min candles ──
    print(f"\n=== Fetching option candles for {len(all_option_syms)} options... ===")
    option_candles = {}

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
                    option_candles[(sym, strike, otype)] = candles
            except Exception:
                pass
        pct = min(100, (i + batch_size) / len(opt_list) * 100)
        print(f"  Fetched {min(i + batch_size, len(opt_list))}/{len(opt_list)} ({pct:.0f}%)", end="\r")
        await asyncio.sleep(0.3)

    print(f"\nGot option candles for {len(option_candles)} options")

    # ── Helper: get option price at time ──
    def get_option_price_at(sym, strike, otype, target_time):
        key = (sym, strike, otype)
        candles = option_candles.get(key, [])
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

    def get_option_candles_after(sym, strike, otype, entry_time):
        key = (sym, strike, otype)
        candles = option_candles.get(key, [])
        result = []
        for c in candles:
            ts = c.get("ts") or c.get("date") or c.get("timestamp")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if hasattr(ts, "astimezone"):
                ts = ts.astimezone(IST)
            ct = ts.replace(tzinfo=None) if ts.tzinfo else ts
            et = datetime.combine(TODAY, entry_time)
            if ct >= et:
                result.append({"time": ct, **{k: c[k] for k in ("open", "high", "low", "close")}})
        return result

    # ── Simulate trade ──
    def simulate_trade(sym, strike, otype, entry_time, lot_qty):
        entry_price = get_option_price_at(sym, strike, otype, entry_time)
        if not entry_price or entry_price <= 0:
            return None

        sl_price = entry_price * (1 - SL_PCT)
        sl_cap = entry_price - (MAX_SL / lot_qty)
        sl = max(sl_price, sl_cap)

        candles_after = get_option_candles_after(sym, strike, otype, entry_time)
        if not candles_after:
            return None

        peak_pnl = 0
        locked_floor = 0
        exit_price = None
        exit_time = None
        exit_reason = None
        is_entry_candle = True

        for c in candles_after:
            current_low_pnl = (c["low"] - entry_price) * lot_qty
            current_high_pnl = (c["high"] - entry_price) * lot_qty
            current_close_pnl = (c["close"] - entry_price) * lot_qty

            # 1) SL hit
            if c["low"] <= sl:
                exit_price = sl
                exit_time = c["time"]
                exit_reason = "max_loss_cap"
                break

            # 2) Floor exit (not on entry candle)
            if not is_entry_candle and locked_floor > 0:
                if current_low_pnl <= locked_floor:
                    exit_price = c["close"]
                    exit_time = c["time"]
                    exit_reason = f"floor_{locked_floor}"
                    break

            # 3) Update peak and floor AFTER exit checks
            peak_pnl = max(peak_pnl, current_high_pnl)
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
                    return {
                        "symbol": f"{sym} {int(strike)} {otype}",
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(last["close"], 2),
                        "qty": lot_qty,
                        "gross_pnl": round((last["close"] - entry_price) * lot_qty, 2),
                        "net_pnl": round((last["close"] - entry_price) * lot_qty, 2),
                        "peak_pnl": round(peak_pnl, 2),
                        "locked_floor": locked_floor,
                        "entry_time": entry_time.strftime("%H:%M"),
                        "exit_time": last_time.strftime("%H:%M") if hasattr(last_time, "strftime") else str(last_time),
                        "exit_reason": "STILL_OPEN",
                    }

        if exit_price is None:
            return None

        gross_pnl = (exit_price - entry_price) * lot_qty
        charges = max(100, abs(gross_pnl) * 0.02)
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

    # ── Check data coverage ──
    sample_candles = next(iter(equity_candles.values()), [])
    if sample_candles:
        last_c = sample_candles[-1]
        last_ts = last_c.get("ts") or last_c.get("date") or last_c.get("timestamp")
        if isinstance(last_ts, str):
            last_ts = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        if hasattr(last_ts, "astimezone"):
            last_ts = last_ts.astimezone(IST)
        if last_ts.hour < 15 or (last_ts.hour == 15 and last_ts.minute < 20):
            print(f"\n⚠️  WARNING: Data only goes up to {last_ts.strftime('%H:%M')} IST")
            print(f"   Run this script AFTER 15:30 for accurate full-day results.")
            print(f"   Trades without SL/floor exit will be marked STILL_OPEN.\n")

    # ════════════════════════════════════════════════════════════════
    #   OEH BACKTEST
    # ════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("  OEH BACKTEST — 12 Oct 2026 — WITH GUARDRAILS")
    print(f"  Max {MAX_ENTRIES_PER_SYMBOL} entries/stock | Cutoff {OEH_TIME_CUTOFF.strftime('%H:%M')} | SL cooldown")
    print("=" * 80)

    oeh_trades = []
    oeh_open_symbols = set()
    oeh_capital = OEH_CAPITAL
    oeh_sym_entry_count = defaultdict(int)    # GUARDRAIL 1: per-stock count
    oeh_sl_blocked = set()                     # GUARDRAIL 3: SL cooldown

    for st in scan_times:
        # GUARDRAIL 2: time cutoff
        if st > OEH_TIME_CUTOFF:
            continue

        cands = oeh_candidates_by_time.get(st, [])
        if not cands:
            continue

        # Reset capital when no trades are open (same fix as live)
        if not oeh_open_symbols:
            oeh_capital = OEH_CAPITAL

        for c in cands:
            sym = c["symbol"]
            if sym in oeh_open_symbols:
                continue

            # GUARDRAIL 1: per-stock limit
            if oeh_sym_entry_count[sym] >= MAX_ENTRIES_PER_SYMBOL:
                continue

            # GUARDRAIL 3: SL cooldown
            if sym in oeh_sl_blocked:
                continue

            strike = find_atm_strike(sym, c["ltp"], "PE")
            if strike is None:
                continue
            if (sym, strike, "PE") not in option_candles:
                continue

            lot = lot_sizes.get(sym, 1) * 2
            entry_premium = get_option_price_at(sym, strike, "PE", st)
            if not entry_premium or entry_premium < 2:
                continue

            trade_cost = entry_premium * lot
            if trade_cost > oeh_capital:
                continue

            is_reentry = oeh_sym_entry_count[sym] > 0
            trade = simulate_trade(sym, strike, "PE", st, lot)
            if trade is None:
                continue

            trade["is_reentry"] = is_reentry
            trade["scan_time"] = st.strftime("%H:%M")
            oeh_trades.append(trade)
            oeh_capital -= trade_cost
            oeh_open_symbols.add(sym)
            oeh_sym_entry_count[sym] += 1

            # Process exit
            oeh_capital += trade["exit_price"] * lot
            oeh_open_symbols.discard(sym)

            # GUARDRAIL 3: if SL hit, block for rest of day
            if trade["exit_reason"] == "max_loss_cap":
                oeh_sl_blocked.add(sym)

    # Print OEH results
    print(f"\n{'Symbol':<25} {'Entry':>7} {'Exit':>7} {'Qty':>5} {'Gross':>8} {'Net':>8} "
          f"{'Peak':>7} {'Floor':>6} {'In':>5} {'Out':>5} {'Reason':<15} {'Re?'}")
    print("-" * 120)

    oeh_total_net = 0
    oeh_total_gross = 0
    oeh_reentry_net = 0
    oeh_wins = 0
    oeh_losses = 0
    oeh_reentry_count = 0
    oeh_open_count = 0
    oeh_open_pnl = 0

    for t in oeh_trades:
        re_tag = "RE" if t.get("is_reentry") else ""
        still = "***" if t["exit_reason"] == "STILL_OPEN" else ""
        print(f"{t['symbol']:<25} {t['entry_price']:>7.2f} {t['exit_price']:>7.2f} {t['qty']:>5} "
              f"{t['gross_pnl']:>+8.0f} {t['net_pnl']:>+8.0f} {t['peak_pnl']:>+7.0f} "
              f"{t['locked_floor']:>6} {t['scan_time']:>5} {t['exit_time']:>5} "
              f"{t['exit_reason']:<15} {re_tag} {still}")
        if t["exit_reason"] == "STILL_OPEN":
            oeh_open_count += 1
            oeh_open_pnl += t["net_pnl"]
        else:
            oeh_total_net += t["net_pnl"]
            oeh_total_gross += t["gross_pnl"]
            if t["net_pnl"] > 0:
                oeh_wins += 1
            else:
                oeh_losses += 1
        if t.get("is_reentry"):
            oeh_reentry_net += t["net_pnl"]
            oeh_reentry_count += 1

    print("-" * 120)
    closed = len(oeh_trades) - oeh_open_count
    print(f"OEH CLOSED: {closed} trades | Gross: ₹{oeh_total_gross:+,.0f} | "
          f"Net: ₹{oeh_total_net:+,.0f} | Wins: {oeh_wins} | Losses: {oeh_losses}")
    if oeh_open_count:
        print(f"OEH STILL OPEN: {oeh_open_count} trades | Unrealized: ₹{oeh_open_pnl:+,.0f}")
    print(f"OEH RE-ENTRIES: {oeh_reentry_count} trades | Net: ₹{oeh_reentry_net:+,.0f}")
    print(f"OEH WITHOUT RE-ENTRIES: Net: ₹{oeh_total_net - oeh_reentry_net:+,.0f}")
    print(f"\nGUARDRAIL STATS:")
    print(f"  Stocks blocked by SL cooldown: {len(oeh_sl_blocked)} {oeh_sl_blocked}")
    print(f"  Stocks hit entry limit ({MAX_ENTRIES_PER_SYMBOL}): {[s for s,c in oeh_sym_entry_count.items() if c >= MAX_ENTRIES_PER_SYMBOL]}")
    print(f"  Trades blocked by time cutoff (>{OEH_TIME_CUTOFF.strftime('%H:%M')}): N/A (filtered at scan level)")

    # ════════════════════════════════════════════════════════════════
    #   OEL BACKTEST
    # ════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("  OEL BACKTEST — 12 Oct 2026 — WITH GUARDRAILS")
    print(f"  Max {MAX_ENTRIES_PER_SYMBOL} entries/stock | Cutoff {OEL_TIME_CUTOFF.strftime('%H:%M')} | SL cooldown")
    print("=" * 80)

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
            if oel_sym_entry_count[sym] >= MAX_ENTRIES_PER_SYMBOL:
                continue
            if sym in oel_sl_blocked:
                continue

            strike = find_atm_strike(sym, c["ltp"], "CE")
            if strike is None:
                continue
            if (sym, strike, "CE") not in option_candles:
                continue

            lot = lot_sizes.get(sym, 1) * 2
            entry_premium = get_option_price_at(sym, strike, "CE", st)
            if not entry_premium or entry_premium < 2:
                continue

            trade_cost = entry_premium * lot
            if trade_cost > oel_capital:
                continue

            is_reentry = oel_sym_entry_count[sym] > 0
            trade = simulate_trade(sym, strike, "CE", st, lot)
            if trade is None:
                continue

            trade["is_reentry"] = is_reentry
            trade["scan_time"] = st.strftime("%H:%M")
            oel_trades.append(trade)
            oel_capital -= trade_cost
            oel_open_symbols.add(sym)
            oel_sym_entry_count[sym] += 1

            oel_capital += trade["exit_price"] * lot
            oel_open_symbols.discard(sym)

            if trade["exit_reason"] == "max_loss_cap":
                oel_sl_blocked.add(sym)

    # Print OEL results
    print(f"\n{'Symbol':<25} {'Entry':>7} {'Exit':>7} {'Qty':>5} {'Gross':>8} {'Net':>8} "
          f"{'Peak':>7} {'Floor':>6} {'In':>5} {'Out':>5} {'Reason':<15} {'Re?'}")
    print("-" * 120)

    oel_total_net = 0
    oel_total_gross = 0
    oel_reentry_net = 0
    oel_wins = 0
    oel_losses = 0
    oel_reentry_count = 0
    oel_open_count = 0
    oel_open_pnl = 0

    for t in oel_trades:
        re_tag = "RE" if t.get("is_reentry") else ""
        still = "***" if t["exit_reason"] == "STILL_OPEN" else ""
        print(f"{t['symbol']:<25} {t['entry_price']:>7.2f} {t['exit_price']:>7.2f} {t['qty']:>5} "
              f"{t['gross_pnl']:>+8.0f} {t['net_pnl']:>+8.0f} {t['peak_pnl']:>+7.0f} "
              f"{t['locked_floor']:>6} {t['scan_time']:>5} {t['exit_time']:>5} "
              f"{t['exit_reason']:<15} {re_tag} {still}")
        if t["exit_reason"] == "STILL_OPEN":
            oel_open_count += 1
            oel_open_pnl += t["net_pnl"]
        else:
            oel_total_net += t["net_pnl"]
            oel_total_gross += t["gross_pnl"]
            if t["net_pnl"] > 0:
                oel_wins += 1
            else:
                oel_losses += 1
        if t.get("is_reentry"):
            oel_reentry_net += t["net_pnl"]
            oel_reentry_count += 1

    print("-" * 120)
    closed = len(oel_trades) - oel_open_count
    print(f"OEL CLOSED: {closed} trades | Gross: ₹{oel_total_gross:+,.0f} | "
          f"Net: ₹{oel_total_net:+,.0f} | Wins: {oel_wins} | Losses: {oel_losses}")
    if oel_open_count:
        print(f"OEL STILL OPEN: {oel_open_count} trades | Unrealized: ₹{oel_open_pnl:+,.0f}")
    print(f"OEL RE-ENTRIES: {oel_reentry_count} trades | Net: ₹{oel_reentry_net:+,.0f}")
    print(f"OEL WITHOUT RE-ENTRIES: Net: ₹{oel_total_net - oel_reentry_net:+,.0f}")
    print(f"\nGUARDRAIL STATS:")
    print(f"  Stocks blocked by SL cooldown: {len(oel_sl_blocked)} {oel_sl_blocked}")
    print(f"  Stocks hit entry limit ({MAX_ENTRIES_PER_SYMBOL}): {[s for s,c in oel_sym_entry_count.items() if c >= MAX_ENTRIES_PER_SYMBOL]}")

    # ── Grand Summary ──
    print("\n" + "=" * 80)
    print("  GRAND SUMMARY (WITH GUARDRAILS)")
    print(f"  Rules: max {MAX_ENTRIES_PER_SYMBOL}/stock | OEH cutoff {OEH_TIME_CUTOFF.strftime('%H:%M')} | OEL cutoff {OEL_TIME_CUTOFF.strftime('%H:%M')} | SL → block")
    print("=" * 80)
    total_closed = (len(oeh_trades) - oeh_open_count) + (len(oel_trades) - oel_open_count)
    total_open = oeh_open_count + oel_open_count
    total_net = oeh_total_net + oel_total_net
    total_open_pnl = oeh_open_pnl + oel_open_pnl
    total_reentry_net = oeh_reentry_net + oel_reentry_net
    total_reentry_count = oeh_reentry_count + oel_reentry_count
    print(f"Closed trades: {total_closed} | Closed Net PnL: ₹{total_net:+,.0f}")
    if total_open:
        print(f"Still open: {total_open} | Unrealized: ₹{total_open_pnl:+,.0f}")
    print(f"Re-entry trades: {total_reentry_count} | Re-entry Net PnL: ₹{total_reentry_net:+,.0f}")
    print(f"Without re-entries (closed only): ₹{total_net - total_reentry_net:+,.0f}")
    print(f"\nVerdict: Re-entries {'HELPED' if total_reentry_net > 0 else 'HURT'} "
          f"by ₹{abs(total_reentry_net):,.0f}")
    if total_open:
        print(f"\n⚠️  {total_open} trades still open — re-run after 15:30 for final numbers.")


if __name__ == "__main__":
    asyncio.run(main())

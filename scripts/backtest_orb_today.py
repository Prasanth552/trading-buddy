#!/usr/bin/env python3
"""
Backtest ORB (Opening Range Breakout) for today (2026-10-12) with guardrails.

Uses real 5-min + 1-min candle data. Mirrors live logic:
- First 5-min candle (9:15-9:20) defines the opening range
- Breakout above range_high → buy CE, below range_low → buy PE
- Rescans every 5 min for new breakouts (re-entries on freed capital)

GUARDRAILS:
1. Per-stock daily trade limit: max 3 entries per symbol
2. Time cutoff: no new entries after 13:00
3. Cooldown after SL: if a stock hits SL, block it for rest of day
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
TODAY = datetime(2026, 10, 12, tzinfo=IST).date()

# Strategy params (mirror channel_listener.py)
ORB_CAPITAL = 300000
ORB_SL_PCT = 0.30
ORB_MAX_SL_RS = 5000
FLOOR_LEVELS = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
ORB_MIN_RANGE_PCT = 0.3
ORB_MAX_RANGE_PCT = 3.0
ORB_BLOCKLIST = {"GODREJCP", "GRASIM"}
ORB_LOSS_CAP = 20000
ORB_PROFIT_CAP = 25000

# GUARDRAIL PARAMS
MAX_ENTRIES_PER_SYMBOL = 3
ORB_TIME_CUTOFF = dtime(13, 0)

RESCAN_INTERVAL_MINS = 5


def get_stepped_floor(net_pnl):
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

    universe = sorted((fo_symbols & set(eq_keys.keys())) - ORB_BLOCKLIST)
    print(f"F&O universe: {len(universe)} stocks (excl blocklist)")

    # Build option key lookup
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

    for sym, strikes in sym_strikes.items():
        ss = sorted(strikes)
        if len(ss) >= 2:
            step = min(ss[j+1] - ss[j] for j in range(min(20, len(ss)-1)))
            strike_steps[sym] = step
        else:
            strike_steps[sym] = 50

    # ── Fetch 5-min equity candles for opening range detection ──
    print("\n=== Fetching 5-min equity candles... ===")
    from_dt = datetime.combine(TODAY, dtime(9, 15))
    to_dt = datetime.combine(TODAY, dtime(15, 30))

    equity_5min = {}
    batch_size = 20
    syms_with_keys = [(sym, eq_keys[sym]) for sym in universe if sym in eq_keys]

    for i in range(0, len(syms_with_keys), batch_size):
        batch = syms_with_keys[i:i + batch_size]
        for sym, key in batch:
            try:
                candles = ud.historical_data(key, from_dt, to_dt, "5minute")
                if candles and len(candles) >= 3:
                    equity_5min[sym] = candles
            except Exception:
                pass
        pct = min(100, (i + batch_size) / len(syms_with_keys) * 100)
        print(f"  Fetched {min(i + batch_size, len(syms_with_keys))}/{len(syms_with_keys)} ({pct:.0f}%)", end="\r")
        await asyncio.sleep(0.3)

    print(f"\nGot 5-min candles for {len(equity_5min)} stocks")

    # ── Identify opening ranges and breakouts at each scan time ──
    def parse_ts(ts_val):
        if isinstance(ts_val, str):
            ts_val = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
        if hasattr(ts_val, "astimezone"):
            ts_val = ts_val.astimezone(IST)
        return ts_val.replace(tzinfo=None) if ts_val.tzinfo else ts_val

    def find_breakouts_at(scan_time):
        """Find ORB breakouts visible by scan_time using 5-min candles."""
        candidates = []
        scan_dt = datetime.combine(TODAY, scan_time)

        for sym, candles in equity_5min.items():
            if not candles or len(candles) < 2:
                continue

            # First candle = opening range
            range_high = candles[0]["high"]
            range_low = candles[0]["low"]
            range_open = candles[0]["open"]
            if range_open <= 0:
                continue

            range_pct = (range_high - range_low) / range_open * 100
            if range_pct < ORB_MIN_RANGE_PCT or range_pct > ORB_MAX_RANGE_PCT:
                continue

            # Check subsequent candles for breakout
            for dc in candles[1:]:
                ts = parse_ts(dc.get("ts") or dc.get("date") or dc.get("timestamp"))
                if ts > scan_dt:
                    break
                if dc["high"] > range_high:
                    candidates.append({
                        "symbol": sym, "direction": "bullish",
                        "range_pct": range_pct, "breakout_price": dc["close"],
                        "ltp": dc["close"],
                    })
                    break
                elif dc["low"] < range_low:
                    candidates.append({
                        "symbol": sym, "direction": "bearish",
                        "range_pct": range_pct, "breakout_price": dc["close"],
                        "ltp": dc["close"],
                    })
                    break

        candidates.sort(key=lambda x: x["range_pct"], reverse=True)
        return candidates

    # Build scan times: 09:25, 09:30, 09:35, ... 15:00
    scan_times = []
    t = dtime(9, 25)
    while t <= dtime(15, 0):
        scan_times.append(t)
        mins = t.hour * 60 + t.minute + RESCAN_INTERVAL_MINS
        if mins >= 15 * 60:
            break
        t = dtime(mins // 60, mins % 60)

    print("\n=== Identifying ORB breakouts at each scan time... ===")
    breakouts_by_time = {}
    all_option_syms = set()

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

    for st in scan_times:
        cands = find_breakouts_at(st)
        breakouts_by_time[st] = cands
        for c in cands:
            otype = "CE" if c["direction"] == "bullish" else "PE"
            s = find_atm_strike(c["symbol"], c["ltp"], otype)
            if s is not None:
                all_option_syms.add((c["symbol"], s, otype))
        if cands:
            print(f"  {st.strftime('%H:%M')} — {len(cands)} breakouts")

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

    # ── Helpers ──
    def get_option_price_at(sym, strike, otype, target_time):
        key = (sym, strike, otype)
        candles = option_candles.get(key, [])
        best = None
        for c in candles:
            ct = parse_ts(c.get("ts") or c.get("date") or c.get("timestamp"))
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
            ct = parse_ts(c.get("ts") or c.get("date") or c.get("timestamp"))
            et = datetime.combine(TODAY, entry_time)
            if ct >= et:
                result.append({"time": ct, **{k: c[k] for k in ("open", "high", "low", "close")}})
        return result

    def simulate_trade(sym, strike, otype, entry_time, lot_qty):
        entry_price = get_option_price_at(sym, strike, otype, entry_time)
        if not entry_price or entry_price <= 0:
            return None

        sl_price = entry_price * (1 - ORB_SL_PCT)
        sl_cap = entry_price - (ORB_MAX_SL_RS / lot_qty)
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
            if c["low"] <= sl:
                exit_price = sl
                exit_time = c["time"]
                exit_reason = "max_loss_cap"
                break

            if not is_entry_candle and locked_floor > 0:
                current_low_pnl = (c["low"] - entry_price) * lot_qty
                if current_low_pnl <= locked_floor:
                    exit_price = entry_price + (locked_floor / lot_qty)
                    exit_time = c["time"]
                    exit_reason = f"floor_{locked_floor}"
                    break

            current_high_pnl = (c["high"] - entry_price) * lot_qty
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
                        "direction": "",
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
            "direction": "",
        }

    # ── Run simulation ──
    # Check data coverage
    sample_candles = next(iter(equity_5min.values()), [])
    if sample_candles:
        last_c = sample_candles[-1]
        last_ts = parse_ts(last_c.get("ts") or last_c.get("date") or last_c.get("timestamp"))
        if last_ts.hour < 15 or (last_ts.hour == 15 and last_ts.minute < 20):
            print(f"\n⚠️  WARNING: Data only goes up to {last_ts.strftime('%H:%M')} IST")

    # ════════════════════════════════════════════════════════════
    #  WITHOUT GUARDRAILS
    # ════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("  ORB BACKTEST — 12 Oct 2026 — NO GUARDRAILS (baseline)")
    print("=" * 80)

    def run_orb_sim(use_guardrails):
        """Realistic simulation with concurrent capital locking.

        Trades stay open and lock capital until they exit via SL/floor/EOD.
        New trades can only be placed with remaining free capital.
        Capital is freed only when a trade actually closes.
        """
        completed_trades = []
        # Open positions: list of dicts with trade state
        open_positions = []  # {sym, strike, otype, lot, entry_price, sl, peak_pnl, locked_floor, entry_time, cost, direction, is_entry_candle, is_reentry}
        capital = ORB_CAPITAL
        capital_locked = {}  # sym → locked amount
        sym_entry_count = defaultdict(int)
        sl_blocked = set()
        daily_pnl = 0

        # Build minute-by-minute timeline from 09:25 to 15:30
        all_minutes = []
        t_min = datetime.combine(TODAY, dtime(9, 25))
        t_end = datetime.combine(TODAY, dtime(15, 30))
        while t_min <= t_end:
            all_minutes.append(t_min)
            t_min += timedelta(minutes=1)

        # Pre-compute which scan times exist (for placing new trades)
        scan_time_set = set()
        for st in scan_times:
            scan_time_set.add(datetime.combine(TODAY, st))

        # Track which breakouts we've already entered (sym → True)
        entered_syms = set()

        for current_dt in all_minutes:
            current_time = current_dt.time()

            # ── Step 1: Check exits on all open positions ──
            to_close = []
            for pos in open_positions:
                key = (pos["sym"], pos["strike"], pos["otype"])
                candles = option_candles.get(key, [])
                # Find the candle at current_dt
                candle = None
                for c_raw in candles:
                    ct = parse_ts(c_raw.get("ts") or c_raw.get("date") or c_raw.get("timestamp"))
                    if ct.hour == current_dt.hour and ct.minute == current_dt.minute:
                        candle = c_raw
                        break
                if candle is None:
                    continue

                entry_price = pos["entry_price"]
                lot_qty = pos["lot"]
                current_low_pnl = (candle["low"] - entry_price) * lot_qty
                current_high_pnl = (candle["high"] - entry_price) * lot_qty

                # SL check
                if candle["low"] <= pos["sl"]:
                    gross = (pos["sl"] - entry_price) * lot_qty
                    charges = max(100, abs(gross) * 0.02)
                    to_close.append((pos, pos["sl"], current_dt, "max_loss_cap", gross, gross - charges))
                    continue

                # Floor exit (not on entry candle)
                if not pos["is_entry_candle"] and pos["locked_floor"] > 0:
                    if current_low_pnl <= pos["locked_floor"]:
                        gross = (candle["close"] - entry_price) * lot_qty
                        charges = max(100, abs(gross) * 0.02)
                        to_close.append((pos, candle["close"], current_dt, f"floor_{pos['locked_floor']}", gross, gross - charges))
                        continue

                # Update peak/floor AFTER exit checks
                pos["peak_pnl"] = max(pos["peak_pnl"], current_high_pnl)
                new_floor = get_stepped_floor(pos["peak_pnl"])
                if new_floor > pos["locked_floor"]:
                    pos["locked_floor"] = new_floor

                pos["is_entry_candle"] = False

            # EOD close at 15:29
            if current_dt.hour == 15 and current_dt.minute >= 29:
                for pos in open_positions:
                    already_closing = any(p[0] is pos for p in to_close)
                    if already_closing:
                        continue
                    key = (pos["sym"], pos["strike"], pos["otype"])
                    candles = option_candles.get(key, [])
                    last_price = pos["entry_price"]
                    for c_raw in candles:
                        last_price = c_raw["close"]
                    gross = (last_price - pos["entry_price"]) * pos["lot"]
                    charges = max(100, abs(gross) * 0.02)
                    to_close.append((pos, last_price, current_dt, "eod_close", gross, gross - charges))

            # Process closures
            for pos, exit_price, exit_dt, exit_reason, gross, net in to_close:
                trade = {
                    "symbol": f"{pos['sym']} {int(pos['strike'])} {pos['otype']}",
                    "entry_price": round(pos["entry_price"], 2),
                    "exit_price": round(exit_price, 2),
                    "qty": pos["lot"],
                    "gross_pnl": round(gross, 2),
                    "net_pnl": round(net, 2),
                    "peak_pnl": round(pos["peak_pnl"], 2),
                    "locked_floor": pos["locked_floor"],
                    "entry_time": pos["entry_time"].strftime("%H:%M"),
                    "exit_time": exit_dt.strftime("%H:%M"),
                    "exit_reason": exit_reason,
                    "direction": pos["direction"],
                    "is_reentry": pos["is_reentry"],
                    "scan_time": pos["scan_time"],
                }
                completed_trades.append(trade)
                daily_pnl += net

                # Free capital
                locked = capital_locked.pop(pos["sym"], 0)
                capital += locked + net + max(100, abs(gross) * 0.02)  # return locked + pnl (add back charges since net already subtracted them)

                if pos in open_positions:
                    open_positions.remove(pos)

                if use_guardrails and exit_reason == "max_loss_cap":
                    sl_blocked.add(pos["sym"])

            # ── Step 2: Place new trades at scan times ──
            if current_dt not in scan_time_set:
                continue

            if use_guardrails and current_time > ORB_TIME_CUTOFF:
                continue

            if daily_pnl <= -ORB_LOSS_CAP or daily_pnl >= ORB_PROFIT_CAP:
                continue

            # Reset capital when no trades are open
            if not open_positions:
                capital = ORB_CAPITAL
                capital_locked.clear()

            cands = breakouts_by_time.get(current_time, [])
            open_syms = {p["sym"] for p in open_positions}

            for c in cands:
                sym = c["symbol"]
                if sym in open_syms:
                    continue

                if use_guardrails:
                    if sym_entry_count[sym] >= MAX_ENTRIES_PER_SYMBOL:
                        continue
                    if sym in sl_blocked:
                        continue

                otype = "CE" if c["direction"] == "bullish" else "PE"
                strike = find_atm_strike(sym, c["ltp"], otype)
                if strike is None:
                    continue
                if (sym, strike, otype) not in option_candles:
                    continue

                lot = lot_sizes.get(sym, 1) * 2
                entry_premium = get_option_price_at(sym, strike, otype, current_time)
                if not entry_premium or entry_premium < 2:
                    continue

                trade_cost = entry_premium * lot
                if trade_cost > capital:
                    continue

                is_reentry = sym_entry_count[sym] > 0

                sl_price = entry_premium * (1 - ORB_SL_PCT)
                sl_cap = entry_premium - (ORB_MAX_SL_RS / lot)
                sl = max(sl_price, sl_cap)

                pos = {
                    "sym": sym, "strike": strike, "otype": otype,
                    "lot": lot, "entry_price": entry_premium, "sl": sl,
                    "peak_pnl": 0, "locked_floor": 0,
                    "entry_time": current_dt, "cost": trade_cost,
                    "direction": c["direction"], "is_entry_candle": True,
                    "is_reentry": is_reentry,
                    "scan_time": current_time.strftime("%H:%M"),
                }
                open_positions.append(pos)
                capital -= trade_cost
                capital_locked[sym] = trade_cost
                sym_entry_count[sym] += 1

        # Any still open at end
        for pos in open_positions:
            key = (pos["sym"], pos["strike"], pos["otype"])
            candles = option_candles.get(key, [])
            last_price = pos["entry_price"]
            if candles:
                last_price = candles[-1]["close"]
            gross = (last_price - pos["entry_price"]) * pos["lot"]
            completed_trades.append({
                "symbol": f"{pos['sym']} {int(pos['strike'])} {pos['otype']}",
                "entry_price": round(pos["entry_price"], 2),
                "exit_price": round(last_price, 2),
                "qty": pos["lot"],
                "gross_pnl": round(gross, 2),
                "net_pnl": round(gross, 2),
                "peak_pnl": round(pos["peak_pnl"], 2),
                "locked_floor": pos["locked_floor"],
                "entry_time": pos["entry_time"].strftime("%H:%M"),
                "exit_time": "15:30",
                "exit_reason": "STILL_OPEN",
                "direction": pos["direction"],
                "is_reentry": pos["is_reentry"],
                "scan_time": pos["scan_time"],
            })

        completed_trades.sort(key=lambda t: t["entry_time"])
        return completed_trades, sl_blocked, sym_entry_count

    def print_results(trades, label, sl_blocked=None, sym_entry_count=None):
        print(f"\n{'Symbol':<25} {'Dir':>4} {'Entry':>7} {'Exit':>7} {'Qty':>5} {'Gross':>8} {'Net':>8} "
              f"{'Peak':>7} {'Floor':>6} {'In':>5} {'Out':>5} {'Reason':<15} {'Re?'}")
        print("-" * 130)

        total_net = 0
        total_gross = 0
        reentry_net = 0
        wins = 0
        losses = 0
        reentry_count = 0
        open_count = 0
        open_pnl = 0

        for t in trades:
            re_tag = "RE" if t.get("is_reentry") else ""
            still = "***" if t["exit_reason"] == "STILL_OPEN" else ""
            d_tag = "▲" if t.get("direction") == "bullish" else "▼"
            print(f"{t['symbol']:<25} {d_tag:>4} {t['entry_price']:>7.2f} {t['exit_price']:>7.2f} {t['qty']:>5} "
                  f"{t['gross_pnl']:>+8.0f} {t['net_pnl']:>+8.0f} {t['peak_pnl']:>+7.0f} "
                  f"{t['locked_floor']:>6} {t['scan_time']:>5} {t['exit_time']:>5} "
                  f"{t['exit_reason']:<15} {re_tag} {still}")
            if t["exit_reason"] == "STILL_OPEN":
                open_count += 1
                open_pnl += t["net_pnl"]
            else:
                total_net += t["net_pnl"]
                total_gross += t["gross_pnl"]
                if t["net_pnl"] > 0:
                    wins += 1
                else:
                    losses += 1
            if t.get("is_reentry"):
                reentry_net += t["net_pnl"]
                reentry_count += 1

        print("-" * 130)
        closed = len(trades) - open_count
        print(f"{label} CLOSED: {closed} trades | Gross: ₹{total_gross:+,.0f} | "
              f"Net: ₹{total_net:+,.0f} | Wins: {wins} | Losses: {losses}")
        if open_count:
            print(f"{label} STILL OPEN: {open_count} trades | Unrealized: ₹{open_pnl:+,.0f}")
        print(f"{label} RE-ENTRIES: {reentry_count} trades | Net: ₹{reentry_net:+,.0f}")
        print(f"{label} WITHOUT RE-ENTRIES: Net: ₹{total_net - reentry_net:+,.0f}")

        if sl_blocked is not None:
            print(f"\nGUARDRAIL STATS:")
            print(f"  Stocks blocked by SL cooldown: {len(sl_blocked)} {sl_blocked}")
            if sym_entry_count:
                maxed = [s for s, c in sym_entry_count.items() if c >= MAX_ENTRIES_PER_SYMBOL]
                print(f"  Stocks hit entry limit ({MAX_ENTRIES_PER_SYMBOL}): {maxed}")

        return total_net, reentry_net

    # Run without guardrails
    trades_no_guard, _, _ = run_orb_sim(use_guardrails=False)
    net_no, re_no = print_results(trades_no_guard, "ORB (no guard)")

    # Run with guardrails
    print("\n" + "=" * 80)
    print("  ORB BACKTEST — 12 Oct 2026 — WITH GUARDRAILS")
    print(f"  Max {MAX_ENTRIES_PER_SYMBOL} entries/stock | Cutoff {ORB_TIME_CUTOFF.strftime('%H:%M')} | SL cooldown")
    print("=" * 80)

    trades_guard, sl_b, sec = run_orb_sim(use_guardrails=True)
    net_g, re_g = print_results(trades_guard, "ORB (guardrails)", sl_b, sec)

    # ── Comparison ──
    print("\n" + "=" * 80)
    print("  COMPARISON")
    print("=" * 80)
    print(f"  Without guardrails: {len(trades_no_guard)} trades | Net: ₹{net_no:+,.0f}")
    print(f"  With guardrails:    {len(trades_guard)} trades | Net: ₹{net_g:+,.0f}")
    print(f"  Guardrails impact:  ₹{net_g - net_no:+,.0f}")
    print(f"\n  Without re-entries (no guard): ₹{net_no - re_no:+,.0f}")
    print(f"  Without re-entries (guard):    ₹{net_g - re_g:+,.0f}")


if __name__ == "__main__":
    asyncio.run(main())

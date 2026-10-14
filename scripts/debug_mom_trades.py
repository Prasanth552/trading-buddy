#!/usr/bin/env python3
"""Debug: show MOM trade details for one day to find what inflates PnL."""

# Realistic tiered slippage based on option premium & liquidity research:
# - Tick size: ₹0.05 for stocks ≥₹250, ₹0.01 for <₹250 (NSE Nov 2025 revision)
# - Liquid large-caps: 0.1-0.3% of premium (half the bid-ask spread with limit orders)
# - Mid-caps: ~0.3-0.5% of premium
# - We use limit orders, so slippage ≈ half the bid-ask spread
def get_slippage(premium):
    """Return per-side slippage based on option premium level."""
    if premium >= 100:    # high-premium, liquid (RELIANCE, TCS, INFY etc)
        return 0.10       # ₹0.10/unit — 1-2 ticks, ~0.1%
    elif premium >= 30:   # mid-premium
        return 0.15       # ₹0.15/unit — 3 ticks, ~0.3%
    elif premium >= 10:   # low-premium, higher lot sizes
        return 0.10       # ₹0.10/unit — 2 ticks (tick=0.05)
    else:                 # very low premium (<₹10), most illiquid
        return 0.05       # ₹0.05/unit — 1 tick minimum

SLIPPAGE_PER_UNIT = None  # now dynamic, see get_slippage()

import asyncio, sys, os, argparse
from datetime import datetime, timedelta, time as dtime, date as dt_date
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from src.broker.upstox_data import UpstoxData, load_cached_token

# Reuse everything from backtest script
from scripts.backtest_4strat_multi import (
    IST, CAPITAL, MAX_SL, SL_PCT, FLOOR_LEVELS, MAX_ENTRIES_PER_SYM,
    TIME_CUTOFF, MOM_BODY_PCT, MOM_VOL_MULT,
    get_stepped_floor, parse_ts, build_5min, calc_ema, calc_rsi,
    _fetch_batch
)

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    DATE = datetime.strptime(args.date, "%Y-%m-%d").date()

    token = load_cached_token()
    ud = UpstoxData(access_token=token)
    master = ud._load_master()
    print(f"Loaded {len(master)} instruments")

    import re as _re
    eq_keys, lot_sizes, fo_symbols = {}, {}, set()
    option_keys = defaultdict(lambda: defaultdict(dict))
    sym_strikes = defaultdict(set)

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
            ot = inst.get("instrument_type", "").upper()
            if ot in ("CE", "PE"):
                strike = float(inst.get("strike_price") or 0)
                if strike <= 0:
                    continue
                asset = (inst.get("asset_symbol") or inst.get("name") or "").upper()
                if not asset and _base:
                    asset = _base.group(1)
                if not asset:
                    continue
                ikey = inst.get("instrument_key")
                expiry = inst.get("expiry")
                sym_strikes[asset].add(strike)
                ex = option_keys[asset][strike].get(ot)
                if ex is None or (expiry and ex.get("expiry") and expiry < ex["expiry"]):
                    option_keys[asset][strike][ot] = {"key": ikey, "expiry": expiry}

    universe = sorted(fo_symbols & set(eq_keys.keys()))
    strike_steps = {}
    for sym, strikes in sym_strikes.items():
        ss = sorted(strikes)
        if len(ss) >= 2:
            strike_steps[sym] = min(ss[j+1] - ss[j] for j in range(min(20, len(ss)-1)))
        else:
            strike_steps[sym] = 50

    print(f"Universe: {len(universe)} stocks")

    # Check lot sizes
    missing_lot = [s for s in universe if s not in lot_sizes]
    print(f"Stocks with NO lot size (default=1): {len(missing_lot)}")
    if missing_lot[:10]:
        print(f"  Examples: {missing_lot[:10]}")

    # Show lot size distribution
    lots = [lot_sizes.get(s, 1) for s in universe]
    print(f"Lot sizes: min={min(lots)} max={max(lots)} median={sorted(lots)[len(lots)//2]}")
    tiny_lot = [(s, lot_sizes.get(s, 1)) for s in universe if lot_sizes.get(s, 1) <= 5]
    print(f"Stocks with lot ≤ 5: {len(tiny_lot)}")
    if tiny_lot[:10]:
        print(f"  {tiny_lot[:10]}")

    # Fetch equity candles
    from_dt = datetime.combine(DATE, dtime(9, 15))
    to_dt = datetime.combine(DATE, dtime(15, 30))
    syms_with_keys = [(s, eq_keys[s]) for s in universe if s in eq_keys]
    print(f"\nFetching equity candles...", flush=True)
    equity_candles = await _fetch_batch(ud, syms_with_keys, from_dt, to_dt, concurrency=15, label="EQ")

    # MOM signals only
    signals = []
    for sym in universe:
        cm = equity_candles.get(sym)
        if not cm or len(cm) < 30:
            continue
        bars5 = build_5min(cm, DATE)
        if len(bars5) < 22:
            continue
        closes5 = [b["close"] for b in bars5]
        times5 = [b["time"] for b in bars5]
        vols5 = [b["volume"] for b in bars5]
        avg_vol = sum(vols5) / max(1, len(vols5))

        for i in range(2, len(bars5)):
            t = times5[i].time()
            if t < dtime(9, 25) or t > TIME_CUTOFF:
                continue
            b = bars5[i]
            body = b["close"] - b["open"]
            bpct = abs(body) / b["open"] * 100 if b["open"] > 0 else 0
            if bpct > MOM_BODY_PCT and b["volume"] > avg_vol * MOM_VOL_MULT:
                ot = "CE" if body > 0 else "PE"
                signals.append((sym, ot, t, b["close"]))

    # Dedup
    signals.sort(key=lambda x: x[2])
    seen = defaultdict(int)
    f = []
    for s in signals:
        if seen[s[0]] < MAX_ENTRIES_PER_SYM:
            f.append(s)
            seen[s[0]] += 1
    signals = f
    print(f"\nMOM signals: {len(signals)}")

    # Find ATM + fetch options
    def find_atm(sym, ltp, otype):
        step = strike_steps.get(sym, 50)
        atm = round(ltp / step) * step
        strikes = option_keys.get(sym, {})
        if not strikes:
            return None
        for off in [0, step, -step, 2*step, -2*step]:
            s = atm + off
            if s in strikes and otype in strikes[s]:
                return s
        valid = [s for s in strikes if otype in strikes[s]]
        return min(valid, key=lambda s: abs(s - ltp)) if valid else None

    needed = set()
    for sym, ot, t, p in signals:
        s = find_atm(sym, p, ot)
        if s is not None:
            needed.add((sym, s, ot))

    print(f"Unique options needed: {len(needed)}")
    print(f"Fetching option candles...", flush=True)

    opt_pairs = []
    opt_map = {}
    for sym, strike, ot in needed:
        info = option_keys.get(sym, {}).get(strike, {}).get(ot)
        if info:
            label = f"{sym}_{strike}_{ot}"
            opt_pairs.append((label, info["key"]))
            opt_map[label] = (sym, strike, ot)
    raw = await _fetch_batch(ud, opt_pairs, from_dt, to_dt, concurrency=15, label="OPT")
    opt_candles = {}
    for label, candles in raw.items():
        key = opt_map.get(label)
        if key:
            opt_candles[key] = candles

    # Simulate with FULL debug
    def sim(sym, strike, ot, entry_time, lot_qty):
        key = (sym, strike, ot)
        candles = opt_candles.get(key, [])
        if not candles:
            return None
        ep = None
        for c in candles:
            ct = parse_ts(c)
            if ct <= datetime.combine(DATE, entry_time):
                ep = c["close"]
            else:
                break
        if not ep or ep <= 0:
            return None
        slip = get_slippage(ep)
        ep += slip  # entry slippage: buy higher
        sl = max(ep * (1 - SL_PCT), ep - MAX_SL / lot_qty)
        after = []
        for c in candles:
            ct = parse_ts(c)
            if ct >= datetime.combine(DATE, entry_time):
                after.append({"time": ct, "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"]})
        if not after:
            return None
        peak, floor, xp, xt, xr = 0, 0, None, None, None
        first = True
        for c in after:
            if c["l"] <= sl:
                xp, xt, xr = sl, c["time"], "sl"
                break
            if not first and floor > 0 and (c["l"] - ep) * lot_qty <= floor:
                xp = ep + floor / lot_qty
                xt, xr = c["time"], "floor"
                break
            peak = max(peak, (c["h"] - ep) * lot_qty)
            nf = get_stepped_floor(peak)
            if nf > floor:
                floor = nf
            first = False
        if xp is None:
            last = after[-1]
            xp, xt, xr = last["c"], last["time"], "eod"
        xp -= slip  # exit slippage: sell lower
        pnl = (xp - ep) * lot_qty
        return {"pnl": round(pnl), "peak": round(peak), "ep": ep, "xp": xp,
                "xt": xt, "xr": xr, "qty": lot_qty, "sl": sl,
                "floor_at_exit": floor, "exit_reason": xr}

    # Run with capital tracking + debug output
    trades = []
    cap = CAPITAL
    open_pos = []
    open_syms = set()
    sl_blocked = set()
    cap_rejects = 0
    total_cost = 0

    print(f"\n{'='*100}")
    print(f"{'#':>3} {'Time':>8} {'Symbol':<20} {'Lot':>5} {'EP':>8} {'Cost':>10} "
          f"{'XP':>8} {'PnL':>8} {'Exit':>6} {'Cap':>10} {'Duration':>8}")
    print("-" * 100)

    for sym, ot, entry_time, price in signals:
        edt = datetime.combine(DATE, entry_time)
        still = []
        for p in open_pos:
            if p["xt"] <= edt:
                cap += p["xp"] * p["qty"]
                open_syms.discard(p["sym"])
            else:
                still.append(p)
        open_pos = still

        if sym in sl_blocked or sym in open_syms:
            continue
        strike = find_atm(sym, price, ot)
        if strike is None or (sym, strike, ot) not in opt_candles:
            continue
        lot = lot_sizes.get(sym, 1) * 2

        ep = None
        for c in opt_candles[(sym, strike, ot)]:
            ct = parse_ts(c)
            if ct <= edt:
                ep = c["close"]
            else:
                break
        if not ep or ep < 2:
            continue
        cost = (ep + get_slippage(ep)) * lot
        if cost > cap:
            cap_rejects += 1
            continue

        t = sim(sym, strike, ot, entry_time, lot)
        if t is None:
            continue

        trades.append(t)
        cap -= cost
        total_cost += cost
        open_syms.add(sym)
        open_pos.append({"xt": t["xt"], "xp": t["xp"], "qty": t["qty"], "sym": sym})
        if t["xr"] == "sl":
            sl_blocked.add(sym)

        duration = (t["xt"] - edt).total_seconds() / 60 if t["xt"] else 0
        label = f"{sym} {strike} {ot}"
        print(f"{len(trades):>3} {str(entry_time)[:5]:>8} {label:<20} {lot:>5} "
              f"{t['ep']:>8.2f} {cost:>10,.0f} {t['xp']:>8.2f} {t['pnl']:>+8,} "
              f"{t['xr']:>6} {cap:>10,.0f} {duration:>7.0f}m")

    print("-" * 100)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = len(trades) - wins
    total_pnl = sum(t["pnl"] for t in trades)
    print(f"\nSUMMARY:")
    print(f"  Trades: {len(trades)} (W:{wins} L:{losses} WR:{wins/max(1,len(trades))*100:.0f}%)")
    print(f"  Total PnL: ₹{total_pnl:+,}")
    print(f"  Cap rejects: {cap_rejects}")
    print(f"  Avg cost per trade: ₹{total_cost/max(1,len(trades)):,.0f}")
    print(f"  Avg PnL per trade: ₹{total_pnl/max(1,len(trades)):+,.0f}")
    print(f"  Slippage model: tiered (₹0.05-0.15/unit based on premium level)")

    # Exit reason breakdown
    reasons = defaultdict(lambda: {"count": 0, "pnl": 0})
    for t in trades:
        reasons[t["exit_reason"]]["count"] += 1
        reasons[t["exit_reason"]]["pnl"] += t["pnl"]
    print(f"\n  Exit reasons:")
    for r, d in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
        print(f"    {r}: {d['count']} trades, ₹{d['pnl']:+,}")

    # Duration breakdown
    durations = []
    for t in trades:
        if t["xt"]:
            durations.append((t["xt"] - datetime.combine(DATE, dtime(9,15))).total_seconds() / 60)
    if durations:
        print(f"\n  Avg exit duration: {sum(d for d in durations)/len(durations):.0f} min from signal")

    # Lot size analysis
    lot_dist = defaultdict(int)
    for t in trades:
        lot_dist[t["qty"]] += 1
    print(f"\n  Lot sizes used:")
    for q, c in sorted(lot_dist.items()):
        print(f"    qty={q}: {c} trades")


if __name__ == "__main__":
    asyncio.run(main())

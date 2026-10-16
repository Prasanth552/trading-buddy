#!/usr/bin/env python3
"""
Risk:Reward grid backtest for OEH+OEL with NIFTY regime filter.

Tests multiple SL/floor combos to find the best risk:reward ratio.
All configs use regime filter (OEH on bearish, OEL on bullish days).

Usage:
    PYTHONPATH=. .venv/bin/python3 -u scripts/backtest_rr_grid.py \
        --from 2026-10-01 --to 2026-10-16
"""

import asyncio, sys, os, argparse, time as _time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo(config.TIMEZONE)

CAPITAL = 300000
SLIPPAGE_PCT = 0.005
TOLERANCE = 0.05
MIN_MOVE_PCT = 0.3
SCAN_TIME = dtime(9, 25)
BLOCKLIST = {"GODREJCP", "GRASIM"}

# ── Parameter grid ──
PARAM_SETS = [
    {
        "name": "CURRENT",
        "sl_pct": 0.30, "max_sl": 5000, "max_entries": 3,
        "floors": [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000],
    },
    {
        "name": "TIGHT_SL",
        "sl_pct": 0.15, "max_sl": 1500, "max_entries": 2,
        "floors": [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000],
    },
    {
        "name": "HIGH_FLOOR",
        "sl_pct": 0.30, "max_sl": 5000, "max_entries": 3,
        "floors": [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500],
    },
    {
        "name": "BALANCED",
        "sl_pct": 0.15, "max_sl": 1500, "max_entries": 2,
        "floors": [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500],
    },
    {
        "name": "TRAILING",
        "sl_pct": 0.15, "max_sl": 1500, "max_entries": 2,
        "floors": [],  # no floor — trailing SL only (50% of peak)
        "trailing_pct": 0.50,  # lock 50% of peak net pnl
    },
    {
        "name": "ULTRA_TIGHT",
        "sl_pct": 0.10, "max_sl": 1000, "max_entries": 1,
        "floors": [1500, 3000, 4500, 6000, 7500, 9000],
    },
]


def calc_charges(entry_price, exit_price, qty):
    buy_tv = entry_price * qty
    sell_tv = exit_price * qty
    total_tv = buy_tv + sell_tv
    brokerage = 40.0
    stt = sell_tv * 0.001
    exchange_txn = total_tv * 0.000495
    sebi = total_tv * 0.000001
    stamp = buy_tv * 0.00003
    gst = (brokerage + exchange_txn) * 0.18
    return brokerage + stt + exchange_txn + sebi + stamp + gst


def get_stepped_floor(net_pnl, floor_levels):
    locked = 0
    for lvl in floor_levels:
        if net_pnl >= lvl:
            locked = lvl
        else:
            break
    return locked


def parse_ts(c):
    ts = c.get("ts") or c.get("date") or c.get("timestamp")
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if hasattr(ts, "astimezone"):
        ts = ts.astimezone(IST)
    return ts.replace(tzinfo=None) if ts.tzinfo else ts


def build_5min(candles_1m):
    bars = []
    bucket = None
    for c in candles_1m:
        ct = parse_ts(c)
        bs = ct.replace(minute=(ct.minute // 5) * 5, second=0, microsecond=0)
        if bucket is None or bucket["time"] != bs:
            if bucket:
                bars.append(bucket)
            bucket = {"time": bs, "open": c["open"], "high": c["high"],
                      "low": c["low"], "close": c["close"], "volume": c.get("volume", 0) or 0}
        else:
            bucket["high"] = max(bucket["high"], c["high"])
            bucket["low"] = min(bucket["low"], c["low"])
            bucket["close"] = c["close"]
            bucket["volume"] += c.get("volume", 0) or 0
    if bucket:
        bars.append(bucket)
    return bars


async def _fetch_one(ud, sym, key, from_dt, to_dt):
    try:
        c = await asyncio.wait_for(
            asyncio.to_thread(ud.historical_data, key, from_dt, to_dt, "1minute"),
            timeout=15
        )
        if c and len(c) > 5:
            return (sym, c)
    except Exception:
        pass
    return None


async def _fetch_batch(ud, pairs, from_dt, to_dt, concurrency=15, label=""):
    sem = asyncio.Semaphore(concurrency)
    done = 0
    total = len(pairs)
    async def _wrapped(sym, key):
        nonlocal done
        async with sem:
            r = await _fetch_one(ud, sym, key, from_dt, to_dt)
            done += 1
            if done % 50 == 0 or done == total:
                sys.stdout.write(f"  [{label}] {done}/{total}\n")
                sys.stdout.flush()
            return r
    tasks = [_wrapped(sym, key) for sym, key in pairs]
    results = await asyncio.gather(*tasks)
    return {sym: c for r in results if r for sym, c in [r]}


def find_oeh_candidates(equity_candles):
    candidates = []
    for sym, candles_1m in equity_candles.items():
        if not candles_1m or len(candles_1m) < 5:
            continue
        bars5 = build_5min(candles_1m)
        if not bars5:
            continue
        open_price = bars5[0]["open"]
        if open_price <= 0:
            continue
        max_high = 0
        latest_close = open_price
        for b in bars5:
            if b["time"].time() > SCAN_TIME:
                break
            max_high = max(max_high, b["high"])
            latest_close = b["close"]
        if max_high > open_price + TOLERANCE:
            continue
        drop_pct = (open_price - latest_close) / open_price * 100
        if drop_pct < MIN_MOVE_PCT:
            continue
        candidates.append({"symbol": sym, "entry_close": latest_close,
                           "move_pct": drop_pct, "option_type": "PE"})
    candidates.sort(key=lambda x: x["move_pct"], reverse=True)
    return candidates


def find_oel_candidates(equity_candles):
    candidates = []
    for sym, candles_1m in equity_candles.items():
        if not candles_1m or len(candles_1m) < 5:
            continue
        bars5 = build_5min(candles_1m)
        if not bars5:
            continue
        open_price = bars5[0]["open"]
        if open_price <= 0:
            continue
        min_low = 999999
        latest_close = open_price
        for b in bars5:
            if b["time"].time() > SCAN_TIME:
                break
            min_low = min(min_low, b["low"])
            latest_close = b["close"]
        if min_low < open_price - TOLERANCE:
            continue
        rise_pct = (latest_close - open_price) / open_price * 100
        if rise_pct < MIN_MOVE_PCT:
            continue
        candidates.append({"symbol": sym, "entry_close": latest_close,
                           "move_pct": rise_pct, "option_type": "CE"})
    candidates.sort(key=lambda x: x["move_pct"], reverse=True)
    return candidates


def sim_trade(candles, entry_price, qty, sl_price, floor_levels, trailing_pct, DATE):
    ep_slip = entry_price * (1 + SLIPPAGE_PCT)
    peak_net = 0
    floor_val = 0
    trailing_floor = 0
    first = True

    after = []
    for c in candles:
        ct = parse_ts(c)
        if ct >= datetime.combine(DATE, SCAN_TIME):
            after.append(c)
    if not after:
        return None

    for c in after:
        low, high, close = c["low"], c["high"], c["close"]

        if low <= sl_price:
            xp = close * (1 - SLIPPAGE_PCT)
            gross = (xp - ep_slip) * qty
            charges = calc_charges(ep_slip, xp, qty)
            return {"pnl": round(gross - charges), "reason": "SL",
                    "xt": parse_ts(c), "xp": xp, "qty": qty}

        if not first:
            # Stepped floor exit
            if floor_levels and floor_val > 0:
                close_gross = (close - ep_slip) * qty
                close_charges = calc_charges(ep_slip, close, qty)
                if close_gross - close_charges <= floor_val:
                    xp = close * (1 - SLIPPAGE_PCT)
                    gross = (xp - ep_slip) * qty
                    charges = calc_charges(ep_slip, xp, qty)
                    return {"pnl": round(gross - charges), "reason": "FLOOR",
                            "xt": parse_ts(c), "xp": xp, "qty": qty}

            # Trailing stop exit
            if trailing_pct and trailing_floor > 0:
                close_gross = (close - ep_slip) * qty
                close_charges = calc_charges(ep_slip, close, qty)
                close_net = close_gross - close_charges
                if close_net <= trailing_floor:
                    xp = close * (1 - SLIPPAGE_PCT)
                    gross = (xp - ep_slip) * qty
                    charges = calc_charges(ep_slip, xp, qty)
                    return {"pnl": round(gross - charges), "reason": "TRAIL",
                            "xt": parse_ts(c), "xp": xp, "qty": qty}

        # Update peak
        high_gross = (high - ep_slip) * qty
        high_charges = calc_charges(ep_slip, high, qty)
        high_net = high_gross - high_charges
        peak_net = max(peak_net, high_net)

        if floor_levels:
            nf = get_stepped_floor(peak_net, floor_levels)
            if nf > floor_val:
                floor_val = nf

        if trailing_pct and peak_net > 500:
            trailing_floor = max(trailing_floor, peak_net * trailing_pct)

        first = False

    last = after[-1]
    xp = last["close"] * (1 - SLIPPAGE_PCT)
    gross = (xp - ep_slip) * qty
    charges = calc_charges(ep_slip, xp, qty)
    return {"pnl": round(gross - charges), "reason": "EOD",
            "xt": parse_ts(last), "xp": xp, "qty": qty}


def run_strategy(candidates, opt_candles, lot_sizes, strike_steps, option_keys, params, DATE):
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

    trades = []
    cap = CAPITAL
    sym_count = defaultdict(int)
    sl_blocked = set()
    open_pos = []

    sl_pct = params["sl_pct"]
    max_sl = params["max_sl"]
    max_entries = params["max_entries"]
    floor_levels = params["floors"]
    trailing_pct = params.get("trailing_pct", 0)

    for c in candidates[:30]:
        sym = c["symbol"]
        if sym in BLOCKLIST:
            continue
        ot = c["option_type"]
        edt = datetime.combine(DATE, SCAN_TIME)

        still = []
        for p in open_pos:
            if p["xt"] <= edt:
                cap += p["xp"] * p["qty"]
            else:
                still.append(p)
        open_pos = still

        if sym_count[sym] >= max_entries:
            continue
        if sym in sl_blocked:
            continue

        strike = find_atm(sym, c["entry_close"], ot)
        if strike is None or (sym, strike, ot) not in opt_candles:
            continue

        candles = opt_candles[(sym, strike, ot)]
        ep = None
        for bar in candles:
            ct = parse_ts(bar)
            if ct <= edt:
                ep = bar["close"]
            else:
                break
        if not ep or ep < 2:
            continue

        lot = lot_sizes.get(sym, 1) * 2
        cost = ep * lot
        if cost > cap:
            continue

        sl_pct_price = ep * (1 - sl_pct)
        sl_cap_price = ep - (max_sl / lot)
        sl_price = max(sl_pct_price, sl_cap_price)

        t = sim_trade(candles, ep, lot, sl_price, floor_levels, trailing_pct, DATE)
        if t is None:
            continue

        trades.append({**t, "sym": sym})
        cap -= cost
        sym_count[sym] += 1
        open_pos.append({"xt": t["xt"], "xp": t["xp"], "qty": t["qty"]})
        if t["reason"] == "SL":
            sl_blocked.add(sym)

    return trades


async def run_one_day(ud, DATE, universe, eq_keys, lot_sizes, option_keys, strike_steps, nifty_key):
    from_dt = datetime.combine(DATE, dtime(9, 15))
    to_dt = datetime.combine(DATE, dtime(15, 30))

    syms_with_keys = [(s, eq_keys[s]) for s in universe if s in eq_keys]
    equity_candles = await _fetch_batch(ud, syms_with_keys, from_dt, to_dt, concurrency=15, label="EQ")
    if len(equity_candles) < 50:
        return None

    # NIFTY regime
    regime = "UNKNOWN"
    if nifty_key:
        try:
            nc = ud.historical_data(nifty_key, from_dt, to_dt, "1minute")
            if nc and len(nc) >= 3:
                bars15 = build_5min(nc)
                nifty_open = bars15[0]["open"] if bars15 else None
                nifty_close_15 = None
                for b in bars15:
                    if b["time"].time() <= dtime(9, 30):
                        nifty_close_15 = b["close"]
                if nifty_open and nifty_close_15:
                    regime = "BEARISH" if nifty_close_15 < nifty_open else "BULLISH"
        except Exception:
            pass

    sys.stdout.write(f"  Regime: {regime}\n")
    sys.stdout.flush()

    oeh_cands = find_oeh_candidates(equity_candles)
    oel_cands = find_oel_candidates(equity_candles)

    # Collect all needed options
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
    for c in oeh_cands[:30]:
        s = find_atm(c["symbol"], c["entry_close"], "PE")
        if s is not None:
            needed.add((c["symbol"], s, "PE"))
    for c in oel_cands[:30]:
        s = find_atm(c["symbol"], c["entry_close"], "CE")
        if s is not None:
            needed.add((c["symbol"], s, "CE"))

    opt_pairs = []
    opt_map = {}
    for sym, strike, ot in needed:
        info = option_keys.get(sym, {}).get(strike, {}).get(ot)
        if info:
            label = f"{sym}_{strike}_{ot}"
            opt_pairs.append((label, info["key"]))
            opt_map[label] = (sym, strike, ot)

    sys.stdout.write(f"  OEH:{len(oeh_cands)} OEL:{len(oel_cands)} opts:{len(opt_pairs)}\n")
    sys.stdout.flush()
    raw = await _fetch_batch(ud, opt_pairs, from_dt, to_dt, concurrency=15, label="OPT")
    opt_candles = {}
    for label, candles in raw.items():
        key = opt_map.get(label)
        if key:
            opt_candles[key] = candles

    # Pick candidates based on regime
    if regime == "BEARISH":
        active_cands = oeh_cands
        strat_label = "OEH"
    elif regime == "BULLISH":
        active_cands = oel_cands
        strat_label = "OEL"
    else:
        active_cands = oeh_cands + oel_cands
        strat_label = "BOTH"

    # Run each param set on the regime-selected candidates
    results = {}
    for ps in PARAM_SETS:
        trades = run_strategy(active_cands, opt_candles, lot_sizes, strike_steps,
                              option_keys, ps, DATE)
        pnl = sum(t["pnl"] for t in trades)
        w = sum(1 for t in trades if t["pnl"] > 0)
        l = len(trades) - w
        sl = sum(1 for t in trades if t["reason"] == "SL")
        fl = sum(1 for t in trades if t["reason"] in ("FLOOR", "TRAIL"))
        eod = sum(1 for t in trades if t["reason"] == "EOD")
        aw = sum(t["pnl"] for t in trades if t["pnl"] > 0) / max(1, w)
        al = sum(t["pnl"] for t in trades if t["pnl"] < 0) / max(1, l)

        results[ps["name"]] = {
            "pnl": pnl, "trades": len(trades), "wins": w, "losses": l,
            "sl": sl, "floor": fl, "eod": eod, "avg_win": aw, "avg_loss": al,
        }

    line_parts = []
    for ps in PARAM_SETS:
        r = results[ps["name"]]
        line_parts.append(f"{ps['name']}:₹{r['pnl']:>+7,}({r['trades']}tr)")
    sys.stdout.write(f"  {strat_label} | {' | '.join(line_parts)}\n")
    sys.stdout.flush()

    return {"regime": regime, "strat": strat_label, "results": results}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", required=True)
    parser.add_argument("--to", dest="to_date", required=True)
    args = parser.parse_args()

    d_from = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    d_to = datetime.strptime(args.to_date, "%Y-%m-%d").date()

    token = load_cached_token()
    if not token:
        print("ERROR: No token")
        return

    ud = UpstoxData(access_token=token)
    master = ud._load_master()
    print(f"Loaded {len(master)} instruments", flush=True)

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

    nifty_key = None
    for inst in master:
        name = inst.get("name", "") or inst.get("trading_symbol", "") or ""
        if "Nifty 50" in name or "NIFTY 50" in name:
            if inst.get("segment") in ("NSE_INDEX", "NSE_EQ"):
                nifty_key = inst.get("instrument_key")
                break
    if not nifty_key:
        nifty_key = getattr(config, "UPSTOX_INDEX_KEYS", {}).get("NSE:NIFTY 50")
    print(f"Universe: {len(universe)} stocks | NIFTY: {nifty_key}", flush=True)

    names = [ps["name"] for ps in PARAM_SETS]
    grand = {n: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0,
                 "sl": 0, "floor": 0, "eod": 0, "days_pos": 0, "days_neg": 0,
                 "sum_avg_win": 0, "sum_avg_loss": 0, "day_count": 0}
             for n in names}
    daily_rows = []
    trading_days = 0

    d = d_from
    while d <= d_to:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue

        t0 = _time.time()
        sys.stdout.write(f"\n{'='*70}\n  {d.strftime('%a %d %b %Y')}\n")
        sys.stdout.flush()

        res = await run_one_day(ud, d, universe, eq_keys, lot_sizes, option_keys, strike_steps, nifty_key)
        elapsed = _time.time() - t0
        if res is None:
            sys.stdout.write(f"  HOLIDAY ({elapsed:.0f}s)\n")
            d += timedelta(days=1)
            continue

        trading_days += 1
        sys.stdout.write(f"  done ({elapsed:.0f}s)\n")
        sys.stdout.flush()

        row = {"date": d, "regime": res["regime"], "strat": res["strat"]}
        for n in names:
            r = res["results"][n]
            row[n] = r["pnl"]
            row[f"{n}_tr"] = r["trades"]
            grand[n]["pnl"] += r["pnl"]
            grand[n]["trades"] += r["trades"]
            grand[n]["wins"] += r["wins"]
            grand[n]["losses"] += r["losses"]
            grand[n]["sl"] += r["sl"]
            grand[n]["floor"] += r["floor"]
            grand[n]["eod"] += r["eod"]
            grand[n]["sum_avg_win"] += r["avg_win"]
            grand[n]["sum_avg_loss"] += r["avg_loss"]
            grand[n]["day_count"] += 1
            if r["pnl"] > 0:
                grand[n]["days_pos"] += 1
            elif r["pnl"] < 0:
                grand[n]["days_neg"] += 1
        daily_rows.append(row)
        d += timedelta(days=1)

    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*100}")
    print(f"  RISK:REWARD GRID — {d_from} to {d_to} ({trading_days} days)")
    print(f"  All configs use NIFTY regime filter (OEH on bearish, OEL on bullish)")
    print(f"{'='*100}")

    # Summary table
    print(f"\n{'Config':<14} {'SL%':>4} {'MaxSL':>6} {'Floor1':>7} {'Tr':>4} "
          f"{'PnL':>10} {'W':>3} {'L':>3} {'WR%':>5} {'SL':>3} {'Fl':>3} {'EOD':>3} "
          f"{'AvgW':>7} {'AvgL':>7} {'D+':>3} {'D-':>3} {'Avg/D':>8}")
    print("-" * 105)

    for ps in PARAM_SETS:
        n = ps["name"]
        g = grand[n]
        wr = g["wins"] / max(1, g["trades"]) * 100
        avg_d = g["pnl"] / max(1, trading_days)
        aw = g["sum_avg_win"] / max(1, g["day_count"])
        al = g["sum_avg_loss"] / max(1, g["day_count"])
        fl1 = ps["floors"][0] if ps["floors"] else 0
        trail = ps.get("trailing_pct", 0)
        fl_label = f"T{trail:.0%}" if trail else str(fl1)
        print(f"{n:<14} {ps['sl_pct']:>3.0%} {ps['max_sl']:>6} {fl_label:>7} {g['trades']:>4} "
              f"{g['pnl']:>+10,} {g['wins']:>3} {g['losses']:>3} {wr:>4.0f}% {g['sl']:>3} {g['floor']:>3} {g['eod']:>3} "
              f"{aw:>+7,.0f} {al:>+7,.0f} {g['days_pos']:>3} {g['days_neg']:>3} {avg_d:>+8,.0f}")

    # Daily breakdown for top 3
    sorted_names = sorted(names, key=lambda n: grand[n]["pnl"], reverse=True)
    top3 = sorted_names[:3]

    print(f"\n{'='*100}")
    print(f"  DAILY BREAKDOWN — Top 3 configs")
    print(f"{'='*100}")
    hdr = f"{'Date':<14} {'Rgm':>4}"
    for n in top3:
        hdr += f" {n:>12}"
    print(hdr)
    print("-" * (20 + len(top3) * 13))
    for r in daily_rows:
        tag = "🔴" if r["regime"] == "BEARISH" else "🟢"
        line = f"{r['date'].strftime('%d %b'):>6} {r['date'].strftime('%a'):<5} {tag:>4}"
        for n in top3:
            line += f" {r[n]:>+12,}"
        print(line)
    print("-" * (20 + len(top3) * 13))
    line = f"{'TOTAL':<14} {'':>4}"
    for n in top3:
        line += f" {grand[n]['pnl']:>+12,}"
    print(line)

    best = sorted_names[0]
    print(f"\n✅ Best config: {best} → ₹{grand[best]['pnl']:+,} ({grand[best]['trades']} trades)")
    bp = [ps for ps in PARAM_SETS if ps["name"] == best][0]
    print(f"   SL={bp['sl_pct']:.0%}, MaxSL=₹{bp['max_sl']}, MaxEntry={bp['max_entries']}")
    if bp["floors"]:
        print(f"   Floors: {bp['floors']}")
    if bp.get("trailing_pct"):
        print(f"   Trailing: {bp['trailing_pct']:.0%} of peak")


if __name__ == "__main__":
    asyncio.run(main())

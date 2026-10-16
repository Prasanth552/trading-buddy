#!/usr/bin/env python3
"""
ORB (Opening Range Breakout) backtest — grid of configs with optional regime filter.

Tests multiple SL / floor / regime combos against the same ORB candidates.

Usage:
    PYTHONPATH=. .venv/bin/python3 -u scripts/backtest_orb_grid.py \
        --from 2026-10-01 --to 2026-10-16
"""

import asyncio, sys, os, argparse, time as _time, re as _re
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo(config.TIMEZONE)

CAPITAL = 300000
SLIPPAGE_PCT = 0.005
BLOCKLIST = {"GODREJCP", "GRASIM"}

# ORB detection params (same as live)
ORB_MIN_RANGE_PCT = 0.3
ORB_MAX_RANGE_PCT = 3.0
ORB_MAX_ENTRIES_PER_SYM = 3
ORB_TIME_CUTOFF = dtime(13, 0)
ORB_SL_COOLDOWN = True

# Configs to test
CONFIGS = {
    "CURRENT": {
        "sl_pct": 0.30, "max_sl": 5000,
        "floors": [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000],
        "regime": False,
    },
    "HIGH_FLOOR": {
        "sl_pct": 0.30, "max_sl": 5000,
        "floors": [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500],
        "regime": False,
    },
    "TIGHT_SL": {
        "sl_pct": 0.15, "max_sl": 2500,
        "floors": [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000],
        "regime": False,
    },
    "TIGHT+HIGH": {
        "sl_pct": 0.15, "max_sl": 2500,
        "floors": [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500],
        "regime": False,
    },
    "REGIME_HF": {
        "sl_pct": 0.30, "max_sl": 5000,
        "floors": [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500],
        "regime": True,  # CE only on BULLISH, PE only on BEARISH
    },
    "REGIME_T+H": {
        "sl_pct": 0.15, "max_sl": 2500,
        "floors": [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500],
        "regime": True,
    },
}


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


def find_orb_candidates(equity_candles):
    """Find ORB breakout candidates from first 5-min candle."""
    candidates = []
    for sym, candles_1m in equity_candles.items():
        if not candles_1m or len(candles_1m) < 10:
            continue
        bars5 = build_5min(candles_1m)
        if not bars5 or len(bars5) < 3:
            continue

        range_high = bars5[0]["high"]
        range_low = bars5[0]["low"]
        range_open = bars5[0]["open"]
        if range_open <= 0:
            continue
        range_pct = (range_high - range_low) / range_open * 100
        if range_pct < ORB_MIN_RANGE_PCT or range_pct > ORB_MAX_RANGE_PCT:
            continue

        for dc in bars5[1:]:
            if dc["high"] > range_high:
                candidates.append({
                    "symbol": sym, "direction": "bullish",
                    "option_type": "CE",
                    "range_pct": range_pct,
                    "breakout_price": dc["close"],
                    "breakout_time": dc["time"],
                })
                break
            elif dc["low"] < range_low:
                candidates.append({
                    "symbol": sym, "direction": "bearish",
                    "option_type": "PE",
                    "range_pct": range_pct,
                    "breakout_price": dc["close"],
                    "breakout_time": dc["time"],
                })
                break

    candidates.sort(key=lambda x: x["range_pct"], reverse=True)
    return candidates


def sim_trade(candles, entry_time, entry_price, qty, sl_price, floor_levels, DATE):
    ep_slip = entry_price * (1 + SLIPPAGE_PCT)
    peak_net = 0
    floor_val = 0
    first = True

    after = []
    for c in candles:
        ct = parse_ts(c)
        if ct >= entry_time:
            after.append(c)
    if not after:
        return None

    for c in after:
        low, high, close = c["low"], c["high"], c["close"]

        if low <= sl_price:
            xp = close * (1 - SLIPPAGE_PCT)
            gross = (xp - ep_slip) * qty
            charges = calc_charges(ep_slip, xp, qty)
            return {"pnl": round(gross - charges), "xp": xp, "reason": "SL",
                    "xt": parse_ts(c), "qty": qty}

        if not first and floor_val > 0:
            close_gross = (close - ep_slip) * qty
            close_charges = calc_charges(ep_slip, close, qty)
            if close_gross - close_charges <= floor_val:
                xp = close * (1 - SLIPPAGE_PCT)
                gross = (xp - ep_slip) * qty
                charges = calc_charges(ep_slip, xp, qty)
                return {"pnl": round(gross - charges), "xp": xp, "reason": "FLOOR",
                        "xt": parse_ts(c), "qty": qty}

        high_gross = (high - ep_slip) * qty
        high_charges = calc_charges(ep_slip, high, qty)
        peak_net = max(peak_net, high_gross - high_charges)
        nf = get_stepped_floor(peak_net, floor_levels)
        if nf > floor_val:
            floor_val = nf
        first = False

    last = after[-1]
    xp = last["close"] * (1 - SLIPPAGE_PCT)
    gross = (xp - ep_slip) * qty
    charges = calc_charges(ep_slip, xp, qty)
    return {"pnl": round(gross - charges), "xp": xp, "reason": "EOD",
            "xt": parse_ts(last), "qty": qty}


def run_orb_config(candidates, opt_candles, lot_sizes, strike_steps, option_keys,
                   cfg, regime, DATE):
    """Run ORB for one config on one day."""

    sl_pct = cfg["sl_pct"]
    max_sl = cfg["max_sl"]
    floors = cfg["floors"]
    use_regime = cfg["regime"]

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
    daily_pnl = 0
    loss_cap = 20000
    profit_cap = 25000

    for c in candidates[:30]:
        if abs(daily_pnl) >= loss_cap or daily_pnl >= profit_cap:
            break

        sym = c["symbol"]
        if sym in BLOCKLIST:
            continue

        ot = c["option_type"]

        # Regime filter: CE only on BULLISH, PE only on BEARISH
        if use_regime:
            if ot == "CE" and regime == "BEARISH":
                continue
            if ot == "PE" and regime == "BULLISH":
                continue

        bt = c["breakout_time"]
        if isinstance(bt, str):
            bt = datetime.fromisoformat(bt)
        if bt.time() >= ORB_TIME_CUTOFF:
            continue

        if sym_count[sym] >= ORB_MAX_ENTRIES_PER_SYM:
            continue
        if ORB_SL_COOLDOWN and sym in sl_blocked:
            continue

        strike = find_atm(sym, c["breakout_price"], ot)
        if strike is None or (sym, strike, ot) not in opt_candles:
            continue

        candles = opt_candles[(sym, strike, ot)]
        ep = None
        for bar in candles:
            ct = parse_ts(bar)
            if ct <= bt:
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

        t = sim_trade(candles, bt, ep, lot, sl_price, floors, DATE)
        if t is None:
            continue

        trades.append({**t, "sym": sym, "otype": ot})
        cap -= cost
        sym_count[sym] += 1
        daily_pnl += t["pnl"]
        if t["reason"] == "SL":
            sl_blocked.add(sym)

    pnl = sum(t["pnl"] for t in trades)
    w = sum(1 for t in trades if t["pnl"] > 0)
    l = sum(1 for t in trades if t["pnl"] <= 0)
    sl = sum(1 for t in trades if t["reason"] == "SL")
    fl = sum(1 for t in trades if t["reason"] == "FLOOR")
    return {"pnl": pnl, "trades": len(trades), "wins": w, "losses": l, "sl": sl, "fl": fl}


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
                    regime = "BULLISH" if nifty_close_15 > nifty_open else "BEARISH"
        except Exception as exc:
            sys.stdout.write(f"  NIFTY fetch error: {exc}\n")

    sys.stdout.write(f"  Regime: {regime}\n")
    sys.stdout.flush()

    # Find ORB candidates
    candidates = find_orb_candidates(equity_candles)
    ce_count = sum(1 for c in candidates if c["option_type"] == "CE")
    pe_count = sum(1 for c in candidates if c["option_type"] == "PE")
    sys.stdout.write(f"  ORB candidates: {len(candidates)} (CE:{ce_count} PE:{pe_count})\n")
    sys.stdout.flush()

    if not candidates:
        return {"regime": regime, "configs": {name: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "sl": 0, "fl": 0} for name in CONFIGS}}

    # Collect needed options
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
    for c in candidates[:30]:
        s = find_atm(c["symbol"], c["breakout_price"], c["option_type"])
        if s is not None:
            needed.add((c["symbol"], s, c["option_type"]))

    opt_pairs = []
    opt_map = {}
    for sym, strike, ot in needed:
        info = option_keys.get(sym, {}).get(strike, {}).get(ot)
        if info:
            label = f"{sym}_{strike}_{ot}"
            opt_pairs.append((label, info["key"]))
            opt_map[label] = (sym, strike, ot)

    sys.stdout.write(f"  Fetching {len(opt_pairs)} options...\n")
    sys.stdout.flush()
    raw = await _fetch_batch(ud, opt_pairs, from_dt, to_dt, concurrency=15, label="OPT")
    opt_candles = {}
    for label, candles in raw.items():
        key = opt_map.get(label)
        if key:
            opt_candles[key] = candles

    # Run each config
    results = {}
    for name, cfg in CONFIGS.items():
        results[name] = run_orb_config(candidates, opt_candles, lot_sizes, strike_steps,
                                        option_keys, cfg, regime, DATE)

    # Print day summary
    parts = []
    for name in CONFIGS:
        r = results[name]
        parts.append(f"{name}:₹{r['pnl']:>+8,}({r['trades']}tr)")
    sys.stdout.write(f"  {' | '.join(parts)}\n")
    sys.stdout.flush()

    return {"regime": regime, "configs": results}


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
    print(f"Universe: {len(universe)} stocks | NIFTY key: {nifty_key}", flush=True)

    config_names = list(CONFIGS.keys())
    grand = {n: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "sl": 0, "fl": 0,
                 "days_pos": 0, "days_neg": 0} for n in config_names}
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

        row = {"date": d, "regime": res["regime"]}
        for n in config_names:
            r = res["configs"][n]
            row[n] = r["pnl"]
            row[f"{n}_tr"] = r["trades"]
            for k in ("pnl", "trades", "wins", "losses", "sl", "fl"):
                grand[n][k] += r[k]
            if r["pnl"] > 0:
                grand[n]["days_pos"] += 1
            elif r["pnl"] < 0:
                grand[n]["days_neg"] += 1
        daily_rows.append(row)
        d += timedelta(days=1)

    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*100}")
    print(f"  ORB BACKTEST GRID — {d_from} to {d_to} ({trading_days} days)")
    print(f"{'='*100}\n")

    hdr = f"{'Config':<14} {'SL%':>4} {'MaxSL':>6} {'Floor1':>7} {'Rgm':>4}   {'PnL':>10} {'W':>4} {'L':>4} {'WR%':>5} {'SL':>3} {'Fl':>3} {'AvgW':>7} {'AvgL':>7} {'D+':>3} {'D-':>3} {'Avg/D':>8}"
    print(hdr)
    print("-" * len(hdr))

    for n in config_names:
        g = grand[n]
        cfg = CONFIGS[n]
        wr = g["wins"] / max(1, g["wins"] + g["losses"]) * 100
        avg_w = g["pnl"] / max(1, g["wins"]) if g["wins"] else 0  # rough
        # compute real avg win/loss from totals... we don't have them per-trade here
        # so just show avg/day
        avg_day = g["pnl"] / max(1, trading_days)
        rgm = "Y" if cfg["regime"] else "N"
        print(f"{n:<14} {cfg['sl_pct']*100:>3.0f}% {cfg['max_sl']:>6} {cfg['floors'][0]:>7}   {rgm:>4}"
              f"  {g['pnl']:>+10,} {g['wins']:>4} {g['losses']:>4} {wr:>4.0f}% {g['sl']:>3} {g['fl']:>3}"
              f"       {'':>7} {g['days_pos']:>3} {g['days_neg']:>3} {avg_day:>+8,.0f}")

    # Daily breakdown — top configs
    top3 = sorted(config_names, key=lambda n: grand[n]["pnl"], reverse=True)[:4]
    print(f"\n{'='*100}")
    print(f"  DAILY BREAKDOWN")
    print(f"{'='*100}")
    hdr2 = f"{'Date':<16} {'Rgm':<5} " + " ".join(f"{n:>12}" for n in top3)
    print(hdr2)
    print("-" * len(hdr2))
    for r in daily_rows:
        regime_tag = "\U0001f534" if r["regime"] == "BEARISH" else "\U0001f7e2" if r["regime"] == "BULLISH" else "?"
        cols = " ".join(f"{r.get(n, 0):>+12,}" for n in top3)
        print(f"{r['date'].strftime('%d %b'):>6} {r['date'].strftime('%a'):<5}  {regime_tag:<5} {cols}")
    print("-" * len(hdr2))
    totals = " ".join(f"{grand[n]['pnl']:>+12,}" for n in top3)
    print(f"{'TOTAL':>14}        {totals}")

    best = max(config_names, key=lambda n: grand[n]["pnl"])
    bc = CONFIGS[best]
    print(f"\n✅ Best config: {best} → ₹{grand[best]['pnl']:>+,} ({grand[best]['trades']} trades)")
    print(f"   SL={bc['sl_pct']*100:.0f}%, MaxSL=₹{bc['max_sl']}, Regime={'Yes' if bc['regime'] else 'No'}")
    print(f"   Floors: {bc['floors']}")


if __name__ == "__main__":
    asyncio.run(main())

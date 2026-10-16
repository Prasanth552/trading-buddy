#!/usr/bin/env python3
"""
OEH + OEL regime-filtered backtest.

Compares 3 modes:
  1. BOTH_ALWAYS — OEH + OEL run every day (current behavior)
  2. REGIME — OEH only on bearish days (NIFTY red), OEL only on bullish days (NIFTY green)
  3. OEH_ONLY / OEL_ONLY — solo performance for reference

NIFTY regime = first 15-min candle direction (red=bearish, green=bullish).

Usage:
    PYTHONPATH=. .venv/bin/python3 -u scripts/backtest_oeh_oel_regime.py \
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

# OEH params (current)
OEH_SL_PCT = 0.30
OEH_MAX_SL = 5000
OEH_MAX_ENTRIES = 3
OEH_FLOORS = [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500]
OEH_SCAN_TIME = dtime(9, 25)
OEH_CUTOFF = dtime(13, 0)

# OEL params (current)
OEL_SL_PCT = 0.30
OEL_MAX_SL = 5000
OEL_MAX_ENTRIES = 1
OEL_FLOORS = [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500]
OEL_SCAN_TIME = dtime(9, 25)
OEL_CUTOFF = dtime(14, 0)

BLOCKLIST = {"GODREJCP", "GRASIM"}


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
    """Open=High stocks → buy PEs."""
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
            if b["time"].time() > OEH_SCAN_TIME:
                break
            max_high = max(max_high, b["high"])
            latest_close = b["close"]
        if max_high > open_price + TOLERANCE:
            continue
        drop_pct = (open_price - latest_close) / open_price * 100
        if drop_pct < MIN_MOVE_PCT:
            continue
        candidates.append({"symbol": sym, "entry_close": latest_close,
                           "move_pct": drop_pct, "entry_time": OEH_SCAN_TIME,
                           "option_type": "PE"})
    candidates.sort(key=lambda x: x["move_pct"], reverse=True)
    return candidates


def find_oel_candidates(equity_candles):
    """Open=Low stocks → buy CEs."""
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
            if b["time"].time() > OEL_SCAN_TIME:
                break
            min_low = min(min_low, b["low"])
            latest_close = b["close"]
        if min_low < open_price - TOLERANCE:
            continue
        rise_pct = (latest_close - open_price) / open_price * 100
        if rise_pct < MIN_MOVE_PCT:
            continue
        candidates.append({"symbol": sym, "entry_close": latest_close,
                           "move_pct": rise_pct, "entry_time": OEL_SCAN_TIME,
                           "option_type": "CE"})
    candidates.sort(key=lambda x: x["move_pct"], reverse=True)
    return candidates


def sim_trade(candles, entry_time, entry_price, qty, sl_price, floor_levels, DATE):
    ep_slip = entry_price * (1 + SLIPPAGE_PCT)
    peak_net = 0
    floor_val = 0
    first = True

    after = []
    for c in candles:
        ct = parse_ts(c)
        if ct >= datetime.combine(DATE, entry_time):
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


def run_strategy(candidates, opt_candles, lot_sizes, strike_steps, option_keys,
                 sl_pct, max_sl, max_entries, floor_levels, DATE):
    """Run one strategy (OEH or OEL) for a day, return list of trade results."""

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

    for c in candidates[:30]:
        sym = c["symbol"]
        if sym in BLOCKLIST:
            continue
        ot = c["option_type"]
        entry_time = c["entry_time"]
        edt = datetime.combine(DATE, entry_time)

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

        t = sim_trade(candles, entry_time, ep, lot, sl_price, floor_levels, DATE)
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

    # Fetch equity candles
    syms_with_keys = [(s, eq_keys[s]) for s in universe if s in eq_keys]
    equity_candles = await _fetch_batch(ud, syms_with_keys, from_dt, to_dt, concurrency=15, label="EQ")
    if len(equity_candles) < 50:
        return None

    # Determine NIFTY regime from first 15-min candle
    regime = "UNKNOWN"
    if nifty_key:
        try:
            nc = ud.historical_data(nifty_key, from_dt, to_dt, "1minute")
            if nc and len(nc) >= 3:
                bars15 = build_5min(nc)
                # Use first 3 five-min bars (= 15 min)
                nifty_open = bars15[0]["open"] if bars15 else None
                nifty_close_15 = None
                for b in bars15:
                    if b["time"].time() <= dtime(9, 30):
                        nifty_close_15 = b["close"]
                if nifty_open and nifty_close_15:
                    if nifty_close_15 > nifty_open:
                        regime = "BULLISH"
                    else:
                        regime = "BEARISH"
        except Exception as exc:
            sys.stdout.write(f"  NIFTY fetch error: {exc}\n")

    sys.stdout.write(f"  NIFTY regime: {regime}\n")
    sys.stdout.flush()

    # Find candidates
    oeh_cands = find_oeh_candidates(equity_candles)
    oel_cands = find_oel_candidates(equity_candles)
    sys.stdout.write(f"  OEH candidates: {len(oeh_cands)} | OEL candidates: {len(oel_cands)}\n")
    sys.stdout.flush()

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

    sys.stdout.write(f"  Fetching {len(opt_pairs)} options...\n")
    sys.stdout.flush()
    raw = await _fetch_batch(ud, opt_pairs, from_dt, to_dt, concurrency=15, label="OPT")
    opt_candles = {}
    for label, candles in raw.items():
        key = opt_map.get(label)
        if key:
            opt_candles[key] = candles

    # Run strategies
    oeh_trades = run_strategy(oeh_cands, opt_candles, lot_sizes, strike_steps, option_keys,
                              OEH_SL_PCT, OEH_MAX_SL, OEH_MAX_ENTRIES, OEH_FLOORS, DATE)
    oel_trades = run_strategy(oel_cands, opt_candles, lot_sizes, strike_steps, option_keys,
                              OEL_SL_PCT, OEL_MAX_SL, OEL_MAX_ENTRIES, OEL_FLOORS, DATE)

    def summarize(trades):
        pnl = sum(t["pnl"] for t in trades)
        w = sum(1 for t in trades if t["pnl"] > 0)
        l = len(trades) - w
        sl = sum(1 for t in trades if t["reason"] == "SL")
        return {"pnl": pnl, "trades": len(trades), "wins": w, "losses": l, "sl": sl}

    oeh_s = summarize(oeh_trades)
    oel_s = summarize(oel_trades)

    # Mode results
    both_pnl = oeh_s["pnl"] + oel_s["pnl"]
    both_trades = oeh_s["trades"] + oel_s["trades"]

    if regime == "BEARISH":
        regime_pnl = oeh_s["pnl"]
        regime_trades = oeh_s["trades"]
        regime_detail = "OEH only"
    elif regime == "BULLISH":
        regime_pnl = oel_s["pnl"]
        regime_trades = oel_s["trades"]
        regime_detail = "OEL only"
    else:
        regime_pnl = both_pnl
        regime_trades = both_trades
        regime_detail = "both (unknown regime)"

    sys.stdout.write(
        f"  OEH: {oeh_s['trades']} tr ₹{oeh_s['pnl']:>+8,} | "
        f"OEL: {oel_s['trades']} tr ₹{oel_s['pnl']:>+8,} | "
        f"BOTH: ₹{both_pnl:>+8,} | REGIME({regime_detail}): ₹{regime_pnl:>+8,}\n"
    )
    sys.stdout.flush()

    return {
        "regime": regime,
        "oeh": oeh_s, "oel": oel_s,
        "BOTH_ALWAYS": {"pnl": both_pnl, "trades": both_trades},
        "REGIME": {"pnl": regime_pnl, "trades": regime_trades, "detail": regime_detail},
        "OEH_ONLY": {"pnl": oeh_s["pnl"], "trades": oeh_s["trades"]},
        "OEL_ONLY": {"pnl": oel_s["pnl"], "trades": oel_s["trades"]},
    }


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
    nifty_names = ["NSE:NIFTY 50", "NSE_INDEX|Nifty 50"]
    for inst in master:
        name = inst.get("name", "") or inst.get("trading_symbol", "") or ""
        if "Nifty 50" in name or "NIFTY 50" in name:
            if inst.get("segment") in ("NSE_INDEX", "NSE_EQ"):
                nifty_key = inst.get("instrument_key")
                break
    if not nifty_key:
        nifty_key = getattr(config, "UPSTOX_INDEX_KEYS", {}).get("NSE:NIFTY 50")
    print(f"Universe: {len(universe)} stocks | NIFTY key: {nifty_key}", flush=True)

    modes = ["BOTH_ALWAYS", "REGIME", "OEH_ONLY", "OEL_ONLY"]
    grand = {m: {"pnl": 0, "trades": 0, "days_pos": 0, "days_neg": 0} for m in modes}
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
        for m in modes:
            row[m] = res[m]["pnl"]
            row[f"{m}_tr"] = res[m]["trades"]
            grand[m]["pnl"] += res[m]["pnl"]
            grand[m]["trades"] += res[m]["trades"]
            if res[m]["pnl"] > 0:
                grand[m]["days_pos"] += 1
            elif res[m]["pnl"] < 0:
                grand[m]["days_neg"] += 1
        if "detail" in res["REGIME"]:
            row["regime_detail"] = res["REGIME"]["detail"]
        daily_rows.append(row)
        d += timedelta(days=1)

    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*90}")
    print(f"  OEH + OEL REGIME BACKTEST — {d_from} to {d_to} ({trading_days} days)")
    print(f"{'='*90}")

    for m in modes:
        g = grand[m]
        avg = g["pnl"] / max(1, trading_days)
        print(f"\n  {m:15s} | Trades: {g['trades']:>4} | PnL: ₹{g['pnl']:>+9,} | "
              f"Days+: {g['days_pos']} Days-: {g['days_neg']} | Avg/Day: ₹{avg:>+7,.0f}")

    # Daily table
    print(f"\n{'='*90}")
    print(f"{'Date':<14} {'Regime':<8} {'BOTH':>10} {'REGIME':>10} {'OEH':>10} {'OEL':>10}")
    print("-" * 70)
    for r in daily_rows:
        regime_tag = "🔴" if r["regime"] == "BEARISH" else "🟢" if r["regime"] == "BULLISH" else "❓"
        print(f"{r['date'].strftime('%d %b'):>6} {r['date'].strftime('%a'):<5} "
              f"{regime_tag:<8} "
              f"{r['BOTH_ALWAYS']:>+10,} "
              f"{r['REGIME']:>+10,} "
              f"{r['OEH_ONLY']:>+10,} "
              f"{r['OEL_ONLY']:>+10,}")
    print("-" * 70)
    print(f"{'TOTAL':<14} {'':8} "
          f"{grand['BOTH_ALWAYS']['pnl']:>+10,} "
          f"{grand['REGIME']['pnl']:>+10,} "
          f"{grand['OEH_ONLY']['pnl']:>+10,} "
          f"{grand['OEL_ONLY']['pnl']:>+10,}")

    # Regime breakdown
    bearish_days = [r for r in daily_rows if r["regime"] == "BEARISH"]
    bullish_days = [r for r in daily_rows if r["regime"] == "BULLISH"]
    print(f"\nBearish days: {len(bearish_days)} | Bullish days: {len(bullish_days)}")
    if bearish_days:
        oeh_on_bear = sum(r["OEH_ONLY"] for r in bearish_days)
        oel_on_bear = sum(r["OEL_ONLY"] for r in bearish_days)
        print(f"  On BEARISH days: OEH total ₹{oeh_on_bear:+,} | OEL total ₹{oel_on_bear:+,}")
    if bullish_days:
        oeh_on_bull = sum(r["OEH_ONLY"] for r in bullish_days)
        oel_on_bull = sum(r["OEL_ONLY"] for r in bullish_days)
        print(f"  On BULLISH days: OEH total ₹{oeh_on_bull:+,} | OEL total ₹{oel_on_bull:+,}")

    better = max(modes, key=lambda m: grand[m]["pnl"])
    print(f"\n✅ Best mode: {better} → ₹{grand[better]['pnl']:+,}")


if __name__ == "__main__":
    asyncio.run(main())

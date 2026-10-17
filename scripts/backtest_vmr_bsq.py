#!/usr/bin/env python3
"""
VMR (VWAP Mean Reversion) + BSQ (Bollinger Squeeze Breakout) backtest.

Usage:
    PYTHONPATH=. .venv/bin/python3 -u scripts/backtest_vmr_bsq.py \
        --from 2026-09-01 --to 2026-10-16
"""

import asyncio, sys, os, argparse, time as _time, re as _re, math
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
TIME_CUTOFF = dtime(13, 0)
LOSS_CAP = 20000
PROFIT_CAP = 30000
MAX_ENTRIES_PER_SYM = 2

# --- VMR configs ---
VMR_CONFIGS = {
    "VMR_K2_HIGH": {
        "k": 2.0, "atr_period": 14,
        "sl_pct": 0.30, "max_sl": 5000,
        "floors": [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500],
    },
    "VMR_K1.5_HIGH": {
        "k": 1.5, "atr_period": 14,
        "sl_pct": 0.30, "max_sl": 5000,
        "floors": [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500],
    },
    "VMR_K2_TIGHT": {
        "k": 2.0, "atr_period": 14,
        "sl_pct": 0.15, "max_sl": 2500,
        "floors": [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500],
    },
}

# --- BSQ configs ---
BSQ_CONFIGS = {
    "BSQ_STD_HIGH": {
        "bb_period": 20, "bb_std": 2.0, "squeeze_pctile": 20,
        "sl_pct": 0.30, "max_sl": 5000,
        "floors": [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500],
    },
    "BSQ_STD_CURR": {
        "bb_period": 20, "bb_std": 2.0, "squeeze_pctile": 20,
        "sl_pct": 0.30, "max_sl": 5000,
        "floors": [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000],
    },
    "BSQ_TIGHT_HIGH": {
        "bb_period": 20, "bb_std": 2.0, "squeeze_pctile": 20,
        "sl_pct": 0.15, "max_sl": 2500,
        "floors": [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500],
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


# ────────────────────── VWAP + ATR computation ──────────────────────

def compute_vwap_atr(bars5):
    """Return bars with vwap, atr fields appended."""
    cum_vol = 0
    cum_tp_vol = 0
    atr_period = 14
    trs = []

    for i, b in enumerate(bars5):
        tp = (b["high"] + b["low"] + b["close"]) / 3
        vol = b.get("volume", 0) or 0
        cum_vol += vol
        cum_tp_vol += tp * vol
        b["vwap"] = cum_tp_vol / cum_vol if cum_vol > 0 else b["close"]

        if i == 0:
            tr = b["high"] - b["low"]
        else:
            prev_c = bars5[i - 1]["close"]
            tr = max(b["high"] - b["low"], abs(b["high"] - prev_c), abs(b["low"] - prev_c))
        trs.append(tr)
        if len(trs) >= atr_period:
            b["atr"] = sum(trs[-atr_period:]) / atr_period
        else:
            b["atr"] = sum(trs) / len(trs) if trs else 0

    return bars5


# ────────────────────── Bollinger Bands ──────────────────────

def compute_bollinger(bars5, period=20, num_std=2.0):
    """Add bb_upper, bb_lower, bb_width to each bar."""
    closes = []
    widths = []
    for b in bars5:
        closes.append(b["close"])
        if len(closes) >= period:
            window = closes[-period:]
            sma = sum(window) / period
            var = sum((x - sma) ** 2 for x in window) / period
            std = math.sqrt(var)
            b["bb_upper"] = sma + num_std * std
            b["bb_lower"] = sma - num_std * std
            b["bb_mid"] = sma
            w = (b["bb_upper"] - b["bb_lower"]) / sma if sma > 0 else 0
            b["bb_width"] = w
            widths.append(w)
            if len(widths) >= 20:
                sorted_w = sorted(widths[-50:])
                rank = 0
                for sw in sorted_w:
                    if w > sw:
                        rank += 1
                b["bb_pctile"] = (rank / len(sorted_w)) * 100
            else:
                b["bb_pctile"] = 50
        else:
            b["bb_upper"] = b["bb_lower"] = b["bb_mid"] = b["close"]
            b["bb_width"] = 0
            b["bb_pctile"] = 50
    return bars5


# ────────────────────── VMR candidate finder ──────────────────────

def find_vmr_candidates(equity_candles, k_val=2.0):
    """Find stocks where price deviated > K*ATR from VWAP."""
    candidates = []
    for sym, candles_1m in equity_candles.items():
        if not candles_1m or len(candles_1m) < 15:
            continue
        bars5 = build_5min(candles_1m)
        if len(bars5) < 5:
            continue
        bars5 = compute_vwap_atr(bars5)

        for ci in range(4, len(bars5)):
            b = bars5[ci]
            if b["time"].time() >= TIME_CUTOFF:
                break
            atr = b.get("atr", 0)
            vwap = b.get("vwap", 0)
            if atr <= 0 or vwap <= 0:
                continue

            deviation = b["close"] - vwap
            threshold = k_val * atr

            if deviation < -threshold:
                # Price well below VWAP → expect reversion up → buy CE
                candidates.append({
                    "symbol": sym,
                    "option_type": "CE",
                    "dev_ratio": abs(deviation) / atr,
                    "close": b["close"],
                    "entry_time": b["time"],
                })
            elif deviation > threshold:
                # Price well above VWAP → expect reversion down → buy PE
                candidates.append({
                    "symbol": sym,
                    "option_type": "PE",
                    "dev_ratio": abs(deviation) / atr,
                    "close": b["close"],
                    "entry_time": b["time"],
                })

    candidates.sort(key=lambda x: x["dev_ratio"], reverse=True)
    return candidates


# ────────────────────── BSQ candidate finder ──────────────────────

def find_bsq_candidates(equity_candles, squeeze_pctile=20):
    """Find stocks where BB squeezed then broke out."""
    candidates = []
    for sym, candles_1m in equity_candles.items():
        if not candles_1m or len(candles_1m) < 100:
            continue
        bars5 = build_5min(candles_1m)
        if len(bars5) < 25:
            continue
        bars5 = compute_bollinger(bars5)

        was_squeezed = False
        for ci in range(20, len(bars5)):
            b = bars5[ci]
            if b["time"].time() >= TIME_CUTOFF:
                break

            if b["bb_pctile"] <= squeeze_pctile:
                was_squeezed = True
                continue

            if was_squeezed:
                # Squeeze ended — check for breakout
                if b["close"] > b["bb_upper"]:
                    candidates.append({
                        "symbol": sym,
                        "option_type": "CE",
                        "breakout_strength": (b["close"] - b["bb_upper"]) / b["bb_upper"] * 100 if b["bb_upper"] > 0 else 0,
                        "close": b["close"],
                        "entry_time": b["time"],
                    })
                    was_squeezed = False
                elif b["close"] < b["bb_lower"]:
                    candidates.append({
                        "symbol": sym,
                        "option_type": "PE",
                        "breakout_strength": (b["bb_lower"] - b["close"]) / b["bb_lower"] * 100 if b["bb_lower"] > 0 else 0,
                        "close": b["close"],
                        "entry_time": b["time"],
                    })
                    was_squeezed = False
                else:
                    was_squeezed = False

    candidates.sort(key=lambda x: x.get("breakout_strength", 0), reverse=True)
    return candidates


# ────────────────────── Trade simulator (shared) ──────────────────────

def sim_trade(candles, entry_time, entry_price, qty, sl_price, floor_levels):
    ep_slip = entry_price * (1 + SLIPPAGE_PCT)
    peak_net = 0
    floor_val = 0
    first = True

    after = [c for c in candles if parse_ts(c) >= entry_time]
    if not after:
        return None

    for c in after:
        low, high, close = c["low"], c["high"], c["close"]

        if low <= sl_price:
            xp = close * (1 - SLIPPAGE_PCT)
            gross = (xp - ep_slip) * qty
            charges = calc_charges(ep_slip, xp, qty)
            return {"pnl": round(gross - charges), "reason": "SL", "xt": parse_ts(c), "qty": qty}

        if not first and floor_val > 0:
            close_gross = (close - ep_slip) * qty
            close_charges = calc_charges(ep_slip, close, qty)
            if close_gross - close_charges <= floor_val:
                xp = close * (1 - SLIPPAGE_PCT)
                gross = (xp - ep_slip) * qty
                charges = calc_charges(ep_slip, xp, qty)
                return {"pnl": round(gross - charges), "reason": "FLOOR", "xt": parse_ts(c), "qty": qty}

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
    return {"pnl": round(gross - charges), "reason": "EOD", "xt": parse_ts(last), "qty": qty}


def _find_atm(sym, ltp, otype, strike_steps, option_keys):
    step = strike_steps.get(sym, 50)
    atm = round(ltp / step) * step
    strikes = option_keys.get(sym, {})
    if not strikes:
        return None
    for off in [0, step, -step, 2 * step, -2 * step]:
        s = atm + off
        if s in strikes and otype in strikes[s]:
            return s
    valid = [s for s in strikes if otype in strikes[s]]
    return min(valid, key=lambda s: abs(s - ltp)) if valid else None


def run_config(candidates, opt_candles, lot_sizes, strike_steps, option_keys, cfg, sort_key="dev_ratio"):
    sl_pct = cfg["sl_pct"]
    max_sl = cfg["max_sl"]
    floors = cfg["floors"]

    trades = []
    cap = CAPITAL
    sym_count = defaultdict(int)
    sl_blocked = set()
    daily_pnl = 0

    for c in candidates[:30]:
        if daily_pnl <= -LOSS_CAP or daily_pnl >= PROFIT_CAP:
            break
        if cap < 5000:
            break

        sym = c["symbol"]
        if sym in BLOCKLIST:
            continue
        ot = c["option_type"]
        if sym_count[sym] >= MAX_ENTRIES_PER_SYM:
            continue
        if sym in sl_blocked:
            continue

        strike = _find_atm(sym, c["close"], ot, strike_steps, option_keys)
        if strike is None or (sym, strike, ot) not in opt_candles:
            continue

        candles = opt_candles[(sym, strike, ot)]
        et = c["entry_time"]

        ep = None
        for bar in candles:
            ct = parse_ts(bar)
            if ct <= et:
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

        t = sim_trade(candles, et, ep, lot, sl_price, floors)
        if t is None:
            continue

        trades.append({**t, "sym": sym})
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


# ────────────────────── Candle fetching ──────────────────────

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


async def run_one_day(ud, DATE, universe, eq_keys, lot_sizes, option_keys, strike_steps):
    from_dt = datetime.combine(DATE, dtime(9, 15))
    to_dt = datetime.combine(DATE, dtime(15, 30))

    syms_with_keys = [(s, eq_keys[s]) for s in universe if s in eq_keys]
    equity_candles = await _fetch_batch(ud, syms_with_keys, from_dt, to_dt, concurrency=15, label="EQ")
    if len(equity_candles) < 50:
        return None

    # Find candidates for both strategies
    vmr_cands = {}
    for cfg_name, cfg in VMR_CONFIGS.items():
        vmr_cands[cfg_name] = find_vmr_candidates(equity_candles, k_val=cfg["k"])

    bsq_cands = {}
    for cfg_name, cfg in BSQ_CONFIGS.items():
        bsq_cands[cfg_name] = find_bsq_candidates(equity_candles, squeeze_pctile=cfg["squeeze_pctile"])

    # Collect all needed option strikes
    all_cands = []
    for cands in vmr_cands.values():
        all_cands.extend(cands[:30])
    for cands in bsq_cands.values():
        all_cands.extend(cands[:30])

    sys.stdout.write(f"  VMR cands: {','.join(str(len(vmr_cands[n])) for n in VMR_CONFIGS)} | "
                     f"BSQ cands: {','.join(str(len(bsq_cands[n])) for n in BSQ_CONFIGS)}\n")
    sys.stdout.flush()

    if not all_cands:
        empty = {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "sl": 0, "fl": 0}
        return {"configs": {n: dict(empty) for n in list(VMR_CONFIGS) + list(BSQ_CONFIGS)}}

    needed = set()
    for c in all_cands:
        s = _find_atm(c["symbol"], c["close"], c["option_type"], strike_steps, option_keys)
        if s:
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

    results = {}
    for name, cfg in VMR_CONFIGS.items():
        results[name] = run_config(vmr_cands[name], opt_candles, lot_sizes, strike_steps,
                                   option_keys, cfg, sort_key="dev_ratio")
    for name, cfg in BSQ_CONFIGS.items():
        results[name] = run_config(bsq_cands[name], opt_candles, lot_sizes, strike_steps,
                                   option_keys, cfg, sort_key="breakout_strength")

    parts = []
    for name in list(VMR_CONFIGS) + list(BSQ_CONFIGS):
        r = results[name]
        parts.append(f"{name}:₹{r['pnl']:>+8,}({r['trades']}tr)")
    sys.stdout.write(f"  {' | '.join(parts)}\n")
    sys.stdout.flush()

    return {"configs": results}


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
            strike_steps[sym] = min(ss[j + 1] - ss[j] for j in range(min(20, len(ss) - 1)))
        else:
            strike_steps[sym] = 50

    print(f"Universe: {len(universe)} stocks", flush=True)

    all_configs = list(VMR_CONFIGS) + list(BSQ_CONFIGS)
    grand = {n: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "sl": 0, "fl": 0,
                 "days_pos": 0, "days_neg": 0} for n in all_configs}
    daily_rows = []
    trading_days = 0

    d = d_from
    while d <= d_to:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue

        t0 = _time.time()
        sys.stdout.write(f"\n{'=' * 70}\n  {d.strftime('%a %d %b %Y')}\n")
        sys.stdout.flush()

        res = await run_one_day(ud, d, universe, eq_keys, lot_sizes, option_keys, strike_steps)
        elapsed = _time.time() - t0
        if res is None:
            sys.stdout.write(f"  HOLIDAY ({elapsed:.0f}s)\n")
            d += timedelta(days=1)
            continue

        trading_days += 1
        sys.stdout.write(f"  done ({elapsed:.0f}s)\n")
        sys.stdout.flush()

        row = {"date": d}
        for n in all_configs:
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

    # ─── Summary ───
    print(f"\n{'=' * 120}")
    print(f"  VMR + BSQ BACKTEST — {d_from} to {d_to} ({trading_days} days)")
    print(f"{'=' * 120}\n")

    print("  ── VMR (VWAP Mean Reversion) ──")
    hdr = f"  {'Config':<18} {'K':>4} {'SL%':>4} {'MaxSL':>6}   {'PnL':>10} {'Trades':>6} {'W':>4} {'L':>4} {'WR%':>5} {'SL':>3} {'Fl':>3} {'D+':>3} {'D-':>3} {'Avg/D':>8}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for n in VMR_CONFIGS:
        g = grand[n]
        cfg = VMR_CONFIGS[n]
        wr = g["wins"] / max(1, g["wins"] + g["losses"]) * 100
        avg_day = g["pnl"] / max(1, trading_days)
        print(f"  {n:<18} {cfg['k']:>4.1f} {cfg['sl_pct'] * 100:>3.0f}% {cfg['max_sl']:>6}"
              f"  {g['pnl']:>+10,} {g['trades']:>6} {g['wins']:>4} {g['losses']:>4} {wr:>4.0f}%"
              f" {g['sl']:>3} {g['fl']:>3} {g['days_pos']:>3} {g['days_neg']:>3} {avg_day:>+8,.0f}")

    print(f"\n  ── BSQ (Bollinger Squeeze Breakout) ──")
    hdr2 = f"  {'Config':<18} {'SqP':>4} {'SL%':>4} {'MaxSL':>6}   {'PnL':>10} {'Trades':>6} {'W':>4} {'L':>4} {'WR%':>5} {'SL':>3} {'Fl':>3} {'D+':>3} {'D-':>3} {'Avg/D':>8}"
    print(hdr2)
    print("  " + "-" * (len(hdr2) - 2))
    for n in BSQ_CONFIGS:
        g = grand[n]
        cfg = BSQ_CONFIGS[n]
        wr = g["wins"] / max(1, g["wins"] + g["losses"]) * 100
        avg_day = g["pnl"] / max(1, trading_days)
        print(f"  {n:<18} {cfg['squeeze_pctile']:>3}% {cfg['sl_pct'] * 100:>3.0f}% {cfg['max_sl']:>6}"
              f"  {g['pnl']:>+10,} {g['trades']:>6} {g['wins']:>4} {g['losses']:>4} {wr:>4.0f}%"
              f" {g['sl']:>3} {g['fl']:>3} {g['days_pos']:>3} {g['days_neg']:>3} {avg_day:>+8,.0f}")

    # Daily breakdown — top configs
    top_vmr = max(VMR_CONFIGS, key=lambda n: grand[n]["pnl"])
    top_bsq = max(BSQ_CONFIGS, key=lambda n: grand[n]["pnl"])
    show = [top_vmr, top_bsq]

    print(f"\n{'=' * 120}")
    print(f"  DAILY BREAKDOWN (best of each)")
    print(f"{'=' * 120}")
    hdr3 = f"  {'Date':<16} " + " ".join(f"{n:>18}" for n in show)
    print(hdr3)
    print("  " + "-" * (len(hdr3) - 2))
    for r in daily_rows:
        cols = " ".join(f"{r.get(n, 0):>+18,}" for n in show)
        print(f"  {r['date'].strftime('%d %b'):>6} {r['date'].strftime('%a'):<5}   {cols}")
    print("  " + "-" * (len(hdr3) - 2))
    totals = " ".join(f"{grand[n]['pnl']:>+18,}" for n in show)
    print(f"  {'TOTAL':>14}   {totals}")


if __name__ == "__main__":
    asyncio.run(main())

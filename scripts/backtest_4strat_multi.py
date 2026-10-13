#!/usr/bin/env python3
"""
Multi-day backtest: VWAP Pullback, EMA Crossover, RSI Bounce, Momentum Scalp.
Runs across a date range with concurrent capital tracking per strategy.

Usage:
    PYTHONPATH=. .venv/bin/python3 -u scripts/backtest_4strat_multi.py \
        --from 2026-09-01 --to 2026-09-30
"""

import asyncio, sys, os, argparse, time as _time
from datetime import datetime, timedelta, time as dtime, date as dt_date
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo(config.TIMEZONE)

CAPITAL = 300000
MAX_SL = 5000
SL_PCT = 0.30
FLOOR_LEVELS = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
MAX_ENTRIES_PER_SYM = 2  # reduced from 3 to cut option fetches
TIME_CUTOFF = dtime(13, 0)

EMA_FAST, EMA_SLOW = 9, 21
RSI_PERIOD = 7
MOM_BODY_PCT = 0.15
MOM_VOL_MULT = 1.5
VWAP_PULLBACK_PCT = 0.15
VWAP_TREND_PCT = 0.3


def get_stepped_floor(net_pnl):
    locked = 0
    for lvl in FLOOR_LEVELS:
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


def calc_ema(values, period):
    if len(values) < period:
        return [None] * len(values)
    ema = [None] * (period - 1)
    sma = sum(values[:period]) / period
    ema.append(sma)
    mult = 2 / (period + 1)
    for i in range(period, len(values)):
        ema.append(values[i] * mult + ema[-1] * (1 - mult))
    return ema


def calc_rsi(closes, period=7):
    if len(closes) < period + 1:
        return [None] * len(closes)
    rsi = [None] * period
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains) / period
    al = sum(losses) / period
    rsi.append(100 if al == 0 else 100 - 100 / (1 + ag / al))
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i-1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        rsi.append(100 if al == 0 else 100 - 100 / (1 + ag / al))
    return rsi


def build_5min(candles_1m, date):
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


def calc_vwap(candles_1m, up_to_time, date):
    cv, ctv = 0, 0
    for c in candles_1m:
        ct = parse_ts(c)
        if ct > datetime.combine(date, up_to_time):
            break
        tp = (c["high"] + c["low"] + c["close"]) / 3
        v = c.get("volume", 0) or 1
        cv += v
        ctv += tp * v
    return ctv / cv if cv > 0 else None


async def _fetch_one(ud, sym, key, from_dt, to_dt):
    try:
        c = await asyncio.to_thread(ud.historical_data, key, from_dt, to_dt, "1minute")
        if c and len(c) > 5:
            return (sym, c)
    except Exception:
        pass
    return None


async def _fetch_batch(ud, pairs, from_dt, to_dt, concurrency=15):
    sem = asyncio.Semaphore(concurrency)
    async def _wrapped(sym, key):
        async with sem:
            return await _fetch_one(ud, sym, key, from_dt, to_dt)
    tasks = [_wrapped(sym, key) for sym, key in pairs]
    results = await asyncio.gather(*tasks)
    return {sym: c for r in results if r for sym, c in [r]}


async def run_one_day(ud, DATE, universe, eq_keys, lot_sizes, option_keys, strike_steps):
    from_dt = datetime.combine(DATE, dtime(9, 15))
    to_dt = datetime.combine(DATE, dtime(15, 30))

    # Fetch equity 1-min candles — concurrent
    syms_with_keys = [(s, eq_keys[s]) for s in universe if s in eq_keys]
    equity_candles = await _fetch_batch(ud, syms_with_keys, from_dt, to_dt, concurrency=15)

    if len(equity_candles) < 50:
        return None  # likely holiday

    # ── Scan signals ──
    signals = {"VWAP": [], "EMA": [], "RSI": [], "MOM": []}

    for sym in universe:
        cm = equity_candles.get(sym)
        if not cm or len(cm) < 30:
            continue
        bars5 = build_5min(cm, DATE)
        if len(bars5) < EMA_SLOW + 2:
            continue
        closes5 = [b["close"] for b in bars5]
        times5 = [b["time"] for b in bars5]
        vols5 = [b["volume"] for b in bars5]
        ema_f = calc_ema(closes5, EMA_FAST)
        ema_s = calc_ema(closes5, EMA_SLOW)
        rsi = calc_rsi(closes5, RSI_PERIOD)
        avg_vol = sum(vols5) / max(1, len(vols5))

        # VWAP Pullback (scan 1-min candles)
        for idx, c1 in enumerate(cm):
            ct = parse_ts(c1)
            t = ct.time()
            if t < dtime(9, 45) or t > TIME_CUTOFF:
                continue
            vwap = calc_vwap(cm, t, DATE)
            if not vwap or vwap <= 0:
                continue
            p = c1["close"]
            d = (p - vwap) / vwap * 100
            if 0 < d < VWAP_PULLBACK_PCT:
                for j in range(max(0, idx-10), idx):
                    if (cm[j]["close"] - vwap) / vwap * 100 > VWAP_TREND_PCT:
                        signals["VWAP"].append((sym, "CE", t, p))
                        break
            elif -VWAP_PULLBACK_PCT < d < 0:
                for j in range(max(0, idx-10), idx):
                    if (cm[j]["close"] - vwap) / vwap * 100 < -VWAP_TREND_PCT:
                        signals["VWAP"].append((sym, "PE", t, p))
                        break

        # EMA Crossover
        for i in range(1, len(bars5)):
            if ema_f[i] is None or ema_s[i] is None or ema_f[i-1] is None or ema_s[i-1] is None:
                continue
            t = times5[i].time()
            if t < dtime(9, 30) or t > TIME_CUTOFF:
                continue
            if ema_f[i-1] <= ema_s[i-1] and ema_f[i] > ema_s[i]:
                signals["EMA"].append((sym, "CE", t, closes5[i]))
            elif ema_f[i-1] >= ema_s[i-1] and ema_f[i] < ema_s[i]:
                signals["EMA"].append((sym, "PE", t, closes5[i]))

        # RSI Bounce
        for i in range(1, len(bars5)):
            if rsi[i] is None or rsi[i-1] is None:
                continue
            t = times5[i].time()
            if t < dtime(9, 30) or t > TIME_CUTOFF:
                continue
            if rsi[i-1] < 30 and rsi[i] >= 30:
                signals["RSI"].append((sym, "CE", t, closes5[i]))
            elif rsi[i-1] > 70 and rsi[i] <= 70:
                signals["RSI"].append((sym, "PE", t, closes5[i]))

        # Momentum Scalp
        for i in range(2, len(bars5)):
            t = times5[i].time()
            if t < dtime(9, 25) or t > TIME_CUTOFF:
                continue
            b = bars5[i]
            body = b["close"] - b["open"]
            bpct = abs(body) / b["open"] * 100 if b["open"] > 0 else 0
            if bpct > MOM_BODY_PCT and b["volume"] > avg_vol * MOM_VOL_MULT:
                ot = "CE" if body > 0 else "PE"
                signals["MOM"].append((sym, ot, t, b["close"]))

    # Deduplicate: max per sym, sort by time
    for k in signals:
        signals[k].sort(key=lambda x: x[2])
        seen = defaultdict(int)
        f = []
        for s in signals[k]:
            if seen[s[0]] < MAX_ENTRIES_PER_SYM:
                f.append(s)
                seen[s[0]] += 1
        signals[k] = f

    # Find ATM strike
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

    # Collect needed options
    needed = set()
    for k, sigs in signals.items():
        for sym, ot, t, p in sigs:
            s = find_atm(sym, p, ot)
            if s is not None:
                needed.add((sym, s, ot))

    # Fetch option candles — concurrent
    opt_pairs = []
    opt_map = {}  # key_str → (sym, strike, ot)
    for sym, strike, ot in needed:
        info = option_keys.get(sym, {}).get(strike, {}).get(ot)
        if info:
            label = f"{sym}_{strike}_{ot}"
            opt_pairs.append((label, info["key"]))
            opt_map[label] = (sym, strike, ot)
    raw = await _fetch_batch(ud, opt_pairs, from_dt, to_dt, concurrency=15)
    opt_candles = {}
    for label, candles in raw.items():
        key = opt_map.get(label)
        if key:
            opt_candles[key] = candles

    # Simulate trade
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
        pnl = (xp - ep) * lot_qty
        return {"pnl": round(pnl), "peak": round(peak), "ep": ep, "xp": xp,
                "xt": xt, "xr": xr, "qty": lot_qty}

    # Run each strategy with capital tracking
    results = {}
    for strat, sigs in signals.items():
        trades = []
        cap = CAPITAL
        open_pos = []
        open_syms = set()
        sl_blocked = set()

        for sym, ot, entry_time, price in sigs:
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

            # Get entry premium
            ep = None
            for c in opt_candles[(sym, strike, ot)]:
                ct = parse_ts(c)
                if ct <= edt:
                    ep = c["close"]
                else:
                    break
            if not ep or ep < 2:
                continue
            cost = ep * lot
            if cost > cap:
                continue

            t = sim(sym, strike, ot, entry_time, lot)
            if t is None:
                continue
            trades.append(t)
            cap -= cost
            open_syms.add(sym)
            open_pos.append({"xt": t["xt"], "xp": t["xp"], "qty": t["qty"], "sym": sym})
            if t["xr"] == "sl":
                sl_blocked.add(sym)

        results[strat] = trades

    return results


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

    print(f"Universe: {len(universe)} stocks", flush=True)

    # ── Day loop ──
    names = ["VWAP", "EMA", "RSI", "MOM"]
    grand = {n: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "days_pos": 0, "days_neg": 0} for n in names}
    daily_rows = []

    d = d_from
    trading_days = 0
    while d <= d_to:
        if d.weekday() >= 5:  # skip weekends
            d += timedelta(days=1)
            continue

        t0 = _time.time()
        sys.stdout.write(f"\n{'='*60}\n  {d.strftime('%a %d %b %Y')} ... ")
        sys.stdout.flush()

        res = await run_one_day(ud, d, universe, eq_keys, lot_sizes, option_keys, strike_steps)
        elapsed = _time.time() - t0
        if res is None:
            sys.stdout.write(f"HOLIDAY ({elapsed:.0f}s)\n")
            sys.stdout.flush()
            d += timedelta(days=1)
            continue

        trading_days += 1
        sys.stdout.write(f"done ({elapsed:.0f}s)\n")
        sys.stdout.flush()
        row = {"date": d}
        for n in names:
            trades = res.get(n, [])
            tp = sum(t["pnl"] for t in trades)
            w = sum(1 for t in trades if t["pnl"] > 0)
            l = len(trades) - w
            wr = w / max(1, len(trades)) * 100
            row[n] = tp
            row[f"{n}_t"] = len(trades)
            sys.stdout.write(f"  {n:<6} {len(trades):>3} trades | ₹{tp:>+8,} | W/L {w}/{l} ({wr:.0f}%)\n")
            grand[n]["pnl"] += tp
            grand[n]["trades"] += len(trades)
            grand[n]["wins"] += w
            grand[n]["losses"] += l
            if tp > 0:
                grand[n]["days_pos"] += 1
            elif tp < 0:
                grand[n]["days_neg"] += 1
        sys.stdout.flush()
        daily_rows.append(row)
        d += timedelta(days=1)

    # ══════════════════════════════════════════════════════════════
    #  GRAND SUMMARY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print(f"  GRAND SUMMARY — {d_from} to {d_to} ({trading_days} trading days)")
    print(f"{'='*80}")
    print(f"{'Strategy':<10} {'Trades':>6} {'Net PnL':>12} {'Wins':>5} {'Loss':>5} "
          f"{'WR%':>5} {'Days+':>5} {'Days-':>5} {'Avg/Day':>10}")
    print("-" * 75)
    for n in names:
        g = grand[n]
        wr = g["wins"] / max(1, g["trades"]) * 100
        avg = g["pnl"] / max(1, trading_days)
        pnl_str = f"₹{g['pnl']:+,}"
        avg_str = f"₹{avg:+,.0f}"
        print(f"{n:<10} {g['trades']:>6} {pnl_str:>12} {g['wins']:>5} {g['losses']:>5} "
              f"{wr:>4.0f}% {g['days_pos']:>5} {g['days_neg']:>5} {avg_str:>10}")
    print("-" * 75)
    best = max(names, key=lambda n: grand[n]["pnl"])
    print(f"\n✅ Best: {best} → ₹{grand[best]['pnl']:+,} total | ₹{grand[best]['pnl']/max(1,trading_days):+,.0f}/day")

    # Daily breakdown table
    print(f"\n{'Date':<12} {'VWAP':>10} {'EMA':>10} {'RSI':>10} {'MOM':>10}")
    print("-" * 55)
    for r in daily_rows:
        print(f"{r['date'].strftime('%d %b'):>6} {r['date'].strftime('%a'):<4}"
              f" {r.get('VWAP',0):>+9,} {r.get('EMA',0):>+9,}"
              f" {r.get('RSI',0):>+9,} {r.get('MOM',0):>+9,}")
    print("-" * 55)


if __name__ == "__main__":
    asyncio.run(main())

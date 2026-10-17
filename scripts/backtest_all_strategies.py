#!/usr/bin/env python3
"""
Combined backtest: OEH + OEL + ORB + MOM — all with current LIVE params.

Shows per-day and per-strategy PnL with the deployed configuration:
  OEH: regime filter (BEARISH only), HIGH_FLOOR, 30% SL, ₹5000 max
  OEL: regime filter (BULLISH only), HIGH_FLOOR, 30% SL, ₹5000 max
  ORB: no regime, TIGHT+HIGH (15% SL, ₹2500 max, HIGH_FLOOR)
  MOM: current params (30% SL, ₹5000 max, old floors — baseline)

Usage:
    PYTHONPATH=. .venv/bin/python3 -u scripts/backtest_all_strategies.py \
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
TOLERANCE = 0.05
MIN_MOVE_PCT = 0.3
BLOCKLIST = {"GODREJCP", "GRASIM"}

# ── OEH (live params) ──
OEH_SL_PCT = 0.30
OEH_MAX_SL = 5000
OEH_MAX_ENTRIES = 3
OEH_FLOORS = [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500]
OEH_SCAN_TIME = dtime(9, 25)

# ── OEL (live params) ──
OEL_SL_PCT = 0.30
OEL_MAX_SL = 5000
OEL_MAX_ENTRIES = 1
OEL_FLOORS = [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500]
OEL_SCAN_TIME = dtime(9, 25)

# ── ORB (live params — TIGHT+HIGH) ──
ORB_SL_PCT = 0.15
ORB_MAX_SL = 2500
ORB_FLOORS = [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500]
ORB_MAX_ENTRIES_PER_SYM = 3
ORB_MIN_RANGE_PCT = 0.3
ORB_MAX_RANGE_PCT = 3.0
ORB_TIME_CUTOFF = dtime(13, 0)
ORB_LOSS_CAP = 20000
ORB_PROFIT_CAP = 25000

# ── MOM (current live params) ──
MOM_SL_PCT = 0.30
MOM_MAX_SL = 5000
MOM_FLOORS = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
MOM_BODY_PCT = 0.15
MOM_VOL_MULT = 1.5
MOM_MAX_ENTRIES_PER_SYM = 2
MOM_TIME_CUTOFF = dtime(13, 0)
MOM_LOSS_CAP = 20000
MOM_PROFIT_CAP = 30000


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


def sim_trade(candles, entry_time, entry_price, qty, sl_price, floor_levels):
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
            return {"pnl": round(gross - charges), "reason": "SL", "xt": parse_ts(c), "qty": qty, "xp": xp}

        if not first and floor_val > 0:
            close_gross = (close - ep_slip) * qty
            close_charges = calc_charges(ep_slip, close, qty)
            if close_gross - close_charges <= floor_val:
                xp = close * (1 - SLIPPAGE_PCT)
                gross = (xp - ep_slip) * qty
                charges = calc_charges(ep_slip, xp, qty)
                return {"pnl": round(gross - charges), "reason": "FLOOR", "xt": parse_ts(c), "qty": qty, "xp": xp}

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
    return {"pnl": round(gross - charges), "reason": "EOD", "xt": parse_ts(last), "qty": qty, "xp": xp}


# ═══════════════════════════════════════════════════════════════
# Candidate finders
# ═══════════════════════════════════════════════════════════════

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
                           "move_pct": rise_pct, "option_type": "CE"})
    candidates.sort(key=lambda x: x["move_pct"], reverse=True)
    return candidates


def find_orb_candidates(equity_candles):
    candidates = []
    for sym, candles_1m in equity_candles.items():
        if not candles_1m or len(candles_1m) < 10:
            continue
        bars5 = build_5min(candles_1m)
        if not bars5 or len(bars5) < 3:
            continue
        rh = bars5[0]["high"]
        rl = bars5[0]["low"]
        ro = bars5[0]["open"]
        if ro <= 0:
            continue
        rpct = (rh - rl) / ro * 100
        if rpct < ORB_MIN_RANGE_PCT or rpct > ORB_MAX_RANGE_PCT:
            continue
        for dc in bars5[1:]:
            if dc["high"] > rh:
                candidates.append({"symbol": sym, "direction": "bullish", "option_type": "CE",
                                   "range_pct": rpct, "breakout_price": dc["close"],
                                   "breakout_time": dc["time"]})
                break
            elif dc["low"] < rl:
                candidates.append({"symbol": sym, "direction": "bearish", "option_type": "PE",
                                   "range_pct": rpct, "breakout_price": dc["close"],
                                   "breakout_time": dc["time"]})
                break
    candidates.sort(key=lambda x: x["range_pct"], reverse=True)
    return candidates


def find_mom_candidates(equity_candles):
    candidates = []
    for sym, candles_1m in equity_candles.items():
        if not candles_1m or len(candles_1m) < 15:
            continue
        bars5 = build_5min(candles_1m)
        if not bars5 or len(bars5) < 3:
            continue
        vols = [b.get("volume", 0) or 0 for b in bars5]
        avg_vol = sum(vols) / max(1, len(vols))
        if avg_vol <= 0:
            continue
        for ci in range(2, len(bars5)):
            b = bars5[ci]
            if b["time"].time() >= MOM_TIME_CUTOFF:
                break
            body = b["close"] - b["open"]
            body_pct = abs(body) / b["open"] * 100 if b["open"] > 0 else 0
            vol = b.get("volume", 0) or 0
            if body_pct >= MOM_BODY_PCT and vol > avg_vol * MOM_VOL_MULT:
                ot = "CE" if body > 0 else "PE"
                candidates.append({
                    "symbol": sym, "option_type": ot, "body_pct": body_pct,
                    "vol_ratio": vol / avg_vol, "close": b["close"],
                    "entry_time": b["time"],
                })
    candidates.sort(key=lambda x: x["body_pct"], reverse=True)
    return candidates


# ═══════════════════════════════════════════════════════════════
# Strategy runners
# ═══════════════════════════════════════════════════════════════

def _find_atm(sym, ltp, otype, strike_steps, option_keys):
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


def run_scan_strategy(candidates, opt_candles, lot_sizes, strike_steps, option_keys,
                      sl_pct, max_sl, max_entries, floor_levels, scan_time, DATE,
                      loss_cap=25000, profit_cap=30000):
    trades = []
    cap = CAPITAL
    sym_count = defaultdict(int)
    sl_blocked = set()
    daily_pnl = 0

    for c in candidates[:30]:
        if daily_pnl <= -loss_cap or daily_pnl >= profit_cap:
            break

        sym = c["symbol"]
        if sym in BLOCKLIST:
            continue
        ot = c["option_type"]

        if sym_count[sym] >= max_entries:
            continue
        if sym in sl_blocked:
            continue

        strike = _find_atm(sym, c.get("entry_close") or c.get("breakout_price") or c.get("close"), ot, strike_steps, option_keys)
        if strike is None or (sym, strike, ot) not in opt_candles:
            continue

        candles = opt_candles[(sym, strike, ot)]

        # Determine entry time
        if "breakout_time" in c:
            et = c["breakout_time"]
            if isinstance(et, str):
                et = datetime.fromisoformat(et)
        elif "entry_time" in c:
            et = c["entry_time"]
            if isinstance(et, dtime):
                et = datetime.combine(DATE, et)
        else:
            et = datetime.combine(DATE, scan_time)

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

        t = sim_trade(candles, et, ep, lot, sl_price, floor_levels)
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
            sys.stdout.write(f"  NIFTY error: {exc}\n")

    sys.stdout.write(f"  Regime: {regime}\n")
    sys.stdout.flush()

    # Find all candidates
    oeh_cands = find_oeh_candidates(equity_candles)
    oel_cands = find_oel_candidates(equity_candles)
    orb_cands = find_orb_candidates(equity_candles)
    mom_cands = find_mom_candidates(equity_candles)

    sys.stdout.write(f"  OEH:{len(oeh_cands)} OEL:{len(oel_cands)} ORB:{len(orb_cands)} MOM:{len(mom_cands)}\n")
    sys.stdout.flush()

    # Apply regime filter to OEH/OEL
    if regime == "BULLISH":
        oeh_cands = []  # skip OEH on bullish days
    elif regime == "BEARISH":
        oel_cands = []  # skip OEL on bearish days

    # Collect all needed option strikes
    needed = set()
    for c in oeh_cands[:30]:
        s = _find_atm(c["symbol"], c["entry_close"], "PE", strike_steps, option_keys)
        if s: needed.add((c["symbol"], s, "PE"))
    for c in oel_cands[:30]:
        s = _find_atm(c["symbol"], c["entry_close"], "CE", strike_steps, option_keys)
        if s: needed.add((c["symbol"], s, "CE"))
    for c in orb_cands[:30]:
        s = _find_atm(c["symbol"], c["breakout_price"], c["option_type"], strike_steps, option_keys)
        if s: needed.add((c["symbol"], s, c["option_type"]))
    for c in mom_cands[:30]:
        s = _find_atm(c["symbol"], c["close"], c["option_type"], strike_steps, option_keys)
        if s: needed.add((c["symbol"], s, c["option_type"]))

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

    # Run each strategy
    oeh_r = run_scan_strategy(oeh_cands, opt_candles, lot_sizes, strike_steps, option_keys,
                              OEH_SL_PCT, OEH_MAX_SL, OEH_MAX_ENTRIES, OEH_FLOORS, OEH_SCAN_TIME, DATE)
    oel_r = run_scan_strategy(oel_cands, opt_candles, lot_sizes, strike_steps, option_keys,
                              OEL_SL_PCT, OEL_MAX_SL, OEL_MAX_ENTRIES, OEL_FLOORS, OEL_SCAN_TIME, DATE)
    orb_r = run_scan_strategy(orb_cands, opt_candles, lot_sizes, strike_steps, option_keys,
                              ORB_SL_PCT, ORB_MAX_SL, ORB_MAX_ENTRIES_PER_SYM, ORB_FLOORS, dtime(9,25), DATE,
                              loss_cap=ORB_LOSS_CAP, profit_cap=ORB_PROFIT_CAP)
    mom_r = run_scan_strategy(mom_cands, opt_candles, lot_sizes, strike_steps, option_keys,
                              MOM_SL_PCT, MOM_MAX_SL, MOM_MAX_ENTRIES_PER_SYM, MOM_FLOORS, dtime(9,30), DATE,
                              loss_cap=MOM_LOSS_CAP, profit_cap=MOM_PROFIT_CAP)

    total = oeh_r["pnl"] + oel_r["pnl"] + orb_r["pnl"] + mom_r["pnl"]
    sys.stdout.write(
        f"  OEH:₹{oeh_r['pnl']:>+8,}({oeh_r['trades']}tr) "
        f"OEL:₹{oel_r['pnl']:>+8,}({oel_r['trades']}tr) "
        f"ORB:₹{orb_r['pnl']:>+8,}({orb_r['trades']}tr) "
        f"MOM:₹{mom_r['pnl']:>+8,}({mom_r['trades']}tr) "
        f"| TOTAL:₹{total:>+9,}\n"
    )
    sys.stdout.flush()

    return {"regime": regime, "OEH": oeh_r, "OEL": oel_r, "ORB": orb_r, "MOM": mom_r, "TOTAL": total}


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

    strats = ["OEH", "OEL", "ORB", "MOM"]
    grand = {s: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "sl": 0, "fl": 0,
                 "days_pos": 0, "days_neg": 0} for s in strats}
    daily_rows = []
    trading_days = 0
    grand_total = 0

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
        day_total = 0
        for s in strats:
            r = res[s]
            row[s] = r["pnl"]
            row[f"{s}_tr"] = r["trades"]
            for k in ("pnl", "trades", "wins", "losses", "sl", "fl"):
                grand[s][k] += r[k]
            if r["pnl"] > 0:
                grand[s]["days_pos"] += 1
            elif r["pnl"] < 0:
                grand[s]["days_neg"] += 1
            day_total += r["pnl"]
        row["TOTAL"] = day_total
        grand_total += day_total
        daily_rows.append(row)
        d += timedelta(days=1)

    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*110}")
    print(f"  ALL STRATEGIES BACKTEST — {d_from} to {d_to} ({trading_days} days)")
    print(f"  OEH/OEL: regime filter + HIGH_FLOOR | ORB: TIGHT+HIGH | MOM: current params")
    print(f"{'='*110}\n")

    hdr = f"{'Strategy':<10} {'SL%':>4} {'MaxSL':>6} {'Floor1':>7}   {'PnL':>10} {'Trades':>6} {'W':>4} {'L':>4} {'WR%':>5} {'SL':>3} {'Fl':>3} {'D+':>3} {'D-':>3} {'Avg/D':>8}"
    print(hdr)
    print("-" * len(hdr))

    configs = {
        "OEH": (OEH_SL_PCT, OEH_MAX_SL, OEH_FLOORS[0]),
        "OEL": (OEL_SL_PCT, OEL_MAX_SL, OEL_FLOORS[0]),
        "ORB": (ORB_SL_PCT, ORB_MAX_SL, ORB_FLOORS[0]),
        "MOM": (MOM_SL_PCT, MOM_MAX_SL, MOM_FLOORS[0]),
    }
    for s in strats:
        g = grand[s]
        sl_p, msl, f1 = configs[s]
        wr = g["wins"] / max(1, g["wins"] + g["losses"]) * 100
        avg_day = g["pnl"] / max(1, trading_days)
        print(f"{s:<10} {sl_p*100:>3.0f}% {msl:>6} {f1:>7}  {g['pnl']:>+10,} {g['trades']:>6} {g['wins']:>4} {g['losses']:>4} {wr:>4.0f}% {g['sl']:>3} {g['fl']:>3} {g['days_pos']:>3} {g['days_neg']:>3} {avg_day:>+8,.0f}")

    total_trades = sum(grand[s]["trades"] for s in strats)
    avg_total = grand_total / max(1, trading_days)
    print(f"{'COMBINED':<10}                      {grand_total:>+10,} {total_trades:>6}                                  {avg_total:>+8,.0f}")

    # Daily breakdown
    print(f"\n{'='*110}")
    print(f"  DAILY BREAKDOWN")
    print(f"{'='*110}")
    hdr2 = f"{'Date':<16} {'Rgm':<5} {'OEH':>10} {'OEL':>10} {'ORB':>10} {'MOM':>10} {'TOTAL':>12}"
    print(hdr2)
    print("-" * len(hdr2))
    for r in daily_rows:
        rtag = "\U0001f534" if r["regime"] == "BEARISH" else "\U0001f7e2" if r["regime"] == "BULLISH" else "?"
        print(f"{r['date'].strftime('%d %b'):>6} {r['date'].strftime('%a'):<5}  {rtag:<5}"
              f" {r['OEH']:>+10,} {r['OEL']:>+10,} {r['ORB']:>+10,} {r['MOM']:>+10,} {r['TOTAL']:>+12,}")
    print("-" * len(hdr2))
    print(f"{'TOTAL':>14}       "
          f" {grand['OEH']['pnl']:>+10,} {grand['OEL']['pnl']:>+10,} {grand['ORB']['pnl']:>+10,} {grand['MOM']['pnl']:>+10,} {grand_total:>+12,}")
    print(f"\n\U0001f4b0 Combined: ₹{grand_total:>+,} over {trading_days} days = ₹{avg_total:>+,.0f}/day")


if __name__ == "__main__":
    asyncio.run(main())

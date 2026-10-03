"""Backtest all 5 strategies: OEH + ORB + PDHL + Gap Fade + Afternoon Momentum.

Optimized: parallel candle fetching with ThreadPoolExecutor, aggressive caching.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/backtest_all5.py --month 2026-09
    PYTHONPATH=. .venv/bin/python3 scripts/backtest_all5.py --date 2026-10-01
"""
from __future__ import annotations
import argparse, hashlib, json, math, re, time as _t
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from src.broker.upstox_data import UpstoxData, load_cached_token, _expiry_to_date

IST = ZoneInfo("Asia/Kolkata")
BLOCKLIST = {"GODREJCP", "GRASIM"}
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "NIFTY BANK", "NIFTY 50"}

SLIPPAGE_PCT = 0.005
MIN_PREMIUM = 0.50
SL_PCT = 0.30
MAX_SL_RS = 5000
FLOOR_STEPS = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
CAPITAL = 150000
BROKERAGE_PER_ORDER = 20
STT_PCT = 0.000625
LOT_MULT = 2
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "candle_cache"

# OEH config
OEH_TOLERANCE = 0.05
OEH_MIN_DROP_PCT = 0.3
OEH_LOSS_CAP = 999999
OEH_PROFIT_CAP = 999999

# ORB config
ORB_MIN_RANGE_PCT = 0.3
ORB_MAX_RANGE_PCT = 3.0
ORB_LOSS_CAP = 999999
ORB_PROFIT_CAP = 999999

# PDHL config
PDHL_LOSS_CAP = 10000
PDHL_PROFIT_CAP = 25000

# Gap Fade config
GAP_MIN_PCT = 0.5
GAP_MAX_PCT = 2.0
GAP_SL_EXTEND_PCT = 0.003
GAP_TIME_EXIT = "12:30"
GAP_LOSS_CAP = 10000
GAP_PROFIT_CAP = 25000

# Afternoon Momentum config
AFT_RANGE_START = "11:30"
AFT_RANGE_END = "13:30"
AFT_VOL_MULT = 2.0
AFT_HARD_EXIT = "15:10"
AFT_MIN_RANGE_PCT = 0.3
AFT_LOSS_CAP = 10000
AFT_PROFIT_CAP = 25000

# ---------------------------------------------------------------------------
# Caching + parallel fetch
# ---------------------------------------------------------------------------
def _cache_key(inst_key, from_dt, to_dt, interval):
    raw = f"{inst_key}|{from_dt}|{to_dt}|{interval}"
    return hashlib.md5(raw.encode()).hexdigest()


def _cached_fetch(ud, inst_key, from_dt, to_dt, interval):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = _cache_key(inst_key, from_dt, to_dt, interval)
    path = CACHE_DIR / f"{key}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    candles = ud.historical_data(inst_key, from_dt, to_dt, interval)
    if candles is not None:
        with open(path, "w") as f:
            json.dump(candles, f)
    return candles


def _parallel_fetch(ud, fetch_jobs, max_workers=8):
    """Fetch candles in parallel. fetch_jobs = list of (sym, inst_key, from_dt, to_dt, interval).
    Returns dict {sym: candles}."""
    results = {}
    uncached = []

    for sym, inst_key, from_dt, to_dt, interval in fetch_jobs:
        key = _cache_key(inst_key, from_dt, to_dt, interval)
        path = CACHE_DIR / f"{key}.json"
        if path.exists():
            with open(path) as f:
                results[sym] = json.load(f)
        else:
            uncached.append((sym, inst_key, from_dt, to_dt, interval))

    if not uncached:
        return results

    def _fetch_one(job):
        sym, inst_key, from_dt, to_dt, interval = job
        try:
            candles = ud.historical_data(inst_key, from_dt, to_dt, interval)
            if candles is not None:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                key = _cache_key(inst_key, from_dt, to_dt, interval)
                path = CACHE_DIR / f"{key}.json"
                with open(path, "w") as f:
                    json.dump(candles, f)
            return sym, candles
        except Exception:
            return sym, None

    batch_size = max_workers
    for i in range(0, len(uncached), batch_size):
        batch = uncached[i:i + batch_size]
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_fetch_one, j): j for j in batch}
            for fut in as_completed(futures):
                sym, candles = fut.result()
                if candles:
                    results[sym] = candles
        if i + batch_size < len(uncached):
            _t.sleep(0.15)

    return results


def _candle_time(cn):
    t = str(cn.get("date", cn.get("timestamp", "")))
    return t[11:16] if len(t) > 16 else t[:5]


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------
def _simulate_trade(ocandles, entry_idx, entry, lot, sl_price, hard_exit_time=None):
    charges_est = (BROKERAGE_PER_ORDER * 2) + (entry * lot * STT_PCT)
    peak_net = 0.0
    stepped_floor = 0

    for i, cn in enumerate(ocandles[entry_idx + 1:], start=entry_idx + 1):
        high, low, close = cn["high"], cn["low"], cn["close"]
        t_short = _candle_time(cn)

        if hard_exit_time and t_short >= hard_exit_time:
            ep = round(close * (1 - SLIPPAGE_PCT), 2)
            return ep, "TIME", t_short, peak_net

        gross_close = (close - entry) * lot
        net_close = gross_close - charges_est
        gross_high = (high - entry) * lot
        net_high = gross_high - charges_est
        if net_high > peak_net:
            peak_net = net_high

        if low <= sl_price:
            ep = round(sl_price * (1 - SLIPPAGE_PCT), 2)
            return ep, "SL", t_short, peak_net

        for fl in FLOOR_STEPS:
            if peak_net >= fl:
                stepped_floor = fl
        if stepped_floor > 0 and net_close <= stepped_floor:
            ep = round(close * (1 - SLIPPAGE_PCT), 2)
            return ep, f"FLOOR ₹{stepped_floor}", t_short, peak_net

    last = ocandles[-1]
    ep = round(last["close"] * (1 - SLIPPAGE_PCT), 2)
    return ep, "EOD", _candle_time(last), peak_net


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------
def _load_master_data(ud, master):
    eq_keys = {}
    for inst in master:
        if inst.get("segment") == "NSE_EQ":
            tsym = (inst.get("trading_symbol") or "").upper()
            if tsym:
                eq_keys[tsym] = inst.get("instrument_key")

    universe = set()
    opt_master = {}
    lot_sizes = {}
    for inst in master:
        if inst.get("segment") != "NSE_FO":
            continue
        itype = (inst.get("instrument_type") or "").upper()
        if itype not in ("CE", "PE"):
            continue
        tsym = (inst.get("trading_symbol") or "").upper()
        base = re.match(r'^([A-Z&]+)', tsym)
        if not base:
            continue
        sym_name = base.group(1)
        if sym_name in INDEX_NAMES:
            continue
        universe.add(sym_name)
        strike_val = float(inst.get("strike_price", 0))
        ed = _expiry_to_date(inst.get("expiry"))
        if ed and strike_val > 0:
            opt_master[(sym_name, ed, strike_val, itype)] = inst.get("instrument_key")
            ls = int(inst.get("lot_size") or 0)
            if ls > 0:
                lot_sizes[sym_name] = ls

    return eq_keys, sorted(universe), opt_master, lot_sizes


def _resolve_option(sym, spot, opt_type, ref_date, opt_master, lot_sizes):
    sym_opts = {k: v for k, v in opt_master.items() if k[0] == sym and k[3] == opt_type}
    if not sym_opts:
        return None, None, None
    expiries = sorted({k[1] for k in sym_opts if k[1] >= ref_date})
    if not expiries:
        return None, None, None
    strikes = sorted({k[2] for k in sym_opts if k[1] == expiries[0]})
    strike = min(strikes, key=lambda s: abs(s - spot))
    opt_key = opt_master.get((sym, expiries[0], strike, opt_type))
    lot = lot_sizes.get(sym, 1) * LOT_MULT
    return opt_key, strike, lot


def _prev_trading_day(d):
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return p


# ---------------------------------------------------------------------------
# Exec trades (shared)
# ---------------------------------------------------------------------------
def _exec_trades(candidates, ref_date, ud, opt_master, lot_sizes,
                 loss_cap, profit_cap, label, verbose, hard_exit_time=None):
    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    full_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    # Phase 1: resolve options + fetch option candles in parallel
    to_resolve = []
    for c in candidates:
        sym = c["symbol"]
        opt_type = c.get("opt_type", "PE")
        spot = c["breakout_price"]
        opt_key, strike, lot = _resolve_option(sym, spot, opt_type, ref_date, opt_master, lot_sizes)
        if opt_key:
            to_resolve.append((c, sym, opt_type, strike, lot, opt_key))

    if not to_resolve:
        if verbose:
            print(f"  {label}: No trades")
        return []

    # Parallel fetch option candles
    opt_fetch_jobs = []
    for c, sym, opt_type, strike, lot, opt_key in to_resolve:
        opt_fetch_jobs.append((f"{sym}_{strike}_{opt_type}", opt_key, from_dt, full_to, "1minute"))

    opt_candles = _parallel_fetch(ud, opt_fetch_jobs)

    prepared = []
    for c, sym, opt_type, strike, lot, opt_key in to_resolve:
        fetch_key = f"{sym}_{strike}_{opt_type}"
        ocandles = opt_candles.get(fetch_key)
        if not ocandles or len(ocandles) < 5:
            continue

        bt = c.get("entry_after", "09:20")
        entry_candle = None
        entry_idx = 0
        for i, cn in enumerate(ocandles):
            if _candle_time(cn) >= bt:
                entry_candle = cn
                entry_idx = i
                break
        if not entry_candle:
            continue

        raw_entry = entry_candle["close"]
        if raw_entry <= 0 or raw_entry < MIN_PREMIUM:
            continue

        entry = round(raw_entry * (1 + SLIPPAGE_PCT), 2)
        sl_pct_price = entry * (1 - SL_PCT)
        sl_cap_price = entry - (MAX_SL_RS / lot)
        sl_price = round(max(sl_pct_price, sl_cap_price), 2)

        exit_price, exit_reason, exit_time, peak_pnl = _simulate_trade(
            ocandles, entry_idx, entry, lot, sl_price, hard_exit_time
        )

        pnl = (exit_price - entry) * lot
        charges = (BROKERAGE_PER_ORDER * 2) + (exit_price * lot * STT_PCT)
        pnl -= charges

        prepared.append({
            "candidate": c, "sym": sym, "opt_type": opt_type, "strike": strike,
            "lot": lot, "entry": entry, "margin": entry * lot,
            "entry_time": bt, "exit_time": exit_time,
            "exit_price": exit_price, "exit_reason": exit_reason,
            "pnl": pnl, "peak": peak_pnl,
        })

    # Phase 2: capital simulation
    prepared.sort(key=lambda x: x["entry_time"])
    avail = CAPITAL
    active = []
    realized_pnl = 0.0
    results = []
    traded_indices = set()
    cap_stopped = False

    if verbose and prepared:
        print(f"\n  {'Symbol':<14s} {'Str':>6s} {'D':>1s} {'Entry':>7s} {'Exit':>7s}"
              f" {'P&L':>9s} {'Peak':>8s} {'Reason':<12s} {'ETime':>5s} {'XTime':>5s}")
        print(f"  {'-'*95}")

    def _do_place(idx):
        nonlocal avail
        t = prepared[idx]
        avail -= t["margin"]
        active.append((t["exit_time"], t["entry_time"], t["margin"], t["pnl"], idx))
        traded_indices.add(idx)
        results.append({"sym": t["sym"], "pnl": t["pnl"], "reason": t["exit_reason"], "peak": t["peak"]})
        if verbose:
            c = t["candidate"]
            d_tag = "▲" if c.get("direction", "bearish") == "bullish" else "▼"
            print(f"  {d_tag} {t['sym']:<12s} {t['strike']:>6.0f}{t['opt_type']} {t['entry']:>7.1f} → {t['exit_price']:>6.1f}"
                  f"  ₹{t['pnl']:>+8,.0f}  ₹{t['peak']:>+7,.0f} {t['exit_reason']:<12s} {t['entry_time']:>5s} {t['exit_time']:>5s}")

    pending = []
    for idx in range(len(prepared)):
        t = prepared[idx]
        if t["margin"] > avail:
            pending.append(idx)
            continue
        _do_place(idx)

    while pending and active:
        active.sort(key=lambda x: x[0])
        ext, _et, margin, pnl, tidx = active.pop(0)
        avail += margin + pnl
        realized_pnl += pnl

        new_active = []
        for a in active:
            if a[0] <= ext:
                avail += a[2] + a[3]
                realized_pnl += a[3]
            else:
                new_active.append(a)
        active = new_active

        if realized_pnl >= profit_cap or realized_pnl <= -loss_cap:
            cap_stopped = True
            break

        if avail < 5000:
            continue

        retryable = [i for i in pending if prepared[i]["entry_time"] <= ext]
        later = [i for i in pending if prepared[i]["entry_time"] > ext]
        placed_now = 0
        still_pending = []
        for idx in retryable:
            if idx in traded_indices:
                continue
            if prepared[idx]["margin"] > avail:
                still_pending.append(idx)
                continue
            _do_place(idx)
            placed_now += 1
        pending = still_pending + later
        if placed_now == 0 and not active:
            break

    skipped = len(pending)
    if results:
        day_pnl = sum(r["pnl"] for r in results)
        wins = sum(1 for r in results if r["pnl"] > 0)
        losses = len(results) - wins
        extra = ""
        if cap_stopped:
            extra = f" | CAP HIT (₹{realized_pnl:>+,.0f})"
        print(f"  {label} TOTAL: {len(results)} trades | {wins}W/{losses}L | Skip: {skipped}{extra} | ₹{day_pnl:>+,.0f}")
    else:
        print(f"  {label}: No trades")

    return results


# ---------------------------------------------------------------------------
# Strategy scanners
# ---------------------------------------------------------------------------
def _prefetch_equity_candles(ud, eq_keys, universe, from_dt, to_dt, interval):
    """Fetch equity candles for all symbols in parallel. Returns dict {sym: candles}."""
    jobs = []
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if inst_key:
            jobs.append((sym, inst_key, from_dt, to_dt, interval))
    return _parallel_fetch(ud, jobs)


def scan_oeh(candles_5m, universe):
    candidates = []
    scanned = 0
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        candles = candles_5m.get(sym)
        if not candles:
            continue
        scanned += 1
        op = candles[0]["open"]
        if op <= 0:
            continue
        mh = candles[0]["high"]
        if mh > op + OEH_TOLERANCE:
            continue
        ep = candles[0]["close"]
        dp = (op - ep) / op * 100
        if dp < OEH_MIN_DROP_PCT:
            continue
        candidates.append({
            "symbol": sym, "direction": "bearish", "opt_type": "PE",
            "breakout_price": ep, "entry_after": "09:20", "drop_pct": dp,
        })
    candidates.sort(key=lambda x: x["drop_pct"], reverse=True)
    return candidates, scanned


def scan_orb(candles_5m, universe):
    candidates = []
    scanned = 0
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        candles = candles_5m.get(sym)
        if not candles or len(candles) < 3:
            continue
        scanned += 1
        rh = candles[0]["high"]
        rl = candles[0]["low"]
        ro = candles[0]["open"]
        if ro <= 0:
            continue
        rp = (rh - rl) / ro * 100
        if rp < ORB_MIN_RANGE_PCT or rp > ORB_MAX_RANGE_PCT:
            continue
        for dc in candles[1:]:
            t = _candle_time(dc)
            if dc["high"] > rh:
                candidates.append({
                    "symbol": sym, "direction": "bullish", "opt_type": "CE",
                    "breakout_price": dc["close"], "entry_after": t, "range_pct": rp,
                })
                break
            elif dc["low"] < rl:
                candidates.append({
                    "symbol": sym, "direction": "bearish", "opt_type": "PE",
                    "breakout_price": dc["close"], "entry_after": t, "range_pct": rp,
                })
                break
    candidates.sort(key=lambda x: x.get("range_pct", 0), reverse=True)
    return candidates, scanned


def scan_pdhl(ud, eq_keys, universe, ref_date, candles_5m):
    prev_date = _prev_trading_day(ref_date)
    prev_from = datetime.combine(prev_date, datetime.min.time()).replace(hour=9, minute=15)
    prev_to = datetime.combine(prev_date, datetime.min.time()).replace(hour=15, minute=30)

    # Fetch prev day candles in parallel
    prev_jobs = []
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if inst_key:
            prev_jobs.append((sym, inst_key, prev_from, prev_to, "day"))
    prev_candles = _parallel_fetch(ud, prev_jobs)

    candidates = []
    scanned = 0
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        pc = prev_candles.get(sym)
        if not pc:
            continue
        pdh = pc[0]["high"]
        pdl = pc[0]["low"]
        if pdh <= 0 or pdl <= 0 or pdh <= pdl:
            continue

        tc = candles_5m.get(sym)
        if not tc:
            continue
        scanned += 1

        for dc in tc:
            t = _candle_time(dc)
            if dc["close"] > pdh:
                candidates.append({
                    "symbol": sym, "direction": "bullish", "opt_type": "CE",
                    "breakout_price": dc["close"], "entry_after": t, "pdh": pdh, "pdl": pdl,
                })
                break
            elif dc["close"] < pdl:
                candidates.append({
                    "symbol": sym, "direction": "bearish", "opt_type": "PE",
                    "breakout_price": dc["close"], "entry_after": t, "pdh": pdh, "pdl": pdl,
                })
                break
    candidates.sort(key=lambda x: x.get("entry_after", ""))
    return candidates, scanned


def scan_gap_fade(ud, eq_keys, universe, ref_date, candles_5m):
    """Gap Fade: stocks gapping 0.5-2% from PDC, first 15-min candle confirms fade."""
    prev_date = _prev_trading_day(ref_date)
    prev_from = datetime.combine(prev_date, datetime.min.time()).replace(hour=9, minute=15)
    prev_to = datetime.combine(prev_date, datetime.min.time()).replace(hour=15, minute=30)

    prev_jobs = []
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if inst_key:
            prev_jobs.append((sym, inst_key, prev_from, prev_to, "day"))
    prev_candles = _parallel_fetch(ud, prev_jobs)

    # Also need 15-min candles for confirmation
    today_from = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    today_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=35)
    c15_jobs = []
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if inst_key:
            c15_jobs.append((sym, inst_key, today_from, today_to, "15minute"))
    candles_15m = _parallel_fetch(ud, c15_jobs)

    candidates = []
    scanned = 0

    for sym in universe:
        if sym in BLOCKLIST:
            continue
        pc = prev_candles.get(sym)
        if not pc:
            continue
        pdc = pc[0]["close"]
        if pdc <= 0:
            continue

        c15 = candles_15m.get(sym)
        if not c15:
            continue
        scanned += 1

        today_open = c15[0]["open"]
        gap_pct = abs(today_open - pdc) / pdc * 100

        if gap_pct < GAP_MIN_PCT or gap_pct > GAP_MAX_PCT:
            continue

        first_15_close = c15[0]["close"]
        first_15_open = c15[0]["open"]

        if today_open > pdc:
            # Gap up — look for fade (first candle closes below open = selling pressure)
            if first_15_close < first_15_open:
                candidates.append({
                    "symbol": sym, "direction": "bearish", "opt_type": "PE",
                    "breakout_price": first_15_close, "entry_after": "09:30",
                    "gap_pct": gap_pct, "pdc": pdc,
                })
        else:
            # Gap down — look for bounce (first candle closes above open = buying pressure)
            if first_15_close > first_15_open:
                candidates.append({
                    "symbol": sym, "direction": "bullish", "opt_type": "CE",
                    "breakout_price": first_15_close, "entry_after": "09:30",
                    "gap_pct": gap_pct, "pdc": pdc,
                })

    candidates.sort(key=lambda x: x.get("gap_pct", 0), reverse=True)
    return candidates, scanned


def scan_afternoon_momentum(candles_5m, universe):
    """Afternoon Momentum: lunchtime range breakout with volume confirmation."""
    candidates = []
    scanned = 0

    for sym in universe:
        if sym in BLOCKLIST:
            continue
        candles = candles_5m.get(sym)
        if not candles or len(candles) < 30:
            continue
        scanned += 1

        lunch_high = 0
        lunch_low = float("inf")
        lunch_vols = []

        for cn in candles:
            t = _candle_time(cn)
            if t < AFT_RANGE_START:
                continue
            if t >= AFT_RANGE_END:
                break
            lunch_high = max(lunch_high, cn["high"])
            lunch_low = min(lunch_low, cn["low"])
            lunch_vols.append(cn.get("volume", 0) or 1)

        if not lunch_vols or lunch_high <= lunch_low:
            continue

        # Filter: lunch range must be meaningful (at least 0.3%)
        lunch_range_pct = (lunch_high - lunch_low) / lunch_low * 100
        if lunch_range_pct < AFT_MIN_RANGE_PCT:
            continue

        avg_lunch_vol = sum(lunch_vols) / len(lunch_vols)
        if avg_lunch_vol <= 0:
            avg_lunch_vol = 1

        for cn in candles:
            t = _candle_time(cn)
            if t < AFT_RANGE_END:
                continue
            if t >= AFT_HARD_EXIT:
                break
            vol = cn.get("volume", 0) or 0

            if cn["close"] > lunch_high and vol >= avg_lunch_vol * AFT_VOL_MULT:
                candidates.append({
                    "symbol": sym, "direction": "bullish", "opt_type": "CE",
                    "breakout_price": cn["close"], "entry_after": t,
                    "vol_ratio": vol / avg_lunch_vol,
                })
                break
            elif cn["close"] < lunch_low and vol >= avg_lunch_vol * AFT_VOL_MULT:
                candidates.append({
                    "symbol": sym, "direction": "bearish", "opt_type": "PE",
                    "breakout_price": cn["close"], "entry_after": t,
                    "vol_ratio": vol / avg_lunch_vol,
                })
                break

    candidates.sort(key=lambda x: x.get("vol_ratio", 0), reverse=True)
    return candidates, scanned


# ---------------------------------------------------------------------------
# Run one day
# ---------------------------------------------------------------------------
def run_day(ud, ref_date, eq_keys, universe, opt_master, lot_sizes, verbose=True):
    print(f"\n{'#'*90}")
    print(f"  {ref_date} ({ref_date.strftime('%A')})")
    print(f"{'#'*90}")

    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    # Prefetch all equity 5-min candles in one parallel batch
    t0 = _t.time()
    candles_5m = _prefetch_equity_candles(ud, eq_keys, universe, from_dt, to_dt, "5minute")
    fetch_time = _t.time() - t0
    print(f"  Fetched {len(candles_5m)} stocks in {fetch_time:.1f}s")

    day_results = {}

    # 1. OEH
    cands, sc = scan_oeh(candles_5m, universe)
    print(f"\n  --- OEH --- Scanned: {sc} | Candidates: {len(cands)}")
    day_results["OEH"] = _exec_trades(cands, ref_date, ud, opt_master, lot_sizes,
                                       OEH_LOSS_CAP, OEH_PROFIT_CAP, "OEH", verbose)

    # 2. ORB
    cands, sc = scan_orb(candles_5m, universe)
    print(f"\n  --- ORB --- Scanned: {sc} | Breakouts: {len(cands)}")
    day_results["ORB"] = _exec_trades(cands, ref_date, ud, opt_master, lot_sizes,
                                       ORB_LOSS_CAP, ORB_PROFIT_CAP, "ORB", verbose)

    # 3. PDHL
    cands, sc = scan_pdhl(ud, eq_keys, universe, ref_date, candles_5m)
    print(f"\n  --- PDHL --- Scanned: {sc} | Breakouts: {len(cands)}")
    day_results["PDHL"] = _exec_trades(cands, ref_date, ud, opt_master, lot_sizes,
                                        PDHL_LOSS_CAP, PDHL_PROFIT_CAP, "PDHL", verbose)

    # 4. Gap Fade
    cands, sc = scan_gap_fade(ud, eq_keys, universe, ref_date, candles_5m)
    print(f"\n  --- GAP FADE --- Scanned: {sc} | Candidates: {len(cands)}")
    day_results["GAP"] = _exec_trades(cands, ref_date, ud, opt_master, lot_sizes,
                                       GAP_LOSS_CAP, GAP_PROFIT_CAP, "GAP-FADE", verbose,
                                       hard_exit_time=GAP_TIME_EXIT)

    # 5. Afternoon Momentum
    cands, sc = scan_afternoon_momentum(candles_5m, universe)
    print(f"\n  --- AFT MOM --- Scanned: {sc} | Breakouts: {len(cands)}")
    day_results["AFT"] = _exec_trades(cands, ref_date, ud, opt_master, lot_sizes,
                                       AFT_LOSS_CAP, AFT_PROFIT_CAP, "AFT-MOM", verbose,
                                       hard_exit_time=AFT_HARD_EXIT)

    # Day summary
    print(f"\n  {'─'*60}")
    grand = 0
    for label in ["OEH", "ORB", "PDHL", "GAP", "AFT"]:
        r = day_results[label]
        if not r:
            print(f"  {label:5s}: --")
            continue
        pnl = sum(x["pnl"] for x in r)
        w = sum(1 for x in r if x["pnl"] > 0)
        l = len(r) - w
        grand += pnl
        print(f"  {label:5s}: {len(r):3d}t {w}W/{l}L ₹{pnl:>+,.0f}")
    print(f"  {'DAY':5s}: ₹{grand:>+,.0f}")

    return day_results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _trading_days(year, month):
    days = []
    d = date(year, month, 1)
    while d.month == month:
        if d.weekday() < 5 and d <= date.today():
            days.append(d)
        d += timedelta(days=1)
    return days


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Single date YYYY-MM-DD")
    parser.add_argument("--month", help="Full month YYYY-MM")
    parser.add_argument("--quiet", action="store_true", help="Only show summaries")
    args = parser.parse_args()

    if args.date:
        dates = [date.fromisoformat(args.date)]
    elif args.month:
        y, m = args.month.split("-")
        dates = _trading_days(int(y), int(m))
    else:
        print("Provide --date or --month")
        return

    token = load_cached_token()
    if not token:
        print("ERROR: No valid Upstox token. Run auto-login first.")
        return
    ud = UpstoxData(access_token=token)
    master = ud._load_master()
    eq_keys, universe, opt_master, lot_sizes = _load_master_data(ud, master)

    verbose = not args.quiet

    print(f"\n  ALL 5 STRATEGIES BACKTEST")
    print(f"  Capital: ₹{CAPITAL:,}/strategy | Lots: {LOT_MULT} | SL: {SL_PCT*100:.0f}% / ₹{MAX_SL_RS:,}")
    print(f"  Universe: {len(universe)} stocks | Days: {len(dates)}")

    all_results = {"OEH": [], "ORB": [], "PDHL": [], "GAP": [], "AFT": []}

    for ref_date in dates:
        day_results = run_day(ud, ref_date, eq_keys, universe, opt_master, lot_sizes, verbose)
        for k in all_results:
            all_results[k].extend(day_results.get(k, []))

    # Monthly summary
    print(f"\n{'='*90}")
    print(f"  MONTHLY SUMMARY — {len(dates)} trading days")
    print(f"{'='*90}")

    grand = 0
    for label in ["OEH", "ORB", "PDHL", "GAP", "AFT"]:
        r = all_results[label]
        if not r:
            print(f"  {label:8s}: No trades")
            continue
        pnl = sum(x["pnl"] for x in r)
        wins = sum(1 for x in r if x["pnl"] > 0)
        losses = len(r) - wins
        wr = wins / len(r) * 100
        avg_w = sum(x["pnl"] for x in r if x["pnl"] > 0) / max(wins, 1)
        avg_l = sum(x["pnl"] for x in r if x["pnl"] <= 0) / max(losses, 1)
        grand += pnl
        print(f"  {label:8s}: {len(r):4d} trades | {wins}W/{losses}L ({wr:.0f}%) | "
              f"Avg W: ₹{avg_w:>+,.0f} | Avg L: ₹{avg_l:>+,.0f} | ₹{pnl:>+,.0f}")
    print(f"  {'TOTAL':8s}: ₹{grand:>+,.0f}")
    print()


if __name__ == "__main__":
    main()

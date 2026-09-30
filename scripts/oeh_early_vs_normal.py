"""Compare OEH entry at 09:16 (1-min candle) vs 09:20 (5-min candle) over N days.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/oeh_early_vs_normal.py --days 5
"""
from __future__ import annotations
import argparse, hashlib, json, re, time as _t
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
OEH_TOLERANCE = 0.05
OEH_MIN_DROP_PCT = 0.3
LOT_MULT = 2

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "candle_cache"


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
    _t.sleep(0.05)
    if candles is not None:
        with open(path, "w") as f:
            json.dump(candles, f)
    return candles


def _simulate_trade(ocandles, entry_idx, entry, lot, sl_price):
    peak_pnl = 0.0
    active_floor = 0

    for i, cn in enumerate(ocandles[entry_idx + 1:], start=entry_idx + 1):
        high, low, close = cn["high"], cn["low"], cn["close"]
        t = str(cn.get("date", cn.get("timestamp", "")))
        t_short = t[11:16] if len(t) > 16 else t[:5]

        pnl_high = (high - entry) * lot
        pnl_close = (close - entry) * lot

        if pnl_high > peak_pnl:
            peak_pnl = pnl_high

        if low <= sl_price:
            ep = round(sl_price * (1 - SLIPPAGE_PCT), 2)
            return ep, "SL", t_short, peak_pnl

        for fl in FLOOR_STEPS:
            if pnl_close >= fl and fl > active_floor:
                active_floor = fl
        if active_floor > 0 and pnl_close <= active_floor:
            ep = round(close * (1 - SLIPPAGE_PCT), 2)
            return ep, f"FLOOR ₹{active_floor}", t_short, peak_pnl

    last = ocandles[-1]
    t = str(last.get("date", last.get("timestamp", "")))
    ep = round(last["close"] * (1 - SLIPPAGE_PCT), 2)
    return ep, "EOD", t[11:16] if len(t) > 16 else t, peak_pnl


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


def _resolve_option(sym, spot, ref_date, opt_master, lot_sizes):
    sym_opts = {k: v for k, v in opt_master.items() if k[0] == sym and k[3] == "PE"}
    if not sym_opts:
        return None, None, None
    expiries = sorted({k[1] for k in sym_opts if k[1] >= ref_date})
    if not expiries:
        return None, None, None
    strikes = sorted({k[2] for k in sym_opts if k[1] == expiries[0]})
    strike = min(strikes, key=lambda s: abs(s - spot))
    opt_key = opt_master.get((sym, expiries[0], strike, "PE"))
    lot = lot_sizes.get(sym, 1) * LOT_MULT
    return opt_key, strike, lot


def _exec_oeh_trades(candidates, ref_date, ud, opt_master, lot_sizes, entry_after, verbose):
    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    full_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    prepared = []
    for c in candidates:
        sym = c["symbol"]
        spot = c["entry"]

        opt_key, strike, lot = _resolve_option(sym, spot, ref_date, opt_master, lot_sizes)
        if not opt_key:
            continue

        try:
            ocandles = _cached_fetch(ud, opt_key, from_dt, full_to, "1minute")
        except Exception:
            continue
        if not ocandles or len(ocandles) < 5:
            continue

        entry_candle = None
        entry_idx = 0
        for i, cn in enumerate(ocandles):
            ct = str(cn.get("date", cn.get("timestamp", "")))
            ct_short = ct[11:16] if len(ct) > 16 else ct[:5]
            if ct_short >= entry_after:
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
            ocandles, entry_idx, entry, lot, sl_price
        )

        pnl = (exit_price - entry) * lot
        charges = (BROKERAGE_PER_ORDER * 2) + (exit_price * lot * STT_PCT)
        pnl -= charges

        prepared.append({
            "candidate": c, "sym": sym, "strike": strike,
            "lot": lot, "entry": entry, "margin": entry * lot,
            "entry_time": entry_after, "exit_time": exit_time,
            "exit_price": exit_price, "exit_reason": exit_reason,
            "pnl": pnl, "peak": peak_pnl,
        })

    # Capital-gated concurrent execution
    prepared.sort(key=lambda x: x["entry_time"])
    avail = CAPITAL
    locked = {}
    total_pnl = 0.0
    results = []
    skipped = 0

    for idx, t in enumerate(prepared):
        freed_ids = []
        for locked_idx, locked_margin in locked.items():
            if prepared[locked_idx]["exit_time"] <= t["entry_time"]:
                avail += locked_margin + prepared[locked_idx]["pnl"]
                freed_ids.append(locked_idx)
        for fid in freed_ids:
            del locked[fid]

        if t["margin"] > avail:
            skipped += 1
            continue

        avail -= t["margin"]
        locked[idx] = t["margin"]
        total_pnl += t["pnl"]
        results.append(t)

    if verbose and results:
        for t in results:
            c = t["candidate"]
            print(f"    ▼ {t['sym']:<12s} {t['strike']:>6.0f}PE {t['entry']:>7.1f} → {t['exit_price']:>6.1f}"
                  f"  ₹{t['pnl']:>+8,.0f}  ₹{t['peak']:>+7,.0f} {t['exit_reason']:<12s} {t['entry_time']:>5s} {t['exit_time']:>5s}")

    return results, skipped


def _prev_trading_day(d):
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return p


def _get_trading_days(end_date, count):
    days = []
    d = end_date
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def run_day(ud, ref_date, eq_keys, universe, opt_master, lot_sizes, verbose=True):
    from_1m = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    to_1m = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=16)
    from_5m = from_1m
    to_5m = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=25)

    # --- Approach A: 1-min candle at 09:16, trade at 09:16 ---
    early_candidates = []
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if not inst_key:
            continue
        try:
            candles = _cached_fetch(ud, inst_key, from_1m, to_1m, "1minute")
        except Exception:
            continue
        if not candles or len(candles) < 1:
            continue
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
        early_candidates.append({"symbol": sym, "entry": ep, "drop_pct": dp})

    early_candidates.sort(key=lambda x: x["drop_pct"], reverse=True)

    # --- Approach B: 5-min candle at 09:20, trade at 09:20 ---
    normal_candidates = []
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if not inst_key:
            continue
        try:
            candles = _cached_fetch(ud, inst_key, from_5m, to_5m, "5minute")
        except Exception:
            continue
        if not candles or len(candles) < 1:
            continue
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
        normal_candidates.append({"symbol": sym, "entry": ep, "drop_pct": dp})

    normal_candidates.sort(key=lambda x: x["drop_pct"], reverse=True)

    # --- Approach C: 1-min filter at 09:16, verify + trade at 09:20 ---
    early_syms = {c["symbol"] for c in early_candidates}
    filtered_candidates = [c for c in normal_candidates if c["symbol"] in early_syms]

    print(f"\n{'='*90}")
    print(f"  {ref_date} — OEH Comparison")
    print(f"{'='*90}")

    # A: Early entry at 09:16
    print(f"\n  [A] 1-min candle → trade at 09:16 ({len(early_candidates)} candidates)")
    print(f"    {'Symbol':<14s} {'Str':>6s} {'Entry':>7s} {'Exit':>7s} {'P&L':>9s} {'Peak':>8s} {'Reason':<12s} {'ETime':>5s} {'XTime':>5s}")
    print(f"    {'-'*85}")
    a_results, a_skip = _exec_oeh_trades(early_candidates, ref_date, ud, opt_master, lot_sizes, "09:16", verbose)
    a_pnl = sum(r["pnl"] for r in a_results)
    a_wins = sum(1 for r in a_results if r["pnl"] > 0)
    print(f"    → {len(a_results)} trades | {a_wins}W/{len(a_results)-a_wins}L | Skip: {a_skip} | ₹{a_pnl:>+,.0f}")

    # B: Normal entry at 09:20
    print(f"\n  [B] 5-min candle → trade at 09:20 ({len(normal_candidates)} candidates)")
    print(f"    {'Symbol':<14s} {'Str':>6s} {'Entry':>7s} {'Exit':>7s} {'P&L':>9s} {'Peak':>8s} {'Reason':<12s} {'ETime':>5s} {'XTime':>5s}")
    print(f"    {'-'*85}")
    b_results, b_skip = _exec_oeh_trades(normal_candidates, ref_date, ud, opt_master, lot_sizes, "09:20", verbose)
    b_pnl = sum(r["pnl"] for r in b_results)
    b_wins = sum(1 for r in b_results if r["pnl"] > 0)
    print(f"    → {len(b_results)} trades | {b_wins}W/{len(b_results)-b_wins}L | Skip: {b_skip} | ₹{b_pnl:>+,.0f}")

    # C: Early filter + 09:20 entry
    print(f"\n  [C] 1-min filter → verify+trade at 09:20 ({len(filtered_candidates)} candidates)")
    print(f"    {'Symbol':<14s} {'Str':>6s} {'Entry':>7s} {'Exit':>7s} {'P&L':>9s} {'Peak':>8s} {'Reason':<12s} {'ETime':>5s} {'XTime':>5s}")
    print(f"    {'-'*85}")
    c_results, c_skip = _exec_oeh_trades(filtered_candidates, ref_date, ud, opt_master, lot_sizes, "09:20", verbose)
    c_pnl = sum(r["pnl"] for r in c_results)
    c_wins = sum(1 for r in c_results if r["pnl"] > 0)
    print(f"    → {len(c_results)} trades | {c_wins}W/{len(c_results)-c_wins}L | Skip: {c_skip} | ₹{c_pnl:>+,.0f}")

    # Show candidate overlap
    normal_syms = {c["symbol"] for c in normal_candidates}
    overlap = early_syms & normal_syms
    only_early = early_syms - normal_syms
    only_normal = normal_syms - early_syms
    print(f"\n  Candidates: early={len(early_syms)} | normal={len(normal_syms)} | overlap={len(overlap)}")
    if only_early:
        print(f"  Only in early (1m): {', '.join(sorted(only_early))}")
    if only_normal:
        print(f"  Only in normal (5m): {', '.join(sorted(only_normal))}")

    return a_pnl, b_pnl, c_pnl, len(a_results), len(b_results), len(c_results)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5, help="Number of trading days to backtest")
    args = parser.parse_args()

    today = date.today()
    trading_days = _get_trading_days(today, args.days)

    print(f"\n  OEH Early (09:16) vs Normal (09:20) — Last {args.days} trading days")
    print(f"  Capital: ₹{CAPITAL:,} | Lots: {LOT_MULT} | SL: {SL_PCT*100:.0f}% (max ₹{MAX_SL_RS:,})")
    print(f"  Slippage: {SLIPPAGE_PCT*100:.1f}% | Brokerage: ₹{BROKERAGE_PER_ORDER}/order")

    token = load_cached_token()
    if not token:
        print("ERROR: No valid Upstox token. Run auto-login first.")
        return
    ud = UpstoxData(access_token=token)
    master = ud._load_master()
    eq_keys, universe, opt_master, lot_sizes = _load_master_data(ud, master)
    print(f"  F&O universe: {len(universe)} stocks")

    totals_a, totals_b, totals_c = 0.0, 0.0, 0.0
    trades_a, trades_b, trades_c = 0, 0, 0

    for day in trading_days:
        a, b, c, ta, tb, tc = run_day(ud, day, eq_keys, universe, opt_master, lot_sizes)
        totals_a += a
        totals_b += b
        totals_c += c
        trades_a += ta
        trades_b += tb
        trades_c += tc

    print(f"\n{'='*90}")
    print(f"  SUMMARY — {args.days} days")
    print(f"{'='*90}")
    print(f"  [A] Early 09:16 entry : {trades_a:3d} trades | ₹{totals_a:>+10,.0f}")
    print(f"  [B] Normal 09:20 entry: {trades_b:3d} trades | ₹{totals_b:>+10,.0f}")
    print(f"  [C] Early filter+09:20: {trades_c:3d} trades | ₹{totals_c:>+10,.0f}")
    print(f"\n  Diff A vs B: ₹{totals_a - totals_b:>+,.0f}")
    print(f"  Diff C vs B: ₹{totals_c - totals_b:>+,.0f}")
    print()


if __name__ == "__main__":
    main()

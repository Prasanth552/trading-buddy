"""Replay OEH + ORB + PDHL strategies for a given date using real candle data.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/replay_day.py --date 2026-09-29
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

# OEH specific
OEH_TOLERANCE = 0.05
OEH_MIN_DROP_PCT = 0.3
OEH_LOSS_CAP = 5000
OEH_PROFIT_CAP = 999999

# ORB specific
ORB_MIN_RANGE_PCT = 0.3
ORB_MAX_RANGE_PCT = 3.0
ORB_LOSS_CAP = 5000
ORB_PROFIT_CAP = 999999

# PDHL specific
PDHL_LOSS_CAP = 10000
PDHL_PROFIT_CAP = 25000

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


def _prev_trading_day(d):
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return p


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


def _exec_trades(candidates, ref_date, ud, opt_master, lot_sizes, loss_cap, profit_cap, label, verbose):
    """Simulate trades with concurrent capital tracking — capital is locked
    until the trade actually exits (minute-by-minute sim)."""
    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    full_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    # Phase 1: resolve all candidates and prefetch option candles
    prepared = []
    for c in candidates:
        sym = c["symbol"]
        opt_type = c.get("opt_type", "PE")
        spot = c["breakout_price"]

        opt_key, strike, lot = _resolve_option(sym, spot, opt_type, ref_date, opt_master, lot_sizes)
        if not opt_key:
            continue

        try:
            ocandles = _cached_fetch(ud, opt_key, from_dt, full_to, "1minute")
        except Exception:
            continue
        if not ocandles or len(ocandles) < 5:
            continue

        bt = c.get("entry_after", "09:20")
        entry_candle = None
        entry_idx = 0
        for i, cn in enumerate(ocandles):
            ct = str(cn.get("date", cn.get("timestamp", "")))
            ct_short = ct[11:16] if len(ct) > 16 else ct[:5]
            if ct_short >= bt:
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
            "candidate": c, "sym": sym, "opt_type": opt_type, "strike": strike,
            "lot": lot, "entry": entry, "margin": entry * lot,
            "entry_time": bt, "exit_time": exit_time,
            "exit_price": exit_price, "exit_reason": exit_reason,
            "pnl": pnl, "peak": peak_pnl,
        })

    # Phase 2: event-driven capital simulation with rescans
    # Sort by entry time; when capital runs out, fast-forward to next exit and retry
    prepared.sort(key=lambda x: x["entry_time"])

    avail = CAPITAL
    active = []  # list of (exit_time, margin, pnl, idx)
    total_pnl = 0.0
    results = []
    traded_indices = set()

    if verbose:
        print(f"\n  {'Symbol':<14s} {'Str':>6s} {'D':>1s} {'Entry':>7s} {'Exit':>7s}"
              f" {'P&L':>9s} {'Peak':>8s} {'Reason':<12s} {'ETime':>5s} {'XTime':>5s}")
        print(f"  {'-'*95}")

    def _try_place(candidates_idx_list, cursor_time=None):
        nonlocal avail, total_pnl
        placed = 0
        still_pending = []
        for idx in candidates_idx_list:
            if idx in traded_indices:
                continue
            if total_pnl >= profit_cap or total_pnl <= -loss_cap:
                break
            t = prepared[idx]
            if t["margin"] > avail:
                still_pending.append(idx)
                continue

            avail -= t["margin"]
            active.append((t["exit_time"], t["margin"], t["pnl"], idx))
            total_pnl += t["pnl"]
            traded_indices.add(idx)
            placed += 1

            results.append({"sym": t["sym"], "pnl": t["pnl"], "reason": t["exit_reason"], "peak": t["peak"]})

            if verbose:
                c = t["candidate"]
                d_tag = "▲" if c.get("direction", "bearish") == "bullish" else "▼"
                print(f"  {d_tag} {t['sym']:<12s} {t['strike']:>6.0f}{t['opt_type']} {t['entry']:>7.1f} → {t['exit_price']:>6.1f}"
                      f"  ₹{t['pnl']:>+8,.0f}  ₹{t['peak']:>+7,.0f} {t['exit_reason']:<12s} {t['entry_time']:>5s} {t['exit_time']:>5s}")
        return placed, still_pending

    # First pass: process all candidates in order
    all_indices = list(range(len(prepared)))
    _, pending = _try_place(all_indices)

    # Rescan loop: free earliest exit, retry pending candidates
    while pending and active:
        if total_pnl >= profit_cap or total_pnl <= -loss_cap:
            break

        # Free the earliest exiting trade
        active.sort(key=lambda x: x[0])
        ext, margin, pnl, tidx = active.pop(0)
        avail += margin + pnl

        # Also free any others that exit at the same time or earlier
        new_active = []
        for a in active:
            if a[0] <= ext:
                avail += a[1] + a[2]
            else:
                new_active.append(a)
        active = new_active

        if avail < 5000:
            continue

        placed, pending = _try_place(pending)
        if placed == 0 and not active:
            break

    skipped = len(pending)
    if results:
        day_pnl = sum(r["pnl"] for r in results)
        wins = sum(1 for r in results if r["pnl"] > 0)
        losses = len(results) - wins
        print(f"  {label} TOTAL: {len(results)} trades | {wins}W/{losses}L | Skipped(cap): {skipped} | ₹{day_pnl:>+,.0f}")
    else:
        print(f"  {label}: No trades")

    return results


def run_oeh(ud, ref_date, eq_keys, universe, opt_master, lot_sizes, verbose=True):
    print(f"\n{'='*90}")
    print(f"  OEH (Open=High) — {ref_date}")
    print(f"{'='*90}")

    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=25)

    candidates = []
    scanned = 0
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if not inst_key:
            continue
        try:
            candles = _cached_fetch(ud, inst_key, from_dt, to_dt, "5minute")
        except Exception:
            continue
        scanned += 1
        if not candles:
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
        candidates.append({
            "symbol": sym, "direction": "bearish", "opt_type": "PE",
            "breakout_price": ep, "entry_after": "09:20",
            "drop_pct": dp,
        })

    candidates.sort(key=lambda x: x["drop_pct"], reverse=True)
    print(f"  Scanned: {scanned} | OEH candidates: {len(candidates)}")

    return _exec_trades(candidates, ref_date, ud, opt_master, lot_sizes,
                        OEH_LOSS_CAP, OEH_PROFIT_CAP, "OEH", verbose)


def run_orb(ud, ref_date, eq_keys, universe, opt_master, lot_sizes, verbose=True):
    print(f"\n{'='*90}")
    print(f"  ORB (Opening Range Breakout) — {ref_date}")
    print(f"{'='*90}")

    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    full_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    candidates = []
    scanned = 0
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if not inst_key:
            continue
        try:
            candles = _cached_fetch(ud, inst_key, from_dt, full_to, "5minute")
        except Exception:
            continue
        scanned += 1
        if not candles or len(candles) < 3:
            continue

        range_high = candles[0]["high"]
        range_low = candles[0]["low"]
        range_open = candles[0]["open"]
        if range_open <= 0:
            continue

        range_pct = (range_high - range_low) / range_open * 100
        if range_pct < ORB_MIN_RANGE_PCT or range_pct > ORB_MAX_RANGE_PCT:
            continue

        for dc in candles[1:]:
            t = str(dc.get("date", dc.get("timestamp", "")))
            t_short = t[11:16] if len(t) > 16 else t[:5]
            if dc["high"] > range_high:
                candidates.append({
                    "symbol": sym, "direction": "bullish", "opt_type": "CE",
                    "breakout_price": dc["close"], "entry_after": t_short,
                    "range_pct": range_pct,
                })
                break
            elif dc["low"] < range_low:
                candidates.append({
                    "symbol": sym, "direction": "bearish", "opt_type": "PE",
                    "breakout_price": dc["close"], "entry_after": t_short,
                    "range_pct": range_pct,
                })
                break

    candidates.sort(key=lambda x: x.get("range_pct", 0), reverse=True)
    print(f"  Scanned: {scanned} | ORB breakouts: {len(candidates)}")

    return _exec_trades(candidates, ref_date, ud, opt_master, lot_sizes,
                        ORB_LOSS_CAP, ORB_PROFIT_CAP, "ORB", verbose)


def run_pdhl(ud, ref_date, eq_keys, universe, opt_master, lot_sizes, verbose=True):
    print(f"\n{'='*90}")
    print(f"  PDHL (Previous Day High/Low Breakout) — {ref_date}")
    print(f"{'='*90}")

    prev_date = _prev_trading_day(ref_date)
    prev_from = datetime.combine(prev_date, datetime.min.time()).replace(hour=9, minute=15)
    prev_to = datetime.combine(prev_date, datetime.min.time()).replace(hour=15, minute=30)
    today_from = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    today_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    candidates = []
    scanned = 0
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if not inst_key:
            continue

        try:
            prev_candles = _cached_fetch(ud, inst_key, prev_from, prev_to, "day")
        except Exception:
            continue
        if not prev_candles:
            continue

        pdh = prev_candles[0]["high"]
        pdl = prev_candles[0]["low"]
        if pdh <= 0 or pdl <= 0 or pdh <= pdl:
            continue

        try:
            today_candles = _cached_fetch(ud, inst_key, today_from, today_to, "5minute")
        except Exception:
            continue
        scanned += 1
        if not today_candles:
            continue

        for dc in today_candles:
            t = str(dc.get("date", dc.get("timestamp", "")))
            t_short = t[11:16] if len(t) > 16 else t[:5]
            if dc["close"] > pdh:
                candidates.append({
                    "symbol": sym, "direction": "bullish", "opt_type": "CE",
                    "breakout_price": dc["close"], "entry_after": t_short,
                    "pdh": pdh, "pdl": pdl,
                })
                break
            elif dc["close"] < pdl:
                candidates.append({
                    "symbol": sym, "direction": "bearish", "opt_type": "PE",
                    "breakout_price": dc["close"], "entry_after": t_short,
                    "pdh": pdh, "pdl": pdl,
                })
                break

    candidates.sort(key=lambda x: x.get("entry_after", ""))
    print(f"  Scanned: {scanned} | PDHL breakouts: {len(candidates)}")

    return _exec_trades(candidates, ref_date, ud, opt_master, lot_sizes,
                        PDHL_LOSS_CAP, PDHL_PROFIT_CAP, "PDHL", verbose)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Date to replay (YYYY-MM-DD)")
    args = parser.parse_args()

    ref_date = date.fromisoformat(args.date)
    print(f"\n  Replaying {ref_date} — OEH + ORB + PDHL")
    print(f"  Capital: ₹{CAPITAL:,} per strategy | Lots: {LOT_MULT} | SL: {SL_PCT*100:.0f}%")
    print(f"  Slippage: {SLIPPAGE_PCT*100:.1f}% | Brokerage: ₹{BROKERAGE_PER_ORDER}/order | STT: {STT_PCT*100:.4f}%")

    token = load_cached_token()
    if not token:
        print("ERROR: No valid Upstox token. Run auto-login first.")
        return
    ud = UpstoxData(access_token=token)
    master = ud._load_master()

    eq_keys, universe, opt_master, lot_sizes = _load_master_data(ud, master)
    print(f"  F&O universe: {len(universe)} stocks")

    oeh_results = run_oeh(ud, ref_date, eq_keys, universe, opt_master, lot_sizes)
    orb_results = run_orb(ud, ref_date, eq_keys, universe, opt_master, lot_sizes)
    pdhl_results = run_pdhl(ud, ref_date, eq_keys, universe, opt_master, lot_sizes)

    print(f"\n{'='*90}")
    print(f"  GRAND TOTAL — {ref_date}")
    print(f"{'='*90}")
    grand = 0
    for label, results in [("OEH", oeh_results), ("ORB", orb_results), ("PDHL", pdhl_results)]:
        pnl = sum(r["pnl"] for r in results)
        wins = sum(1 for r in results if r["pnl"] > 0)
        losses = len(results) - wins
        wr = wins / len(results) * 100 if results else 0
        grand += pnl
        print(f"  {label:5s}: {len(results):3d} trades | {wins}W/{losses}L ({wr:.0f}%) | ₹{pnl:>+,.0f}")
    print(f"  {'TOTAL':5s}: ₹{grand:>+,.0f}")
    print()


if __name__ == "__main__":
    main()

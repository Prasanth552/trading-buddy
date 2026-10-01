"""Backtest 52-week High/Low reversal strategy.

Logic: When a stock touches its 52-week high intraday → buy PE (expect reversal down)
       When a stock touches its 52-week low intraday  → buy CE (expect reversal up)
Exit:  Same as ORB/PDHL — floor stepping + 30% SL + ₹5K max SL, 2 lots

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/replay_52w.py --date 2026-10-01
    PYTHONPATH=. .venv/bin/python3 scripts/replay_52w.py --date 2026-10-01 --days 5
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
MAX_SL_RS = 10000
FLOOR_STEPS = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
CAPITAL = 150000
BROKERAGE_PER_ORDER = 20
STT_PCT = 0.000625
LOT_MULT = 1
LOSS_CAP = 999999
PROFIT_CAP = 999999

W52_LOOKBACK_DAYS = 365
W52_TOUCH_PCT = 0.003  # within 0.3% of 52W high/low counts as "touch"

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


def _get_52w_high_low(ud, inst_key, ref_date):
    """Fetch daily candles for ~1 year before ref_date to compute 52W high/low."""
    from_dt = datetime.combine(ref_date - timedelta(days=W52_LOOKBACK_DAYS), datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(_prev_trading_day(ref_date), datetime.min.time()).replace(hour=15, minute=30)

    candles = _cached_fetch(ud, inst_key, from_dt, to_dt, "day")
    if not candles or len(candles) < 20:
        return None, None

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    return max(highs), min(lows)


def run_52w(ud, ref_date, eq_keys, universe, opt_master, lot_sizes, verbose=True):
    print(f"\n{'='*90}")
    print(f"  52W High/Low Reversal — {ref_date}")
    print(f"{'='*90}")

    today_from = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    today_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    # Step 1: Get 52W high/low for all stocks
    print(f"  Fetching 52W high/low for {len(universe)} stocks...")
    w52_data = {}
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if not inst_key:
            continue
        w52h, w52l = _get_52w_high_low(ud, inst_key, ref_date)
        if w52h and w52l:
            w52_data[sym] = (inst_key, w52h, w52l)

    print(f"  52W data loaded for {len(w52_data)} stocks")

    # Step 2: Scan today's 5-min candles for 52W touches
    candidates = []
    scanned = 0
    w52h_count = 0
    w52l_count = 0

    for sym, (inst_key, w52h, w52l) in w52_data.items():
        try:
            candles = _cached_fetch(ud, inst_key, today_from, today_to, "5minute")
        except Exception:
            continue
        scanned += 1
        if not candles:
            continue

        for dc in candles:
            t = str(dc.get("date", dc.get("timestamp", "")))
            t_short = t[11:16] if len(t) > 16 else t[:5]

            # Touch 52W high → expect reversal down → buy PE
            if dc["high"] >= w52h * (1 - W52_TOUCH_PCT):
                candidates.append({
                    "symbol": sym, "direction": "bearish", "opt_type": "PE",
                    "breakout_price": dc["close"], "entry_after": t_short,
                    "w52h": w52h, "w52l": w52l, "touch": "52W HIGH",
                })
                w52h_count += 1
                break

            # Touch 52W low → expect reversal up → buy CE
            if dc["low"] <= w52l * (1 + W52_TOUCH_PCT):
                candidates.append({
                    "symbol": sym, "direction": "bullish", "opt_type": "CE",
                    "breakout_price": dc["close"], "entry_after": t_short,
                    "w52h": w52h, "w52l": w52l, "touch": "52W LOW",
                })
                w52l_count += 1
                break

    candidates.sort(key=lambda x: x.get("entry_after", ""))
    print(f"  Scanned: {scanned} | 52W HIGH touches: {w52h_count} | 52W LOW touches: {w52l_count} | Total: {len(candidates)}")

    # Step 3: Execute trades using same simulation as replay_day
    return _exec_trades(candidates, ref_date, ud, opt_master, lot_sizes, verbose)


def _exec_trades(candidates, ref_date, ud, opt_master, lot_sizes, verbose):
    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    full_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

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

    prepared.sort(key=lambda x: x["entry_time"])

    avail = CAPITAL
    active = []
    realized_pnl = 0.0
    results = []
    traded_indices = set()

    if verbose:
        print(f"\n  {'Symbol':<14s} {'Str':>6s} {'D':>1s} {'Entry':>7s} {'Exit':>7s}"
              f" {'P&L':>9s} {'Peak':>8s} {'Reason':<12s} {'ETime':>5s} {'XTime':>5s} {'Touch'}")
        print(f"  {'-'*110}")

    def _do_place(idx):
        nonlocal avail
        t = prepared[idx]
        avail -= t["margin"]
        active.append((t["exit_time"], t["entry_time"], t["margin"], t["pnl"], idx))
        traded_indices.add(idx)
        results.append({"sym": t["sym"], "pnl": t["pnl"], "reason": t["exit_reason"], "peak": t["peak"],
                        "touch": t["candidate"].get("touch", "")})
        if verbose:
            c = t["candidate"]
            d_tag = "▲" if c.get("direction", "bearish") == "bullish" else "▼"
            touch = c.get("touch", "")
            print(f"  {d_tag} {t['sym']:<12s} {t['strike']:>6.0f}{t['opt_type']} {t['entry']:>7.1f} → {t['exit_price']:>6.1f}"
                  f"  ₹{t['pnl']:>+8,.0f}  ₹{t['peak']:>+7,.0f} {t['exit_reason']:<12s} {t['entry_time']:>5s} {t['exit_time']:>5s} {touch}")

    # First pass
    pending = []
    for idx in range(len(prepared)):
        t = prepared[idx]
        if t["margin"] > avail:
            pending.append(idx)
            continue
        _do_place(idx)

    # Rescan loop
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

        if realized_pnl >= PROFIT_CAP or realized_pnl <= -LOSS_CAP:
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
            t = prepared[idx]
            if t["margin"] > avail:
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
        wr = wins / len(results) * 100 if results else 0

        # Breakdown by touch type
        h_trades = [r for r in results if r["touch"] == "52W HIGH"]
        l_trades = [r for r in results if r["touch"] == "52W LOW"]
        h_pnl = sum(r["pnl"] for r in h_trades)
        l_pnl = sum(r["pnl"] for r in l_trades)
        h_wins = sum(1 for r in h_trades if r["pnl"] > 0)
        l_wins = sum(1 for r in l_trades if r["pnl"] > 0)

        print(f"\n  52W TOTAL: {len(results)} trades | {wins}W/{losses}L ({wr:.0f}%) | Skipped(cap): {skipped} | ₹{day_pnl:>+,.0f}")
        if h_trades:
            print(f"    52W HIGH (PE): {len(h_trades)} trades | {h_wins}W/{len(h_trades)-h_wins}L | ₹{h_pnl:>+,.0f}")
        if l_trades:
            print(f"    52W LOW  (CE): {len(l_trades)} trades | {l_wins}W/{len(l_trades)-l_wins}L | ₹{l_pnl:>+,.0f}")
    else:
        print(f"  52W: No trades")

    return results


def _get_trading_days(end_date, num_days):
    days = []
    d = end_date
    while len(days) < num_days:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=1, help="Number of trading days to backtest")
    args = parser.parse_args()

    end_date = date.fromisoformat(args.date)
    trading_days = _get_trading_days(end_date, args.days)

    print(f"\n  52W High/Low Reversal Backtest")
    print(f"  Capital: ₹{CAPITAL:,} | Lots: {LOT_MULT} | SL: {SL_PCT*100:.0f}% / ₹{MAX_SL_RS:,} max")
    print(f"  Touch threshold: within {W52_TOUCH_PCT*100:.1f}% of 52W high/low")
    print(f"  Days: {len(trading_days)} ({trading_days[0]} to {trading_days[-1]})")

    token = load_cached_token()
    if not token:
        print("ERROR: No valid Upstox token. Run auto-login first.")
        return
    ud = UpstoxData(access_token=token)
    master = ud._load_master()

    eq_keys, universe, opt_master, lot_sizes = _load_master_data(ud, master)
    print(f"  F&O universe: {len(universe)} stocks")

    all_results = []
    for day in trading_days:
        day_results = run_52w(ud, day, eq_keys, universe, opt_master, lot_sizes)
        all_results.append((day, day_results))

    if len(trading_days) > 1:
        print(f"\n{'='*90}")
        print(f"  MULTI-DAY SUMMARY — {trading_days[0]} to {trading_days[-1]}")
        print(f"{'='*90}")
        grand_pnl = 0
        grand_trades = 0
        grand_wins = 0
        for day, results in all_results:
            pnl = sum(r["pnl"] for r in results)
            wins = sum(1 for r in results if r["pnl"] > 0)
            wr = wins / len(results) * 100 if results else 0
            grand_pnl += pnl
            grand_trades += len(results)
            grand_wins += wins
            print(f"  {day}: {len(results):3d} trades | {wins}W/{len(results)-wins}L ({wr:4.0f}%) | ₹{pnl:>+10,.0f}")
        wr = grand_wins / grand_trades * 100 if grand_trades else 0
        print(f"  {'TOTAL':10s}: {grand_trades:3d} trades | {grand_wins}W/{grand_trades-grand_wins}L ({wr:4.0f}%) | ₹{grand_pnl:>+10,.0f}")


if __name__ == "__main__":
    main()

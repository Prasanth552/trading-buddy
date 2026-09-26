"""PDH/PDL Breakout Backtest — Buy CE on break above Previous Day High,
Buy PE on break below Previous Day Low.

Same conditions as ORB: ₹500 stepped floors, 30% SL (₹5K cap),
₹1.5L capital, 2 lots, 0.5% slippage, ₹0.50 min premium, ₹25K profit cap.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/pdhl_backtest.py --date 2026-09-25
    PYTHONPATH=. .venv/bin/python3 scripts/pdhl_backtest.py --from 2026-08-25 --to 2026-09-25
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
PROFIT_CAP = 25000
BREAKOUT_WINDOW_END = "14:00"

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
    _t.sleep(0.15)
    if candles is not None:
        with open(path, "w") as f:
            json.dump(candles, f)
    return candles


def _simulate_trade(ocandles, entry_idx, entry, lot, sl_price):
    peak_pnl = 0.0
    active_floor = 0

    for i, cn in enumerate(ocandles[entry_idx + 1:], start=entry_idx + 1):
        high, low = cn["high"], cn["low"]
        t = str(cn.get("date", cn.get("timestamp", "")))
        t_short = t[11:16] if len(t) > 16 else t[:5]

        pnl_high = (high - entry) * lot
        pnl_low = (low - entry) * lot

        if pnl_high > peak_pnl:
            peak_pnl = pnl_high

        if low <= sl_price:
            ep = round(sl_price * (1 - SLIPPAGE_PCT), 2)
            return ep, "SL", t_short, peak_pnl

        for fl in FLOOR_STEPS:
            if pnl_high >= fl and fl > active_floor:
                active_floor = fl
        if active_floor > 0 and pnl_low <= active_floor:
            ep = entry + active_floor / lot
            ep = round(ep * (1 - SLIPPAGE_PCT), 2)
            return ep, f"FLOOR ₹{active_floor}", t_short, peak_pnl

    last = ocandles[-1]
    t = str(last.get("date", last.get("timestamp", "")))
    ep = round(last["close"] * (1 - SLIPPAGE_PCT), 2)
    return ep, "eod", t[11:16] if len(t) > 16 else t, peak_pnl


def _prev_trading_day(d):
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return p


def run_day(ud, ref_date, eq_keys, matched, opt_master, lot_sizes, lot_mult, verbose=True):
    prev_date = _prev_trading_day(ref_date)
    prev_from = datetime.combine(prev_date, datetime.min.time()).replace(hour=9, minute=15)
    prev_to = datetime.combine(prev_date, datetime.min.time()).replace(hour=15, minute=30)
    today_from = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    today_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    pdh_pdl = {}
    today_candles = {}
    scanned = 0

    for sym in matched:
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
        pdh_pdl[sym] = (pdh, pdl)

        try:
            tcandles = _cached_fetch(ud, inst_key, today_from, today_to, "5minute")
        except Exception:
            continue
        scanned += 1
        if not tcandles or len(tcandles) < 3:
            continue
        today_candles[sym] = tcandles

    breakouts = []
    for sym, tcandles in today_candles.items():
        pdh, pdl = pdh_pdl[sym]
        for cn in tcandles:
            t = str(cn.get("date", cn.get("timestamp", "")))
            t_short = t[11:16] if len(t) > 16 else t[:5]
            if t_short >= BREAKOUT_WINDOW_END:
                break
            if cn["high"] > pdh:
                breakouts.append({
                    "symbol": sym, "direction": "bullish",
                    "breakout_price": cn["close"], "breakout_time": t_short,
                    "pdh": pdh, "pdl": pdl,
                    "range_pct": (pdh - pdl) / pdl * 100,
                })
                break
            elif cn["low"] < pdl:
                breakouts.append({
                    "symbol": sym, "direction": "bearish",
                    "breakout_price": cn["close"], "breakout_time": t_short,
                    "pdh": pdh, "pdl": pdl,
                    "range_pct": (pdh - pdl) / pdl * 100,
                })
                break

    breakouts.sort(key=lambda x: x["breakout_time"])

    if verbose:
        print(f"\n  --- {ref_date} | Scanned: {scanned} | Breakouts: {len(breakouts)} ---")

    if not breakouts:
        return [], 0

    avail = CAPITAL
    total_pnl = 0.0
    results = []
    active_trades = []

    if verbose:
        print(f"  {'Symbol':<14s} {'Str':>6s} {'D':>1s} {'Entry':>7s} {'Exit':>7s}"
              f" {'P&L':>9s} {'Peak':>8s} {'Reason':<12s} {'Time':>5s}")
        print(f"  {'-'*85}")

    for b in breakouts:
        if total_pnl >= PROFIT_CAP:
            break

        sym = b["symbol"]
        opt_type = "CE" if b["direction"] == "bullish" else "PE"
        spot = b["breakout_price"]

        sym_opts = {k: v for k, v in opt_master.items() if k[0] == sym and k[3] == opt_type}
        if not sym_opts:
            continue
        expiries = sorted({k[1] for k in sym_opts if k[1] >= ref_date})
        if not expiries:
            continue
        strikes = sorted({k[2] for k in sym_opts if k[1] == expiries[0]})
        strike = min(strikes, key=lambda s: abs(s - spot))
        opt_key = opt_master.get((sym, expiries[0], strike, opt_type))
        if not opt_key:
            continue
        lot = lot_sizes.get(sym, 1) * lot_mult

        try:
            ocandles = _cached_fetch(ud, opt_key, today_from, today_to, "1minute")
        except Exception:
            continue
        if not ocandles or len(ocandles) < 5:
            continue

        bt_short = b["breakout_time"]
        entry_candle = None
        entry_idx = 0
        for i, cn in enumerate(ocandles):
            ct = str(cn.get("date", cn.get("timestamp", "")))
            ct_short = ct[11:16] if len(ct) > 16 else ct[:5]
            if ct_short >= bt_short:
                entry_candle = cn
                entry_idx = i
                break
        if not entry_candle:
            continue

        raw_entry = entry_candle["close"]
        if raw_entry <= 0 or raw_entry < MIN_PREMIUM:
            continue

        entry = round(raw_entry * (1 + SLIPPAGE_PCT), 2)
        margin = entry * lot
        if margin > avail:
            continue
        avail -= margin

        sl_pct_price = entry * (1 - SL_PCT)
        sl_cap_price = entry - (MAX_SL_RS / lot)
        sl_price = round(max(sl_pct_price, sl_cap_price), 2)

        exit_price, exit_reason, exit_time, peak_pnl = _simulate_trade(
            ocandles, entry_idx, entry, lot, sl_price
        )

        pnl = (exit_price - entry) * lot
        total_pnl += pnl
        avail += margin + pnl

        results.append({"sym": sym, "pnl": pnl, "reason": exit_reason, "peak": peak_pnl})

        if verbose:
            icon = "+" if pnl > 0 else ""
            d_tag = "▲" if b["direction"] == "bullish" else "▼"
            print(f"  {d_tag} {sym:<12s} {strike:>6.0f}{opt_type} {entry:>7.1f} → {exit_price:>6.1f}"
                  f"  ₹{pnl:>+8,.0f}  ₹{peak_pnl:>+7,.0f} {exit_reason:<12s} {exit_time:>5s}")

    if verbose and results:
        day_pnl = sum(r["pnl"] for r in results)
        wins = sum(1 for r in results if r["pnl"] > 0)
        losses = len(results) - wins
        print(f"  DAY: {len(results)} trades | {wins}W/{losses}L | ₹{day_pnl:>+,.0f}")

    return results, len(results)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    parser.add_argument("--from", dest="from_date", default=None)
    parser.add_argument("--to", dest="to_date", default=None)
    parser.add_argument("--lots", type=int, default=2)
    args = parser.parse_args()

    if args.date:
        dates = [date.fromisoformat(args.date)]
    elif args.from_date and args.to_date:
        d = date.fromisoformat(args.from_date)
        end = date.fromisoformat(args.to_date)
        dates = []
        while d <= end:
            if d.weekday() < 5:
                dates.append(d)
            d += timedelta(days=1)
    else:
        print("Provide --date or --from/--to")
        return

    lot_mult = args.lots
    token = load_cached_token()
    ud = UpstoxData(access_token=token)
    master = ud._load_master()

    universe = set()
    for inst in master:
        if inst.get("segment") != "NSE_FO":
            continue
        if (inst.get("instrument_type") or "").upper() not in ("CE", "PE"):
            continue
        tsym = (inst.get("trading_symbol") or "").upper()
        base = re.match(r'^([A-Z&]+)', tsym)
        if base:
            name = base.group(1)
            if name and name not in INDEX_NAMES and len(name) >= 2:
                universe.add(name)

    eq_keys = {}
    for inst in master:
        if inst.get("segment") == "NSE_EQ":
            tsym = (inst.get("trading_symbol") or "").upper()
            if tsym:
                eq_keys[tsym] = inst.get("instrument_key")

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
        strike_val = float(inst.get("strike_price", 0))
        ed = _expiry_to_date(inst.get("expiry"))
        if ed and strike_val > 0:
            opt_master[(sym_name, ed, strike_val, itype)] = inst.get("instrument_key")
            ls = int(inst.get("lot_size") or 0)
            if ls > 0:
                lot_sizes[sym_name] = ls

    matched = sorted(s for s in universe if s in eq_keys)

    print(f"\n{'='*90}")
    print(f"  PDH/PDL BREAKOUT BACKTEST — {dates[0]} to {dates[-1]} ({len(dates)} days)")
    print(f"  Capital: ₹{CAPITAL:,.0f} | Slippage: {SLIPPAGE_PCT*100}% | Floors: ₹500 steps")
    print(f"  SL: {SL_PCT*100:.0f}% / ₹{MAX_SL_RS:,} cap | Lots: {lot_mult} | Profit cap: ₹{PROFIT_CAP:,}")
    print(f"  Breakout window: 09:15 – {BREAKOUT_WINDOW_END}")
    print(f"{'='*90}")

    all_results = []
    day_pnls = []
    total_trades = 0

    t_start = _t.time()
    for di, ref_date in enumerate(dates):
        elapsed = _t.time() - t_start
        if di > 0 and not (args.date):
            per_day = elapsed / di
            eta = per_day * (len(dates) - di)
            print(f"\r  Day {di+1}/{len(dates)}: {ref_date} | Elapsed: {elapsed:.0f}s | ETA: {eta:.0f}s   ",
                  end="", flush=True)
        elif not args.date:
            print(f"\r  Day {di+1}/{len(dates)}: {ref_date}   ", end="", flush=True)

        verbose = bool(args.date)
        results, n = run_day(ud, ref_date, eq_keys, matched, opt_master, lot_sizes, lot_mult, verbose=verbose)
        total_trades += n
        day_pnl = sum(r["pnl"] for r in results)
        all_results.extend(results)
        day_pnls.append((ref_date, day_pnl, n))

    if not args.date:
        print(f"\r  Done! {len(dates)} days, {total_trades} trades in {_t.time()-t_start:.0f}s{' '*30}")

    if len(dates) > 1:
        print(f"\n  {'='*60}")
        print(f"  PER-DAY P&L")
        print(f"  {'='*60}")
        print(f"  {'Date':<12s} {'Trades':>6s} {'P&L':>12s}")
        print(f"  {'-'*32}")
        for d, pnl, n in day_pnls:
            print(f"  {str(d):<12s} {n:>6d}  ₹{pnl:>+10,.0f}")
        total = sum(p for _, p, _ in day_pnls)
        print(f"  {'TOTAL':<12s} {total_trades:>6d}  ₹{total:>+10,.0f}")

    if not all_results:
        print("\n  No trades found.")
        return

    wins = [r for r in all_results if r["pnl"] > 0]
    losses = [r for r in all_results if r["pnl"] <= 0]
    total_pnl = sum(r["pnl"] for r in all_results)
    win_days = sum(1 for _, p, _ in day_pnls if p > 0)
    loss_days = sum(1 for _, p, n in day_pnls if p <= 0 and n > 0)
    no_trade_days = sum(1 for _, _, n in day_pnls if n == 0)

    print(f"\n  {'='*60}")
    print(f"  AGGREGATE — {len(dates)} days, {len(all_results)} trades")
    print(f"  {'='*60}")
    print(f"  Total P&L:        ₹{total_pnl:>+10,.0f}")
    print(f"  Avg P&L/Day:      ₹{total_pnl/len(dates):>+10,.0f}")
    print(f"  Wins / Losses:    {len(wins)} / {len(losses)}")
    print(f"  Win Rate:         {len(wins)/len(all_results)*100:.0f}%")
    print(f"  Avg Win:          ₹{sum(r['pnl'] for r in wins)/(len(wins) or 1):>+10,.0f}")
    print(f"  Avg Loss:         ₹{sum(r['pnl'] for r in losses)/(len(losses) or 1):>+10,.0f}")
    print(f"  Max Win:          ₹{max(r['pnl'] for r in all_results):>+10,.0f}")
    print(f"  Max Loss:         ₹{min(r['pnl'] for r in all_results):>+10,.0f}")
    print(f"  Win Days:         {win_days}")
    print(f"  Loss Days:        {loss_days}")
    print(f"  No-Trade Days:    {no_trade_days}")
    floor_exits = sum(1 for r in all_results if "FLOOR" in r["reason"])
    sl_exits = sum(1 for r in all_results if r["reason"] == "SL")
    eod_exits = sum(1 for r in all_results if r["reason"] == "eod")
    print(f"  Floor / SL / EOD: {floor_exits} / {sl_exits} / {eod_exits}")
    print(f"  {'='*60}\n")


if __name__ == "__main__":
    main()

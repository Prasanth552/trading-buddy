"""Compare multiple OEH exit strategies across multiple days.

Fetches candle data once per day, runs each trade through all strategies,
prints per-day and aggregate results.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/oeh_strategy_compare.py --from 2026-09-22 --to 2026-09-25
    PYTHONPATH=. .venv/bin/python3 scripts/oeh_strategy_compare.py --date 2026-09-25
"""
from __future__ import annotations
import argparse, re, time as _t
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from src.broker.upstox_data import UpstoxData, load_cached_token, _expiry_to_date

IST = ZoneInfo("Asia/Kolkata")
BLOCKLIST = {"GODREJCP", "GRASIM"}
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "NIFTY BANK", "NIFTY 50"}
OEH_TOLERANCE = 0.05
OEH_MIN_DROP_PCT = 0.3
SL_PCT = 0.30
MAX_SL_RS = 5000
SLIPPAGE_PCT = 0.005
MIN_PREMIUM = 0.50
CAPITAL = 150000


def _sim(ocandles, entry_idx, entry, lot, sl_price, strategy):
    """Run one trade through a strategy. Returns (exit_price, reason, time, peak_pnl)."""
    peak_pnl = 0.0

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

        if strategy == "floor_1500":
            if peak_pnl >= 1500 and pnl_low <= 1500:
                ep = entry + 1500 / lot
                ep = round(ep * (1 - SLIPPAGE_PCT), 2)
                return ep, "FLOOR", t_short, peak_pnl

        elif strategy == "floor_2000":
            if peak_pnl >= 2000 and pnl_low <= 2000:
                ep = entry + 2000 / lot
                ep = round(ep * (1 - SLIPPAGE_PCT), 2)
                return ep, "FLOOR", t_short, peak_pnl

        elif strategy == "floor_2500":
            if peak_pnl >= 2500 and pnl_low <= 2500:
                ep = entry + 2500 / lot
                ep = round(ep * (1 - SLIPPAGE_PCT), 2)
                return ep, "FLOOR", t_short, peak_pnl

        elif strategy == "floor_3000":
            if peak_pnl >= 3000 and pnl_low <= 3000:
                ep = entry + 3000 / lot
                ep = round(ep * (1 - SLIPPAGE_PCT), 2)
                return ep, "FLOOR", t_short, peak_pnl

        elif strategy == "floor_2000_trail40":
            # ₹2000 floor + 40% trail above ₹5000 (intra-low)
            if peak_pnl >= 5000:
                trail = peak_pnl * 0.60
                floor = max(2000, trail)
            elif peak_pnl >= 2000:
                floor = 2000
            else:
                continue
            if pnl_low <= floor:
                ep = entry + floor / lot
                ep = round(ep * (1 - SLIPPAGE_PCT), 2)
                return ep, "HYB", t_short, peak_pnl

        elif strategy == "floor_2500_trail40":
            # ₹2500 floor + 40% trail above ₹5000 (intra-low)
            if peak_pnl >= 5000:
                trail = peak_pnl * 0.60
                floor = max(2500, trail)
            elif peak_pnl >= 2500:
                floor = 2500
            else:
                continue
            if pnl_low <= floor:
                ep = entry + floor / lot
                ep = round(ep * (1 - SLIPPAGE_PCT), 2)
                return ep, "HYB", t_short, peak_pnl

        elif strategy == "no_floor":
            pass

    last = ocandles[-1]
    t = str(last.get("date", last.get("timestamp", "")))
    ep = round(last["close"] * (1 - SLIPPAGE_PCT), 2)
    return ep, "eod", t[11:16] if len(t) > 16 else t, peak_pnl


STRATEGIES = [
    "floor_1500",
    "floor_2000",
    "floor_2500",
    "floor_3000",
    "floor_2000_trail40",
    "floor_2500_trail40",
    "no_floor",
]

LABELS = {
    "floor_1500": "₹1.5K Flr",
    "floor_2000": "₹2K Flr",
    "floor_2500": "₹2.5K Flr",
    "floor_3000": "₹3K Flr",
    "floor_2000_trail40": "₹2K+Tr40",
    "floor_2500_trail40": "₹2.5K+Tr40",
    "no_floor": "No Floor",
}


def run_day(ud, ref_date, eq_keys, matched, opt_master, lot_sizes, lot_mult, verbose=True):
    """Run all strategies for one day. Returns {strategy: [pnl_list]}."""
    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt_scan = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=25)
    full_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    candidates = []
    scanned = 0
    for sym in matched:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if not inst_key:
            continue
        try:
            candles = ud.historical_data(inst_key, from_dt, to_dt_scan, "5minute")
            _t.sleep(0.12)
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
        candidates.append({"symbol": sym, "open": op, "close": ep, "drop_pct": dp})

    candidates.sort(key=lambda x: x["drop_pct"], reverse=True)

    if verbose:
        print(f"\n  --- {ref_date} | Scanned: {scanned} | Candidates: {len(candidates)} ---")

    if not candidates:
        return {s: [] for s in STRATEGIES}, 0

    trade_data = []
    for c in candidates:
        sym = c["symbol"]
        spot = c["close"]
        sym_pe = {k: v for k, v in opt_master.items() if k[0] == sym and k[3] == "PE"}
        if not sym_pe:
            continue
        expiries = sorted({k[1] for k in sym_pe if k[1] >= ref_date})
        if not expiries:
            continue
        strikes = sorted({k[2] for k in sym_pe if k[1] == expiries[0]})
        strike = min(strikes, key=lambda s: abs(s - spot))
        opt_key = opt_master.get((sym, expiries[0], strike, "PE"))
        if not opt_key:
            continue
        lot = lot_sizes.get(sym, 1) * lot_mult

        try:
            ocandles = ud.historical_data(opt_key, from_dt, full_to, "1minute")
            _t.sleep(0.12)
        except Exception:
            continue
        if not ocandles or len(ocandles) < 5:
            continue

        entry_candle = None
        entry_idx = 0
        for i, cn in enumerate(ocandles):
            t = str(cn.get("date", cn.get("timestamp", "")))
            if "09:2" in t:
                entry_candle = cn
                entry_idx = i
                break
        if not entry_candle:
            entry_candle = ocandles[0]
            entry_idx = 0

        raw_entry = entry_candle["close"]
        if raw_entry <= 0 or raw_entry < MIN_PREMIUM:
            continue

        entry = round(raw_entry * (1 + SLIPPAGE_PCT), 2)
        sl_pct_price = entry * (1 - SL_PCT)
        sl_cap_price = entry - (MAX_SL_RS / lot)
        sl_price = round(max(sl_pct_price, sl_cap_price), 2)

        trade_data.append({
            "sym": sym, "strike": strike, "lot": lot,
            "entry": entry, "sl_price": sl_price,
            "ocandles": ocandles, "entry_idx": entry_idx,
        })

    strat_results = {s: [] for s in STRATEGIES}

    if verbose and trade_data:
        col_w = 10
        header = f"  {'Symbol':<12s} {'Peak':>6s}"
        for s in STRATEGIES:
            header += f" {LABELS[s]:>{col_w}s}"
        print(header)
        print(f"  {'-' * (14 + 8 + len(STRATEGIES) * (col_w + 1))}")

    for td in trade_data:
        _, _, _, peak = _sim(td["ocandles"], td["entry_idx"], td["entry"], td["lot"], td["sl_price"], "no_floor")

        row = f"  {td['sym']:<12s} {peak:>+6,.0f}" if verbose else ""

        for s in STRATEGIES:
            ep, reason, etime, pk = _sim(td["ocandles"], td["entry_idx"], td["entry"], td["lot"], td["sl_price"], s)
            pnl = (ep - td["entry"]) * td["lot"]
            strat_results[s].append(pnl)
            if verbose:
                icon = "+" if pnl > 0 else ""
                row += f" {icon}{pnl:>{col_w-1},.0f}"

        if verbose:
            print(row)

    if verbose:
        # Day summary line
        day_line = f"  {'DAY TOTAL':<12s} {'':>6s}"
        for s in STRATEGIES:
            total = sum(strat_results[s])
            day_line += f" ₹{total:>+8,.0f}"
        print(day_line)

    return strat_results, len(trade_data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="Single date")
    parser.add_argument("--from", dest="from_date", default=None, help="Start date")
    parser.add_argument("--to", dest="to_date", default=None, help="End date")
    parser.add_argument("--lots", type=int, default=2)
    args = parser.parse_args()

    if args.date:
        dates = [date.fromisoformat(args.date)]
    elif args.from_date and args.to_date:
        d = date.fromisoformat(args.from_date)
        end = date.fromisoformat(args.to_date)
        dates = []
        while d <= end:
            if d.weekday() < 5:  # Mon-Fri
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

    print(f"\n{'='*100}")
    print(f"  OEH STRATEGY COMPARISON — {dates[0]} to {dates[-1]} ({len(dates)} days)")
    print(f"  Capital: ₹{CAPITAL:,.0f} | Slippage: {SLIPPAGE_PCT*100}% | Min premium: ₹{MIN_PREMIUM}")
    print(f"{'='*100}")

    # Aggregate across days
    all_results = {s: [] for s in STRATEGIES}
    day_totals = {s: [] for s in STRATEGIES}
    total_trades = 0

    for ref_date in dates:
        day_results, n_trades = run_day(
            ud, ref_date, eq_keys, matched, opt_master, lot_sizes, lot_mult,
            verbose=(len(dates) <= 3)
        )
        total_trades += n_trades
        for s in STRATEGIES:
            all_results[s].extend(day_results[s])
            day_totals[s].append(sum(day_results[s]))

    # If multi-day, print per-day summary table
    if len(dates) > 1:
        print(f"\n  {'='*100}")
        print(f"  PER-DAY P&L")
        print(f"  {'='*100}")
        col_w = 11
        header = f"  {'Date':<12s}"
        for s in STRATEGIES:
            header += f" {LABELS[s]:>{col_w}s}"
        print(header)
        print(f"  {'-' * (12 + len(STRATEGIES) * (col_w + 1))}")

        for i, d in enumerate(dates):
            row = f"  {str(d):<12s}"
            for s in STRATEGIES:
                v = day_totals[s][i]
                row += f" ₹{v:>+9,.0f}"
            print(row)

        # Totals row
        row = f"  {'TOTAL':<12s}"
        for s in STRATEGIES:
            v = sum(day_totals[s])
            row += f" ₹{v:>+9,.0f}"
        print(row)

    # Aggregate summary
    print(f"\n  {'='*100}")
    print(f"  AGGREGATE SUMMARY — {len(dates)} days, {total_trades} trades")
    print(f"  {'='*100}")

    col_w = 11
    summary_header = f"  {'Metric':<20s}"
    for s in STRATEGIES:
        summary_header += f" {LABELS[s]:>{col_w}s}"
    print(summary_header)
    print(f"  {'-' * (20 + len(STRATEGIES) * (col_w + 1))}")

    for label, fn in [
        ("Total P&L", lambda r: f"₹{sum(r):>+9,.0f}"),
        ("Avg P&L/Day", lambda r, dt=day_totals: f"₹{sum(r)/len(dates):>+9,.0f}"),
        ("Wins", lambda r: f"{sum(1 for x in r if x > 0):>10d}"),
        ("Losses", lambda r: f"{sum(1 for x in r if x <= 0):>10d}"),
        ("Win Rate", lambda r: f"{sum(1 for x in r if x > 0)/len(r)*100 if r else 0:>9.0f}%"),
        ("Avg Win", lambda r: f"₹{sum(x for x in r if x>0)/(sum(1 for x in r if x>0) or 1):>+9,.0f}"),
        ("Avg Loss", lambda r: f"₹{sum(x for x in r if x<=0)/(sum(1 for x in r if x<=0) or 1):>+9,.0f}"),
        ("Max Win", lambda r: f"₹{max(r):>+9,.0f}" if r else "—"),
        ("Max Loss", lambda r: f"₹{min(r):>+9,.0f}" if r else "—"),
        ("Win Days", lambda r, dt=day_totals: f"{sum(1 for d in dt[STRATEGIES[0]] if d > 0) if r is all_results[STRATEGIES[0]] else sum(1 for i,d in enumerate(dates) if day_totals[STRATEGIES[list(LABELS.keys())[list(LABELS.values()).index(label)]] if False else [s for s in STRATEGIES if all_results[s] is r][0]][i] > 0):>10d}" if len(dates) > 1 else "—"),
    ]:
        row = f"  {label:<20s}"
        for s in STRATEGIES:
            try:
                row += f" {fn(all_results[s]):>{col_w}s}"
            except Exception:
                row += f" {'—':>{col_w}s}"
        print(row)

    # Win days (simpler)
    if len(dates) > 1:
        row = f"  {'Win Days':<20s}"
        for s in STRATEGIES:
            wd = sum(1 for d in day_totals[s] if d > 0)
            row += f" {wd:>{col_w}d}"
        print(row)

        row = f"  {'Loss Days':<20s}"
        for s in STRATEGIES:
            ld = sum(1 for d in day_totals[s] if d <= 0)
            row += f" {ld:>{col_w}d}"
        print(row)

    print(f"  {'='*100}")

    # Ranking
    print(f"\n  RANKING (by Total P&L):")
    ranked = sorted(STRATEGIES, key=lambda s: sum(all_results[s]), reverse=True)
    for i, s in enumerate(ranked, 1):
        total = sum(all_results[s])
        avg = total / len(dates)
        print(f"    {i}. {LABELS[s]:<15s}  Total: ₹{total:>+9,.0f}  |  Avg/Day: ₹{avg:>+8,.0f}")
    print()


if __name__ == "__main__":
    main()

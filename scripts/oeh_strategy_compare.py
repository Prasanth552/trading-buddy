"""Compare multiple OEH exit strategies side by side on the same data.

Fetches candle data once, runs each trade through all strategies, prints
a comparison table. All strategies use slippage + min premium filter.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/oeh_strategy_compare.py --date 2026-09-25
"""
from __future__ import annotations
import argparse, re, time as _t
from datetime import datetime, date
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
        pnl_close = (cn["close"] - entry) * lot
        if pnl_high > peak_pnl:
            peak_pnl = pnl_high

        # SL always active
        if low <= sl_price:
            ep = round(sl_price * (1 - SLIPPAGE_PCT), 2)
            return ep, "SL", t_short, peak_pnl

        if strategy == "floor_1500":
            # ₹1500 fixed floor, intra-candle low
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

        elif strategy == "time_gate_1015_trail40":
            # 10:15 time gate + 40% trail activate ₹1500, candle close
            if t_short < "10:15":
                continue
            if peak_pnl >= 1500:
                trail = peak_pnl * 0.60
                if pnl_close <= trail:
                    ep = round(cn["close"] * (1 - SLIPPAGE_PCT), 2)
                    return ep, f"TR40", t_short, peak_pnl

        elif strategy == "stepped_peak":
            # Stepped trailing based on achieved peak (candle close)
            # No exit before 09:50. After 09:50:
            #   peak < 2000: no protection
            #   peak 2000-5000: lock ₹1000
            #   peak 5000-10000: lock 50%
            #   peak > 10000: lock 60%
            if t_short < "09:50":
                continue
            if peak_pnl >= 10000:
                floor = peak_pnl * 0.60
            elif peak_pnl >= 5000:
                floor = peak_pnl * 0.50
            elif peak_pnl >= 2000:
                floor = 1000
            else:
                continue
            if pnl_close <= floor:
                ep = round(cn["close"] * (1 - SLIPPAGE_PCT), 2)
                return ep, f"STEP", t_short, peak_pnl

        elif strategy == "time_sliced":
            # Current: 09:50 gate, 40/30/20 tightening
            if t_short < "09:50":
                continue
            if t_short < "10:30":
                tp, act = 0.40, 3000
            elif t_short < "13:00":
                tp, act = 0.30, 1500
            else:
                tp, act = 0.20, 1500
            if peak_pnl >= act:
                trail = peak_pnl * (1 - tp)
                if pnl_close <= trail:
                    ep = round(cn["close"] * (1 - SLIPPAGE_PCT), 2)
                    return ep, f"TS", t_short, peak_pnl

        elif strategy == "momentum_floor":
            # Hybrid: ₹1500 floor (intra-low) BUT skip first dip
            # First time peak crosses ₹1500, set a grace period of 15 candles
            # After grace, apply floor
            if not hasattr(_sim, '_grace'):
                _sim._grace = {}
            key = id(ocandles)
            if key not in _sim._grace:
                _sim._grace[key] = {"crossed": False, "grace_remaining": 0}
            g = _sim._grace[key]
            if peak_pnl >= 1500 and not g["crossed"]:
                g["crossed"] = True
                g["grace_remaining"] = 15
            if g["crossed"] and g["grace_remaining"] > 0:
                g["grace_remaining"] -= 1
                continue
            if g["crossed"] and pnl_low <= 1500:
                ep = entry + 1500 / lot
                ep = round(ep * (1 - SLIPPAGE_PCT), 2)
                if key in _sim._grace:
                    del _sim._grace[key]
                return ep, "MFLR", t_short, peak_pnl

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
    "time_gate_1015_trail40",
    "stepped_peak",
    "time_sliced",
    "momentum_floor",
    "no_floor",
]

LABELS = {
    "floor_1500": "₹1.5K Floor",
    "floor_2000": "₹2K Floor",
    "floor_2500": "₹2.5K Floor",
    "time_gate_1015_trail40": "10:15+40%Tr",
    "stepped_peak": "Stepped",
    "time_sliced": "TimeSliced",
    "momentum_floor": "MomentumFlr",
    "no_floor": "No Floor",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--lots", type=int, default=2)
    args = parser.parse_args()

    ref_date = date.fromisoformat(args.date)
    lot_mult = args.lots

    token = load_cached_token()
    ud = UpstoxData(access_token=token)
    master = ud._load_master()

    # Build universe
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

    # Phase 1: Find OEH candidates
    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt_scan = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=25)
    full_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    print(f"\n{'='*90}")
    print(f"  OEH STRATEGY COMPARISON — {ref_date}")
    print(f"  Capital: ₹{CAPITAL:,.0f} | Slippage: {SLIPPAGE_PCT*100}% | Min premium: ₹{MIN_PREMIUM}")
    print(f"{'='*90}")

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
    print(f"  Scanned: {scanned} | OEH Candidates: {len(candidates)}")

    # Phase 2: Fetch option candles for all candidates
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

    print(f"  Trades ready: {len(trade_data)}\n")

    # Phase 3: Run all strategies
    # Per-trade results
    strat_results = {s: [] for s in STRATEGIES}

    # Print per-trade comparison header
    col_w = 10
    header = f"  {'Symbol':<12s} {'Peak':>6s}"
    for s in STRATEGIES:
        header += f" {LABELS[s]:>{col_w}s}"
    print(header)
    print(f"  {'-' * (14 + 8 + len(STRATEGIES) * (col_w + 1))}")

    for td in trade_data:
        # Reset momentum_floor grace state
        if hasattr(_sim, '_grace'):
            _sim._grace = {}

        row = f"  {td['sym']:<12s}"

        # Get peak from no_floor strategy
        _, _, _, peak = _sim(td["ocandles"], td["entry_idx"], td["entry"], td["lot"], td["sl_price"], "no_floor")
        row += f" {peak:>+6,.0f}"

        for s in STRATEGIES:
            if hasattr(_sim, '_grace'):
                _sim._grace = {}
            ep, reason, etime, pk = _sim(td["ocandles"], td["entry_idx"], td["entry"], td["lot"], td["sl_price"], s)
            pnl = (ep - td["entry"]) * td["lot"]
            strat_results[s].append(pnl)
            icon = "+" if pnl > 0 else ""
            row += f" {icon}{pnl:>{col_w-1},.0f}"

        print(row)

    # Summary
    print(f"\n  {'='*90}")
    print(f"  STRATEGY COMPARISON SUMMARY")
    print(f"  {'='*90}")

    summary_header = f"  {'Metric':<20s}"
    for s in STRATEGIES:
        summary_header += f" {LABELS[s]:>{col_w}s}"
    print(summary_header)
    print(f"  {'-' * (20 + len(STRATEGIES) * (col_w + 1))}")

    for label, fn in [
        ("Total P&L", lambda r: f"₹{sum(r):>+8,.0f}"),
        ("Wins", lambda r: f"{sum(1 for x in r if x > 0):>9d}"),
        ("Losses", lambda r: f"{sum(1 for x in r if x <= 0):>9d}"),
        ("Win Rate", lambda r: f"{sum(1 for x in r if x > 0)/len(r)*100 if r else 0:>8.0f}%"),
        ("Avg Win", lambda r: f"₹{sum(x for x in r if x>0)/(sum(1 for x in r if x>0) or 1):>+8,.0f}"),
        ("Avg Loss", lambda r: f"₹{sum(x for x in r if x<=0)/(sum(1 for x in r if x<=0) or 1):>+8,.0f}"),
        ("Max Win", lambda r: f"₹{max(r):>+8,.0f}" if r else "—"),
        ("Max Loss", lambda r: f"₹{min(r):>+8,.0f}" if r else "—"),
    ]:
        row = f"  {label:<20s}"
        for s in STRATEGIES:
            row += f" {fn(strat_results[s]):>{col_w}s}"
        print(row)

    print(f"  {'='*90}")

    # Rank
    print(f"\n  RANKING (by Total P&L):")
    ranked = sorted(STRATEGIES, key=lambda s: sum(strat_results[s]), reverse=True)
    for i, s in enumerate(ranked, 1):
        total = sum(strat_results[s])
        print(f"    {i}. {LABELS[s]:<15s}  ₹{total:>+8,.0f}")
    print()


if __name__ == "__main__":
    main()

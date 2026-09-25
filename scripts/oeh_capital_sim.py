"""OEH capital simulation — run unlimited trades with a fixed capital pool.

Capital is allocated per trade (entry × lot), freed when trade exits (floor/SL/eod).
All OEH entries happen at ~09:20, so capital recycling only helps if early floor exits
free capital for a second pass over remaining candidates.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/oeh_capital_sim.py --date 2026-09-25 --capital 150000
"""
from __future__ import annotations

import argparse
import re
import time as _t
from datetime import datetime, date
from zoneinfo import ZoneInfo

from src.broker.upstox_data import UpstoxData, load_cached_token, _expiry_to_date

IST = ZoneInfo("Asia/Kolkata")
BLOCKLIST = {"GODREJCP", "GRASIM"}
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "NIFTY BANK", "NIFTY 50"}

OEH_TOLERANCE = 0.5
OEH_MIN_DROP_PCT = 0.3
SL_PCT = 0.30
MAX_SL_RS = 5000
FLOOR_LEVELS = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]


def build_fno_universe(master):
    syms = set()
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
                syms.add(name)
    return sorted(syms)


def _simulate_trade(ocandles, entry_idx, entry, lot, sl_price):
    active_floor = 0
    peak_pnl = 0.0

    for i, cn in enumerate(ocandles[entry_idx + 1:], start=entry_idx + 1):
        high = cn["high"]
        low = cn["low"]
        t = str(cn.get("date", cn.get("timestamp", "")))

        pnl_high = (high - entry) * lot
        pnl_low = (low - entry) * lot

        if pnl_high > peak_pnl:
            peak_pnl = pnl_high

        for fl in FLOOR_LEVELS:
            if pnl_high >= fl and fl > active_floor:
                active_floor = fl

        if low <= sl_price:
            return sl_price, "SL", t[11:16] if len(t) > 16 else t, i, peak_pnl

        if active_floor > 0 and pnl_low <= active_floor:
            exit_p = entry + active_floor / lot
            return exit_p, f"FLOOR ₹{active_floor}", t[11:16] if len(t) > 16 else t, i, peak_pnl

    last = ocandles[-1]
    t = str(last.get("date", last.get("timestamp", "")))
    return last["close"], "eod", t[11:16] if len(t) > 16 else t, len(ocandles) - 1, peak_pnl


def main():
    parser = argparse.ArgumentParser(description="OEH capital simulation")
    parser.add_argument("--date", default=None, help="Date (YYYY-MM-DD)")
    parser.add_argument("--capital", type=float, default=150000, help="Starting capital (default 150000)")
    parser.add_argument("--lots", type=int, default=2, help="Lot multiplier (default 2)")
    args = parser.parse_args()

    STARTING_CAPITAL = args.capital
    LOT_MULT = args.lots
    ref_date = date.fromisoformat(args.date) if args.date else datetime.now(IST).date()

    token = load_cached_token()
    ud = UpstoxData(access_token=token)
    master = ud._load_master()

    universe = build_fno_universe(master)
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

    matched = [s for s in universe if s in eq_keys]

    # ── Phase 1: Scan for OEH candidates ──
    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt_scan = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=25)
    full_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    print(f"\n{'='*65}")
    print(f"  OEH CAPITAL SIM — {ref_date} | Capital: ₹{STARTING_CAPITAL:,.0f} | {LOT_MULT} lots")
    print(f"{'='*65}")
    print(f"\n  Scanning {len(matched)} stocks for Open=High pattern...")

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
        if not candles or len(candles) < 1:
            continue

        open_price = candles[0]["open"]
        if open_price <= 0:
            continue
        max_high = candles[0]["high"]
        if max_high > open_price + OEH_TOLERANCE:
            continue

        entry_price = candles[0]["close"]
        drop_pct = (open_price - entry_price) / open_price * 100
        if drop_pct < OEH_MIN_DROP_PCT:
            continue

        candidates.append({
            "symbol": sym, "open": open_price,
            "close": entry_price, "high": max_high,
            "drop_pct": drop_pct,
        })

    candidates.sort(key=lambda x: x["drop_pct"], reverse=True)
    print(f"  Scanned: {scanned} | OEH Candidates: {len(candidates)}")

    if not candidates:
        print("  No OEH candidates found.")
        return

    # ── Phase 2: Simulate with capital management ──
    capital = STARTING_CAPITAL
    results = []
    wins = 0
    losses = 0
    total_pnl = 0.0
    skipped_no_capital = 0

    # Pre-fetch all option candles to enable capital recycling simulation
    trade_data = []
    for c in candidates:
        sym = c["symbol"]
        spot = c["close"]
        lot = lot_sizes.get(sym, 1) * LOT_MULT

        sym_pe_keys = {k: v for k, v in opt_master.items() if k[0] == sym and k[3] == "PE"}
        if not sym_pe_keys:
            continue
        avail_expiries = sorted({k[1] for k in sym_pe_keys if k[1] >= ref_date})
        if not avail_expiries:
            continue
        used_expiry = avail_expiries[0]
        expiry_strikes = sorted({k[2] for k in sym_pe_keys if k[1] == used_expiry})
        strike = min(expiry_strikes, key=lambda s: abs(s - spot))
        opt_key = opt_master.get((sym, used_expiry, strike, "PE"))
        if not opt_key:
            continue

        try:
            ocandles = ud.historical_data(opt_key, from_dt, full_to, "1minute")
            _t.sleep(0.12)
        except Exception:
            continue
        if not ocandles or len(ocandles) < 5:
            continue

        # Entry = first candle close after 09:20
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

        entry = entry_candle["close"]
        if entry <= 0:
            continue

        sl_pct_price = entry * (1 - SL_PCT)
        sl_cap_price = entry - (MAX_SL_RS / lot)
        sl_price = round(max(sl_pct_price, sl_cap_price), 2)

        trade_data.append({
            "candidate": c, "sym": sym, "strike": strike, "lot": lot,
            "entry": entry, "sl_price": sl_price,
            "ocandles": ocandles, "entry_idx": entry_idx,
        })

    # Simulate all trades — first pass uses starting capital
    # After first pass, do a second pass with freed capital (from floor/SL exits)
    active_trades = []  # list of dicts with exit_min_idx, capital_locked, pnl

    print(f"\n  {'Symbol':<14s} {'Str':>6s} {'Entry':>7s} {'Exit':>7s} {'P&L':>9s} {'Peak':>8s} {'Reason':<12s} {'Time':>5s} {'Cap':>8s}")
    print(f"  {'-'*85}")

    remaining = list(trade_data)
    pass_num = 0

    while remaining and pass_num < 3:
        pass_num += 1
        still_remaining = []

        for td in remaining:
            trade_capital = td["entry"] * td["lot"]

            if trade_capital > capital:
                skipped_no_capital += 1
                still_remaining.append(td)
                continue

            capital -= trade_capital
            exit_price, exit_reason, exit_time, exit_idx, peak_pnl = _simulate_trade(
                td["ocandles"], td["entry_idx"], td["entry"], td["lot"], td["sl_price"]
            )

            pnl_rs = (exit_price - td["entry"]) * td["lot"]
            won = pnl_rs > 0
            if won:
                wins += 1
            else:
                losses += 1
            total_pnl += pnl_rs

            # Free capital immediately (sim is retrospective)
            capital += trade_capital + pnl_rs

            c = td["candidate"]
            icon = "✅" if won else "❌"
            results.append({
                "icon": icon, "sym": td["sym"], "strike": td["strike"],
                "lot": td["lot"], "entry": td["entry"], "exit": exit_price,
                "pnl": pnl_rs, "peak_pnl": peak_pnl, "reason": exit_reason,
                "time": exit_time, "capital_used": trade_capital,
            })

            print(f"  {icon} {td['sym']:<12s} {td['strike']:>6.0f}PE {td['entry']:>7.1f} → {exit_price:>6.1f}  "
                  f"₹{pnl_rs:>+8,.0f}  ₹{peak_pnl:>+7,.0f} {exit_reason:<12s} {exit_time:>5s} ₹{capital:>7,.0f}")

        if len(still_remaining) == len(remaining):
            skipped_no_capital = len(still_remaining)
            break
        remaining = still_remaining
        skipped_no_capital = 0  # reset — will recount on next pass

    if remaining:
        skipped_no_capital = len(remaining)

    total = wins + losses
    wr = wins / total * 100 if total else 0
    floor_exits = sum(1 for r in results if "FLOOR" in r["reason"])
    sl_exits = sum(1 for r in results if r["reason"] == "SL")
    eod_exits = sum(1 for r in results if r["reason"] == "eod")

    print(f"\n  {'='*65}")
    print(f"  RESULTS — {ref_date}")
    print(f"  {'='*65}")
    print(f"  Starting Capital:   ₹{STARTING_CAPITAL:>10,.0f}")
    print(f"  Final Capital:      ₹{capital:>10,.0f}")
    print(f"  Total P&L:          ₹{total_pnl:>+10,.0f}")
    print(f"  Return:             {total_pnl / STARTING_CAPITAL * 100:>+9.1f}%")
    print(f"  ")
    print(f"  Trades Taken:       {total:>10d}")
    print(f"  Wins / Losses:      {wins:>4d} / {losses}")
    print(f"  Win Rate:           {wr:>9.0f}%")
    print(f"  Skipped (no cap):   {skipped_no_capital:>10d}")
    print(f"  Floor / SL / EOD:   {floor_exits:>4d} / {sl_exits} / {eod_exits}")
    print(f"  {'='*65}\n")


if __name__ == "__main__":
    main()

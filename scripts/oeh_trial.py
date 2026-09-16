"""OEH trial scanner — scan full F&O universe for Open=High pattern and check EOD performance.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/oeh_trial.py [--date 2026-09-16]
"""
from __future__ import annotations

import argparse
import time as _t
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from src.broker.upstox_data import UpstoxData, load_cached_token
from src.utils.logging import get_logger

log = get_logger("oeh_trial")
IST = ZoneInfo("Asia/Kolkata")

OEH_TOLERANCE = 0.5
OEH_MIN_DROP_PCT = 0.3
BLOCKLIST = {"GODREJCP", "GRASIM"}
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "NIFTY BANK", "NIFTY 50"}


def build_fno_universe(master):
    syms = set()
    for inst in master:
        if inst.get("segment") != "NSE_FO":
            continue
        if (inst.get("instrument_type") or "").upper() not in ("CE", "PE"):
            continue
        tsym = (inst.get("trading_symbol") or "").upper()
        # trading_symbol is like "RELIANCE24SEP26CE2600" — extract the base name
        # by stripping digits and option suffixes
        import re
        base = re.match(r'^([A-Z&]+)', tsym)
        if base:
            name = base.group(1)
            if name and name not in INDEX_NAMES and len(name) >= 2:
                syms.add(name)
    return sorted(syms)


def main():
    parser = argparse.ArgumentParser(description="OEH trial scanner")
    parser.add_argument("--date", default=None, help="Date to scan (YYYY-MM-DD), default today")
    args = parser.parse_args()

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

    # Debug: check how many F&O names match EQ symbols
    matched = [s for s in universe if s in eq_keys]
    unmatched = [s for s in universe if s not in eq_keys]
    if unmatched[:5]:
        print(f"  Sample unmatched F&O names: {unmatched[:5]}")
        # Try to find what EQ symbols look like for these
        for um in unmatched[:3]:
            close = [k for k in eq_keys if um in k or k in um]
            if close:
                print(f"    {um} → possible EQ matches: {close[:3]}")

    print(f"\n{'='*60}")
    print(f"  OEH TRIAL SCAN — {ref_date}")
    print(f"  F&O Universe: {len(universe)} stocks | EQ matched: {len(matched)}")
    print(f"{'='*60}\n")

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
            candles = ud.historical_data(inst_key, from_dt, to_dt, "5minute")
            _t.sleep(0.15)
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

    print(f"  Scanned: {scanned} stocks")
    print(f"  OEH Candidates: {len(candidates)}\n")

    if not candidates:
        print("  No OEH candidates found.")
        return

    print(f"  {'Symbol':<15s} {'Open':>8s} {'High':>8s} {'Close':>8s} {'Drop%':>6s}")
    print(f"  {'-'*50}")
    for c in candidates:
        print(f"  {c['symbol']:<15s} {c['open']:>8.1f} {c['high']:>8.1f} {c['close']:>8.1f} {c['drop_pct']:>5.2f}%")

    # PE option performance — rupee-based stepping floors
    SL_PCT = 0.30
    FLOOR_STEP = 1500  # ₹1500 steps: lock ₹1500, then ₹3000, ₹4500 ...

    print(f"\n{'='*60}")
    print(f"  PE OPTION PERFORMANCE (1-min candles)")
    print(f"  SL: {SL_PCT*100:.0f}% of entry | Floor steps: ₹{FLOOR_STEP}")
    print(f"{'='*60}\n")

    # Build option master + lot_size map from raw master
    from src.broker.upstox_data import _expiry_to_date
    import re as _re

    opt_master = {}   # (base_sym, expiry, strike, "PE"/"CE") -> instrument_key
    lot_sizes = {}    # base_sym -> lot_size
    for inst in master:
        if inst.get("segment") != "NSE_FO":
            continue
        itype = (inst.get("instrument_type") or "").upper()
        if itype not in ("CE", "PE"):
            continue
        tsym = (inst.get("trading_symbol") or "").upper()
        base = _re.match(r'^([A-Z&]+)', tsym)
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

    print(f"  Option master: {len(opt_master)} entries\n")

    wins = 0
    losses = 0
    total_pnl = 0.0
    results = []

    for c in candidates:
        sym = c["symbol"]
        spot = c["close"]
        lot = lot_sizes.get(sym, 1) * 2  # 2 lots

        # Find all PE options for this symbol
        sym_pe_keys = {k: v for k, v in opt_master.items() if k[0] == sym and k[3] == "PE"}
        if not sym_pe_keys:
            print(f"  ⚠️  {sym:<15s} — no PE options in master")
            continue

        # Find nearest expiry >= ref_date
        avail_expiries = sorted({k[1] for k in sym_pe_keys if k[1] >= ref_date})
        if not avail_expiries:
            print(f"  ⚠️  {sym:<15s} — no future expiry found")
            continue
        used_expiry = avail_expiries[0]

        # ATM strike closest to spot
        expiry_strikes = sorted({k[2] for k in sym_pe_keys if k[1] == used_expiry})
        strike = min(expiry_strikes, key=lambda s: abs(s - spot))
        opt_key = opt_master.get((sym, used_expiry, strike, "PE"))
        if not opt_key:
            continue

        # Fetch 1-min PE candles
        opt_from = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
        opt_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)
        try:
            candles = ud.historical_data(opt_key, opt_from, opt_to, "1minute")
            _t.sleep(0.15)
        except Exception as e:
            print(f"  ⚠️  {sym:<15s} — candle fetch failed: {e}")
            continue
        if not candles or len(candles) < 5:
            print(f"  ⚠️  {sym:<15s} — not enough candles ({len(candles) if candles else 0})")
            continue

        # Entry = first candle close after 09:20
        entry_candle = None
        for cn in candles:
            t = cn.get("date", cn.get("timestamp", ""))
            if isinstance(t, str) and "09:2" in t:
                entry_candle = cn
                break
        if not entry_candle:
            entry_candle = candles[0]

        entry = entry_candle["close"]
        if entry <= 0:
            continue

        sl_price = round(entry * (1 - SL_PCT), 2)

        # Simulate with stepping rupee floors
        peak = entry
        active_floor = 0        # current locked floor in ₹
        exit_price = entry
        exit_reason = "eod"
        exit_time = ""
        peak_pnl = 0.0

        entry_idx = candles.index(entry_candle) if entry_candle in candles else 0

        for cn in candles[entry_idx + 1:]:
            high = cn["high"]
            low = cn["low"]
            t = str(cn.get("date", cn.get("timestamp", "")))

            if high > peak:
                peak = high

            # Current P&L range in this candle
            pnl_high = (high - entry) * lot
            pnl_low = (low - entry) * lot

            if pnl_high > peak_pnl:
                peak_pnl = pnl_high

            # Check if we've crossed a new floor step
            new_floor = (int(pnl_high // FLOOR_STEP)) * FLOOR_STEP
            if new_floor > active_floor:
                active_floor = new_floor

            # Check SL (percentage-based on premium)
            if low <= sl_price:
                exit_price = sl_price
                exit_reason = "SL"
                exit_time = t[11:16] if len(t) > 16 else t
                break

            # Check floor breach — if P&L drops to active floor, exit
            if active_floor > 0 and pnl_low <= active_floor:
                exit_price = entry + active_floor / lot
                exit_reason = f"FLOOR ₹{active_floor}"
                exit_time = t[11:16] if len(t) > 16 else t
                break

        if exit_reason == "eod":
            exit_price = candles[-1]["close"]
            t = str(candles[-1].get("date", candles[-1].get("timestamp", "")))
            exit_time = t[11:16] if len(t) > 16 else t

        pnl_rs = (exit_price - entry) * lot
        won = pnl_rs > 0
        if won:
            wins += 1
        else:
            losses += 1
        total_pnl += pnl_rs

        icon = "✅" if won else "❌"
        results.append({
            "icon": icon, "sym": sym, "strike": strike, "lot": lot,
            "entry": entry, "exit": exit_price, "pnl": pnl_rs,
            "peak_pnl": peak_pnl, "reason": exit_reason, "time": exit_time,
        })

        print(f"  {icon} {sym:<12s} {strike:.0f}PE (lot={lot})  "
              f"entry=₹{entry:.1f} → exit=₹{exit_price:.1f}  "
              f"P&L=₹{pnl_rs:+,.0f}  peak=₹{peak_pnl:+,.0f} | {exit_reason} @ {exit_time}")

    total = wins + losses
    wr = wins / total * 100 if total else 0
    floor_exits = sum(1 for r in results if "FLOOR" in r["reason"])
    sl_exits = sum(1 for r in results if r["reason"] == "SL")
    eod_exits = sum(1 for r in results if r["reason"] == "eod")
    print(f"\n  {'='*55}")
    print(f"  Results: {wins}/{total} wins ({wr:.0f}% WR)")
    print(f"  Total P&L: ₹{total_pnl:+,.0f}")
    print(f"  Floor exits: {floor_exits} | SL exits: {sl_exits} | EOD exits: {eod_exits}")
    print(f"  {'='*55}\n")


if __name__ == "__main__":
    main()

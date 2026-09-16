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

    # EOD performance check
    print(f"\n{'='*60}")
    print(f"  EOD PERFORMANCE CHECK")
    print(f"{'='*60}\n")

    eod_from = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=15)
    eod_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    wins = 0
    total = 0
    for c in candidates:
        inst_key = eq_keys.get(c["symbol"])
        if not inst_key:
            continue
        try:
            eod = ud.historical_data(inst_key, eod_from, eod_to, "5minute")
            _t.sleep(0.15)
        except Exception:
            continue

        if not eod:
            continue

        total += 1
        eod_close = eod[-1]["close"]
        still_down = eod_close < c["open"]
        if still_down:
            wins += 1
        icon = "✅" if still_down else "❌"
        move_pct = (eod_close - c["open"]) / c["open"] * 100
        pe_pnl = c["open"] - eod_close
        print(f"  {icon} {c['symbol']:<15s} open={c['open']:.1f} → eod={eod_close:.1f} "
              f"({move_pct:+.2f}%) PE profit direction: {'YES' if still_down else 'NO'}")

    wr = wins / total * 100 if total else 0
    print(f"\n  {'='*50}")
    print(f"  Results: {wins}/{total} would have closed in profit ({wr:.0f}% WR)")
    print(f"  (PE buyers profit when stock closes below open)")
    print(f"  {'='*50}\n")


if __name__ == "__main__":
    main()

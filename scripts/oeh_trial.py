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

    # PE option performance check with 1-min candles
    SL_PCT = 0.30
    FLOOR_MULT = 1.5
    TGT_MULT = 2.0

    print(f"\n{'='*60}")
    print(f"  PE OPTION PERFORMANCE (1-min candles)")
    print(f"  SL: {SL_PCT*100:.0f}% | Floor TGT: {FLOOR_MULT}x | Full TGT: {TGT_MULT}x")
    print(f"{'='*60}\n")

    # Build option master from raw UpstoxData master
    from src.broker.upstox_data import _expiry_to_date
    import re as _re

    opt_master = {}  # (base_sym, expiry_date, strike, "PE"/"CE") -> instrument_key
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

    print(f"  Option master: {len(opt_master)} entries")

    # Debug: show what symbols we have in opt_master for first 3 candidates
    cand_syms = {c["symbol"] for c in candidates}
    for dbg_sym in list(cand_syms)[:3]:
        matching = {k for k in opt_master if k[0] == dbg_sym}
        if matching:
            expiries = sorted({k[1] for k in matching})
            strikes_sample = sorted({k[2] for k in matching if k[1] == expiries[0]})[:5]
            print(f"  DEBUG {dbg_sym}: {len(matching)} options, expiries={expiries[:3]}, "
                  f"sample strikes={strikes_sample}")
        else:
            # Check partial matches
            partials = {k[0] for k in opt_master if dbg_sym in k[0] or k[0] in dbg_sym}
            print(f"  DEBUG {dbg_sym}: 0 options! Partial matches in master: {list(partials)[:5]}")

    print()

    wins = 0
    losses = 0
    total_pnl = 0
    results = []

    for c in candidates:
        sym = c["symbol"]
        spot = c["close"]

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

        # Find ATM strike closest to spot for that expiry
        expiry_strikes = sorted({k[2] for k in sym_pe_keys if k[1] == used_expiry})
        if not expiry_strikes:
            print(f"  ⚠️  {sym:<15s} — no strikes for expiry {used_expiry}")
            continue

        strike = min(expiry_strikes, key=lambda s: abs(s - spot))
        opt_key = opt_master.get((sym, used_expiry, strike, "PE"))

        if not opt_key:
            print(f"  ⚠️  {sym:<15s} — key lookup failed for {strike:.0f}PE exp={used_expiry}")
            continue

        # Fetch 1-min PE candles from 09:20 to 15:30
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

        sl = round(entry * (1 - SL_PCT), 2)
        floor_tgt = round(entry * FLOOR_MULT, 2)
        full_tgt = round(entry * TGT_MULT, 2)

        # Track through 1-min candles
        peak = entry
        exit_price = entry
        exit_reason = "eod"
        exit_time = ""

        entry_idx = candles.index(entry_candle) if entry_candle in candles else 0

        for cn in candles[entry_idx + 1:]:
            high = cn["high"]
            low = cn["low"]
            close = cn["close"]
            t = str(cn.get("date", cn.get("timestamp", "")))

            if high > peak:
                peak = high

            if low <= sl:
                exit_price = sl
                exit_reason = "SL"
                exit_time = t[11:16] if len(t) > 16 else t
                break

            if high >= floor_tgt:
                exit_price = floor_tgt
                exit_reason = "FLOOR"
                exit_time = t[11:16] if len(t) > 16 else t
                break

        if exit_reason == "eod":
            exit_price = candles[-1]["close"]
            t = str(candles[-1].get("date", candles[-1].get("timestamp", "")))
            exit_time = t[11:16] if len(t) > 16 else t

        pnl_pct = (exit_price - entry) / entry * 100
        won = exit_price > entry
        if won:
            wins += 1
        else:
            losses += 1

        # Assume 1 lot for P&L
        lot_pnl = (exit_price - entry) * 1
        total_pnl += lot_pnl

        icon = "✅" if won else "❌"
        results.append((icon, sym, strike, entry, exit_price, pnl_pct, exit_reason, exit_time, peak))

        print(f"  {icon} {sym:<15s} {strike:.0f}PE  entry=₹{entry:.1f} → exit=₹{exit_price:.1f} "
              f"({pnl_pct:+.1f}%) peak=₹{peak:.1f} | {exit_reason} @ {exit_time}")

    total = wins + losses
    wr = wins / total * 100 if total else 0
    print(f"\n  {'='*55}")
    print(f"  Results: {wins}/{total} wins ({wr:.0f}% WR)")
    print(f"  Floor hits: {sum(1 for r in results if r[6] == 'FLOOR')}")
    print(f"  SL hits: {sum(1 for r in results if r[6] == 'SL')}")
    print(f"  EOD exits: {sum(1 for r in results if r[6] == 'eod')}")
    print(f"  {'='*55}\n")


if __name__ == "__main__":
    main()

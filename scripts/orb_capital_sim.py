"""ORB capital simulation — run unlimited trades with a fixed capital pool.

Capital is allocated per trade (entry × lot), freed when trade exits (floor/SL/eod).
Trades are processed chronologically by breakout time so capital recycling is realistic.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/orb_capital_sim.py --date 2026-09-24 --capital 150000
"""
from __future__ import annotations

import argparse
import re
import time as _t
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from src.broker.upstox_data import UpstoxData, load_cached_token, _expiry_to_date

IST = ZoneInfo("Asia/Kolkata")
BLOCKLIST = {"GODREJCP", "GRASIM"}
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "NIFTY BANK", "NIFTY 50"}

SL_PCT = 0.30
MAX_SL_RS = 5000
FLOOR_LEVELS = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
MIN_RANGE_PCT = 0.3
MAX_RANGE_PCT = 3.0


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
    """Simulate a single trade on 1-min candles. Returns (exit_price, exit_reason, exit_time, exit_min_idx, peak_pnl)."""
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
    parser = argparse.ArgumentParser(description="ORB capital simulation")
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

    # ── Phase 1: Scan for all breakouts ──
    full_from = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    full_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)
    all_day_candles = {}
    candidates = []
    scanned = 0

    print(f"\n{'='*65}")
    print(f"  ORB CAPITAL SIM — {ref_date} | Capital: ₹{STARTING_CAPITAL:,.0f} | {LOT_MULT} lots")
    print(f"{'='*65}")
    print(f"\n  Scanning {len(matched)} stocks...")

    for sym in matched:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if not inst_key:
            continue
        try:
            candles = ud.historical_data(inst_key, full_from, full_to, "5minute")
            _t.sleep(0.12)
        except Exception:
            continue
        scanned += 1
        if not candles or len(candles) < 6:
            continue
        all_day_candles[sym] = candles
        range_high = candles[0]["high"]
        range_low = candles[0]["low"]
        range_open = candles[0]["open"]
        if range_open <= 0:
            continue
        range_pct = (range_high - range_low) / range_open * 100
        if range_pct < MIN_RANGE_PCT or range_pct > MAX_RANGE_PCT:
            continue
        candidates.append({
            "symbol": sym, "open": range_open,
            "range_high": range_high, "range_low": range_low, "range_pct": range_pct,
        })

    # Find breakouts
    breakouts = []
    for c in candidates:
        sym = c["symbol"]
        day_candles = all_day_candles.get(sym)
        if not day_candles:
            continue
        for dc in day_candles[1:]:
            t = str(dc.get("date", dc.get("timestamp", "")))
            if dc["high"] > c["range_high"]:
                breakouts.append({**c, "direction": "bullish", "breakout_price": dc["close"], "breakout_time": t})
                break
            elif dc["low"] < c["range_low"]:
                breakouts.append({**c, "direction": "bearish", "breakout_price": dc["close"], "breakout_time": t})
                break

    # Sort by breakout time (chronological), then by range_pct (prefer stronger ranges)
    breakouts.sort(key=lambda x: (x["breakout_time"], -x["range_pct"]))

    print(f"  Scanned: {scanned} | Breakouts: {len(breakouts)}")

    # ── Phase 2: Simulate with capital management ──
    capital = STARTING_CAPITAL
    capital_in_use = 0.0
    peak_capital_used = 0.0
    active_trades = []  # (exit_min_from_915, capital_to_free, trade_info)
    results = []
    wins = 0
    losses = 0
    total_pnl = 0.0
    skipped_no_capital = 0

    print(f"\n  {'Symbol':<14s} {'Str':>6s} {'Dir':>1s} {'Entry':>7s} {'Exit':>7s} {'P&L':>9s} {'Peak':>8s} {'Reason':<12s} {'Time':>5s} {'Cap':>8s}")
    print(f"  {'-'*95}")

    for b in breakouts:
        sym = b["symbol"]
        opt_type = "CE" if b["direction"] == "bullish" else "PE"
        spot = b["breakout_price"]
        lot = lot_sizes.get(sym, 1) * LOT_MULT

        # Find option
        sym_opts = {k: v for k, v in opt_master.items() if k[0] == sym and k[3] == opt_type}
        if not sym_opts:
            continue
        avail_expiries = sorted({k[1] for k in sym_opts if k[1] >= ref_date})
        if not avail_expiries:
            continue
        used_expiry = avail_expiries[0]
        expiry_strikes = sorted({k[2] for k in sym_opts if k[1] == used_expiry})
        strike = min(expiry_strikes, key=lambda s: abs(s - spot))
        opt_key = opt_master.get((sym, used_expiry, strike, opt_type))
        if not opt_key:
            continue

        # Fetch 1-min option candles
        try:
            ocandles = ud.historical_data(opt_key, full_from, full_to, "1minute")
            _t.sleep(0.12)
        except Exception:
            continue
        if not ocandles or len(ocandles) < 5:
            continue

        # Find entry candle
        bt = b["breakout_time"]
        bt_short = bt[11:16] if len(bt) > 16 else bt[:5]
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

        entry = entry_candle["close"]
        if entry <= 0:
            continue

        # Capital required for this trade
        trade_capital = entry * lot

        # Free capital from trades that have exited by this breakout time
        # We use breakout_time to check if earlier trades have freed capital
        still_active = []
        for at in active_trades:
            at_exit_time = at["exit_time"]
            if at_exit_time <= bt_short:
                capital += at["capital_locked"]
                capital_in_use -= at["capital_locked"]
                capital += at["pnl"]  # add P&L back to capital
            else:
                still_active.append(at)
        active_trades = still_active

        # Check capital
        if trade_capital > capital:
            skipped_no_capital += 1
            continue

        # Allocate capital
        capital -= trade_capital
        capital_in_use += trade_capital
        if capital_in_use > peak_capital_used:
            peak_capital_used = capital_in_use

        # SL calculation
        sl_pct_price = entry * (1 - SL_PCT)
        sl_cap_price = entry - (MAX_SL_RS / lot)
        sl_price = round(max(sl_pct_price, sl_cap_price), 2)

        # Simulate
        exit_price, exit_reason, exit_time, exit_idx, peak_pnl = _simulate_trade(
            ocandles, entry_idx, entry, lot, sl_price
        )

        pnl_rs = (exit_price - entry) * lot
        won = pnl_rs > 0
        if won:
            wins += 1
        else:
            losses += 1
        total_pnl += pnl_rs

        # Track active trade for capital recycling
        active_trades.append({
            "exit_time": exit_time,
            "capital_locked": trade_capital,
            "pnl": pnl_rs,
        })

        icon = "✅" if won else "❌"
        dir_tag = "▲" if b["direction"] == "bullish" else "▼"
        avail = capital + sum(at["capital_locked"] + at["pnl"] for at in active_trades)

        results.append({
            "icon": icon, "sym": sym, "strike": strike, "opt": opt_type,
            "lot": lot, "entry": entry, "exit": exit_price, "pnl": pnl_rs,
            "peak_pnl": peak_pnl, "reason": exit_reason, "time": exit_time,
            "direction": dir_tag, "capital_used": trade_capital,
        })

        print(f"  {icon} {dir_tag} {sym:<12s} {strike:>6.0f}{opt_type} {entry:>7.1f} → {exit_price:>6.1f}  "
              f"₹{pnl_rs:>+8,.0f}  ₹{peak_pnl:>+7,.0f} {exit_reason:<12s} {exit_time:>5s} ₹{avail:>7,.0f}")

    # Free remaining active trades
    for at in active_trades:
        capital += at["capital_locked"] + at["pnl"]

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
    print(f"  Peak Capital Used:  ₹{peak_capital_used:>10,.0f}")
    print(f"  ")
    print(f"  Trades Taken:       {total:>10d}")
    print(f"  Wins / Losses:      {wins:>4d} / {losses}")
    print(f"  Win Rate:           {wr:>9.0f}%")
    print(f"  Skipped (no cap):   {skipped_no_capital:>10d}")
    print(f"  Floor / SL / EOD:   {floor_exits:>4d} / {sl_exits} / {eod_exits}")
    print(f"  {'='*65}\n")


if __name__ == "__main__":
    main()

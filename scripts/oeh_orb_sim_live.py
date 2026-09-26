"""OEH + ORB combined capital simulation with live-realistic settings.

Simulates both scanners with:
- ₹1.5L capital pool each (independent)
- No trade limit — capital-gated
- ₹500 stepped floor exits
- 0.5% entry/exit slippage (bid-ask simulation)
- ₹2 min option premium filter (liquidity)
- ₹25K profit cap per scanner (stop trading after hitting target)

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/oeh_orb_sim_live.py --date 2026-09-25
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

# Live-realistic settings
SLIPPAGE_PCT = 0.005      # 0.5%
MIN_PREMIUM = 0.5         # skip options below ₹0.50
SL_PCT = 0.30
MAX_SL_RS = 5000
FLOOR_LEVELS_500 = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
FLOOR_LEVELS_1500 = [1500, 3000, 4500, 6000, 7500, 9000, 10500, 12000]
OEH_TOLERANCE = 0.05
OEH_MIN_DROP_PCT = 0.3
ORB_MIN_RANGE_PCT = 0.3
ORB_MAX_RANGE_PCT = 3.0
PROFIT_CAP = 25000        # stop trading after ₹25K profit per scanner


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


def build_opt_master(master):
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
    return opt_master, lot_sizes


def _simulate_trade(ocandles, entry_idx, entry, lot, sl_price, floor_levels=None):
    if floor_levels is None:
        floor_levels = FLOOR_LEVELS_500
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

        for fl in floor_levels:
            if pnl_high >= fl and fl > active_floor:
                active_floor = fl

        if low <= sl_price:
            # Exit at SL with slippage
            exit_p = round(sl_price * (1 - SLIPPAGE_PCT), 2)
            return exit_p, "SL", t[11:16] if len(t) > 16 else t, i, peak_pnl

        if active_floor > 0 and pnl_low <= active_floor:
            exit_p = entry + active_floor / lot
            # Apply slippage on exit
            exit_p = round(exit_p * (1 - SLIPPAGE_PCT), 2)
            return exit_p, f"FLOOR ₹{active_floor}", t[11:16] if len(t) > 16 else t, i, peak_pnl

    last = ocandles[-1]
    t = str(last.get("date", last.get("timestamp", "")))
    exit_p = round(last["close"] * (1 - SLIPPAGE_PCT), 2)
    return exit_p, "eod", t[11:16] if len(t) > 16 else t, len(ocandles) - 1, peak_pnl


def resolve_option(sym, opt_type, spot, ref_date, opt_master, lot_sizes, lot_mult):
    sym_keys = {k: v for k, v in opt_master.items() if k[0] == sym and k[3] == opt_type}
    if not sym_keys:
        return None
    avail_expiries = sorted({k[1] for k in sym_keys if k[1] >= ref_date})
    if not avail_expiries:
        return None
    used_expiry = avail_expiries[0]
    expiry_strikes = sorted({k[2] for k in sym_keys if k[1] == used_expiry})
    strike = min(expiry_strikes, key=lambda s: abs(s - spot))
    opt_key = opt_master.get((sym, used_expiry, strike, opt_type))
    if not opt_key:
        return None
    lot = lot_sizes.get(sym, 1) * lot_mult
    return {"strike": strike, "opt_key": opt_key, "lot": lot}


def run_oeh(ud, ref_date, eq_keys, matched, opt_master, lot_sizes, capital, lot_mult):
    print(f"\n{'='*70}")
    print(f"  OEH CAPITAL SIM (LIVE ENV) — {ref_date}")
    print(f"  Capital: ₹{capital:,.0f} | Slippage: {SLIPPAGE_PCT*100}% | Min premium: ₹{MIN_PREMIUM}")
    print(f"  Profit cap: ₹{PROFIT_CAP:,.0f}")
    print(f"{'='*70}")

    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt_scan = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=25)
    full_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    # Phase 1: Find OEH candidates
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
    print(f"\n  Scanned: {scanned} | OEH Candidates: {len(candidates)}")

    if not candidates:
        print("  No OEH candidates found.")
        return 0, 0, 0, 0.0

    # Phase 2: Simulate with capital + profit cap
    avail = capital
    results = []
    wins = losses = 0
    total_pnl = 0.0
    skipped_cap = skipped_premium = skipped_profitcap = 0

    print(f"\n  {'Symbol':<14s} {'Str':>6s} {'Entry':>7s} {'Exit':>7s} {'P&L':>9s} {'Peak':>8s} {'Reason':<12s} {'Time':>5s} {'Cap':>8s}")
    print(f"  {'-'*90}")

    for c in candidates:
        if total_pnl >= PROFIT_CAP:
            skipped_profitcap += 1
            continue

        opt = resolve_option(c["symbol"], "PE", c["close"], ref_date, opt_master, lot_sizes, lot_mult)
        if not opt:
            continue

        try:
            ocandles = ud.historical_data(opt["opt_key"], from_dt, full_to, "1minute")
            _t.sleep(0.12)
        except Exception:
            continue
        if not ocandles or len(ocandles) < 5:
            continue

        # Find entry candle (~09:20)
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
        if raw_entry <= 0:
            continue

        # Min premium filter
        if raw_entry < MIN_PREMIUM:
            skipped_premium += 1
            continue

        # Apply entry slippage
        entry = round(raw_entry * (1 + SLIPPAGE_PCT), 2)
        lot = opt["lot"]
        trade_capital = entry * lot

        if trade_capital > avail:
            skipped_cap += 1
            continue

        avail -= trade_capital

        sl_pct_price = entry * (1 - SL_PCT)
        sl_cap_price = entry - (MAX_SL_RS / lot)
        sl_price = round(max(sl_pct_price, sl_cap_price), 2)

        exit_price, exit_reason, exit_time, _, peak_pnl = _simulate_trade(
            ocandles, entry_idx, entry, lot, sl_price, floor_levels=FLOOR_LEVELS_1500
        )

        pnl_rs = (exit_price - entry) * lot
        won = pnl_rs > 0
        if won:
            wins += 1
        else:
            losses += 1
        total_pnl += pnl_rs
        avail += trade_capital + pnl_rs

        icon = "✅" if won else "❌"
        results.append({"sym": c["symbol"], "pnl": pnl_rs, "reason": exit_reason})
        print(f"  {icon} {c['symbol']:<12s} {opt['strike']:>6.0f}PE {entry:>7.1f} → {exit_price:>6.1f}  "
              f"₹{pnl_rs:>+8,.0f}  ₹{peak_pnl:>+7,.0f} {exit_reason:<12s} {exit_time:>5s} ₹{avail:>7,.0f}")

        if total_pnl >= PROFIT_CAP:
            print(f"\n  🎯 PROFIT CAP HIT: ₹{total_pnl:+,.0f} >= ₹{PROFIT_CAP:,} — stopping OEH")

    total = wins + losses
    wr = wins / total * 100 if total else 0
    floor_exits = sum(1 for r in results if "FLOOR" in r["reason"])
    sl_exits = sum(1 for r in results if r["reason"] == "SL")
    eod_exits = sum(1 for r in results if r["reason"] == "eod")

    print(f"\n  {'='*65}")
    print(f"  OEH RESULTS — {ref_date} (with slippage + filters)")
    print(f"  {'='*65}")
    print(f"  Starting Capital:   ₹{capital:>10,.0f}")
    print(f"  Final Capital:      ₹{avail:>10,.0f}")
    print(f"  Total P&L:          ₹{total_pnl:>+10,.0f}")
    print(f"  Return:             {total_pnl / capital * 100:>+9.1f}%")
    print(f"  ")
    print(f"  Trades Taken:       {total:>10d}")
    print(f"  Wins / Losses:      {wins:>4d} / {losses}")
    print(f"  Win Rate:           {wr:>9.0f}%")
    print(f"  Skipped (capital):  {skipped_cap:>10d}")
    print(f"  Skipped (premium):  {skipped_premium:>10d}")
    print(f"  Skipped (cap hit):  {skipped_profitcap:>10d}")
    print(f"  Floor / SL / EOD:   {floor_exits:>4d} / {sl_exits} / {eod_exits}")
    print(f"  {'='*65}")

    return total, wins, losses, total_pnl


def run_orb(ud, ref_date, eq_keys, matched, opt_master, lot_sizes, capital, lot_mult):
    print(f"\n{'='*70}")
    print(f"  ORB CAPITAL SIM (LIVE ENV) — {ref_date}")
    print(f"  Capital: ₹{capital:,.0f} | Slippage: {SLIPPAGE_PCT*100}% | Min premium: ₹{MIN_PREMIUM}")
    print(f"  Profit cap: ₹{PROFIT_CAP:,.0f}")
    print(f"{'='*70}")

    full_from = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    full_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    # Phase 1: Scan for breakouts
    all_day_candles = {}
    candidates = []
    scanned = 0

    print(f"\n  Scanning {len(matched)} stocks for ORB breakouts...")

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
        if range_pct < ORB_MIN_RANGE_PCT or range_pct > ORB_MAX_RANGE_PCT:
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

    breakouts.sort(key=lambda x: (x["breakout_time"], -x["range_pct"]))
    print(f"  Scanned: {scanned} | Breakouts: {len(breakouts)}")

    if not breakouts:
        print("  No breakouts found.")
        return 0, 0, 0, 0.0

    # Phase 2: Simulate with capital + profit cap
    avail = capital
    active_trades = []
    results = []
    wins = losses = 0
    total_pnl = 0.0
    skipped_cap = skipped_premium = skipped_profitcap = 0

    print(f"\n  {'Symbol':<14s} {'Str':>6s} {'D':>1s} {'Entry':>7s} {'Exit':>7s} {'P&L':>9s} {'Peak':>8s} {'Reason':<12s} {'Time':>5s} {'Cap':>8s}")
    print(f"  {'-'*95}")

    for b in breakouts:
        if total_pnl >= PROFIT_CAP:
            skipped_profitcap += 1
            continue

        sym = b["symbol"]
        opt_type = "CE" if b["direction"] == "bullish" else "PE"
        spot = b["breakout_price"]

        opt = resolve_option(sym, opt_type, spot, ref_date, opt_master, lot_sizes, lot_mult)
        if not opt:
            continue

        try:
            ocandles = ud.historical_data(opt["opt_key"], full_from, full_to, "1minute")
            _t.sleep(0.12)
        except Exception:
            continue
        if not ocandles or len(ocandles) < 5:
            continue

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

        raw_entry = entry_candle["close"]
        if raw_entry <= 0:
            continue

        if raw_entry < MIN_PREMIUM:
            skipped_premium += 1
            continue

        entry = round(raw_entry * (1 + SLIPPAGE_PCT), 2)
        lot = opt["lot"]
        trade_capital = entry * lot

        # Free capital from earlier trades that exited before this breakout
        still_active = []
        for at in active_trades:
            if at["exit_time"] <= bt_short:
                avail += at["capital_locked"] + at["pnl"]
            else:
                still_active.append(at)
        active_trades = still_active

        if trade_capital > avail:
            skipped_cap += 1
            continue

        avail -= trade_capital

        sl_pct_price = entry * (1 - SL_PCT)
        sl_cap_price = entry - (MAX_SL_RS / lot)
        sl_price = round(max(sl_pct_price, sl_cap_price), 2)

        exit_price, exit_reason, exit_time, _, peak_pnl = _simulate_trade(
            ocandles, entry_idx, entry, lot, sl_price
        )

        pnl_rs = (exit_price - entry) * lot
        won = pnl_rs > 0
        if won:
            wins += 1
        else:
            losses += 1
        total_pnl += pnl_rs

        active_trades.append({
            "exit_time": exit_time,
            "capital_locked": trade_capital,
            "pnl": pnl_rs,
        })

        dir_tag = "▲" if b["direction"] == "bullish" else "▼"
        icon = "✅" if won else "❌"
        cap_display = avail + sum(at["capital_locked"] + at["pnl"] for at in active_trades)
        results.append({"sym": sym, "pnl": pnl_rs, "reason": exit_reason})

        print(f"  {icon} {dir_tag} {sym:<12s} {opt['strike']:>6.0f}{opt_type} {entry:>7.1f} → {exit_price:>6.1f}  "
              f"₹{pnl_rs:>+8,.0f}  ₹{peak_pnl:>+7,.0f} {exit_reason:<12s} {exit_time:>5s} ₹{cap_display:>7,.0f}")

        if total_pnl >= PROFIT_CAP:
            print(f"\n  🎯 PROFIT CAP HIT: ₹{total_pnl:+,.0f} >= ₹{PROFIT_CAP:,} — stopping ORB")

    # Free remaining
    for at in active_trades:
        avail += at["capital_locked"] + at["pnl"]

    total = wins + losses
    wr = wins / total * 100 if total else 0
    floor_exits = sum(1 for r in results if "FLOOR" in r["reason"])
    sl_exits = sum(1 for r in results if r["reason"] == "SL")
    eod_exits = sum(1 for r in results if r["reason"] == "eod")

    print(f"\n  {'='*65}")
    print(f"  ORB RESULTS — {ref_date} (with slippage + filters)")
    print(f"  {'='*65}")
    print(f"  Starting Capital:   ₹{capital:>10,.0f}")
    print(f"  Final Capital:      ₹{avail:>10,.0f}")
    print(f"  Total P&L:          ₹{total_pnl:>+10,.0f}")
    print(f"  Return:             {total_pnl / capital * 100:>+9.1f}%")
    print(f"  ")
    print(f"  Trades Taken:       {total:>10d}")
    print(f"  Wins / Losses:      {wins:>4d} / {losses}")
    print(f"  Win Rate:           {wr:>9.0f}%")
    print(f"  Skipped (capital):  {skipped_cap:>10d}")
    print(f"  Skipped (premium):  {skipped_premium:>10d}")
    print(f"  Skipped (cap hit):  {skipped_profitcap:>10d}")
    print(f"  Floor / SL / EOD:   {floor_exits:>4d} / {sl_exits} / {eod_exits}")
    print(f"  {'='*65}")

    return total, wins, losses, total_pnl


def main():
    parser = argparse.ArgumentParser(description="OEH + ORB combined sim (live env)")
    parser.add_argument("--date", default=None, help="Date (YYYY-MM-DD)")
    parser.add_argument("--capital", type=float, default=150000)
    parser.add_argument("--lots", type=int, default=2)
    parser.add_argument("--profit-cap", type=float, default=25000)
    args = parser.parse_args()

    global PROFIT_CAP
    PROFIT_CAP = args.profit_cap

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

    matched = [s for s in universe if s in eq_keys]
    opt_master, lot_sizes = build_opt_master(master)

    oeh_total, oeh_w, oeh_l, oeh_pnl = run_oeh(
        ud, ref_date, eq_keys, matched, opt_master, lot_sizes, args.capital, args.lots
    )

    orb_total, orb_w, orb_l, orb_pnl = run_orb(
        ud, ref_date, eq_keys, matched, opt_master, lot_sizes, args.capital, args.lots
    )

    combined_pnl = oeh_pnl + orb_pnl
    combined_trades = oeh_total + orb_total
    combined_wins = oeh_w + orb_w
    combined_losses = oeh_l + orb_l

    print(f"\n{'='*70}")
    print(f"  COMBINED SUMMARY — {ref_date} (LIVE ENV SIMULATION)")
    print(f"{'='*70}")
    print(f"  OEH:  {oeh_total:>3d} trades | {oeh_w}W/{oeh_l}L | ₹{oeh_pnl:>+10,.0f}")
    print(f"  ORB:  {orb_total:>3d} trades | {orb_w}W/{orb_l}L | ₹{orb_pnl:>+10,.0f}")
    print(f"  {'─'*50}")
    print(f"  TOTAL: {combined_trades:>2d} trades | {combined_wins}W/{combined_losses}L | ₹{combined_pnl:>+10,.0f}")
    print(f"  Capital deployed: ₹{args.capital:,.0f} × 2 = ₹{args.capital * 2:,.0f}")
    print(f"  Combined return:  {combined_pnl / (args.capital * 2) * 100:>+.1f}%")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()

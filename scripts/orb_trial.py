"""ORB (Opening Range Breakout) trial scanner.

Scans all F&O stocks for 15-min opening range breakout, buys ATM CE/PE options.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/orb_trial.py --date 2026-09-22
    PYTHONPATH=. .venv/bin/python3 scripts/orb_trial.py --from 2026-09-10 --to 2026-09-22
"""
from __future__ import annotations

import argparse
import re
import time as _t
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from src.broker.upstox_data import UpstoxData, load_cached_token, _expiry_to_date

log_prefix = "[ORB]"
IST = ZoneInfo("Asia/Kolkata")

BLOCKLIST = {"GODREJCP", "GRASIM"}
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "NIFTY BANK", "NIFTY 50"}

SL_PCT = 0.30
MAX_SL_RS = 5000
FLOOR_LEVELS = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
MAX_TRADES = 10
MIN_RANGE_PCT = 0.3   # opening range must be >= 0.3% of open (skip tiny ranges)
MAX_RANGE_PCT = 3.0   # skip if range is too wide (gap day chaos)


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


def run_orb_day(ref_date, ud, master, eq_keys, universe, opt_master, lot_sizes):
    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=30)

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
            _t.sleep(0.12)
        except Exception:
            continue

        scanned += 1
        if not candles or len(candles) < 1:
            continue

        # Opening range = high/low of first 15 min (up to 3 candles of 5-min)
        range_high = max(c["high"] for c in candles)
        range_low = min(c["low"] for c in candles)
        range_open = candles[0]["open"]

        if range_open <= 0:
            continue

        range_pct = (range_high - range_low) / range_open * 100
        if range_pct < MIN_RANGE_PCT or range_pct > MAX_RANGE_PCT:
            continue

        candidates.append({
            "symbol": sym,
            "open": range_open,
            "range_high": range_high,
            "range_low": range_low,
            "range_pct": range_pct,
        })

    # Now fetch 5-min candles for the full day to detect breakout
    breakouts = []
    for c in candidates:
        sym = c["symbol"]
        inst_key = eq_keys.get(sym)
        full_from = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=30)
        full_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)
        try:
            day_candles = ud.historical_data(inst_key, full_from, full_to, "5minute")
            _t.sleep(0.12)
        except Exception:
            continue
        if not day_candles:
            continue

        # Find first breakout candle (close above range_high or below range_low)
        for dc in day_candles:
            t = str(dc.get("date", dc.get("timestamp", "")))
            if dc["close"] > c["range_high"]:
                breakouts.append({
                    **c,
                    "direction": "bullish",
                    "breakout_price": dc["close"],
                    "breakout_time": t,
                })
                break
            elif dc["close"] < c["range_low"]:
                breakouts.append({
                    **c,
                    "direction": "bearish",
                    "breakout_price": dc["close"],
                    "breakout_time": t,
                })
                break

    # Sort by range_pct descending (strongest ranges first)
    breakouts.sort(key=lambda x: x["range_pct"], reverse=True)
    top = breakouts[:MAX_TRADES]

    print(f"\n  Scanned: {scanned} | Ranges valid: {len(candidates)} | Breakouts: {len(breakouts)} | Taking: {len(top)}")

    if not top:
        print("  No ORB breakouts found.")
        return [], 0, 0, 0

    # Simulate option trades
    wins = 0
    losses = 0
    total_pnl = 0.0
    results = []

    for b in top:
        sym = b["symbol"]
        opt_type = "CE" if b["direction"] == "bullish" else "PE"
        spot = b["breakout_price"]
        lot = lot_sizes.get(sym, 1) * 2

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
        # Entry after breakout time
        bt = b["breakout_time"]
        opt_from = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
        opt_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)
        try:
            ocandles = ud.historical_data(opt_key, opt_from, opt_to, "1minute")
            _t.sleep(0.12)
        except Exception:
            continue
        if not ocandles or len(ocandles) < 5:
            continue

        # Find entry candle — first candle at or after breakout time
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
            entry_candle = ocandles[len(ocandles) // 4]
            entry_idx = len(ocandles) // 4

        entry = entry_candle["close"]
        if entry <= 0:
            continue

        sl_pct_price = entry * (1 - SL_PCT)
        sl_cap_price = entry - (MAX_SL_RS / lot)
        sl_price = round(max(sl_pct_price, sl_cap_price), 2)

        # Simulate with stepping floors
        active_floor = 0
        exit_price = entry
        exit_reason = "eod"
        exit_time = ""
        peak_pnl = 0.0

        for cn in ocandles[entry_idx + 1:]:
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
                exit_price = sl_price
                exit_reason = "SL"
                exit_time = t[11:16] if len(t) > 16 else t
                break

            if active_floor > 0 and pnl_low <= active_floor:
                exit_price = entry + active_floor / lot
                exit_reason = f"FLOOR ₹{active_floor}"
                exit_time = t[11:16] if len(t) > 16 else t
                break

        if exit_reason == "eod":
            exit_price = ocandles[-1]["close"]
            t = str(ocandles[-1].get("date", ocandles[-1].get("timestamp", "")))
            exit_time = t[11:16] if len(t) > 16 else t

        pnl_rs = (exit_price - entry) * lot
        won = pnl_rs > 0
        if won:
            wins += 1
        else:
            losses += 1
        total_pnl += pnl_rs

        icon = "✅" if won else "❌"
        dir_tag = "▲" if b["direction"] == "bullish" else "▼"
        results.append({
            "icon": icon, "sym": sym, "strike": strike, "opt": opt_type,
            "lot": lot, "entry": entry, "exit": exit_price, "pnl": pnl_rs,
            "peak_pnl": peak_pnl, "reason": exit_reason, "time": exit_time,
            "direction": dir_tag, "range_pct": b["range_pct"],
            "bt_time": bt_short,
        })

        print(f"  {icon} {dir_tag} {sym:<12s} {strike:.0f}{opt_type} (lot={lot})  "
              f"entry=₹{entry:.1f} → exit=₹{exit_price:.1f}  "
              f"P&L=₹{pnl_rs:+,.0f}  peak=₹{peak_pnl:+,.0f} | {exit_reason} @ {exit_time} "
              f"[BO@{bt_short} rng={b['range_pct']:.1f}%]")

    return results, wins, losses, total_pnl


def main():
    parser = argparse.ArgumentParser(description="ORB trial scanner")
    parser.add_argument("--date", default=None, help="Single date (YYYY-MM-DD)")
    parser.add_argument("--from", dest="from_date", default=None, help="Start date")
    parser.add_argument("--to", dest="to_date", default=None, help="End date")
    parser.add_argument("--max-trades", type=int, default=MAX_TRADES, help="Max trades per day")
    args = parser.parse_args()

    global MAX_TRADES
    MAX_TRADES = args.max_trades

    if args.date:
        dates = [date.fromisoformat(args.date)]
    elif args.from_date and args.to_date:
        start = date.fromisoformat(args.from_date)
        end = date.fromisoformat(args.to_date)
        dates = []
        d = start
        while d <= end:
            if d.weekday() < 5:  # skip weekends
                dates.append(d)
            d += timedelta(days=1)
    else:
        dates = [datetime.now(IST).date()]

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

    # Build option master
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
    print(f"F&O Universe: {len(universe)} | EQ matched: {len(matched)} | Option master: {len(opt_master)}")

    grand_wins = 0
    grand_losses = 0
    grand_pnl = 0.0
    day_results = []

    for ref_date in dates:
        print(f"\n{'='*60}")
        print(f"  ORB SCAN — {ref_date} ({ref_date.strftime('%A')})")
        print(f"{'='*60}")

        results, wins, losses, total_pnl = run_orb_day(
            ref_date, ud, master, eq_keys, matched, opt_master, lot_sizes
        )
        grand_wins += wins
        grand_losses += losses
        grand_pnl += total_pnl
        total = wins + losses
        wr = wins / total * 100 if total else 0
        day_results.append({"date": ref_date, "wins": wins, "losses": losses, "pnl": total_pnl, "wr": wr})

        print(f"\n  Day: {wins}/{total} wins ({wr:.0f}% WR) | P&L: ₹{total_pnl:+,.0f}")

    if len(dates) > 1:
        print(f"\n{'='*60}")
        print(f"  ORB SUMMARY — {dates[0]} to {dates[-1]} ({len(dates)} days)")
        print(f"{'='*60}")
        grand_total = grand_wins + grand_losses
        grand_wr = grand_wins / grand_total * 100 if grand_total else 0
        print(f"  Total: {grand_wins}/{grand_total} wins ({grand_wr:.0f}% WR)")
        print(f"  Total P&L: ₹{grand_pnl:+,.0f}")
        print(f"  Avg P&L/day: ₹{grand_pnl / len(dates):+,.0f}")
        print(f"\n  Per-day breakdown:")
        for d in day_results:
            t = d["wins"] + d["losses"]
            print(f"    {d['date']} ({d['date'].strftime('%a')}): {d['wins']}/{t} ({d['wr']:.0f}%) ₹{d['pnl']:+,.0f}")


if __name__ == "__main__":
    main()

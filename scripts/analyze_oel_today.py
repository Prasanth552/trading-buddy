#!/usr/bin/env python3
"""Analyze OEL (Open=Low) candidates for a given day with custom SL/TGT parameters.

Usage: .venv/bin/python3 scripts/analyze_oel_today.py [--date 2026-08-28] [--sl 2000] [--tgt 2000] [--lots 2]
"""
import sys, os, re, time as _time, argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

parser = argparse.ArgumentParser()
parser.add_argument("--date", default=None)
parser.add_argument("--sl", type=float, default=2000, help="Hard SL in rupees")
parser.add_argument("--tgt", type=float, default=2000, help="Profit target in rupees")
parser.add_argument("--lots", type=int, default=2)
parser.add_argument("--mode", choices=["oel", "oeh", "both"], default="both")
args = parser.parse_args()

target_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")
year, month, day = [int(x) for x in target_date.split("-")]

from src.notify.channel_listener import OEH_UNIVERSE, OEL_UNIVERSE, OEH_BLOCKLIST
from src.broker.upstox_data import UpstoxData, load_cached_token
from src.notify import config

LOT_SIZES = config.LOT_SIZES
DEFAULT_LOT = 25
OEH_TOLERANCE = 0.05
OEL_TOLERANCE = 0.05
OEL_MIN_RISE_PCT = 0.3
OEH_MIN_DROP_PCT = 0.3

token = load_cached_token()
if not token:
    print("ERROR: No valid Upstox token")
    sys.exit(1)

ud = UpstoxData(access_token=token)
master = ud._load_master()

eq_keys = {}
for inst in master:
    if inst.get("segment") == "NSE_EQ":
        tsym = (inst.get("trading_symbol") or "").upper()
        if tsym:
            eq_keys[tsym] = inst.get("instrument_key")

opt_master = {}
for inst in master:
    seg = inst.get("segment", "")
    if seg in ("NSE_FO", "BSE_FO"):
        tsym = (inst.get("trading_symbol") or "").upper()
        opt_master[tsym] = inst


def resolve_atm_option(sym, opt_type, entry_price_equity):
    from src.broker.upstox_client import _expiry_to_date
    base = sym.upper().replace(" ", "")
    lot_size = LOT_SIZES.get(base, DEFAULT_LOT)

    strike_step = 10 if entry_price_equity < 500 else (50 if entry_price_equity < 2000 else 100)
    atm_strike = round(entry_price_equity / strike_step) * strike_step

    candidates = []
    for inst in master:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO",):
            continue
        tsym = (inst.get("trading_symbol") or "").upper()
        i_name = (inst.get("name") or "").upper().replace(" ", "")
        if i_name != base:
            continue
        i_type = (inst.get("instrument_type") or "").upper()
        if i_type not in ("CE", "PE"):
            continue
        if i_type != opt_type:
            continue
        i_strike = float(inst.get("strike_price", 0))
        if i_strike != atm_strike:
            continue
        exp_raw = inst.get("expiry")
        if not exp_raw:
            continue
        exp_date = _expiry_to_date(exp_raw)
        if not exp_date:
            continue
        today = datetime(year, month, day, tzinfo=IST).date()
        if exp_date < today:
            continue
        candidates.append((exp_date, inst.get("instrument_key"), i_strike, lot_size))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    exp_date, inst_key, strike, lot_size = candidates[0]
    return {"inst_key": inst_key, "strike": strike, "lot_size": lot_size, "exp_date": exp_date, "symbol": f"{sym} {int(strike)} {opt_type}"}


def walk_candles(candles, entry, sl_rupees, tgt_rupees, qty):
    for c in candles:
        low_pnl = (c["low"] - entry) * qty
        high_pnl = (c["high"] - entry) * qty
        if low_pnl <= -sl_rupees:
            exit_price = entry - (sl_rupees / qty)
            return exit_price, -sl_rupees, "SL"
        if high_pnl >= tgt_rupees:
            exit_price = entry + (tgt_rupees / qty)
            return exit_price, tgt_rupees, "TGT"
    eod_pnl = (candles[-1]["close"] - entry) * qty
    return candles[-1]["close"], eod_pnl, "EOD"


def scan_and_simulate(universe, scan_type, blocklist):
    from_dt = datetime(year, month, day, 9, 15, 0, tzinfo=IST)
    scan_to = datetime(year, month, day, 9, 25, 0, tzinfo=IST)
    market_to = datetime(year, month, day, 15, 30, 0, tzinfo=IST)

    candidates = []
    scanned = 0

    for sym in universe:
        if sym in blocklist:
            continue
        inst_key = eq_keys.get(sym)
        if not inst_key:
            continue

        try:
            candles = ud.historical_data(inst_key, from_dt, scan_to, "5minute")
            _time.sleep(0.25)
        except Exception:
            _time.sleep(0.5)
            continue

        scanned += 1
        if not candles or len(candles) < 1:
            continue

        open_price = candles[0]["open"]
        if open_price <= 0:
            continue

        if scan_type == "oel":
            min_low = candles[0]["low"]
            if min_low < open_price - OEL_TOLERANCE:
                continue
            entry_eq = candles[0]["close"]
            change_pct = (entry_eq - open_price) / open_price * 100
            if change_pct < OEL_MIN_RISE_PCT:
                continue
            opt_type = "CE"
        else:
            max_high = candles[0]["high"]
            if max_high > open_price + OEH_TOLERANCE:
                continue
            entry_eq = candles[0]["close"]
            change_pct = (open_price - entry_eq) / open_price * 100
            if change_pct < OEH_MIN_DROP_PCT:
                continue
            opt_type = "PE"

        candidates.append({
            "symbol": sym, "open": open_price, "entry_eq": entry_eq,
            "change_pct": change_pct, "opt_type": opt_type,
        })

    candidates.sort(key=lambda x: x["change_pct"], reverse=True)
    print(f"\n{'='*100}")
    print(f"  {scan_type.upper()} ANALYSIS — {target_date} — {args.lots} lots, ₹{args.sl:,.0f} SL, ₹{args.tgt:,.0f} TGT")
    print(f"{'='*100}")
    print(f"  Scanned {scanned} stocks, found {len(candidates)} {scan_type.upper()} candidates\n")

    if not candidates:
        print("  No candidates found.")
        return 0, 0, 0

    print(f"  {'#':<3} {'Symbol':<28} {'EqOpen':>7} {'EqEntry':>8} {'Chg%':>6} "
          f"{'OptEntry':>8} {'Lots':>4} {'Qty':>5} {'Result':<5} {'P&L':>10}")
    print(f"  {'─'*95}")

    total_pnl = 0
    wins = 0
    losses = 0

    for i, c in enumerate(candidates, 1):
        opt = resolve_atm_option(c["symbol"], c["opt_type"], c["entry_eq"])
        if not opt:
            print(f"  {i:<3} {c['symbol']:<28} {c['open']:>7.1f} {c['entry_eq']:>8.1f} "
                  f"{c['change_pct']:>+5.1f}% {'NO_INST':>8}")
            continue

        lot_size = opt["lot_size"]
        qty = lot_size * args.lots

        try:
            opt_candles = ud.historical_data(opt["inst_key"], from_dt, market_to, "5minute")
            _time.sleep(0.25)
        except Exception:
            _time.sleep(0.5)
            print(f"  {i:<3} {opt['symbol']:<28} {c['open']:>7.1f} {c['entry_eq']:>8.1f} "
                  f"{c['change_pct']:>+5.1f}% {'NO_DATA':>8}")
            continue

        if not opt_candles:
            print(f"  {i:<3} {opt['symbol']:<28} {c['open']:>7.1f} {c['entry_eq']:>8.1f} "
                  f"{c['change_pct']:>+5.1f}% {'NO_DATA':>8}")
            continue

        entry_candles = [cn for cn in opt_candles if cn["date"][11:16] >= "09:20"]
        if not entry_candles:
            entry_candles = opt_candles

        opt_entry = entry_candles[0]["open"]
        exit_price, pnl, result = walk_candles(entry_candles, opt_entry, args.sl, args.tgt, qty)

        if pnl >= 0:
            wins += 1
            icon = "W"
        else:
            losses += 1
            icon = "L"
        total_pnl += pnl

        print(f"  {i:<3} {opt['symbol']:<28} {c['open']:>7.1f} {c['entry_eq']:>8.1f} "
              f"{c['change_pct']:>+5.1f}% {opt_entry:>8.1f} {args.lots:>4} {qty:>5} "
              f"[{icon}] {result:<3} {pnl:>+10,.0f}")

    print(f"\n  {scan_type.upper()}: {wins}W/{losses}L | P&L: ₹{total_pnl:+,.0f}")
    return total_pnl, wins, losses


if args.mode in ("oel", "both"):
    oel_pnl, oel_w, oel_l = scan_and_simulate(OEL_UNIVERSE, "oel", set())

if args.mode in ("oeh", "both"):
    oeh_pnl, oeh_w, oeh_l = scan_and_simulate(OEH_UNIVERSE, "oeh", OEH_BLOCKLIST)

if args.mode == "both":
    print(f"\n{'='*100}")
    print(f"  COMBINED: OEL {oel_w}W/{oel_l}L (₹{oel_pnl:+,.0f}) + OEH {oeh_w}W/{oeh_l}L (₹{oeh_pnl:+,.0f})")
    print(f"  TOTAL: ₹{oel_pnl + oeh_pnl:+,.0f}")
    print(f"{'='*100}")

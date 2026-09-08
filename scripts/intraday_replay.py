"""Replay strategies in 15-min intervals to show intraday P&L evolution."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date, datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()

import config
from src.broker.upstox_data import UpstoxData
from src.strategy.live_runner import (
    INDEXES, STRATEGIES, est_prem, round_strike, calc_charges,
    fetch_candles, _days_to_expiry, _dte_fraction, _candle_hm,
    _first_candle_range,
)

IST = ZoneInfo("Asia/Kolkata")
ref_date = date(2026, 9, 8)

uclient = UpstoxData()

# Fetch 5-min candles for all indexes
candle_cache = {}
for idx_name in INDEXES:
    candle_cache[idx_name] = fetch_candles(uclient, idx_name, ref_date, "5minute")
    print(f"Fetched {len(candle_cache[idx_name])} candles for {idx_name}")

# Build 15-min time slots
time_slots = []
for h in range(9, 16):
    for m in (0, 15, 30, 45):
        if h == 9 and m < 15:
            continue
        if h == 15 and m > 30:
            continue
        time_slots.append((h, m))

print("\n" + "="*120)
print(f"{'':15s}", end="")
for idx_name in INDEXES:
    print(f"{'--- ' + idx_name + ' ---':>35s}", end="")
print(f"{'--- COMBINED ---':>20s}")

print(f"{'TIME':15s}", end="")
for idx_name in INDEXES:
    print(f"{'Spot':>10s}{'CE':>8s}{'PE':>8s}{'P&L':>9s}", end="")
print(f"{'Total P&L':>12s}{'Cum P&L':>10s}")
print("-"*120)

# For each strategy, replay
for sname, params in STRATEGIES.items():
    print(f"\n{'='*120}")
    print(f"STRATEGY: {sname}")
    print(f"  Entry: {params['entry_hour']}:{params['entry_min']:02d} | SL: {params['sl_pct']*100:.0f}% {'combined' if params.get('combined_sl') else 'per-leg'} | Trailing: {params.get('trailing',False)} | Vol filter: {params.get('vol_filter',False)}")
    print(f"{'='*120}")

    # Track positions per index
    positions = {}
    for idx_name, idx in INDEXES.items():
        iv = idx["iv_annual"]
        step = idx["strike_step"]
        lot_size = idx["lot_size"]
        dte = _days_to_expiry(ref_date, idx_name)
        candles = candle_cache[idx_name]

        # Vol filter
        if params.get("vol_filter"):
            fr = _first_candle_range(candles)
            if fr > idx["vol_skip_range"]:
                positions[idx_name] = {"skipped": True, "reason": f"vol_filter ({fr:.0f}>{idx['vol_skip_range']})"}
                continue

        # Find entry candle
        entry_candle = None
        for c in candles:
            h, m = _candle_hm(c)
            if h > params["entry_hour"] or (h == params["entry_hour"] and m >= params["entry_min"]):
                entry_candle = c
                break

        if not entry_candle:
            positions[idx_name] = {"skipped": True, "reason": "no_entry"}
            continue

        spot = entry_candle["close"]
        atm = round_strike(spot, step)
        eh, em = _candle_hm(entry_candle)
        mins_entry = (eh - 9) * 60 + (em - 15)
        T_entry = _dte_fraction(ref_date, idx_name, mins_entry)
        ce_entry = est_prem(spot, atm, "CE", T_entry, iv)
        pe_entry = est_prem(spot, atm, "PE", T_entry, iv)
        total_prem = ce_entry + pe_entry

        if params.get("combined_sl"):
            sl_level = total_prem * (1 + params["sl_pct"])
            ce_sl = pe_sl = 0
        else:
            sl_level = 0
            ce_sl = ce_entry * (1 + params["sl_pct"])
            pe_sl = pe_entry * (1 + params["sl_pct"])

        positions[idx_name] = {
            "skipped": False, "atm": atm, "lot_size": lot_size, "dte": dte, "iv": iv,
            "ce_entry": ce_entry, "pe_entry": pe_entry, "total_prem": total_prem,
            "sl_level": sl_level, "ce_sl": ce_sl, "pe_sl": pe_sl,
            "ce_alive": True, "pe_alive": True,
            "ce_exit_prem": None, "pe_exit_prem": None,
            "exit_reason": None, "exit_time": None,
            "trail_best": 0, "trail_active": False,
            "entry_time": f"{eh}:{em:02d}",
        }

    # Now replay time slots
    print(f"\n{'TIME':15s}", end="")
    for idx_name in INDEXES:
        print(f"{'Spot':>10s}{'CE':>8s}{'PE':>8s}{'P&L':>9s}", end="")
    print(f"{'Total':>12s}")
    print("-"*120)

    for slot_h, slot_m in time_slots:
        slot_str = f"{slot_h:02d}:{slot_m:02d}"
        slot_total = 0
        line = f"{slot_str:15s}"

        for idx_name, idx in INDEXES.items():
            pos = positions[idx_name]
            if pos["skipped"]:
                line += f"{'SKIPPED':>35s}"
                continue

            # Entry not happened yet
            eh, em = map(int, pos["entry_time"].split(":"))
            if slot_h < eh or (slot_h == eh and slot_m < em):
                line += f"{'WAITING':>35s}"
                continue

            # Already exited
            if pos["exit_reason"]:
                ce_exit = pos["ce_exit_prem"] or 0
                pe_exit = pos["pe_exit_prem"] or 0
                pnl = ((pos["ce_entry"] - ce_exit) + (pos["pe_entry"] - pe_exit)) * pos["lot_size"]
                charges = calc_charges(pos["ce_entry"], ce_exit, pos["lot_size"]) + \
                          calc_charges(pos["pe_entry"], pe_exit, pos["lot_size"])
                net = pnl - charges
                slot_total += net
                line += f"{'EXIT':>10s}{ce_exit:>8.1f}{pe_exit:>8.1f}{net:>+9,.0f}"
                continue

            # Find the candle closest to this time slot
            candles = candle_cache[idx_name]
            best_candle = None
            for c in candles:
                ch, cm = _candle_hm(c)
                if ch > slot_h or (ch == slot_h and cm > slot_m + 5):
                    break
                best_candle = c

            if not best_candle:
                line += f"{'N/A':>35s}"
                continue

            spot = best_candle["close"]
            atm = pos["atm"]
            mins = (slot_h - 9) * 60 + (slot_m - 15)
            T = _dte_fraction(ref_date, idx_name, mins)
            ce_now = est_prem(spot, atm, "CE", T, pos["iv"])
            pe_now = est_prem(spot, atm, "PE", T, pos["iv"])

            # Check SL/trailing/time
            ce_worst = est_prem(best_candle["high"], atm, "CE", T, pos["iv"])
            pe_worst = est_prem(best_candle["low"], atm, "PE", T, pos["iv"])

            exit_this = None
            if params.get("combined_sl") and pos["ce_alive"] and pos["pe_alive"]:
                if ce_worst + pe_worst >= pos["sl_level"]:
                    exit_this = "combined_sl"
            else:
                if pos["ce_alive"] and ce_worst >= pos["ce_sl"]:
                    pos["ce_exit_prem"] = pos["ce_sl"]
                    pos["ce_alive"] = False
                if pos["pe_alive"] and pe_worst >= pos["pe_sl"]:
                    pos["pe_exit_prem"] = pos["pe_sl"]
                    pos["pe_alive"] = False

            if params.get("trailing") and pos["ce_alive"] and pos["pe_alive"] and not exit_this:
                current_profit = pos["total_prem"] - (ce_now + pe_now)
                pos["trail_best"] = max(pos["trail_best"], current_profit)
                if current_profit / pos["total_prem"] >= 0.40:
                    pos["trail_active"] = True
                if pos["trail_active"] and pos["trail_best"] > 0:
                    give_back = pos["trail_best"] * 0.20
                    if current_profit < pos["trail_best"] - give_back:
                        exit_this = "trailing"

            if slot_h >= 15 and slot_m >= 10 and not exit_this:
                exit_this = "time_3:10"

            if exit_this:
                if pos["ce_exit_prem"] is None:
                    pos["ce_exit_prem"] = ce_now
                if pos["pe_exit_prem"] is None:
                    pos["pe_exit_prem"] = pe_now
                pos["exit_reason"] = exit_this
                pos["exit_time"] = slot_str

            # Calc P&L at this point
            ce_val = pos["ce_exit_prem"] if pos["ce_exit_prem"] is not None else ce_now
            pe_val = pos["pe_exit_prem"] if pos["pe_exit_prem"] is not None else pe_now
            gross = ((pos["ce_entry"] - ce_val) + (pos["pe_entry"] - pe_val)) * pos["lot_size"]
            charges = calc_charges(pos["ce_entry"], ce_val, pos["lot_size"]) + \
                      calc_charges(pos["pe_entry"], pe_val, pos["lot_size"])
            net = gross - charges
            slot_total += net

            line += f"{spot:>10,.0f}{ce_now:>8.1f}{pe_now:>8.1f}{net:>+9,.0f}"

        line += f"{slot_total:>+12,.0f}"
        print(line)

    # Final summary
    grand = 0
    print(f"\n  Final results:")
    for idx_name in INDEXES:
        pos = positions[idx_name]
        if pos["skipped"]:
            print(f"    {idx_name}: SKIPPED ({pos['reason']})")
            continue
        ce_exit = pos["ce_exit_prem"] or 0
        pe_exit = pos["pe_exit_prem"] or 0
        gross = ((pos["ce_entry"] - ce_exit) + (pos["pe_entry"] - pe_exit)) * pos["lot_size"]
        charges = calc_charges(pos["ce_entry"], ce_exit, pos["lot_size"]) + \
                  calc_charges(pos["pe_entry"], pe_exit, pos["lot_size"])
        net = gross - charges
        grand += net
        print(f"    {idx_name}: CE {pos['ce_entry']:.1f}->{ce_exit:.1f} | PE {pos['pe_entry']:.1f}->{pe_exit:.1f} | Net: {net:+,.0f} | Exit: {pos['exit_reason']} @ {pos['exit_time']} | DTE: {pos['dte']}")
    print(f"    TOTAL: {grand:+,.0f}")


# Stock credit spreads summary
print("\n\n" + "="*120)
print("STOCK CREDIT SPREADS — Sep 8")
print("="*120)

from src.strategy.stock_runner import (
    STOCKS, STRATEGIES as STOCK_STRATEGIES,
    fetch_daily_candles, _find_signal_for_date, _days_to_expiry as stock_dte,
    _monthly_expiry_for,
)
from datetime import timedelta

buffer_start = ref_date - timedelta(days=60)
for stock_name in STOCKS:
    daily = fetch_daily_candles(uclient, stock_name, buffer_start, ref_date + timedelta(days=35))
    if not daily or len(daily) < 30:
        continue
    for strat_name, params in STOCK_STRATEGIES.items():
        sig = _find_signal_for_date(daily, ref_date, stock_name, params)
        if sig:
            dte = stock_dte(ref_date)
            expiry = _monthly_expiry_for(ref_date)
            print(f"\n  {stock_name} / {strat_name}:")
            print(f"    Signal: {sig['direction']} | Spot: {sig['spot']:.1f} | EMA20: {sig['ema']:.1f} | RSI: {sig['rsi']:.1f}")
            print(f"    DTE: {dte} | Expiry: {expiry}")

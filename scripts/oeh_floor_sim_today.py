"""Simulate today's OEH trades with 2-lot + 1.5x floor using ACTUAL option candles.

Fetches 1-min option candles from Upstox and replays entry→exit with the new
strategy: 2 lots, exit all at 1.5x entry (floor target).

Compares:
  - Actual result (1 lot, 2x target)
  - Simple 2-lot (same exits, doubled)
  - Simulated 2-lot floor (1.5x target on real option candles)
"""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()

import config
from src.storage.db import get_conn
from src.broker.upstox_data import UpstoxData, load_cached_token, _expiry_to_date

IST = ZoneInfo("Asia/Kolkata")
TODAY = datetime.now(IST).date().isoformat()
FLOOR_MULT = 1.5
LOTS = 2


def find_option_instrument(master, stock_name, strike, opt_type):
    """Find the option instrument_key from Upstox master."""
    from datetime import date
    today = date.fromisoformat(TODAY)

    _ALIASES = {
        "KALYANJIL": "KALYANKJIL", "LIC": "LICI",
        "BAJAJAUTO": "BAJAJ-AUTO", "M&M": "M_M", "M&MFIN": "M_MFIN",
    }
    sym = _ALIASES.get(stock_name.upper(), stock_name.upper())
    ot = "PE" if opt_type.upper() == "PE" else "CE"

    candidates = []
    for inst in master:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO"):
            continue
        asym = (inst.get("asset_symbol") or inst.get("name") or "").upper()
        if asym != sym:
            continue
        itype = (inst.get("instrument_type") or "").upper()
        if itype != ot:
            continue
        inst_strike = float(inst.get("strike_price", -1))
        if abs(inst_strike - strike) > 0.01:
            continue
        exp = _expiry_to_date(inst.get("expiry"))
        if exp is None or exp < today:
            continue
        candidates.append((exp, inst))

    if not candidates:
        return None, None, None

    candidates.sort(key=lambda x: x[0])
    chosen = candidates[0][1]
    return (
        chosen.get("instrument_key"),
        int(chosen.get("lot_size", 0)),
        candidates[0][0],
    )


def calc_charges(entry_price, exit_price, qty):
    buy_turnover = entry_price * qty
    sell_turnover = exit_price * qty
    brokerage = min(buy_turnover * 0.0003, 20) + min(sell_turnover * 0.0003, 20)
    stt = sell_turnover * 0.000625
    txn = (buy_turnover + sell_turnover) * 0.0000345
    gst = brokerage * 0.18
    sebi = (buy_turnover + sell_turnover) * 0.000001
    stamp = buy_turnover * 0.00003
    return brokerage + stt + txn + gst + sebi + stamp


def simulate_trade(ud, master, trade):
    tid, ts, symbol, qty, entry, exit_price, actual_pnl, sl_price, target_price, status, charges = trade

    parts = symbol.strip().split()
    opt_type = parts[-1]
    strike = float(parts[-2])
    stock_name = " ".join(parts[:-2])

    opt_key, lot_size, expiry = find_option_instrument(master, stock_name, strike, opt_type)
    if not opt_key:
        return {"error": f"Option not found: {symbol}", "id": tid, "symbol": symbol}

    entry_dt = datetime.fromisoformat(str(ts))
    try:
        entry_ist = entry_dt.astimezone(IST)
    except Exception:
        entry_ist = entry_dt.replace(tzinfo=IST)
    trade_date = entry_ist.date()

    from_dt = datetime.combine(trade_date, datetime.min.time().replace(hour=9, minute=15))
    to_dt = datetime.combine(trade_date, datetime.min.time().replace(hour=15, minute=30))

    try:
        candles = ud.historical_data(opt_key, from_dt, to_dt, "1minute")
        time.sleep(0.4)
    except Exception as e:
        return {"error": f"Candle fetch failed: {e}", "id": tid, "symbol": symbol}

    if not candles:
        return {"error": "No candles returned", "id": tid, "symbol": symbol}

    floor_target = round(entry * FLOOR_MULT, 2)
    entry_mins = entry_ist.hour * 60 + entry_ist.minute

    # Replay candles from entry time
    sim_exit = None
    sim_reason = None
    sim_time = None
    peak_high = 0
    trough_low = 999999

    candle_log = []
    for c in candles:
        ct = c["date"]
        if isinstance(ct, str):
            try:
                cdt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(IST)
            except Exception:
                continue
        else:
            cdt = ct.astimezone(IST) if hasattr(ct, "astimezone") else ct

        cmins = cdt.hour * 60 + cdt.minute
        if cmins < entry_mins:
            continue

        high = c["high"]
        low = c["low"]
        close = c["close"]

        if high > peak_high:
            peak_high = high
        if low < trough_low:
            trough_low = low

        candle_log.append({
            "time": f"{cdt.hour}:{cdt.minute:02d}",
            "open": c["open"], "high": high, "low": low, "close": close,
        })

        # Floor target hit
        if high >= floor_target and sim_exit is None:
            sim_exit = floor_target
            sim_reason = "FLOOR_1.5x"
            sim_time = f"{cdt.hour}:{cdt.minute:02d}"

        # SL hit (only if floor hasn't been hit yet)
        if low <= sl_price and sim_exit is None:
            sim_exit = sl_price
            sim_reason = "SL"
            sim_time = f"{cdt.hour}:{cdt.minute:02d}"

        if sim_exit is not None:
            break

    # If neither hit, use last candle
    if sim_exit is None:
        last = candles[-1]
        sim_exit = last["close"]
        sim_reason = "EOD" if status == "OPEN" else "TIME"
        ct = last["date"]
        if isinstance(ct, str):
            try:
                cdt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(IST)
                sim_time = f"{cdt.hour}:{cdt.minute:02d}"
            except Exception:
                sim_time = "?"
        else:
            sim_time = "?"

    lot_size_actual = qty  # 1-lot qty = lot_size
    sim_pnl_2lot = (sim_exit - entry) * lot_size_actual * LOTS
    sim_charges = calc_charges(entry, sim_exit, lot_size_actual * LOTS)
    sim_pnl_net = sim_pnl_2lot - sim_charges

    actual_pnl_val = actual_pnl or 0

    return {
        "id": tid,
        "symbol": symbol,
        "stock": stock_name,
        "strike": strike,
        "opt_type": opt_type,
        "entry": entry,
        "sl": sl_price,
        "old_tgt": target_price,
        "floor_tgt": floor_target,
        "lot_size": lot_size_actual,
        "expiry": str(expiry),
        "status": status,
        "actual_exit": exit_price or 0,
        "actual_pnl": actual_pnl_val,
        "sim_exit": sim_exit,
        "sim_reason": sim_reason,
        "sim_time": sim_time,
        "sim_pnl_gross": sim_pnl_2lot,
        "sim_charges": sim_charges,
        "sim_pnl_net": sim_pnl_net,
        "peak_high": peak_high,
        "trough_low": trough_low,
        "candle_count": len(candle_log),
        "candles": candle_log[:5],  # first 5 for debug
    }


def main():
    token = load_cached_token()
    if not token:
        print("ERROR: No Upstox token for today. Run login first.")
        sys.exit(1)

    ud = UpstoxData(access_token=token)
    master = ud._load_master()

    with get_conn() as conn:
        trades = conn.execute(
            "SELECT id, ts, symbol, qty, price, exit_price, pnl, stop_price, "
            "target_price, status, charges "
            "FROM trades WHERE channel='oeh' AND date(ts)=? ORDER BY ts",
            (TODAY,)
        ).fetchall()

    if not trades:
        print(f"No OEH trades found for {TODAY}")
        sys.exit(0)

    print(f"\n{'=' * 110}")
    print(f"  OEH FLOOR SIMULATION — {TODAY}")
    print(f"  Strategy: {LOTS} lots, exit at {FLOOR_MULT}x entry (floor target)")
    print(f"  Using ACTUAL option candles (1-min), not B-S estimates")
    print(f"{'=' * 110}\n")

    total_actual = 0
    total_sim = 0
    results = []

    for trade in trades:
        r = simulate_trade(ud, master, trade)
        results.append(r)

        if "error" in r:
            print(f"  #{r['id']} {r['symbol']:28} ERROR: {r['error']}")
            continue

        total_actual += r["actual_pnl"]
        total_sim += r["sim_pnl_net"]
        delta = r["sim_pnl_net"] - r["actual_pnl"]

        print(f"  --- #{r['id']} {r['symbol']} ---")
        print(f"  Entry: {r['entry']:.1f} | SL: {r['sl']:.1f} | Old TGT (2x): {r['old_tgt']:.1f} | Floor (1.5x): {r['floor_tgt']:.1f}")
        print(f"  Lot size: {r['lot_size']} | Expiry: {r['expiry']} | Candles after entry: {r['candle_count']}")
        print(f"  Option peak: {r['peak_high']:.1f} | Option trough: {r['trough_low']:.1f}")
        print(f"")
        print(f"  {'Actual (1 lot):':<25} exit={r['actual_exit']:.1f}  pnl={r['actual_pnl']:>+8,.0f}  [{r['status']}]")
        print(f"  {'Sim Floor (2 lot):':<25} exit={r['sim_exit']:.1f}  pnl={r['sim_pnl_net']:>+8,.0f}  [{r['sim_reason']}] at {r['sim_time']}")
        print(f"  {'Delta vs actual:':<25} {delta:>+8,.0f}")

        if r["sim_reason"] == "FLOOR_1.5x" and r["status"] == "CLOSED_SL":
            print(f"  ** RESCUED — SL trade converted to winner with floor! **")
        elif r["sim_reason"] == "SL" and r["status"] == "CLOSED_SL":
            print(f"  ** DOUBLE SL — floor not hit, 2x loss **")
        print()

    print(f"{'=' * 110}")
    print(f"  SUMMARY")
    print(f"{'=' * 110}")
    print(f"  {'Actual total (1 lot, 2x tgt):':<35} {total_actual:>+10,.0f}")
    print(f"  {'Sim total (2 lot, 1.5x floor):':<35} {total_sim:>+10,.0f}")
    print(f"  {'Improvement:':<35} {total_sim - total_actual:>+10,.0f}")
    if total_actual != 0:
        print(f"  {'% change:':<35} {((total_sim - total_actual) / abs(total_actual) * 100):>9.0f}%")
    print(f"{'=' * 110}\n")


if __name__ == "__main__":
    main()

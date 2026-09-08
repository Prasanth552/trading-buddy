"""Backtest OEH trades: compare 1-lot (old) vs 2-lot floor target (new).

For CLOSED (target hit): they reached 2x, so they definitely hit 1.5x first.
  New P&L = 2 lots × (floor_price - entry) × qty - charges

For CLOSED_SL: use stock candles + B-S to check if floor was hit before SL.
  If yes: exit at floor. If no: 2 × SL loss.

For OPEN: show current state only.
"""
import os, sys, time, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()

import config
from src.storage.db import get_conn
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo("Asia/Kolkata")
FLOOR_MULT = 1.5  # exit at 1.5x entry with 2 lots

token = load_cached_token()
ud = UpstoxData(access_token=token)
master = ud._load_master()

def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs_put(S, K, T, sigma):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * _norm_cdf(-d2) - S * _norm_cdf(-d1)

STOCK_IV = {
    "SHREECEM": 0.30, "ITC": 0.22, "AMBUJACEM": 0.32, "VEDL": 0.40,
    "INFY": 0.28, "TCS": 0.25, "ONGC": 0.35, "HDFCBANK": 0.25,
    "RELIANCE": 0.28, "SBIN": 0.32, "TATAMOTORS": 0.38, "BAJFINANCE": 0.30,
    "LT": 0.28, "TATASTEEL": 0.38, "NESTLEIND": 0.25, "ASIANPAINT": 0.28,
    "SUNPHARMA": 0.28, "BHEL": 0.45, "DABUR": 0.25, "COALINDIA": 0.32,
    "NTPC": 0.28, "MARUTI": 0.28, "BRITANNIA": 0.25, "ULTRACEMCO": 0.28,
    "HCLTECH": 0.28,
}

def find_eq_key(name):
    for inst in master:
        if inst.get("segment") == "NSE_EQ":
            tsym = (inst.get("trading_symbol") or "").upper()
            if tsym == name.upper():
                return inst.get("instrument_key")
    return None

def _monthly_expiry(dt):
    import calendar
    y, m = dt.year, dt.month
    if dt.day > 25:
        m += 1
        if m > 12:
            m = 1; y += 1
    last_day = calendar.monthrange(y, m)[1]
    d = datetime(y, m, last_day).date()
    while d.weekday() != 3:
        d -= timedelta(days=1)
    return d

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


def check_floor_hit_bs(stock_name, strike, entry, floor_price, sl_price, trade_date, entry_dt):
    """Use B-S on stock candles to check if floor was hit before SL."""
    eq_key = find_eq_key(stock_name)
    if not eq_key:
        return None

    from_dt = datetime.combine(trade_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(trade_date, datetime.min.time()).replace(hour=15, minute=30)

    try:
        candles = ud.historical_data(eq_key, from_dt, to_dt, "5minute")
        time.sleep(0.3)
    except Exception:
        return None

    if not candles:
        return None

    iv = STOCK_IV.get(stock_name, 0.30)
    expiry = _monthly_expiry(trade_date)
    dte = (expiry - trade_date).days
    entry_mins = entry_dt.hour * 60 + entry_dt.minute

    peak_prem = 0
    for c in candles:
        ct = c.get("date") or c.get("timestamp") or c.get("ts") or ""
        if isinstance(ct, str):
            try:
                cdt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            except:
                continue
        else:
            cdt = ct
        try:
            cdt_ist = cdt.astimezone(IST)
        except:
            cdt_ist = cdt
        cmins = cdt_ist.hour * 60 + cdt_ist.minute
        if cmins < entry_mins:
            continue

        spot_low = c["low"]
        mins_in_day = cmins - (9*60+15)
        T = max((dte - mins_in_day / (6.25*60)) / 365, 0.0001)
        pe_best = bs_put(spot_low, strike, T, iv)

        if pe_best > peak_prem:
            peak_prem = pe_best

        if pe_best >= floor_price:
            return {"hit": True, "peak": peak_prem, "time": f"{cdt_ist.hour}:{cdt_ist.minute:02d}"}

    return {"hit": False, "peak": peak_prem}


with get_conn() as conn:
    rows = conn.execute(
        "SELECT id, ts, symbol, qty, price, exit_price, pnl, stop_price, target_price, status, charges "
        "FROM trades WHERE channel='oeh' AND date(ts) >= '2026-09-01' ORDER BY ts"
    ).fetchall()

print(f"{'='*120}")
print(f"OEH BACKTEST: Old (1 lot, 2x target) vs New (2 lots, 1.5x floor target)")
print(f"{'='*120}")
print(f"\n{'ID':>5} {'Date':>6} {'Symbol':28} {'Status':10} {'Old P&L':>10} {'New P&L':>10} {'Delta':>10} {'Note':30}")
print(f"{'─'*120}")

old_total = 0
new_total = 0
trade_count = 0
open_count = 0

for r in rows:
    tid = r[0]
    ts = r[1]
    sym = r[2]
    qty = r[3]
    entry = r[4]
    exit_price = r[5]
    old_pnl = r[6] or 0
    sl_price = r[7]
    target_price = r[8]
    status = r[9]
    old_charges = r[10] or 0

    parts = sym.strip().split()
    stock_name = parts[0]
    strike = float(parts[1])

    entry_dt = datetime.fromisoformat(ts)
    trade_date = entry_dt.date()
    date_str = trade_date.strftime("%m/%d")

    floor_price = round(entry * FLOOR_MULT, 2)

    if status == "OPEN":
        open_count += 1
        print(f"{tid:>5} {date_str:>6} {sym:28} {'OPEN':10} {'—':>10} {'—':>10} {'—':>10} {'still open'}")
        continue

    trade_count += 1
    old_total += old_pnl

    if status == "CLOSED":
        # Winner — it reached 2x target, so definitely passed 1.5x floor
        # New: exit at floor with 2 lots
        new_exit = floor_price
        new_qty = qty * 2
        new_gross = (new_exit - entry) * new_qty
        new_charges = calc_charges(entry, new_exit, new_qty)
        new_pnl = new_gross - new_charges
        delta = new_pnl - old_pnl
        note = f"floor@{floor_price:.1f} (was tgt@{target_price:.1f})"
        new_total += new_pnl
        print(f"{tid:>5} {date_str:>6} {sym:28} {'CLOSED':10} {old_pnl:>+10,.0f} {new_pnl:>+10,.0f} {delta:>+10,.0f} {note}")

    elif status == "CLOSED_SL":
        # Check if floor was hit before SL using candle data
        result = check_floor_hit_bs(stock_name, strike, entry, floor_price, sl_price, trade_date, entry_dt)

        if result and result["hit"]:
            # Floor was hit! Exit at floor with 2 lots
            new_exit = floor_price
            new_qty = qty * 2
            new_gross = (new_exit - entry) * new_qty
            new_charges = calc_charges(entry, new_exit, new_qty)
            new_pnl = new_gross - new_charges
            note = f"RESCUED floor@{floor_price:.1f} peak={result['peak']:.1f}"
        else:
            # Floor not hit — SL with 2 lots (double loss)
            new_pnl = old_pnl * 2
            peak = result["peak"] if result else 0
            note = f"2x SL loss (peak={peak:.1f} < floor={floor_price:.1f})"

        delta = new_pnl - old_pnl
        new_total += new_pnl
        print(f"{tid:>5} {date_str:>6} {sym:28} {'SL→' + ('FLOOR' if result and result['hit'] else 'SL'):10} {old_pnl:>+10,.0f} {new_pnl:>+10,.0f} {delta:>+10,.0f} {note}")

print(f"{'─'*120}")
print(f"\n{'SUMMARY':>40}")
print(f"{'─'*60}")
print(f"  {'Closed trades:':30} {trade_count}")
print(f"  {'Open trades:':30} {open_count}")
print(f"  {'Old strategy (1 lot, 2x tgt):':30} {old_total:>+10,.0f}")
print(f"  {'New strategy (2 lot, 1.5x floor):':30} {new_total:>+10,.0f}")
print(f"  {'Improvement:':30} {new_total - old_total:>+10,.0f}")
print(f"  {'% improvement:':30} {((new_total - old_total) / abs(old_total) * 100) if old_total else 0:>9.0f}%")
print(f"{'='*120}")

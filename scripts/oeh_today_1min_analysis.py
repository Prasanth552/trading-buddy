"""Analyze today's OEH trades using 1-minute option candles.

Since today's options are current-month (not expired), we can fetch
actual option instrument candles — no B-S estimation needed.
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
FLOOR_MULT = 1.5

token = load_cached_token()
ud = UpstoxData(access_token=token)
master = ud._load_master()


def find_option_key(stock, strike, opt_type="PE"):
    strike_int = int(strike)
    for inst in master:
        if inst.get("segment") != "NSE_FO":
            continue
        tsym = (inst.get("trading_symbol") or "").upper()
        if stock.upper() in tsym and opt_type in tsym and str(strike_int) in tsym:
            itype = (inst.get("instrument_type") or "").upper()
            if itype in ("PE", "PUT"):
                exp = inst.get("expiry") or ""
                if "2026-09" in str(exp) or "250925" in tsym or "25SEP" in tsym:
                    return inst.get("instrument_key"), tsym
    return None, None


def find_eq_key(name):
    for inst in master:
        if inst.get("segment") == "NSE_EQ":
            tsym = (inst.get("trading_symbol") or "").upper()
            if tsym == name.upper():
                return inst.get("instrument_key")
    return None


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
    "HCLTECH": 0.28, "JSWSTEEL": 0.35,
}


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


def analyze_trade(tid, ts, sym, qty, entry, exit_price, pnl, sl_price, target_price, status, peak_price):
    parts = sym.strip().split()
    stock_name = parts[0]
    strike = float(parts[1])

    entry_dt = datetime.fromisoformat(ts)
    trade_date = entry_dt.date()
    floor_price = round(entry * FLOOR_MULT, 2)

    print(f"\n{'='*100}")
    print(f"Trade #{tid}: {sym} | Qty: {qty} ({'2 lots' if qty > 50 else '1 lot?'})")
    print(f"  Entry: {entry:.2f} | Floor Target (1.5x): {floor_price:.2f} | SL: {sl_price:.2f} | Old Target (2x): {target_price:.2f}")
    pnl_str = f"{pnl:+,.0f}" if pnl else "—"
    print(f"  Status: {status} | Exit: {exit_price} | P&L: {pnl_str} | Peak: {peak_price}")
    print(f"{'='*100}")

    from_dt = datetime.combine(trade_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(trade_date, datetime.min.time()).replace(hour=15, minute=30)

    # Try actual option candles first
    opt_key, opt_tsym = find_option_key(stock_name, strike)
    use_bs = False

    if opt_key:
        print(f"  Using ACTUAL option candles: {opt_tsym} ({opt_key})")
        try:
            candles = ud.historical_data(opt_key, from_dt, to_dt, "1minute")
            time.sleep(0.5)
        except Exception as e:
            print(f"  Option candle fetch failed ({e}), falling back to B-S")
            candles = None

        if not candles:
            use_bs = True
    else:
        print(f"  Option instrument not found for {sym}, using B-S estimation")
        use_bs = True

    if use_bs:
        eq_key = find_eq_key(stock_name)
        if not eq_key:
            print(f"  ERROR: No equity key for {stock_name}")
            return
        try:
            candles = ud.historical_data(eq_key, from_dt, to_dt, "1minute")
            time.sleep(0.5)
        except Exception as e:
            print(f"  Candle fetch failed: {e}")
            return

    if not candles:
        print(f"  No candles returned")
        return

    iv = STOCK_IV.get(stock_name, 0.30)
    expiry = _monthly_expiry(trade_date)
    dte = (expiry - trade_date).days
    entry_mins = entry_dt.hour * 60 + entry_dt.minute

    print(f"\n  {'Time':>7} {'High':>8} {'Low':>8} {'Close':>8} {'PE Est':>8} {'Chg':>7} {'Floor':>6} {'SL':>6}")
    print(f"  {'─'*70}")

    peak_prem = 0
    peak_time = ""
    floor_hit = False
    floor_hit_time = ""
    sl_hit_time = ""

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

        timestr = f"{cdt_ist.hour}:{cdt_ist.minute:02d}"

        if use_bs:
            spot_low = c["low"]
            spot_high = c["high"]
            spot_close = c["close"]
            mins_in_day = cmins - (9 * 60 + 15)
            T = max((dte - mins_in_day / (6.25 * 60)) / 365, 0.0001)
            pe_high = bs_put(spot_low, strike, T, iv)
            pe_low = bs_put(spot_high, strike, T, iv)
            pe_close = bs_put(spot_close, strike, T, iv)
            label_high = f"{spot_low:.1f}"
            label_low = f"{spot_high:.1f}"
            label_close = f"{spot_close:.1f}"
        else:
            pe_high = c["high"]
            pe_low = c["low"]
            pe_close = c["close"]
            label_high = f"{pe_high:.2f}"
            label_low = f"{pe_low:.2f}"
            label_close = f"{pe_close:.2f}"

        chg = pe_close - entry

        if pe_high > peak_prem:
            peak_prem = pe_high
            peak_time = timestr

        fl_mark = "✓" if pe_high >= floor_price else ""
        sl_mark = "✗" if pe_low <= sl_price else ""

        if pe_high >= floor_price and not floor_hit:
            floor_hit = True
            floor_hit_time = timestr
            fl_mark = "◀ HIT"

        if pe_low <= sl_price and not sl_hit_time:
            sl_hit_time = timestr
            sl_mark = "◀ HIT"

        marker = ""
        if pe_high == peak_prem and pe_high > entry:
            marker = " PEAK"

        # Print every minute for the first 30 mins, then every 5 mins
        elapsed = cmins - entry_mins
        if elapsed <= 30 or elapsed % 5 == 0 or fl_mark or sl_mark or marker:
            print(f"  {timestr:>7} {label_high:>8} {label_low:>8} {label_close:>8} {pe_close:>8.2f} {chg:>+7.2f} {fl_mark:>6} {sl_mark:>6}{marker}")

    # Summary
    print(f"\n  {'─'*70}")
    print(f"  SUMMARY:")
    print(f"    Peak premium: {peak_prem:.2f} at {peak_time}")
    print(f"    Floor target: {floor_price:.2f} (1.5x entry)")
    print(f"    Floor hit:    {'YES at ' + floor_hit_time if floor_hit else 'NO'}")
    print(f"    SL hit:       {'YES at ' + sl_hit_time if sl_hit_time else 'NO'}")

    if floor_hit:
        floor_pnl_2lot = (floor_price - entry) * qty  # qty is already 2 lots worth
        print(f"    Floor P&L (2 lots): {floor_pnl_2lot:+,.0f}")
        if floor_hit_time and sl_hit_time:
            print(f"    Floor hit BEFORE SL? Floor@{floor_hit_time} vs SL@{sl_hit_time}")
    elif status == "CLOSED_SL" and pnl:
        print(f"    2-lot SL loss would be: {pnl * 2:+,.0f} (vs 1-lot: {pnl:+,.0f})")
    elif status == "CLOSED":
        print(f"    Trade hit target — floor was definitely crossed")


# Main
with get_conn() as conn:
    rows = conn.execute(
        "SELECT id, ts, symbol, qty, price, exit_price, pnl, stop_price, target_price, status, peak_price "
        "FROM trades WHERE channel='oeh' AND date(ts) >= '2026-09-09' ORDER BY ts"
    ).fetchall()

print(f"\n{'#'*100}")
print(f"OEH 1-MINUTE CANDLE ANALYSIS — {datetime.now(IST).strftime('%Y-%m-%d')}")
print(f"{'#'*100}")
print(f"Found {len(rows)} OEH trades today\n")

for r in rows:
    analyze_trade(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10])

print(f"\n{'#'*100}")
print(f"ANALYSIS COMPLETE")
print(f"{'#'*100}")

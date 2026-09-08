"""Analyze OEH SL-hit trades: did the premium spike enough that 2 lots
would have reached the target floor before SL was triggered?

Uses underlying stock candles + Black-Scholes to estimate option premium
at each interval (expired contracts aren't in the current instrument master)."""
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

token = load_cached_token()
ud = UpstoxData(access_token=token)
master = ud._load_master()

# --- Black-Scholes for PE estimation ---
def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs_put(S, K, T, sigma):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * _norm_cdf(-d2) - S * _norm_cdf(-d1)

# Rough IV per stock (annualised) — conservative estimates
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

# Monthly expiry (last Thursday) for DTE calc
def _monthly_expiry(dt):
    import calendar
    y, m = dt.year, dt.month
    if dt.day > 25:
        m += 1
        if m > 12:
            m = 1; y += 1
    last_day = calendar.monthrange(y, m)[1]
    d = datetime(y, m, last_day).date()
    while d.weekday() != 3:  # Thursday
        d -= timedelta(days=1)
    return d


with get_conn() as conn:
    rows = conn.execute(
        "SELECT id, ts, symbol, qty, price, exit_price, pnl, stop_price, target_price "
        "FROM trades WHERE channel='oeh' AND status='CLOSED_SL' "
        "AND date(ts) >= '2026-09-01' ORDER BY ts"
    ).fetchall()

print(f"{'='*100}")
print(f"OEH SL-Hit Trade Spike Analysis (B-S estimated premiums)")
print(f"{'='*100}")
print(f"Found {len(rows)} SL-hit trades to analyze\n")

rescued_count = 0
total_saved = 0

for r in rows:
    tid, ts, sym, qty, entry, sl_exit, pnl, sl_price, target = r
    lot_size = qty

    # Parse symbol: "INFY 1120 PE" -> stock=INFY, strike=1120
    parts = sym.strip().split()
    stock_name = parts[0]
    strike = float(parts[1])

    print(f"\n{'─'*90}")
    print(f"Trade #{tid}: {sym}")
    print(f"  Entry: {entry:.1f} | SL: {sl_price:.1f} | Target: {target:.1f} | Qty(1 lot): {qty}")
    print(f"  Actual SL Loss (1 lot): {pnl:+,.0f}")

    target_pnl_1 = (target - entry) * qty
    print(f"  Target P&L: 1 lot = +{target_pnl_1:,.0f} | 2 lots = +{target_pnl_1*2:,.0f}")
    print(f"  Floor test: 2-lot peak P&L >= 1-lot target ({target_pnl_1:,.0f})?")

    entry_dt = datetime.fromisoformat(ts)
    trade_date = entry_dt.date()
    from_dt = datetime.combine(trade_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(trade_date, datetime.min.time()).replace(hour=15, minute=30)

    eq_key = find_eq_key(stock_name)
    if not eq_key:
        print(f"  ⚠ Could not find equity key for {stock_name}")
        continue

    try:
        candles = ud.historical_data(eq_key, from_dt, to_dt, "5minute")
        time.sleep(0.4)
    except Exception as e:
        print(f"  ⚠ Candle fetch failed: {e}")
        continue

    if not candles:
        print(f"  ⚠ No candles returned")
        continue

    iv = STOCK_IV.get(stock_name, 0.30)
    expiry = _monthly_expiry(trade_date)
    dte = (expiry - trade_date).days
    entry_mins = entry_dt.hour * 60 + entry_dt.minute

    print(f"  IV: {iv*100:.0f}% | DTE: {dte} | Expiry: {expiry}")
    print(f"\n  {'Time':>7} {'Spot':>9} {'Est PE':>8} {'Chg':>7} {'P&L 1L':>10} {'P&L 2L':>10} {'%Tgt':>7}")
    print(f"  {'─'*65}")

    peak_prem = 0
    peak_time = ""
    peak_pnl_1 = 0
    peak_pnl_2 = 0
    sl_hit_shown = False

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

        spot = c["close"]
        spot_low = c["low"]   # worst for PE (spot goes up = PE drops)
        spot_high = c["high"] # best for PE (spot goes down = PE rises... wait)

        # For PE: lower spot = higher premium. So check c["low"] for peak PE premium
        # and c["high"] for worst PE premium (potential SL)
        mins_in_day = cmins - (9*60+15)
        T = max((dte - mins_in_day / (6.25*60)) / 365, 0.0001)

        # Best case (spot at low of candle = PE highest)
        pe_best = bs_put(spot_low, strike, T, iv)
        # Close estimate
        pe_close = bs_put(spot, strike, T, iv)

        chg = pe_close - entry
        p1 = chg * qty
        p2 = chg * qty * 2
        pct = (pe_close - entry) / (target - entry) * 100 if target != entry else 0

        if pe_best > peak_prem:
            peak_prem = pe_best
            peak_time = f"{cdt_ist.hour}:{cdt_ist.minute:02d}"
            peak_pnl_1 = (pe_best - entry) * qty
            peak_pnl_2 = (pe_best - entry) * qty * 2

        # Show at intervals or notable moments
        marker = ""
        if pe_best == peak_prem and pe_best > entry:
            marker = " ◀ PEAK"
        if pe_close <= sl_price and not sl_hit_shown:
            marker = " ◀ SL HIT"
            sl_hit_shown = True

        timestr = f"{cdt_ist.hour}:{cdt_ist.minute:02d}"
        print(f"  {timestr:>7} {spot:>9,.1f} {pe_close:>8.1f} {chg:>+7.1f} {p1:>+10,.0f} {p2:>+10,.0f} {pct:>6.0f}%{marker}")

    # Summary
    floor_hit = peak_pnl_2 >= target_pnl_1
    pct_of_target = (peak_prem - entry) / (target - entry) * 100 if target != entry else 0

    print(f"\n  ▸ Peak premium: {peak_prem:.1f} at {peak_time} ({pct_of_target:.0f}% of target)")
    print(f"  ▸ Peak P&L — 1 lot: {peak_pnl_1:+,.0f} | 2 lots: {peak_pnl_2:+,.0f}")

    if floor_hit:
        saved = peak_pnl_2 - (pnl * 2)  # what we'd gain vs 2-lot SL
        rescued_count += 1
        total_saved += peak_pnl_1  # conservative: book at 1-lot-target equivalent
        print(f"  ✅ 2 LOTS WOULD HIT THE FLOOR! Peak 2-lot P&L ({peak_pnl_2:+,.0f}) >= 1-lot target ({target_pnl_1:+,.0f})")
        print(f"     → With a trailing exit at peak, 2 lots saves {saved:+,.0f} vs taking the SL")
    else:
        shortfall = target_pnl_1 - peak_pnl_2
        print(f"  ❌ Not enough — 2-lot peak ({peak_pnl_2:+,.0f}) still short of 1-lot target ({target_pnl_1:+,.0f}) by {shortfall:,.0f}")

print(f"\n{'='*100}")
print(f"SUMMARY")
print(f"{'='*100}")
print(f"  SL trades analyzed: {len(rows)}")
print(f"  Rescued by 2 lots: {rescued_count}/{len(rows)}")
print(f"  Potential savings:  +{total_saved:,.0f} (if booked at peak)")
print(f"{'='*100}")

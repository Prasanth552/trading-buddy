"""OEL 9:15→9:30 scalp backtest — last 1 month.

Strategy:
  - At 9:15, scan F&O universe for Open=Low stocks (low ≈ open, price rising).
  - Buy ATM CE for top 5 candidates (by rise%).
  - Close at 9:30 regardless of P&L.

Uses real 1-min candles from Upstox historical API.

Usage: PYTHONPATH=. .venv/bin/python3 scripts/oel_915_930_backtest.py
"""
import os, sys, time, math
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()

import config
from src.broker.upstox_data import UpstoxData, load_cached_token
from src.broker.upstox_client import UpstoxClient, pick_upstox_option, _expiry_to_date

IST = ZoneInfo("Asia/Kolkata")

OEL_TOLERANCE = 0.05
OEL_MIN_RISE_PCT = 0.3
OEL_MAX_TRADES = 5
LOTS = 1

UNIVERSE = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "BHARTIARTL",
    "SBIN", "ITC", "BAJFINANCE", "LT", "KOTAKBANK", "AXISBANK",
    "TITAN", "MARUTI", "SUNPHARMA", "HCLTECH", "WIPRO", "TATASTEEL",
    "ADANIENT", "CIPLA", "DRREDDY", "M&M", "ASIANPAINT", "HINDUNILVR",
    "NESTLEIND", "ONGC", "ULTRACEMCO", "JSWSTEEL", "TRENT",
    "BAJAJFINSV", "VEDL", "HINDALCO", "BPCL", "HEROMOTOCO", "EICHERMOT",
    "TATAPOWER", "BEL", "NTPC", "POWERGRID", "COALINDIA", "PIDILITIND",
    "SHREECEM", "DABUR", "COLPAL", "AMBUJACEM", "BHEL",
    "DIVISLAB", "BRITANNIA",
]

# Strike step per stock (from STOCKS config or auto-detect)
STRIKE_STEPS = {
    "RELIANCE": 20, "TCS": 50, "HDFCBANK": 20, "INFY": 20, "ICICIBANK": 20,
    "BHARTIARTL": 20, "SBIN": 10, "ITC": 10, "BAJFINANCE": 50, "LT": 25,
    "KOTAKBANK": 20, "AXISBANK": 20, "TITAN": 50, "MARUTI": 100,
    "SUNPHARMA": 20, "HCLTECH": 20, "WIPRO": 10, "TATASTEEL": 5,
    "ADANIENT": 50, "CIPLA": 20, "DRREDDY": 50, "M&M": 20,
    "ASIANPAINT": 25, "HINDUNILVR": 25, "NESTLEIND": 50, "ONGC": 5,
    "ULTRACEMCO": 100, "JSWSTEEL": 10, "TRENT": 50, "BAJAJFINSV": 20,
    "VEDL": 5, "HINDALCO": 10, "BPCL": 10, "HEROMOTOCO": 50,
    "EICHERMOT": 50, "TATAPOWER": 5, "BEL": 5, "NTPC": 5,
    "POWERGRID": 5, "COALINDIA": 10, "PIDILITIND": 25, "SHREECEM": 100,
    "DABUR": 10, "COLPAL": 20, "AMBUJACEM": 10, "BHEL": 5,
    "DIVISLAB": 50, "BRITANNIA": 50, "TATAMOTORS": 10,
}

# Lot sizes (approximate — will verify from master)
LOT_SIZES = {
    "RELIANCE": 250, "TCS": 175, "HDFCBANK": 550, "INFY": 400, "ICICIBANK": 700,
    "BHARTIARTL": 475, "SBIN": 750, "ITC": 1600, "BAJFINANCE": 125, "LT": 150,
    "KOTAKBANK": 400, "AXISBANK": 625, "TITAN": 175, "MARUTI": 50,
    "SUNPHARMA": 300, "HCLTECH": 350, "WIPRO": 1500, "TATASTEEL": 5000,
    "ADANIENT": 250, "CIPLA": 325, "DRREDDY": 75, "M&M": 175,
    "ASIANPAINT": 200, "HINDUNILVR": 150, "NESTLEIND": 25, "ONGC": 3250,
    "ULTRACEMCO": 50, "JSWSTEEL": 750, "TRENT": 100, "BAJAJFINSV": 250,
    "VEDL": 1500, "HINDALCO": 1075, "BPCL": 1800, "HEROMOTOCO": 100,
    "EICHERMOT": 100, "TATAPOWER": 1350, "BEL": 1500, "NTPC": 1400,
    "POWERGRID": 1800, "COALINDIA": 1050, "PIDILITIND": 250, "SHREECEM": 12,
    "DABUR": 600, "COLPAL": 150, "AMBUJACEM": 600, "BHEL": 1750,
    "DIVISLAB": 100, "BRITANNIA": 75, "TATAMOTORS": 1400,
}

token = load_cached_token()
ud = UpstoxData(access_token=token)
uc = UpstoxClient()
master = uc.load_instruments()
print(f"  Loaded {len(master)} instruments from Upstox master\n")

# Build equity instrument key map
eq_keys = {}
for inst in master:
    if inst.get("segment") == "NSE_EQ":
        tsym = (inst.get("trading_symbol") or "").upper()
        if tsym:
            eq_keys[tsym] = inst.get("instrument_key")

# Build stock option expiry map (for resolving ATM CEs)
stock_expiries = defaultdict(set)
for inst in master:
    if inst.get("segment") != "NSE_FO":
        continue
    tsym = (inst.get("trading_symbol") or "").upper()
    for sym in UNIVERSE:
        if tsym.startswith(sym + " "):
            ed = _expiry_to_date(inst.get("expiry"))
            if ed:
                stock_expiries[sym].add(ed)
            break


def _nearest_expiry(sym, ref_date):
    """Find the nearest future expiry for a stock from master."""
    expiries = sorted(e for e in stock_expiries.get(sym, set()) if e >= ref_date)
    return expiries[0] if expiries else None


def _find_option_key(sym, strike, opt_type, expiry):
    """Find instrument_key for a stock option from master."""
    name_upper = sym.upper()
    for inst in master:
        if inst.get("segment") != "NSE_FO":
            continue
        tsym = (inst.get("trading_symbol") or "").upper()
        if not tsym.startswith(name_upper + " "):
            continue
        if inst.get("instrument_type") != opt_type:
            continue
        if abs(float(inst.get("strike_price", -1)) - float(strike)) > 0.001:
            continue
        ed = _expiry_to_date(inst.get("expiry"))
        if ed == expiry:
            return inst.get("instrument_key")
    return None


def _get_1min_candles(inst_key, day):
    """Fetch 1-min candles for 9:15-9:31 window."""
    from_dt = datetime(day.year, day.month, day.day, 9, 14, tzinfo=IST)
    to_dt = datetime(day.year, day.month, day.day, 9, 32, tzinfo=IST)
    try:
        candles = ud.historical_data(inst_key, from_dt, to_dt, "1minute")
        time.sleep(0.35)
        return candles
    except Exception as e:
        return None


def _get_candle_at(candles, hour, minute):
    """Get candle at specific time from list."""
    for c in candles:
        ct = c.get("date") or c.get("timestamp") or ""
        if isinstance(ct, str):
            try:
                cdt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(IST)
            except:
                continue
        else:
            cdt = ct.astimezone(IST) if hasattr(ct, 'astimezone') else ct
        if cdt.hour == hour and cdt.minute == minute:
            return c
    return None


def charges_per_trade(buy_prem, sell_prem, lot_size, lots=1):
    """Calculate F&O charges for a buy-then-sell option trade."""
    qty = lot_size * lots
    buy_to = buy_prem * qty
    sell_to = sell_prem * qty

    brokerage = 40  # flat per order × 2
    stt = sell_to * 0.000625
    txn = (buy_to + sell_to) * 0.00053
    gst = (brokerage + txn) * 0.18
    sebi = (buy_to + sell_to) * 0.000001
    stamp = buy_to * 0.00003
    return round(brokerage + stt + txn + gst + sebi + stamp, 2)


# ── Backtest ──
days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 22
end_date = date.today()
start_date = end_date - timedelta(days=days_back + 10)  # buffer for weekends

# Generate trading days
trading_days = []
d = start_date
while d <= end_date:
    if d.weekday() < 5:
        trading_days.append(d)
    d += timedelta(days=1)
trading_days = trading_days[-days_back:] if len(trading_days) >= days_back else trading_days

print(f"{'='*120}")
print(f"  OEL 9:15→9:30 SCALP BACKTEST — {LOTS} lot(s), last {len(trading_days)} trading days")
print(f"  Dates: {trading_days[0]} to {trading_days[-1]}")
print(f"  Strategy: Buy ATM CE at 9:15 for top {OEL_MAX_TRADES} Open=Low stocks, close at 9:30")
print(f"{'='*120}\n")

all_trades = []
day_results = {}

for day in trading_days:
    print(f"  --- {day} ---")

    # Step 1: Scan universe for OEL pattern using 9:15 candle
    candidates = []
    for sym in UNIVERSE:
        eq_key = eq_keys.get(sym)
        if not eq_key:
            continue

        candles = _get_1min_candles(eq_key, day)
        if not candles:
            continue

        # Get the 9:15 candle (first candle of the day)
        c915 = _get_candle_at(candles, 9, 15)
        if not c915:
            continue

        open_price = c915["open"]
        low_price = c915["low"]
        close_price = c915["close"]

        if open_price <= 0:
            continue

        # OEL check: low ≈ open (stock opened at its low and moved up)
        if low_price < open_price - OEL_TOLERANCE:
            continue

        rise_pct = (close_price - open_price) / open_price * 100
        if rise_pct < OEL_MIN_RISE_PCT:
            continue

        candidates.append({
            "symbol": sym,
            "open": open_price,
            "close_915": close_price,
            "low": low_price,
            "rise_pct": rise_pct,
        })

    if not candidates:
        print(f"    No OEL candidates")
        day_results[day.isoformat()] = {"trades": 0, "net": 0, "charges": 0}
        continue

    candidates.sort(key=lambda x: x["rise_pct"], reverse=True)
    top = candidates[:OEL_MAX_TRADES]
    print(f"    {len(candidates)} candidates, picking top {len(top)}: "
          f"{', '.join(c['symbol'] for c in top)}")

    day_net = 0
    day_charges = 0
    day_count = 0

    for c in top:
        sym = c["symbol"]
        stock_price = c["close_915"]
        strike_step = STRIKE_STEPS.get(sym, 50)
        atm_strike = round(stock_price / strike_step) * strike_step
        lot_size = LOT_SIZES.get(sym, 100)

        # Find nearest expiry
        expiry = _nearest_expiry(sym, day)
        if not expiry:
            print(f"      {sym}: no expiry found, skip")
            continue

        # Find option instrument key
        opt_key = _find_option_key(sym, atm_strike, "CE", expiry)
        if not opt_key:
            # Try ±1 strike step
            opt_key = _find_option_key(sym, atm_strike + strike_step, "CE", expiry)
            if opt_key:
                atm_strike += strike_step
            else:
                opt_key = _find_option_key(sym, atm_strike - strike_step, "CE", expiry)
                if opt_key:
                    atm_strike -= strike_step
        if not opt_key:
            print(f"      {sym}: CE {atm_strike} instrument not found, skip")
            continue

        # Get option 1-min candles for 9:15-9:30
        opt_candles = _get_1min_candles(opt_key, day)
        if not opt_candles:
            print(f"      {sym}: no option candles, skip")
            continue

        # Entry at 9:15 close, exit at 9:30 close
        entry_candle = _get_candle_at(opt_candles, 9, 15)
        exit_candle = _get_candle_at(opt_candles, 9, 30)

        if not entry_candle:
            # Try 9:16 as fallback
            entry_candle = _get_candle_at(opt_candles, 9, 16)
        if not exit_candle:
            exit_candle = _get_candle_at(opt_candles, 9, 29)

        if not entry_candle or not exit_candle:
            print(f"      {sym}: missing entry/exit candles, skip")
            continue

        entry_prem = entry_candle["close"]
        exit_prem = exit_candle["close"]
        qty = lot_size * LOTS

        gross = (exit_prem - entry_prem) * qty
        ch = charges_per_trade(entry_prem, exit_prem, lot_size, LOTS)
        net = gross - ch

        trade = {
            "date": day.isoformat(),
            "symbol": sym,
            "strike": atm_strike,
            "expiry": expiry.isoformat(),
            "entry_prem": entry_prem,
            "exit_prem": exit_prem,
            "qty": qty,
            "lot_size": lot_size,
            "gross": gross,
            "charges": ch,
            "net": net,
            "rise_pct": c["rise_pct"],
            "stock_open": c["open"],
            "stock_close_915": c["close_915"],
        }
        all_trades.append(trade)
        day_count += 1
        day_net += net
        day_charges += ch

        icon = "+" if net > 0 else ""
        print(f"      {sym:12} CE {atm_strike:>6.0f} | "
              f"entry={entry_prem:.2f} exit={exit_prem:.2f} | "
              f"{icon}₹{net:,.0f} (rise {c['rise_pct']:.1f}%)")

    day_results[day.isoformat()] = {"trades": day_count, "net": day_net, "charges": day_charges}

# ── Summary ──
print(f"\n{'='*120}")
print(f"  TRADE DETAILS")
print(f"  {'Date':>12} {'Symbol':12} {'Strike':>8} {'Entry':>8} {'Exit':>8} "
      f"{'Gross':>10} {'Charges':>8} {'Net P&L':>10} {'Rise%':>6}")
print(f"  {'─'*100}")

total_gross = 0
total_charges = 0
total_net = 0
wins = losses = 0

for t in all_trades:
    icon = "✅" if t["net"] > 0 else "❌"
    print(f"  {t['date']:>12} {t['symbol']:12} {t['strike']:>8.0f} "
          f"{t['entry_prem']:>8.2f} {t['exit_prem']:>8.2f} "
          f"{t['gross']:>+10,.0f} {t['charges']:>8,.0f} {t['net']:>+10,.0f} "
          f"{t['rise_pct']:>5.1f}% {icon}")
    total_gross += t["gross"]
    total_charges += t["charges"]
    total_net += t["net"]
    if t["net"] > 0:
        wins += 1
    elif t["net"] < 0:
        losses += 1

print(f"\n{'='*120}")
print(f"  SUMMARY — OEL 9:15→9:30 Scalp, {LOTS} lot(s), {len(trading_days)} trading days")
print(f"  {'─'*60}")
print(f"  {'Trades:':30} {len(all_trades)}")
if wins + losses > 0:
    print(f"  {'Win / Loss:':30} {wins}W / {losses}L ({wins/(wins+losses)*100:.0f}% win)")
print(f"  {'':30}")
print(f"  {'Total Gross P&L:':30} ₹{total_gross:>+12,.0f}")
print(f"  {'Total Charges:':30} ₹{total_charges:>12,.0f}")
print(f"  {'Total Net P&L:':30} ₹{total_net:>+12,.0f}")
print(f"  {'':30}")
if all_trades:
    print(f"  {'Avg net P&L per trade:':30} ₹{total_net/len(all_trades):>12,.0f}")
    print(f"  {'Avg net P&L per day:':30} ₹{total_net/len(trading_days):>12,.0f}")
    print(f"  {'Avg charges per trade:':30} ₹{total_charges/len(all_trades):>12,.0f}")

# Per-day breakdown
print(f"\n  PER-DAY:")
print(f"  {'Date':>12} {'Trades':>8} {'Net P&L':>12} {'Charges':>10}")
print(f"  {'─'*46}")
for d in sorted(day_results):
    dr = day_results[d]
    icon = "🟢" if dr["net"] > 0 else "🔴" if dr["net"] < 0 else "⚪"
    print(f"  {d:>12} {dr['trades']:>8} {dr['net']:>+12,.0f} {dr['charges']:>10,.0f} {icon}")

# Weekly breakdown
weeks = defaultdict(lambda: {"net": 0, "count": 0, "charges": 0, "wins": 0, "losses": 0})
for t in all_trades:
    d = date.fromisoformat(t["date"])
    iso = d.isocalendar()
    wk = f"{iso.year}-W{iso.week:02d}"
    weeks[wk]["net"] += t["net"]
    weeks[wk]["count"] += 1
    weeks[wk]["charges"] += t["charges"]
    if t["net"] > 0: weeks[wk]["wins"] += 1
    elif t["net"] < 0: weeks[wk]["losses"] += 1

print(f"\n  WEEKLY:")
print(f"  {'Week':>10} {'Trades':>8} {'W/L':>8} {'Net P&L':>12} {'Charges':>10}")
print(f"  {'─'*52}")
for wk in sorted(weeks):
    w = weeks[wk]
    icon = "🟢" if w["net"] > 0 else "🔴"
    print(f"  {wk:>10} {w['count']:>8} {w['wins']}W/{w['losses']}L {w['net']:>+12,.0f} {w['charges']:>10,.0f} {icon}")

print(f"\n{'='*120}")
print(f"  BACKTEST COMPLETE")
print(f"{'='*120}")

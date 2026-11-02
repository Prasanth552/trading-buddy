"""What-if: trades that got max_hold exit at a loss — what if we held until SL or EOD?"""
import sys, json
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from src.storage import db
from src.broker.upstox_data import UpstoxData, load_cached_token
import config

IST = ZoneInfo("Asia/Kolkata")
token = load_cached_token()
ud = UpstoxData(token)

db.init_db()

# Get all CLOSED (not CLOSED_SL) trades with losses today from MOM/ORB/BSQ
with db.get_conn() as conn:
    rows = conn.execute(
        "SELECT id, channel, symbol, price, qty, pnl, stop_price, status, ts "
        "FROM trades WHERE status='CLOSED' AND pnl < 0 AND ts LIKE '2026-10-28%' "
        "AND channel IN ('orb_ce','orb_pe','mom_ce','mom_pe','bsq_ce','bsq_pe') "
        "ORDER BY channel, pnl"
    ).fetchall()

print(f"Found {len(rows)} trades closed at a loss (not via SL)\n")
print(f"{'Ch':>8} {'Symbol':<25} {'Qty':>5} {'Entry':>8} {'ActualPnL':>10} {'HoldPnL':>10} {'Diff':>8} {'HoldResult':<15}")
print("-" * 105)

# Load instrument master for eq_keys
import requests, gzip
resp = requests.get("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz", timeout=30)
instruments = json.loads(gzip.decompress(resp.content))
eq_keys = {}
for inst in instruments:
    if inst.get("segment") == "NSE_FO" and inst.get("asset_type") == "EQUITY" and inst.get("instrument_type") in ("CE", "PE"):
        sym = inst.get("underlying_symbol", "")
        if sym and sym not in eq_keys:
            eq_keys[sym] = inst.get("underlying_key", "")

from_dt = datetime(2026, 10, 28, 9, 15, tzinfo=IST)
to_dt = datetime(2026, 10, 28, 15, 30, tzinfo=IST)

total_actual = 0
total_hold = 0

for r in rows:
    symbol = r["symbol"]
    ch = r["channel"]
    entry_price = r["price"]
    qty = r["qty"]
    actual_pnl = r["pnl"]
    sl_price = r["stop_price"] or 0
    total_actual += actual_pnl

    # Extract base symbol from option name (e.g. "ICICIBANK 1360 PE" -> "ICICIBANK")
    parts = symbol.split()
    base_sym = parts[0]
    option_type = parts[-1] if len(parts) >= 3 else "PE"

    eq_key = eq_keys.get(base_sym, "")
    if not eq_key:
        print(f"{ch:>8} {symbol:<25} {qty:>5} {entry_price:>8.2f} {actual_pnl:>+10.0f} {'N/A':>10} {'':>8} NO EQ KEY")
        total_hold += actual_pnl  # assume same
        continue

    try:
        candles = ud.historical_data(eq_key, from_dt, to_dt, "1minute")
    except Exception as e:
        print(f"{ch:>8} {symbol:<25} {qty:>5} {entry_price:>8.2f} {actual_pnl:>+10.0f} {'ERR':>10} {'':>8} {e}")
        total_hold += actual_pnl
        continue

    if not candles:
        total_hold += actual_pnl
        continue

    # Find entry candle by timestamp
    entry_ts = r["ts"]
    entry_dt = datetime.fromisoformat(entry_ts) if isinstance(entry_ts, str) else entry_ts
    if entry_dt.tzinfo is None:
        entry_dt = entry_dt.replace(tzinfo=IST)

    # Find the candle closest to entry time
    entry_idx = 0
    for idx, c in enumerate(candles):
        c_ts = str(c.get("date", ""))
        try:
            c_dt = datetime.fromisoformat(c_ts)
            if c_dt.tzinfo is None:
                c_dt = c_dt.replace(tzinfo=IST)
            if c_dt <= entry_dt:
                entry_idx = idx
        except Exception:
            pass

    stock_entry = candles[entry_idx]["close"]
    est_premium = entry_price  # use actual entry price (option premium)

    # SL pct from entry
    sl_pct = (est_premium - sl_price) / est_premium if est_premium > 0 else 0.30

    # Simulate holding until SL or EOD (3:25 PM)
    hold_pnl = None
    for c in candles[entry_idx+1:]:
        stock_now = c["close"]
        stock_low = c["low"]
        stock_high = c["high"]

        if option_type == "PE":
            # PE profits when stock drops
            worst_move = (stock_high - stock_entry) / stock_entry  # stock going up = PE loss
            option_worst = est_premium * (1 + (-worst_move) * 3)
        else:
            # CE profits when stock rises
            worst_move = (stock_low - stock_entry) / stock_entry  # stock going down = CE loss
            option_worst = est_premium * (1 + worst_move * 3)

        if option_worst <= sl_price:
            hold_pnl = (sl_price - est_premium) * qty
            break

    if hold_pnl is None:
        # EOD exit
        last = candles[-1]
        if option_type == "PE":
            final_move = (stock_entry - last["close"]) / stock_entry
        else:
            final_move = (last["close"] - stock_entry) / stock_entry
        final_premium = est_premium * (1 + final_move * 3)
        hold_pnl = (final_premium - est_premium) * qty

    # Cap at max SL
    max_sl = 5000 if "mom" in ch else (2500 if "orb" in ch else 5000)
    if hold_pnl < -max_sl:
        hold_pnl = -max_sl

    hold_pnl = round(hold_pnl)
    total_hold += hold_pnl
    diff = hold_pnl - actual_pnl

    if hold_pnl > 0:
        result = "RECOVERED"
    elif hold_pnl <= actual_pnl:
        result = "HIT SL"
    else:
        result = "STILL LOSS"

    print(f"{ch:>8} {symbol:<25} {qty:>5} {entry_price:>8.2f} {actual_pnl:>+10.0f} {hold_pnl:>+10.0f} {diff:>+8.0f} {result:<15}")

print("-" * 105)
print(f"{'':>8} {'TOTAL':<25} {'':>5} {'':>8} {total_actual:>+10.0f} {total_hold:>+10.0f} {total_hold-total_actual:>+8.0f}")
print(f"\n  Actual (cut early):  {total_actual:>+10,}")
print(f"  If held to SL/EOD:  {total_hold:>+10,}")
print(f"  Difference:         {total_hold - total_actual:>+10,}")

recovered = sum(1 for _ in [])  # placeholder

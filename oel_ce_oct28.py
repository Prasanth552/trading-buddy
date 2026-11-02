"""OEL CE what-if backtest for Oct 28 — Open=Low bullish scan.
OEL was skipped by Gift Nifty gate. This shows what would have happened."""
import sys, json
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from src.broker.upstox_data import UpstoxData, load_cached_token
import config

IST = ZoneInfo("Asia/Kolkata")

token = load_cached_token()
if not token:
    print("ERROR: No token"); sys.exit(1)
ud = UpstoxData(token)

import requests, gzip
resp = requests.get("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz", timeout=30)
instruments = json.loads(gzip.decompress(resp.content))

fo_stocks = {}
for inst in instruments:
    if (inst.get("segment") == "NSE_FO"
        and inst.get("asset_type") == "EQUITY"
        and inst.get("instrument_type") in ("CE", "PE")):
        sym = inst.get("underlying_symbol", "")
        if sym and sym not in fo_stocks:
            fo_stocks[sym] = {
                "lot": inst.get("lot_size", 1),
                "eq_key": inst.get("underlying_key", ""),
            }

fo_stocks = {s: v for s, v in fo_stocks.items() if v["eq_key"]}
print(f"Found {len(fo_stocks)} F&O stocks")

# OEL params
OEL_TOLERANCE = 0.05
OEL_MIN_RISE_PCT = 0.3
OEL_SL_PCT = 0.30
OEL_MAX_SL = 5000
OEL_BLOCKLIST = {"GODREJCP", "GRASIM"}
OEL_CAPITAL = 300000
OEL_FLOOR_LEVELS = [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500]

today = datetime(2026, 10, 28, tzinfo=IST).date()
from_dt = datetime.combine(today, datetime.min.time().replace(hour=9, minute=15))
to_dt = datetime.combine(today, datetime.min.time().replace(hour=15, minute=30))
from_dt = from_dt.replace(tzinfo=IST)
to_dt = to_dt.replace(tzinfo=IST)

# Step 1: Find Open=Low candidates using first few candles (9:15-9:20)
scan_from = from_dt
scan_to = datetime.combine(today, datetime.min.time().replace(hour=9, minute=50)).replace(tzinfo=IST)

print("\nScanning for Open=Low stocks...")
candidates = []
scanned = 0

for sym, info in sorted(fo_stocks.items()):
    if sym in OEL_BLOCKLIST:
        continue
    eq_key = info["eq_key"]
    lot = info["lot"]

    try:
        candles = ud.historical_data(eq_key, scan_from, scan_to, "1minute")
    except Exception:
        continue
    if not candles or len(candles) < 3:
        continue

    scanned += 1
    open_price = candles[0]["open"]
    if open_price <= 0:
        continue

    # Check Open=Low: low must be >= open - tolerance across first few candles
    min_low = min(c["low"] for c in candles[:5])
    if min_low < open_price - OEL_TOLERANCE:
        continue

    # Check minimum rise
    entry_price = candles[4]["close"] if len(candles) > 4 else candles[-1]["close"]
    rise_pct = (entry_price - open_price) / open_price * 100
    if rise_pct < OEL_MIN_RISE_PCT:
        continue

    candidates.append({
        "symbol": sym,
        "open": open_price,
        "entry": entry_price,
        "low": min_low,
        "rise_pct": rise_pct,
        "lot": lot,
        "eq_key": eq_key,
    })

candidates.sort(key=lambda x: x["rise_pct"], reverse=True)
print(f"Scanned {scanned} stocks, found {len(candidates)} Open=Low candidates\n")

if not candidates:
    print("No OEL candidates found for Oct 28")
    sys.exit(0)

# Step 2: Simulate CE trades with capital allocation
print("=" * 70)
print("OEL CE What-If — Oct 28")
print("=" * 70)

capital_avail = OEL_CAPITAL
total_pnl = 0
trades = 0
wins = 0

for c in candidates:
    sym = c["symbol"]
    entry_price = c["entry"]
    lot = c["lot"]
    est_premium = entry_price * 0.015

    # Floor calculation
    trade_cost = est_premium * lot
    floor = OEL_FLOOR_LEVELS[0]
    for fl in OEL_FLOOR_LEVELS:
        if trade_cost >= fl:
            floor = fl

    if capital_avail < floor:
        print(f"  {sym:<12} SKIP — capital exhausted (avail={capital_avail:.0f}, need={floor})")
        continue

    # Fetch full day candles for simulation
    try:
        full_candles = ud.historical_data(c["eq_key"], from_dt, to_dt, "1minute")
    except Exception:
        continue
    if not full_candles or len(full_candles) < 10:
        continue

    # Entry at ~9:20 (5th minute candle)
    entry_idx = min(5, len(full_candles) - 1)
    entry_price = full_candles[entry_idx]["close"]
    est_premium = entry_price * 0.015
    sl_price = est_premium * (1 - OEL_SL_PCT)
    trade_cost = est_premium * lot

    capital_avail -= min(trade_cost, floor)

    # Simulate: CE profits when stock goes up
    exit_pnl = None
    for fc in full_candles[entry_idx+1:]:
        low_move = (fc["low"] - entry_price) / entry_price
        option_low = est_premium * (1 + low_move * 3)
        if option_low <= sl_price:
            exit_pnl = (sl_price - est_premium) * lot
            break

    if exit_pnl is None:
        last = full_candles[-1]
        final_move = (last["close"] - entry_price) / entry_price
        final_premium = est_premium * (1 + final_move * 3)
        exit_pnl = (final_premium - est_premium) * lot

    if exit_pnl < -OEL_MAX_SL:
        exit_pnl = -OEL_MAX_SL

    exit_pnl = round(exit_pnl)
    total_pnl += exit_pnl
    trades += 1
    if exit_pnl > 0:
        wins += 1

    capital_avail += min(trade_cost, floor) + exit_pnl
    tag = "WIN" if exit_pnl > 0 else "LOSS"
    print(f"  {sym:<12} CE lot={lot:>5} entry={entry_price:>8.2f} rise={c['rise_pct']:.1f}% → PnL {exit_pnl:>+8,} [{tag}]")

wr = round(wins / trades * 100) if trades else 0
charges = trades * 70
print(f"\n  TOTAL: {trades} trades, {wr}% WR, Raw PnL {total_pnl:>+10,}")
print(f"  Est. charges ({trades} × ₹70): ₹{charges:,}")
print(f"  Net after charges: {total_pnl - charges:>+10,}")

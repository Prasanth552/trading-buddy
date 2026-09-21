"""Debug OEH for Sep 21 — check how many stocks match Open=High pattern."""
from __future__ import annotations
import time as _t, re
from datetime import datetime, date
from zoneinfo import ZoneInfo
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo("Asia/Kolkata")
ref_date = date(2026, 9, 21)

# Check if Sep 21 is a trading day (Monday)
print(f"Sep 21 is a {ref_date.strftime('%A')}")

token = load_cached_token()
if not token:
    print("ERROR: No Upstox token — run automated_login() first")
    exit(1)

ud = UpstoxData(access_token=token)
master = ud._load_master()

# Build universe
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
        if name and name not in {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "NIFTY BANK", "NIFTY 50"} and len(name) >= 2:
            syms.add(name)

eq_keys = {}
for inst in master:
    if inst.get("segment") == "NSE_EQ":
        tsym = (inst.get("trading_symbol") or "").upper()
        if tsym:
            eq_keys[tsym] = inst.get("instrument_key")

universe = sorted(syms)
matched = [s for s in universe if s in eq_keys]
print(f"F&O universe: {len(universe)} | EQ matched: {len(matched)}")

from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
to_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=25)

# Try fetching NIFTY first to verify data exists for this date
from src.config import config
nifty_key = config.UPSTOX_INDEX_KEYS.get("NSE:NIFTY 50")
if nifty_key:
    try:
        nc = ud.historical_data(nifty_key, from_dt, to_dt, "5minute")
        if nc:
            print(f"NIFTY data exists: open={nc[0]['open']}, close={nc[0]['close']}, candles={len(nc)}")
        else:
            print("NIFTY returned NO candles — market might be closed!")
            exit(0)
    except Exception as e:
        print(f"NIFTY fetch error: {e}")

# Scan with both tolerances
TOLERANCES = [0.05, 0.5, 1.0, 2.0]
MIN_DROP = 0.3

all_first_candles = []
oeh_by_tolerance = {t: [] for t in TOLERANCES}

scanned = 0
no_data = 0
errors = 0

for sym in matched:
    if sym in {"GODREJCP", "GRASIM"}:
        continue
    inst_key = eq_keys.get(sym)
    if not inst_key:
        continue
    try:
        candles = ud.historical_data(inst_key, from_dt, to_dt, "5minute")
        _t.sleep(0.12)
    except Exception as e:
        errors += 1
        if "429" in str(e):
            _t.sleep(2)
        continue

    scanned += 1
    if not candles or len(candles) < 1:
        no_data += 1
        continue

    c = candles[0]
    o, h, cl = c["open"], c["high"], c["close"]
    if o <= 0:
        continue

    drop_pct = (o - cl) / o * 100
    gap = h - o

    all_first_candles.append({"sym": sym, "open": o, "high": h, "close": cl, "gap": gap, "drop": drop_pct})

    for tol in TOLERANCES:
        if gap <= tol and drop_pct >= MIN_DROP:
            oeh_by_tolerance[tol].append({"sym": sym, "open": o, "high": h, "close": cl, "gap": gap, "drop": drop_pct})

print(f"\nScanned: {scanned} | No data: {no_data} | Errors: {errors}")
print(f"\nOEH candidates by tolerance:")
for tol in TOLERANCES:
    cands = oeh_by_tolerance[tol]
    print(f"  tol=₹{tol:.2f}: {len(cands)} candidates")
    for c in sorted(cands, key=lambda x: x["drop"], reverse=True)[:10]:
        print(f"    {c['sym']:<15s} open={c['open']:>8.1f} high={c['open']+c['gap']:>8.1f} gap=₹{c['gap']:+.2f} drop={c['drop']:.2f}%")

# Show near-misses: high - open between 0.05 and 2.0
near = [c for c in all_first_candles if 0 < c["gap"] <= 2.0 and c["drop"] >= MIN_DROP]
near.sort(key=lambda x: x["gap"])
print(f"\nNear-miss OEH (gap 0-₹2, drop>=0.3%): {len(near)}")
for c in near[:15]:
    print(f"  {c['sym']:<15s} open={c['open']:>8.1f} high={c['open']+c['gap']:>8.1f} gap=₹{c['gap']:+.2f} drop={c['drop']:.2f}%")

# Check: how many stocks had open > close (bearish first candle)?
bearish = [c for c in all_first_candles if c["close"] < c["open"]]
print(f"\nBearish 1st candle (close < open): {len(bearish)}/{len(all_first_candles)}")

"""MOM CE what-if backtest for Oct 28 — momentum bullish breakouts."""
import sys, json
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, ".")
from src.broker.upstox_data import UpstoxData, load_cached_token

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
print(f"Found {len(fo_stocks)} F&O stocks\n")

from_dt = datetime(2026, 10, 28, 9, 15, tzinfo=IST)
to_dt = datetime(2026, 10, 28, 15, 30, tzinfo=IST)

# MOM params (from channel_listener.py)
MOM_BODY_PCT_MIN = 1.0    # min body % for momentum candle
MOM_VOL_MULT = 1.5        # volume must be 1.5x avg
SL_PCT = 0.30
MAX_SL = 5000
MAX_HOLD_CANDLES = 12

mom_ce_total = 0
mom_ce_trades = 0
mom_ce_wins = 0
mom_pe_total = 0
mom_pe_trades = 0
mom_pe_wins = 0

stock_list = sorted(fo_stocks.keys())
processed = 0

for sym in stock_list:
    info = fo_stocks[sym]
    eq_key = info["eq_key"]
    lot = info["lot"]
    try:
        candles = ud.historical_data(eq_key, from_dt, to_dt, "5minute")
    except Exception:
        continue
    if not candles or len(candles) < 10:
        continue

    processed += 1
    if processed % 50 == 0:
        print(f"  {processed}/{len(stock_list)} stocks...")

    # Compute avg volume from first 6 candles (first 30 min)
    vols = [c.get("volume", 0) for c in candles[:6]]
    avg_vol = sum(vols) / len(vols) if vols else 0
    if avg_vol < 100:
        continue

    for i, c in enumerate(candles[6:], start=6):
        body = abs(c["close"] - c["open"])
        body_pct = body / c["open"] * 100 if c["open"] > 0 else 0
        vol = c.get("volume", 0)

        if body_pct < MOM_BODY_PCT_MIN:
            continue
        if vol < avg_vol * MOM_VOL_MULT:
            continue

        # Determine direction
        if c["close"] > c["open"]:
            direction = "bullish"
        else:
            direction = "bearish"

        entry_price = c["close"]
        est_premium = entry_price * 0.015
        sl_price = est_premium * (1 - SL_PCT)

        remaining = candles[i+1:i+1+MAX_HOLD_CANDLES]
        if not remaining:
            continue

        exit_pnl = None
        for rc in remaining:
            if direction == "bullish":
                low_move = (rc["low"] - entry_price) / entry_price
                option_low = est_premium * (1 + low_move * 3)
            else:
                low_move = (entry_price - rc["high"]) / entry_price
                option_low = est_premium * (1 + low_move * 3)

            if option_low <= sl_price:
                exit_pnl = (sl_price - est_premium) * lot
                break

        if exit_pnl is None:
            last = remaining[-1]
            if direction == "bullish":
                final_move = (last["close"] - entry_price) / entry_price
            else:
                final_move = (entry_price - last["close"]) / entry_price
            final_premium = est_premium * (1 + final_move * 3)
            exit_pnl = (final_premium - est_premium) * lot

        if exit_pnl < -MAX_SL:
            exit_pnl = -MAX_SL

        exit_pnl = round(exit_pnl)

        if direction == "bullish":
            mom_ce_trades += 1
            mom_ce_total += exit_pnl
            if exit_pnl > 0:
                mom_ce_wins += 1
        else:
            mom_pe_trades += 1
            mom_pe_total += exit_pnl
            if exit_pnl > 0:
                mom_pe_wins += 1

        # Update rolling avg volume
        vols.append(vol)
        avg_vol = sum(vols[-20:]) / min(len(vols), 20)

print(f"\nProcessed {processed} stocks\n")
print("=" * 60)
print("MOM Oct 28 What-If")
print("=" * 60)
ce_wr = round(mom_ce_wins / mom_ce_trades * 100) if mom_ce_trades else 0
pe_wr = round(mom_pe_wins / mom_pe_trades * 100) if mom_pe_trades else 0
print(f"  MOM CE: {mom_ce_trades:>4} trades, {ce_wr}% WR, PnL {mom_ce_total:>+10,}")
print(f"  MOM PE: {mom_pe_trades:>4} trades, {pe_wr}% WR, PnL {mom_pe_total:>+10,}")
print(f"  TOTAL:  {mom_ce_trades+mom_pe_trades:>4} trades, PnL {mom_ce_total+mom_pe_total:>+10,}")

# Charges estimate
ce_charges = mom_ce_trades * 70
pe_charges = mom_pe_trades * 70
print(f"\n  Est. charges (~₹70/trade):")
print(f"  MOM CE: {mom_ce_trades} × ₹70 = ₹{ce_charges:,} → Net {mom_ce_total - ce_charges:>+10,}")
print(f"  MOM PE: {mom_pe_trades} × ₹70 = ₹{pe_charges:,} → Net {mom_pe_total - pe_charges:>+10,}")
print(f"  TOTAL net after charges: {mom_ce_total + mom_pe_total - ce_charges - pe_charges:>+10,}")

"""What-if backtest: Oct 28 CE trades that were blocked by BEARISH regime.
Runs on VM via SSH — uses live Upstox token to fetch today's candle data."""
import sys, json, math, statistics
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, ".")
from src.broker.upstox_data import UpstoxData, load_cached_token
import config

IST = ZoneInfo("Asia/Kolkata")
today = datetime(2026, 10, 28, tzinfo=IST)

token = load_cached_token()
if not token:
    print("ERROR: No token"); sys.exit(1)
ud = UpstoxData(token)

# Load instrument master
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

# ===== OEH CE what-if =====
# OEH signals come from Telegram channel — we can't replay those.
# But we can check: the 7 stocks that got PE trades, what would CE have done?
oeh_stocks = {
    "RADICO": {"strike": 4150, "lot": 300},
    "TECHM": {"strike": 1740, "lot": 1200},
    "TORNTPHARM": {"strike": 4550, "lot": 250},
    "TATACONSUM": {"strike": 980, "lot": 1100},
    "NATIONALUM": {"strike": 315, "lot": 3750},
    "DMART": {"strike": 3150, "lot": 300},
    "APLAPOLLO": {"strike": 2220, "lot": 700},
}

print("=" * 70)
print("OEH CE What-If (same 7 stocks, CE instead of PE)")
print("=" * 70)
oeh_total = 0
for sym, info in oeh_stocks.items():
    eq_key = fo_stocks.get(sym, {}).get("eq_key", "")
    if not eq_key:
        print(f"  {sym}: no equity key found")
        continue
    try:
        candles = ud.historical_data(eq_key, from_dt, to_dt, "1minute")
    except Exception as e:
        print(f"  {sym}: error {e}")
        continue
    if not candles or len(candles) < 5:
        print(f"  {sym}: insufficient data")
        continue

    # Entry at ~09:17 (3rd candle), SL=30%, MaxSL=5000
    entry_candle = candles[2] if len(candles) > 2 else candles[0]
    entry_price = entry_candle["close"]
    est_premium = entry_price * 0.015  # ~1.5% ATM estimate
    sl_price = est_premium * 0.70  # 30% SL
    lot = info["lot"]

    # CE profits when stock goes UP
    exit_pnl = None
    for c in candles[3:]:
        low_move = (c["low"] - entry_price) / entry_price
        option_low = est_premium * (1 + low_move * 3)
        if option_low <= sl_price:
            exit_pnl = (sl_price - est_premium) * lot
            break

    if exit_pnl is None:
        last = candles[-1]
        final_move = (last["close"] - entry_price) / entry_price
        final_premium = est_premium * (1 + final_move * 3)
        exit_pnl = (final_premium - est_premium) * lot

    if exit_pnl < -5000:
        exit_pnl = -5000

    exit_pnl = round(exit_pnl)
    oeh_total += exit_pnl
    tag = "WIN" if exit_pnl > 0 else "LOSS"
    print(f"  {sym:<12} CE lot={lot:>5} est_prem={est_premium:.1f} → PnL {exit_pnl:>+8,} [{tag}]")

print(f"  {'TOTAL':<12} {'':>5} {'':>20} PnL {oeh_total:>+8,}")

# ===== ORB CE what-if =====
print("\n" + "=" * 70)
print("ORB CE What-If (all stocks with bullish breakouts)")
print("=" * 70)

ORB_SL_PCT = 0.15
ORB_MAX_SL = 2500

orb_ce_total = 0
orb_ce_trades = 0
orb_ce_wins = 0

stock_list = sorted(fo_stocks.keys())
for sym in stock_list:
    info = fo_stocks[sym]
    eq_key = info["eq_key"]
    lot = info["lot"]
    try:
        candles = ud.historical_data(eq_key, from_dt, to_dt, "5minute")
    except Exception:
        continue
    if not candles or len(candles) < 8:
        continue

    # ORB: first 15-min range (candles 0-2), breakout after
    orb_candles = candles[:3]
    orb_high = max(c["high"] for c in orb_candles)
    orb_low = min(c["low"] for c in orb_candles)
    orb_range_pct = (orb_high - orb_low) / orb_low * 100

    if orb_range_pct < 0.5 or orb_range_pct > 5.0:
        continue

    # Look for bullish breakout (CE)
    for i, c in enumerate(candles[3:], start=3):
        if c["close"] > orb_high:
            entry_price = c["close"]
            est_premium = entry_price * 0.015
            sl_price = est_premium * (1 - ORB_SL_PCT)

            exit_pnl = None
            remaining = candles[i+1:i+1+12]
            for rc in remaining:
                low_move = (rc["low"] - entry_price) / entry_price
                option_low = est_premium * (1 + low_move * 3)
                if option_low <= sl_price:
                    exit_pnl = (sl_price - est_premium) * lot
                    break

            if exit_pnl is None and remaining:
                last = remaining[-1]
                final_move = (last["close"] - entry_price) / entry_price
                final_premium = est_premium * (1 + final_move * 3)
                exit_pnl = (final_premium - est_premium) * lot

            if exit_pnl is None:
                break

            if exit_pnl < -ORB_MAX_SL:
                exit_pnl = -ORB_MAX_SL

            exit_pnl = round(exit_pnl)
            orb_ce_total += exit_pnl
            orb_ce_trades += 1
            if exit_pnl > 0:
                orb_ce_wins += 1
            break

orb_wr = round(orb_ce_wins / orb_ce_trades * 100) if orb_ce_trades else 0
print(f"  ORB CE: {orb_ce_trades} trades, {orb_wr}% WR, PnL {orb_ce_total:>+10,}")

# ===== BSQ CE what-if =====
print("\n" + "=" * 70)
print("BSQ CE What-If (Bollinger squeeze bullish breakouts)")
print("=" * 70)

BB_PERIOD = 14
BB_STD = 2.0
SQUEEZE_PCTILE = 35
BSQ_SL_PCT = 0.30
BSQ_MAX_SL = 5000
BSQ_MAX_HOLD = 12

bsq_ce_total = 0
bsq_ce_trades = 0
bsq_ce_wins = 0

for sym in stock_list:
    info = fo_stocks[sym]
    eq_key = info["eq_key"]
    lot = info["lot"]
    try:
        # Need 14+ prior candles, fetch from yesterday
        candles = ud.historical_data(
            eq_key,
            datetime(2026, 10, 27, 9, 15, tzinfo=IST),
            datetime(2026, 10, 28, 15, 30, tzinfo=IST),
            "5minute"
        )
    except Exception:
        continue
    if not candles or len(candles) < 25:
        continue

    # Only today's candles for signals, but use full history for BB
    today_start_idx = None
    for idx, c in enumerate(candles):
        ts = str(c.get("date", ""))[:10]
        if ts == "2026-10-28":
            today_start_idx = idx
            break
    if today_start_idx is None:
        continue

    closes = [c["close"] for c in candles[:today_start_idx]]
    bandwidths = []
    day_trades = 0

    for i in range(today_start_idx, len(candles)):
        c = candles[i]
        closes.append(c["close"])
        if len(closes) < BB_PERIOD:
            continue
        window = closes[-BB_PERIOD:]
        sma = sum(window) / BB_PERIOD
        std = statistics.stdev(window)
        upper = sma + BB_STD * std
        lower = sma - BB_STD * std
        bw = (upper - lower) / sma * 100 if sma > 0 else 0
        bandwidths.append(bw)
        if len(bandwidths) < 5:
            continue

        recent_bw = bandwidths[-50:] if len(bandwidths) >= 50 else bandwidths
        pctile = sum(1 for b in recent_bw if b < bw) / len(recent_bw) * 100
        if pctile > SQUEEZE_PCTILE:
            continue

        close = c["close"]
        prev_close = closes[-2]

        # Only bullish breakout (CE)
        if close > upper and prev_close <= upper:
            est_premium = close * 0.015
            sl_price = est_premium * (1 - BSQ_SL_PCT)
            remaining = candles[i+1:i+1+BSQ_MAX_HOLD]
            if not remaining:
                continue

            exit_pnl = None
            for rc in remaining:
                low_move = (rc["low"] - close) / close
                option_low = est_premium * (1 + low_move * 3)
                if option_low <= sl_price:
                    exit_pnl = (sl_price - est_premium) * lot
                    break

            if exit_pnl is None:
                last = remaining[-1]
                final_move = (last["close"] - close) / close
                final_premium = est_premium * (1 + final_move * 3)
                exit_pnl = (final_premium - est_premium) * lot

            if exit_pnl < -BSQ_MAX_SL:
                exit_pnl = -BSQ_MAX_SL

            exit_pnl = round(exit_pnl)
            day_trades += 1
            if day_trades <= 15:
                bsq_ce_total += exit_pnl
                bsq_ce_trades += 1
                if exit_pnl > 0:
                    bsq_ce_wins += 1

bsq_wr = round(bsq_ce_wins / bsq_ce_trades * 100) if bsq_ce_trades else 0
print(f"  BSQ CE: {bsq_ce_trades} trades, {bsq_wr}% WR, PnL {bsq_ce_total:>+10,}")

# ===== SUMMARY =====
print("\n" + "=" * 70)
print("SUMMARY: What CE trades would have added today")
print("=" * 70)
grand_total = oeh_total + orb_ce_total + bsq_ce_total
print(f"  OEH CE:  {oeh_total:>+10,}")
print(f"  ORB CE:  {orb_ce_total:>+10,}")
print(f"  BSQ CE:  {bsq_ce_total:>+10,}")
print(f"  TOTAL:   {grand_total:>+10,}")
print(f"\n  Today's actual PnL was ~-42,565")
print(f"  With CE trades it would have been ~{-42565 + grand_total:+,}")

"""Realistic backtest: BSQ + ORB + MOM for last month WITH charges + slippage.
Shows results at different trade caps: ALL, top 10, top 5 per day per side."""
import sys, json, math, statistics, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, ".")
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo("Asia/Kolkata")

CHARGE_PER_TRADE = 70       # ₹70 avg (brokerage + STT + exchange + GST)
SLIPPAGE_PER_UNIT = 0.10    # ₹0.10 per unit slippage on entry+exit

# Params
BB_PERIOD = 14; BB_STD = 2.0; SQUEEZE_PCTILE = 35
BSQ_SL_PCT = 0.30; BSQ_MAX_SL = 5000; BSQ_MAX_HOLD = 12
ORB_SL_PCT = 0.15; ORB_MAX_SL = 2500
MOM_BODY_PCT_MIN = 1.0; MOM_VOL_MULT = 1.5
MOM_SL_PCT = 0.30; MOM_MAX_SL = 5000; MOM_MAX_HOLD = 12

token = load_cached_token()
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
            fo_stocks[sym] = {"lot": inst.get("lot_size", 1), "eq_key": inst.get("underlying_key", "")}
fo_stocks = {s: v for s, v in fo_stocks.items() if v["eq_key"]}
print(f"Found {len(fo_stocks)} F&O stocks")

end_date = datetime(2026, 10, 27, tzinfo=IST)
start_date = end_date - timedelta(days=30)

def sim_trade(candles, i, direction, lot, sl_pct, max_sl, max_hold_candles):
    """Simulate a single trade. Returns raw PnL (no charges)."""
    close = candles[i]["close"]
    est_premium = close * 0.015
    sl_price = est_premium * (1 - sl_pct)
    remaining = candles[i+1:i+1+max_hold_candles]
    if not remaining:
        return None

    # Add slippage on entry
    slippage_cost = SLIPPAGE_PER_UNIT * lot * 2  # entry + exit

    exit_pnl = None
    for rc in remaining:
        if direction == "bullish":
            low_move = (rc["low"] - close) / close
            option_low = est_premium * (1 + low_move * 3)
        else:
            low_move = (close - rc["high"]) / close
            option_low = est_premium * (1 + low_move * 3)
        if option_low <= sl_price:
            exit_pnl = (sl_price - est_premium) * lot
            break

    if exit_pnl is None:
        last = remaining[-1]
        if direction == "bullish":
            final_move = (last["close"] - close) / close
        else:
            final_move = (close - last["close"]) / close
        final_premium = est_premium * (1 + final_move * 3)
        exit_pnl = (final_premium - est_premium) * lot

    if exit_pnl < -max_sl:
        exit_pnl = -max_sl

    # Subtract slippage
    exit_pnl -= slippage_cost
    return round(exit_pnl)


# ==================== COLLECT ALL SIGNALS ====================
# Structure: {strategy: {day: [(pnl, strength, side), ...]}}
all_signals = {"bsq": defaultdict(list), "orb": defaultdict(list), "mom": defaultdict(list)}

stock_list = sorted(fo_stocks.keys())
processed = 0

for sym in stock_list:
    info = fo_stocks[sym]
    eq_key = info["eq_key"]
    lot = info["lot"]

    try:
        candles = ud.historical_data(
            eq_key,
            datetime(start_date.year, start_date.month, start_date.day, 9, 15, tzinfo=IST),
            datetime(end_date.year, end_date.month, end_date.day, 15, 30, tzinfo=IST),
            "5minute"
        )
        if not candles:
            continue
    except Exception:
        continue

    processed += 1
    if processed % 25 == 0:
        print(f"  {processed}/{len(stock_list)} stocks...")

    day_candles = defaultdict(list)
    for c in candles:
        ts = str(c.get("date", ""))[:10]
        day_candles[ts].append(c)

    for day_str, dcs in sorted(day_candles.items()):
        if len(dcs) < 25:
            continue

        # ===== BSQ =====
        closes_bsq = []
        bandwidths = []
        for i, c in enumerate(dcs):
            closes_bsq.append(c["close"])
            if len(closes_bsq) < BB_PERIOD:
                continue
            window = closes_bsq[-BB_PERIOD:]
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
            prev_close = closes_bsq[-2]
            direction = None
            strength = 0
            if close > upper and prev_close <= upper:
                direction = "bullish"
                strength = (close - upper) / upper * 100
            elif close < lower and prev_close >= lower:
                direction = "bearish"
                strength = (lower - close) / lower * 100
            if direction:
                pnl = sim_trade(dcs, i, direction, lot, BSQ_SL_PCT, BSQ_MAX_SL, BSQ_MAX_HOLD)
                if pnl is not None:
                    side = "CE" if direction == "bullish" else "PE"
                    all_signals["bsq"][day_str].append((pnl, strength, side))

        # ===== ORB =====
        if len(dcs) >= 8:
            orb_candles = dcs[:3]
            orb_high = max(c["high"] for c in orb_candles)
            orb_low = min(c["low"] for c in orb_candles)
            orb_range_pct = (orb_high - orb_low) / orb_low * 100
            if 0.5 <= orb_range_pct <= 5.0:
                for i, c in enumerate(dcs[3:], start=3):
                    direction = None
                    strength = 0
                    if c["close"] > orb_high:
                        direction = "bullish"
                        strength = (c["close"] - orb_high) / orb_high * 100
                    elif c["close"] < orb_low:
                        direction = "bearish"
                        strength = (orb_low - c["close"]) / orb_low * 100
                    if direction:
                        pnl = sim_trade(dcs, i, direction, lot, ORB_SL_PCT, ORB_MAX_SL, BSQ_MAX_HOLD)
                        if pnl is not None:
                            side = "CE" if direction == "bullish" else "PE"
                            all_signals["orb"][day_str].append((pnl, strength, side))
                        break  # one ORB signal per stock per day

        # ===== MOM =====
        vols = [c.get("volume", 0) for c in dcs[:6]]
        avg_vol = sum(vols) / len(vols) if vols else 0
        if avg_vol >= 100:
            for i, c in enumerate(dcs[6:], start=6):
                body = abs(c["close"] - c["open"])
                body_pct = body / c["open"] * 100 if c["open"] > 0 else 0
                vol = c.get("volume", 0)
                if body_pct < MOM_BODY_PCT_MIN or vol < avg_vol * MOM_VOL_MULT:
                    vols.append(vol)
                    avg_vol = sum(vols[-20:]) / min(len(vols), 20)
                    continue
                direction = "bullish" if c["close"] > c["open"] else "bearish"
                pnl = sim_trade(dcs, i, direction, lot, MOM_SL_PCT, MOM_MAX_SL, MOM_MAX_HOLD)
                if pnl is not None:
                    side = "CE" if direction == "bullish" else "PE"
                    all_signals["mom"][day_str].append((pnl, body_pct, side))
                vols.append(vol)
                avg_vol = sum(vols[-20:]) / min(len(vols), 20)

print(f"\nProcessed {processed} stocks\n")

# ==================== ANALYZE WITH DIFFERENT CAPS ====================
def analyze(strategy, signals, caps):
    """Analyze a strategy at different trade caps."""
    all_days = sorted(signals.keys())
    print(f"\n{'='*90}")
    print(f"  {strategy.upper()} — {len(all_days)} trading days")
    print(f"{'='*90}")

    for cap_label, cap in caps:
        total_pnl = 0
        total_trades = 0
        total_wins = 0
        total_charges = 0
        daily_pnls = []

        for day in all_days:
            day_sigs = signals[day]
            # Sort by strength descending, take top N
            day_sigs_sorted = sorted(day_sigs, key=lambda x: x[1], reverse=True)
            if cap:
                # Cap per side
                ce_sigs = [s for s in day_sigs_sorted if s[2] == "CE"][:cap]
                pe_sigs = [s for s in day_sigs_sorted if s[2] == "PE"][:cap]
                selected = ce_sigs + pe_sigs
            else:
                selected = day_sigs_sorted

            day_pnl = 0
            for pnl, strength, side in selected:
                net = pnl - CHARGE_PER_TRADE
                total_pnl += net
                day_pnl += net
                total_trades += 1
                total_charges += CHARGE_PER_TRADE
                if net > 0:
                    total_wins += 1
            daily_pnls.append(day_pnl)

        wr = round(total_wins / total_trades * 100) if total_trades else 0
        avg_daily = total_pnl / len(all_days) if all_days else 0
        worst_day = min(daily_pnls) if daily_pnls else 0
        best_day = max(daily_pnls) if daily_pnls else 0
        avg_trades_day = total_trades / len(all_days) if all_days else 0

        print(f"\n  Cap: {cap_label}")
        print(f"    Trades: {total_trades} ({avg_trades_day:.0f}/day) | WR: {wr}%")
        print(f"    Raw PnL:     {total_pnl + total_charges:>+10,}")
        print(f"    Charges:     {total_charges:>10,}")
        print(f"    Slippage:    (included in PnL)")
        print(f"    NET PnL:     {total_pnl:>+10,}")
        print(f"    Per day avg: {avg_daily:>+10,.0f}")
        print(f"    Per month:   {avg_daily * 22:>+10,.0f}")
        print(f"    Best day:    {best_day:>+10,}")
        print(f"    Worst day:   {worst_day:>+10,}")

caps = [("ALL (no limit)", None), ("Top 10/side/day", 10), ("Top 5/side/day", 5), ("Top 3/side/day", 3)]

analyze("BSQ", all_signals["bsq"], caps)
analyze("ORB", all_signals["orb"], caps)
analyze("MOM", all_signals["mom"], caps)

# ==================== COMBINED BEST CONFIG ====================
print(f"\n{'='*90}")
print(f"  COMBINED — Best config (top 5/side/day for each)")
print(f"{'='*90}")

total = 0
for strat in ("bsq", "orb", "mom"):
    all_days = sorted(all_signals[strat].keys())
    strat_total = 0
    for day in all_days:
        sigs = all_signals[strat][day]
        sigs_sorted = sorted(sigs, key=lambda x: x[1], reverse=True)
        ce = [s for s in sigs_sorted if s[2] == "CE"][:5]
        pe = [s for s in sigs_sorted if s[2] == "PE"][:5]
        for pnl, _, _ in ce + pe:
            strat_total += pnl - CHARGE_PER_TRADE
    print(f"  {strat.upper()}: {strat_total:>+10,}")
    total += strat_total
print(f"  TOTAL: {total:>+10,} ({total/21*22:>+10,.0f}/month est)")

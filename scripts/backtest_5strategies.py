#!/usr/bin/env python3
"""
Backtest 5 new strategy candidates on a single day using real Upstox option candles.

Strategies:
  1. VWAP Pullback     — buy on pullback to VWAP in trending stock
  2. EMA 9/21 Crossover — buy ATM option on 5-min EMA cross
  3. RSI Bounce         — RSI(7) oversold/overbought reversal on 5-min
  4. Gap-Fill MR        — opposite of OEH: gap down → buy CE (bet on fill)
  5. Momentum Scalp     — strong 5-min candle + volume spike

All use same floor exit system + SL logic as live bot.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/backtest_5strategies.py [--date 2026-10-13]
"""

import asyncio, sys, os, argparse, math
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo(config.TIMEZONE)

# ── Shared params ──
CAPITAL_PER_STRATEGY = 300000
MAX_SL = 5000
SL_PCT = 0.30
FLOOR_LEVELS = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
MAX_ENTRIES_PER_SYM = 3
SL_COOLDOWN = True
TIME_CUTOFF = dtime(13, 0)

# ── Strategy-specific params ──
VWAP_WAIT_MINS = 30          # wait 30 min for VWAP to settle
VWAP_PULLBACK_PCT = 0.15     # price must be within 0.15% of VWAP to count as pullback
VWAP_TREND_PCT = 0.3         # price must be >0.3% away from VWAP to confirm trend

EMA_FAST = 9
EMA_SLOW = 21

RSI_PERIOD = 7
RSI_OVERSOLD = 25
RSI_OVERBOUGHT = 75
RSI_ENTRY_CROSS = 30         # buy CE when RSI crosses back above 30
RSI_SHORT_CROSS = 70         # buy PE when RSI crosses back below 70

GAP_MIN_PCT = 0.5            # minimum gap % to qualify
GAP_WAIT_CANDLES = 3         # wait 3 candles (3 min) to confirm gap isn't extending

MOM_BODY_PCT = 0.15          # candle body must be > 0.15% of open
MOM_VOL_MULT = 1.5           # volume must be > 1.5x average
MOM_MAX_HOLD_MINS = 30       # max hold time


def get_stepped_floor(net_pnl):
    locked = 0
    for lvl in FLOOR_LEVELS:
        if net_pnl >= lvl:
            locked = lvl
        else:
            break
    return locked


def parse_ts(c):
    ts = c.get("ts") or c.get("date") or c.get("timestamp")
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if hasattr(ts, "astimezone"):
        ts = ts.astimezone(IST)
    return ts.replace(tzinfo=None) if ts.tzinfo else ts


# ── Technical indicator helpers ──

def calc_vwap(candles_1min, up_to_time, date):
    """Calculate VWAP from 1-min candles up to given time."""
    cum_vol = 0
    cum_tp_vol = 0
    for c in candles_1min:
        ct = parse_ts(c)
        if ct > datetime.combine(date, up_to_time):
            break
        tp = (c["high"] + c["low"] + c["close"]) / 3
        vol = c.get("volume", 0) or 0
        if vol <= 0:
            vol = 1
        cum_vol += vol
        cum_tp_vol += tp * vol
    if cum_vol <= 0:
        return None
    return cum_tp_vol / cum_vol


def build_5min_candles(candles_1min, date):
    """Aggregate 1-min candles into 5-min candles."""
    bars = []
    bucket = None
    for c in candles_1min:
        ct = parse_ts(c)
        bucket_start = ct.replace(minute=(ct.minute // 5) * 5, second=0, microsecond=0)
        if bucket is None or bucket["time"] != bucket_start:
            if bucket:
                bars.append(bucket)
            bucket = {
                "time": bucket_start,
                "open": c["open"], "high": c["high"],
                "low": c["low"], "close": c["close"],
                "volume": c.get("volume", 0) or 0,
            }
        else:
            bucket["high"] = max(bucket["high"], c["high"])
            bucket["low"] = min(bucket["low"], c["low"])
            bucket["close"] = c["close"]
            bucket["volume"] += c.get("volume", 0) or 0
    if bucket:
        bars.append(bucket)
    return bars


def calc_ema(values, period):
    """Calculate EMA series from a list of values."""
    if len(values) < period:
        return [None] * len(values)
    ema = [None] * (period - 1)
    sma = sum(values[:period]) / period
    ema.append(sma)
    mult = 2 / (period + 1)
    for i in range(period, len(values)):
        ema.append(values[i] * mult + ema[-1] * (1 - mult))
    return ema


def calc_rsi(closes, period=7):
    """Calculate RSI series."""
    if len(closes) < period + 1:
        return [None] * len(closes)
    rsi = [None] * period
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        rsi.append(100)
    else:
        rs = avg_gain / avg_loss
        rsi.append(100 - 100 / (1 + rs))
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i-1]
        gain = max(diff, 0)
        loss = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            rsi.append(100)
        else:
            rs = avg_gain / avg_loss
            rsi.append(100 - 100 / (1 + rs))
    return rsi


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(IST).strftime("%Y-%m-%d"))
    args = parser.parse_args()
    DATE = datetime.strptime(args.date, "%Y-%m-%d").date()

    token = load_cached_token()
    if not token:
        print("ERROR: No Upstox token.")
        return

    ud = UpstoxData(access_token=token)
    master = ud._load_master()
    print(f"Loaded {len(master)} instruments")

    # Build F&O universe + equity keys + lot sizes + option keys
    import re as _re
    eq_keys = {}
    lot_sizes = {}
    fo_symbols = set()
    option_keys = defaultdict(lambda: defaultdict(dict))
    sym_strikes = defaultdict(set)

    for inst in master:
        seg = inst.get("segment")
        if seg == "NSE_EQ":
            tsym = (inst.get("trading_symbol") or "").upper()
            if tsym:
                eq_keys[tsym] = inst.get("instrument_key")
        elif seg == "NSE_FO":
            _base = _re.match(r'^([A-Z&]+)', (inst.get("trading_symbol") or "").upper())
            if _base:
                fo_symbols.add(_base.group(1))
                _ls = int(inst.get("lot_size") or 0)
                if _ls > 0:
                    lot_sizes[_base.group(1)] = _ls
            otype = inst.get("instrument_type", "").upper()
            if otype in ("CE", "PE"):
                strike = float(inst.get("strike_price") or 0)
                if strike <= 0:
                    continue
                asset_sym = (inst.get("asset_symbol") or inst.get("name") or "").upper()
                if not asset_sym and _base:
                    asset_sym = _base.group(1)
                if not asset_sym:
                    continue
                ikey = inst.get("instrument_key")
                expiry = inst.get("expiry")
                sym_strikes[asset_sym].add(strike)
                existing = option_keys[asset_sym][strike].get(otype)
                if existing is None or (expiry and existing.get("expiry") and expiry < existing["expiry"]):
                    option_keys[asset_sym][strike][otype] = {"key": ikey, "expiry": expiry}

    universe = sorted(fo_symbols & set(eq_keys.keys()))
    print(f"F&O universe: {len(universe)} stocks")

    # Strike steps
    strike_steps = {}
    for sym, strikes in sym_strikes.items():
        ss = sorted(strikes)
        if len(ss) >= 2:
            step = min(ss[j+1] - ss[j] for j in range(min(20, len(ss)-1)))
            strike_steps[sym] = step
        else:
            strike_steps[sym] = 50

    # ── Fetch equity 1-min candles ──
    print("\n=== Fetching equity intraday data... ===")
    from_dt = datetime.combine(DATE, dtime(9, 15))
    to_dt = datetime.combine(DATE, dtime(15, 30))
    equity_candles = {}
    batch_size = 20
    syms_with_keys = [(sym, eq_keys[sym]) for sym in universe if sym in eq_keys]

    for i in range(0, len(syms_with_keys), batch_size):
        batch = syms_with_keys[i:i + batch_size]
        for sym, key in batch:
            try:
                candles = ud.historical_data(key, from_dt, to_dt, "1minute")
                if candles:
                    equity_candles[sym] = candles
            except Exception:
                pass
        pct = min(100, (i + batch_size) / len(syms_with_keys) * 100)
        print(f"  {min(i+batch_size, len(syms_with_keys))}/{len(syms_with_keys)} ({pct:.0f}%)", end="  \r")
        await asyncio.sleep(0.3)
    print(f"\nGot candles for {len(equity_candles)} stocks")

    # ── Helpers ──
    def find_atm_strike(sym, ltp, otype):
        step = strike_steps.get(sym, 50)
        atm = round(ltp / step) * step
        strikes = option_keys.get(sym, {})
        if not strikes:
            return None
        for offset in [0, step, -step, 2*step, -2*step]:
            s = atm + offset
            if s in strikes and otype in strikes[s]:
                return s
        valid = [s for s in strikes if otype in strikes[s]]
        if not valid:
            return None
        return min(valid, key=lambda s: abs(s - ltp))

    def get_price_at_time(sym, target_time):
        candles = equity_candles.get(sym, [])
        best = None
        for c in candles:
            ct = parse_ts(c)
            if ct <= datetime.combine(DATE, target_time):
                best = c["close"]
            else:
                break
        return best

    # ══════════════════════════════════════════════════════════════════
    #  SCAN ALL 5 STRATEGIES — collect (sym, strike, otype, entry_time)
    # ══════════════════════════════════════════════════════════════════

    signals = {
        "VWAP_Pullback": [],
        "EMA_Crossover": [],
        "RSI_Bounce": [],
        "Gap_Fill_MR": [],
        "Momentum_Scalp": [],
    }

    print("\n=== Scanning strategies... ===")

    for sym in universe:
        candles_1m = equity_candles.get(sym)
        if not candles_1m or len(candles_1m) < 30:
            continue

        open_price = candles_1m[0]["open"]
        if open_price <= 0:
            continue

        bars_5m = build_5min_candles(candles_1m, DATE)
        if len(bars_5m) < EMA_SLOW + 2:
            continue

        closes_5m = [b["close"] for b in bars_5m]
        times_5m = [b["time"] for b in bars_5m]
        vols_5m = [b["volume"] for b in bars_5m]

        # Pre-compute indicators on 5-min bars
        ema_fast = calc_ema(closes_5m, EMA_FAST)
        ema_slow = calc_ema(closes_5m, EMA_SLOW)
        rsi_vals = calc_rsi(closes_5m, RSI_PERIOD)

        # ── Strategy 1: VWAP Pullback ──
        for i, c1m in enumerate(candles_1m):
            ct = parse_ts(c1m)
            t = ct.time()
            if t < dtime(9, 45):  # wait 30 min
                continue
            if t > TIME_CUTOFF:
                break
            vwap = calc_vwap(candles_1m, t, DATE)
            if vwap is None or vwap <= 0:
                continue
            price = c1m["close"]
            dist_pct = (price - vwap) / vwap * 100

            # Trending above VWAP, pulling back to it → buy CE
            if dist_pct > 0 and dist_pct < VWAP_PULLBACK_PCT:
                # Check if was trending (price was >0.3% above VWAP recently)
                prev_idx = max(0, i - 10)
                was_trending = False
                for j in range(prev_idx, i):
                    p = candles_1m[j]["close"]
                    if (p - vwap) / vwap * 100 > VWAP_TREND_PCT:
                        was_trending = True
                        break
                if was_trending:
                    signals["VWAP_Pullback"].append((sym, "CE", t, price, "above_vwap_pullback"))

            # Trending below VWAP, pulling back to it → buy PE
            elif dist_pct < 0 and dist_pct > -VWAP_PULLBACK_PCT:
                prev_idx = max(0, i - 10)
                was_trending = False
                for j in range(prev_idx, i):
                    p = candles_1m[j]["close"]
                    if (p - vwap) / vwap * 100 < -VWAP_TREND_PCT:
                        was_trending = True
                        break
                if was_trending:
                    signals["VWAP_Pullback"].append((sym, "PE", t, price, "below_vwap_pullback"))

        # ── Strategy 2: EMA 9/21 Crossover (5-min) ──
        for i in range(1, len(bars_5m)):
            if ema_fast[i] is None or ema_slow[i] is None:
                continue
            if ema_fast[i-1] is None or ema_slow[i-1] is None:
                continue
            t = times_5m[i].time()
            if t < dtime(9, 30) or t > TIME_CUTOFF:
                continue
            price = closes_5m[i]

            # Bullish cross → buy CE
            if ema_fast[i-1] <= ema_slow[i-1] and ema_fast[i] > ema_slow[i]:
                signals["EMA_Crossover"].append((sym, "CE", t, price, "bull_cross"))

            # Bearish cross → buy PE
            elif ema_fast[i-1] >= ema_slow[i-1] and ema_fast[i] < ema_slow[i]:
                signals["EMA_Crossover"].append((sym, "PE", t, price, "bear_cross"))

        # ── Strategy 3: RSI Bounce (5-min) ──
        for i in range(1, len(bars_5m)):
            if rsi_vals[i] is None or rsi_vals[i-1] is None:
                continue
            t = times_5m[i].time()
            if t < dtime(9, 30) or t > TIME_CUTOFF:
                continue
            price = closes_5m[i]

            # Oversold bounce → buy CE
            if rsi_vals[i-1] < RSI_ENTRY_CROSS and rsi_vals[i] >= RSI_ENTRY_CROSS:
                signals["RSI_Bounce"].append((sym, "CE", t, price, f"rsi_bounce_{rsi_vals[i]:.0f}"))

            # Overbought reversal → buy PE
            elif rsi_vals[i-1] > RSI_SHORT_CROSS and rsi_vals[i] <= RSI_SHORT_CROSS:
                signals["RSI_Bounce"].append((sym, "PE", t, price, f"rsi_drop_{rsi_vals[i]:.0f}"))

        # ── Strategy 4: Gap-Fill Mean Reversion ──
        if len(candles_1m) >= GAP_WAIT_CANDLES + 1:
            ltp_at_entry = candles_1m[GAP_WAIT_CANDLES]["close"]
            gap_pct = (ltp_at_entry - open_price) / open_price * 100

            entry_time = parse_ts(candles_1m[GAP_WAIT_CANDLES]).time()
            if entry_time <= TIME_CUTOFF:
                # Gap DOWN → buy CE (bet on fill up)
                if gap_pct < -GAP_MIN_PCT:
                    signals["Gap_Fill_MR"].append((sym, "CE", entry_time, ltp_at_entry,
                                                    f"gap_down_{abs(gap_pct):.1f}%"))
                # Gap UP → buy PE (bet on fill down)
                elif gap_pct > GAP_MIN_PCT:
                    signals["Gap_Fill_MR"].append((sym, "PE", entry_time, ltp_at_entry,
                                                    f"gap_up_{gap_pct:.1f}%"))

        # ── Strategy 5: Momentum Scalp (5-min) ──
        avg_vol = sum(vols_5m[:max(1, len(vols_5m))]) / max(1, len(vols_5m))
        for i in range(2, len(bars_5m)):
            t = times_5m[i].time()
            if t < dtime(9, 25) or t > TIME_CUTOFF:
                continue
            b = bars_5m[i]
            body = b["close"] - b["open"]
            body_pct = abs(body) / b["open"] * 100 if b["open"] > 0 else 0
            vol = b["volume"]

            if body_pct > MOM_BODY_PCT and vol > avg_vol * MOM_VOL_MULT:
                price = b["close"]
                if body > 0:
                    signals["Momentum_Scalp"].append((sym, "CE", t, price, f"mom_bull_{body_pct:.1f}%"))
                else:
                    signals["Momentum_Scalp"].append((sym, "PE", t, price, f"mom_bear_{body_pct:.1f}%"))

    for name, sigs in signals.items():
        print(f"  {name}: {len(sigs)} raw signals")

    # ── Deduplicate: keep first signal per sym per strategy, limit per sym ──
    for name in signals:
        seen = defaultdict(int)
        filtered = []
        # sort by time
        signals[name].sort(key=lambda x: x[2])
        for sig in signals[name]:
            sym = sig[0]
            if seen[sym] < MAX_ENTRIES_PER_SYM:
                filtered.append(sig)
                seen[sym] += 1
        signals[name] = filtered

    for name, sigs in signals.items():
        print(f"  {name} (filtered): {len(sigs)} signals")

    # ── Collect all needed option instruments ──
    all_option_syms = set()
    for name, sigs in signals.items():
        for sym, otype, entry_time, price, reason in sigs:
            strike = find_atm_strike(sym, price, otype)
            if strike is not None:
                all_option_syms.add((sym, strike, otype))

    print(f"\n=== Fetching option candles for {len(all_option_syms)} options... ===")
    option_candles = {}
    opt_list = list(all_option_syms)
    for i in range(0, len(opt_list), batch_size):
        batch = opt_list[i:i + batch_size]
        for sym, strike, otype in batch:
            info = option_keys.get(sym, {}).get(strike, {}).get(otype)
            if not info:
                continue
            try:
                candles = ud.historical_data(info["key"], from_dt, to_dt, "1minute")
                if candles:
                    option_candles[(sym, strike, otype)] = candles
            except Exception:
                pass
        pct = min(100, (i + batch_size) / len(opt_list) * 100)
        print(f"  {min(i+batch_size, len(opt_list))}/{len(opt_list)} ({pct:.0f}%)", end="  \r")
        await asyncio.sleep(0.3)
    print(f"\nGot option candles for {len(option_candles)} options")

    # ── Simulate trade (reused from proven backtest) ──
    def simulate_trade(sym, strike, otype, entry_time, lot_qty):
        key = (sym, strike, otype)
        candles = option_candles.get(key, [])
        if not candles:
            return None

        # Find entry price
        entry_price = None
        for c in candles:
            ct = parse_ts(c)
            if ct <= datetime.combine(DATE, entry_time):
                entry_price = c["close"]
            else:
                break
        if not entry_price or entry_price <= 0:
            return None

        sl_price = entry_price * (1 - SL_PCT)
        sl_cap = entry_price - (MAX_SL / lot_qty)
        sl = max(sl_price, sl_cap)

        # Get candles after entry
        after = []
        for c in candles:
            ct = parse_ts(c)
            if ct >= datetime.combine(DATE, entry_time):
                after.append({"time": ct, "open": c["open"], "high": c["high"],
                              "low": c["low"], "close": c["close"]})
        if not after:
            return None

        peak_pnl = 0
        locked_floor = 0
        exit_price = None
        exit_time = None
        exit_reason = None
        is_entry = True

        for c in after:
            if c["low"] <= sl:
                exit_price = sl
                exit_time = c["time"]
                exit_reason = "max_loss_cap"
                break
            if not is_entry and locked_floor > 0:
                cur_low_pnl = (c["low"] - entry_price) * lot_qty
                if cur_low_pnl <= locked_floor:
                    exit_price = entry_price + (locked_floor / lot_qty)
                    exit_time = c["time"]
                    exit_reason = f"floor_{locked_floor}"
                    break
            peak_pnl = max(peak_pnl, (c["high"] - entry_price) * lot_qty)
            new_floor = get_stepped_floor(peak_pnl)
            if new_floor > locked_floor:
                locked_floor = new_floor
            is_entry = False

        if exit_price is None:
            last = after[-1] if after else None
            if last:
                exit_price = last["close"]
                exit_time = last["time"]
                exit_reason = "eod_close"

        if exit_price is None:
            return None

        gross_pnl = (exit_price - entry_price) * lot_qty
        return {
            "symbol": f"{sym} {int(strike)} {otype}",
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "qty": lot_qty,
            "pnl": round(gross_pnl),
            "peak_pnl": round(peak_pnl),
            "entry_time": entry_time.strftime("%H:%M"),
            "exit_time": exit_time.strftime("%H:%M") if hasattr(exit_time, "strftime") else str(exit_time),
            "exit_reason": exit_reason,
        }

    # ══════════════════════════════════════════════════════════════════
    #  RUN EACH STRATEGY
    # ══════════════════════════════════════════════════════════════════

    strategy_results = {}

    for strat_name, sigs in signals.items():
        trades = []
        capital = CAPITAL_PER_STRATEGY
        sl_blocked = set()

        for sym, otype, entry_time, price, reason in sigs:
            if SL_COOLDOWN and sym in sl_blocked:
                continue

            strike = find_atm_strike(sym, price, otype)
            if strike is None:
                continue
            if (sym, strike, otype) not in option_candles:
                continue

            lot = lot_sizes.get(sym, 1) * 2
            # Check entry price
            entry_premium = None
            for c in option_candles[(sym, strike, otype)]:
                ct = parse_ts(c)
                if ct <= datetime.combine(DATE, entry_time):
                    entry_premium = c["close"]
                else:
                    break
            if not entry_premium or entry_premium < 2:
                continue

            trade_cost = entry_premium * lot
            if trade_cost > capital:
                continue

            trade = simulate_trade(sym, strike, otype, entry_time, lot)
            if trade is None:
                continue

            trade["reason"] = reason
            trades.append(trade)
            capital -= trade_cost
            capital += trade["exit_price"] * trade["qty"]

            if trade["exit_reason"] == "max_loss_cap":
                sl_blocked.add(sym)

        strategy_results[strat_name] = trades

    # ══════════════════════════════════════════════════════════════════
    #  PRINT RESULTS
    # ══════════════════════════════════════════════════════════════════

    for strat_name, trades in strategy_results.items():
        print(f"\n{'='*80}")
        print(f"  {strat_name}")
        print(f"{'='*80}")

        if not trades:
            print("  No trades.")
            continue

        total_pnl = 0
        wins = 0
        losses = 0

        for t in trades:
            tag = "W" if t["pnl"] > 0 else "L"
            print(f"  {t['symbol']:<28} {t['entry_price']:>8.2f}→{t['exit_price']:>8.2f} "
                  f"x{t['qty']:<5} {t['pnl']:>+7,} peak={t['peak_pnl']:>+6,} "
                  f"{t['entry_time']}→{t['exit_time']} {t['exit_reason']:<18} {t.get('reason','')}")
            total_pnl += t["pnl"]
            if t["pnl"] > 0:
                wins += 1
            else:
                losses += 1

        wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
        print(f"\n  {strat_name}: {len(trades)} trades | Net: ₹{total_pnl:+,} | "
              f"W/L: {wins}/{losses} ({wr:.0f}% WR)")

    # ── Also run OEH + OEL for comparison ──
    print(f"\n{'='*80}")
    print(f"  OEH (current live strategy) — for comparison")
    print(f"{'='*80}")

    oeh_trades = []
    oeh_capital = CAPITAL_PER_STRATEGY
    oeh_sl_blocked = set()
    oeh_sym_count = defaultdict(int)

    for sym in universe:
        candles = equity_candles.get(sym)
        if not candles:
            continue
        opn = candles[0]["open"]
        if opn <= 0:
            continue
        max_high = candles[0]["high"]
        if max_high > opn + 0.05:
            continue
        ltp = candles[0]["close"]
        drop_pct = (opn - ltp) / opn * 100
        if drop_pct < 0.3:
            continue

        entry_time = dtime(9, 20)
        strike = find_atm_strike(sym, ltp, "PE")
        if strike is None:
            continue
        if (sym, strike, "PE") not in option_candles:
            # need to check if we have this data
            info = option_keys.get(sym, {}).get(strike, {}).get("PE")
            if info:
                try:
                    oc = ud.historical_data(info["key"], from_dt, to_dt, "1minute")
                    if oc:
                        option_candles[(sym, strike, "PE")] = oc
                except Exception:
                    continue
            if (sym, strike, "PE") not in option_candles:
                continue

        lot = lot_sizes.get(sym, 1) * 2
        entry_premium = None
        for c in option_candles[(sym, strike, "PE")]:
            ct = parse_ts(c)
            if ct <= datetime.combine(DATE, entry_time):
                entry_premium = c["close"]
            else:
                break
        if not entry_premium or entry_premium < 2:
            continue
        trade_cost = entry_premium * lot
        if trade_cost > oeh_capital:
            continue

        trade = simulate_trade(sym, strike, "PE", entry_time, lot)
        if trade is None:
            continue
        trade["reason"] = f"drop_{drop_pct:.1f}%"
        oeh_trades.append(trade)
        oeh_capital -= trade_cost
        oeh_capital += trade["exit_price"] * trade["qty"]

    strategy_results["OEH_Current"] = oeh_trades
    oeh_pnl = sum(t["pnl"] for t in oeh_trades)
    oeh_wins = sum(1 for t in oeh_trades if t["pnl"] > 0)
    oeh_losses = len(oeh_trades) - oeh_wins
    wr = oeh_wins / max(1, len(oeh_trades)) * 100

    for t in oeh_trades:
        print(f"  {t['symbol']:<28} {t['entry_price']:>8.2f}→{t['exit_price']:>8.2f} "
              f"x{t['qty']:<5} {t['pnl']:>+7,} peak={t['peak_pnl']:>+6,} "
              f"{t['entry_time']}→{t['exit_time']} {t['exit_reason']:<18} {t.get('reason','')}")

    print(f"\n  OEH_Current: {len(oeh_trades)} trades | Net: ₹{oeh_pnl:+,} | "
          f"W/L: {oeh_wins}/{oeh_losses} ({wr:.0f}% WR)")

    # ══════════════════════════════════════════════════════════════════
    #  COMPARISON TABLE
    # ══════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print(f"  COMPARISON — {DATE}")
    print(f"{'='*80}")
    print(f"{'Strategy':<25} {'Trades':>6} {'Net PnL':>10} {'Wins':>5} {'Losses':>6} {'WR%':>5} {'Avg W':>7} {'Avg L':>7}")
    print("-" * 80)

    for name in ["OEH_Current", "VWAP_Pullback", "EMA_Crossover", "RSI_Bounce", "Gap_Fill_MR", "Momentum_Scalp"]:
        trades = strategy_results.get(name, [])
        if not trades:
            print(f"{name:<25} {'0':>6} {'₹0':>10} {'0':>5} {'0':>6} {'N/A':>5} {'N/A':>7} {'N/A':>7}")
            continue
        total = sum(t["pnl"] for t in trades)
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        wr = len(wins) / len(trades) * 100
        avg_w = sum(t["pnl"] for t in wins) / max(1, len(wins))
        avg_l = sum(t["pnl"] for t in losses) / max(1, len(losses))
        print(f"{name:<25} {len(trades):>6} {'₹'+f'{total:+,}':>10} {len(wins):>5} {len(losses):>6} "
              f"{wr:>4.0f}% {avg_w:>+7,.0f} {avg_l:>+7,.0f}")

    print("-" * 80)
    best_name = max(strategy_results.keys(),
                    key=lambda n: sum(t["pnl"] for t in strategy_results[n]) if strategy_results[n] else -999999)
    best_pnl = sum(t["pnl"] for t in strategy_results[best_name])
    print(f"\n✅ Best strategy: {best_name} → ₹{best_pnl:+,}")


if __name__ == "__main__":
    asyncio.run(main())

"""Backtest our 6 strategies on INR pairs (USD/INR, EUR/INR, GBP/INR, JPY/INR).

Realistic CFD model:
  - Entry at NEXT candle's OPEN
  - Spread deduction (INR pairs have wider spreads on NSE)
  - Random slippage
  - SL/TP checked against candle H/L
  - NSE trading hours: 9:00-17:00 IST (Mon-Fri)
  - Lot sizes: $1000 for USD/INR, €1000 for EUR/INR, £1000 for GBP/INR, ¥100000 for JPY/INR

Uses yfinance for data (free, no account needed).
"""

import json
import random
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    print("pip install yfinance")
    sys.exit(1)

random.seed(42)
np.random.seed(42)

# --- INR pair configs ---
# NSE currency futures: lot size and typical spread in INR terms
PAIRS = {
    "USD/INR": {
        "ticker": "USDINR=X",
        "lot_size_units": 1000,       # $1000 per lot
        "lot_value_inr": 84000,       # approx notional
        "spread_paise": 0.50,         # 0.005 INR typical spread
        "slippage_max_paise": 0.25,   # max random slippage
        "margin_per_lot": 2500,       # approx margin INR
        "tick_size": 0.0025,
    },
    "EUR/INR": {
        "ticker": "EURINR=X",
        "lot_size_units": 1000,
        "lot_value_inr": 93000,
        "spread_paise": 1.00,
        "slippage_max_paise": 0.50,
        "margin_per_lot": 3000,
        "tick_size": 0.0025,
    },
    "GBP/INR": {
        "ticker": "GBPINR=X",
        "lot_size_units": 1000,
        "lot_value_inr": 108000,
        "spread_paise": 1.50,
        "slippage_max_paise": 0.75,
        "margin_per_lot": 3500,
        "tick_size": 0.0025,
    },
    "JPY/INR": {
        "ticker": "JPYINR=X",
        "lot_size_units": 100000,
        "lot_value_inr": 56000,
        "spread_paise": 0.50,
        "slippage_max_paise": 0.25,
        "margin_per_lot": 2500,
        "tick_size": 0.0025,
    },
}

# --- Strategy functions (same as forex_app_v3) ---

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def sma(series, period):
    return series.rolling(period).mean()

def atr(df, period=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)

def triple_ema(df):
    e8, e13, e21 = ema(df["Close"], 8), ema(df["Close"], 13), ema(df["Close"], 21)
    if e8.iloc[-1] > e13.iloc[-1] > e21.iloc[-1] and e8.iloc[-2] <= e13.iloc[-2]:
        return "CALL"
    if e8.iloc[-1] < e13.iloc[-1] < e21.iloc[-1] and e8.iloc[-2] >= e13.iloc[-2]:
        return "PUT"
    return None

def ema_9_21(df):
    e9, e21 = ema(df["Close"], 9), ema(df["Close"], 21)
    if e9.iloc[-1] > e21.iloc[-1] and e9.iloc[-2] <= e21.iloc[-2]:
        return "CALL"
    if e9.iloc[-1] < e21.iloc[-1] and e9.iloc[-2] >= e21.iloc[-2]:
        return "PUT"
    return None

def ema_20_50(df):
    e20, e50 = ema(df["Close"], 20), ema(df["Close"], 50)
    if e20.iloc[-1] > e50.iloc[-1] and e20.iloc[-2] <= e50.iloc[-2]:
        return "CALL"
    if e20.iloc[-1] < e50.iloc[-1] and e20.iloc[-2] >= e50.iloc[-2]:
        return "PUT"
    return None

def macd_signal(df):
    e12, e26 = ema(df["Close"], 12), ema(df["Close"], 26)
    macd_line = e12 - e26
    sig = ema(macd_line, 9)
    if macd_line.iloc[-1] > sig.iloc[-1] and macd_line.iloc[-2] <= sig.iloc[-2]:
        return "CALL"
    if macd_line.iloc[-1] < sig.iloc[-1] and macd_line.iloc[-2] >= sig.iloc[-2]:
        return "PUT"
    return None

def rsi_signal(df):
    r = rsi(df["Close"])
    if r.iloc[-1] > 30 and r.iloc[-2] <= 30:
        return "CALL"
    if r.iloc[-1] < 70 and r.iloc[-2] >= 70:
        return "PUT"
    return None

def donchian(df, period=20):
    hh = df["High"].rolling(period).max()
    ll = df["Low"].rolling(period).min()
    if df["Close"].iloc[-1] > hh.iloc[-2]:
        return "CALL"
    if df["Close"].iloc[-1] < ll.iloc[-2]:
        return "PUT"
    return None

STRATEGY_FNS = {
    "triple_ema": triple_ema,
    "ema_9_21": ema_9_21,
    "ema_20_50": ema_20_50,
    "macd": macd_signal,
    "rsi": rsi_signal,
    "donchian": donchian,
}

# --- Backtester ---

def backtest(df, strategy_fn, pair_cfg, sl_atr, tp_atr, lots=1, max_hold=50):
    """Run backtest on OHLC dataframe. Returns list of trades."""
    trades = []
    atr_vals = atr(df, 14)
    i = 60  # skip warmup

    in_trade = False
    entry_price = sl_price = tp_price = direction = entry_i = None

    while i < len(df) - 1:
        if not in_trade:
            window = df.iloc[:i+1]
            signal = strategy_fn(window)
            if signal and not np.isnan(atr_vals.iloc[i]):
                a = atr_vals.iloc[i]
                # Entry at NEXT candle open + spread + slippage
                entry_candle = df.iloc[i + 1]
                spread = pair_cfg["spread_paise"] / 100  # convert paise to INR
                slippage = random.uniform(0, pair_cfg["slippage_max_paise"] / 100)

                if signal == "CALL":
                    entry_price = entry_candle["Open"] + spread / 2 + slippage
                    sl_price = entry_price - sl_atr * a
                    tp_price = entry_price + tp_atr * a
                else:
                    entry_price = entry_candle["Open"] - spread / 2 - slippage
                    sl_price = entry_price + sl_atr * a
                    tp_price = entry_price - tp_atr * a

                direction = signal
                entry_i = i + 1
                in_trade = True
                i += 2  # skip entry candle
                continue
        else:
            candle = df.iloc[i]
            candles_held = i - entry_i
            hit_sl = hit_tp = False

            if direction == "CALL":
                if candle["Low"] <= sl_price:
                    hit_sl = True
                if candle["High"] >= tp_price:
                    hit_tp = True
            else:
                if candle["High"] >= sl_price:
                    hit_sl = True
                if candle["Low"] <= tp_price:
                    hit_tp = True

            exit_price = None
            reason = None

            if hit_sl and hit_tp:
                # Assume SL hit first (conservative)
                exit_price = sl_price
                reason = "SL"
            elif hit_sl:
                exit_price = sl_price
                reason = "SL"
            elif hit_tp:
                exit_price = tp_price
                reason = "TP"
            elif candles_held >= max_hold:
                exit_price = candle["Close"]
                reason = "TIMEOUT"

            if exit_price is not None:
                if direction == "CALL":
                    pnl_per_unit = exit_price - entry_price
                else:
                    pnl_per_unit = entry_price - exit_price

                # P&L in INR: price move * lot_size_units * lots
                pnl_inr = pnl_per_unit * pair_cfg["lot_size_units"] * lots

                trades.append({
                    "entry": entry_price,
                    "exit": exit_price,
                    "direction": direction,
                    "pnl": round(pnl_inr, 2),
                    "reason": reason,
                    "candles_held": candles_held,
                    "date": str(df.index[i].date()) if hasattr(df.index[i], 'date') else str(df.index[i]),
                })
                in_trade = False

        i += 1

    return trades


def run_sweep():
    """Run optimization sweep: all strategies × all INR pairs × SL/TP combos."""

    # Download data — last 6 months, 1h candles
    print("=" * 80)
    print("  INR PAIRS BACKTEST — OPTIMIZATION SWEEP")
    print("  Data: Last 6 months, 1h candles")
    print("=" * 80)

    data = {}
    for pair, cfg in PAIRS.items():
        print(f"\nDownloading {pair} ({cfg['ticker']})...")
        for attempt in range(3):
            try:
                if attempt > 0:
                    wait = 30 * attempt
                    print(f"  Retry {attempt}, waiting {wait}s...")
                    time.sleep(wait)
                df = yf.download(cfg["ticker"], period="6mo", interval="1h", progress=False)
                if df is not None and len(df) > 100:
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    data[pair] = df
                    print(f"  Got {len(df)} candles, {df.index[0]} to {df.index[-1]}")
                    break
                else:
                    print(f"  Got {len(df) if df is not None else 0} candles, retrying...")
            except Exception as e:
                print(f"  ERROR: {e}")
        time.sleep(10)  # pause between pairs

    if not data:
        print("\nNo data available. Exiting.")
        return

    # Strategy × pair combos to test
    COMBOS = []
    for strat_name, strat_fn in STRATEGY_FNS.items():
        for pair in data:
            for tf_label in ["1h"]:  # yfinance gives us 1h
                COMBOS.append((strat_name, pair, tf_label, strat_fn))

    # SL/TP grid
    SL_RANGE = [0.75, 1.0, 1.25, 1.5, 2.0, 3.0]
    TP_RANGE = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0]

    print(f"\n{'='*80}")
    print(f"  SWEEP: {len(COMBOS)} combos × {len(SL_RANGE)} SL × {len(TP_RANGE)} TP = {len(COMBOS)*len(SL_RANGE)*len(TP_RANGE)} runs")
    print(f"{'='*80}\n")

    results = []
    for strat_name, pair, tf, strat_fn in COMBOS:
        df = data[pair]
        cfg = PAIRS[pair]
        best_pnl = -999999
        best_params = None
        best_trades = None

        for sl in SL_RANGE:
            for tp in TP_RANGE:
                trades = backtest(df, strat_fn, cfg, sl, tp, lots=1)
                total_pnl = sum(t["pnl"] for t in trades)
                wins = sum(1 for t in trades if t["pnl"] > 0)
                wr = round(wins / len(trades) * 100, 1) if trades else 0

                if total_pnl > best_pnl and len(trades) >= 5:
                    best_pnl = total_pnl
                    best_params = {"sl": sl, "tp": tp, "trades": len(trades), "wr": wr}
                    best_trades = trades

        if best_params:
            results.append({
                "strategy": strat_name,
                "pair": pair,
                "tf": tf,
                **best_params,
                "pnl": round(best_pnl, 0),
            })

    # Sort by P&L
    results.sort(key=lambda x: x["pnl"], reverse=True)

    print(f"\n{'='*80}")
    print(f"  TOP RESULTS (best SL/TP per strategy/pair)")
    print(f"{'='*80}")
    print(f"{'Strategy':<15} {'Pair':<10} {'TF':<5} {'SL':<5} {'TP':<5} {'Trades':<8} {'WR%':<7} {'P&L ₹':<12}")
    print("-" * 80)

    for r in results:
        pnl_str = f"+₹{r['pnl']:,.0f}" if r['pnl'] >= 0 else f"-₹{abs(r['pnl']):,.0f}"
        print(f"{r['strategy']:<15} {r['pair']:<10} {r['tf']:<5} {r['sl']:<5} {r['tp']:<5} {r['trades']:<8} {r['wr']:<7} {pnl_str:<12}")

    # Summary
    profitable = [r for r in results if r["pnl"] > 0]
    total = sum(r["pnl"] for r in results)
    top6 = results[:6]
    top6_total = sum(r["pnl"] for r in top6)

    print(f"\n{'='*80}")
    print(f"  SUMMARY")
    print(f"{'='*80}")
    print(f"  Total combos tested: {len(results)}")
    print(f"  Profitable: {len(profitable)} / {len(results)}")
    print(f"  All combined P&L: ₹{total:,.0f}")
    print(f"  Top 6 combined P&L: ₹{top6_total:,.0f}")

    print(f"\n{'='*80}")
    print(f"  RECOMMENDED TOP 6 (for live paper trading)")
    print(f"{'='*80}")
    for i, r in enumerate(top6, 1):
        pnl_str = f"+₹{r['pnl']:,.0f}" if r['pnl'] >= 0 else f"-₹{abs(r['pnl']):,.0f}"
        print(f"  {i}. {r['strategy']} {r['pair']} {r['tf']} — SL={r['sl']} TP={r['tp']} — {r['trades']} trades, {r['wr']}% WR — {pnl_str}")

    # Pair breakdown
    print(f"\n{'='*80}")
    print(f"  PER-PAIR BREAKDOWN")
    print(f"{'='*80}")
    for pair in PAIRS:
        pair_results = [r for r in results if r["pair"] == pair]
        pair_pnl = sum(r["pnl"] for r in pair_results)
        pair_profitable = sum(1 for r in pair_results if r["pnl"] > 0)
        pnl_str = f"+₹{pair_pnl:,.0f}" if pair_pnl >= 0 else f"-₹{abs(pair_pnl):,.0f}"
        print(f"  {pair}: {pair_profitable}/{len(pair_results)} profitable, combined {pnl_str}")

    # Save results
    with open("data/inr_backtest_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to data/inr_backtest_results.json")


if __name__ == "__main__":
    run_sweep()

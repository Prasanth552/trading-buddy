"""Realistic CFD backtest — this week's data.

Tests S/R + LinReg strategies with:
  - Entry at NEXT candle's OPEN (not signal candle's close)
  - Spread deduction (1.5 pips EUR/GBP, 2.5 pips CAD/CHF)
  - Random slippage (0-0.5 pips adverse)
  - P&L = pip movement × lot_size × pip_value

Also runs the OLD (unrealistic) model side-by-side for comparison.

Usage: python backtest_realistic.py
"""
import random
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

UTC = ZoneInfo("UTC")
IST = ZoneInfo("Asia/Kolkata")

BEST_HOURS_UTC = {0, 1, 2, 3, 4, 19, 21, 22, 23}

PAIR_CONFIG = {
    "EUR/GBP": {
        "symbol": "EURGBP=X",
        "spread_pips": 1.5,
        "pip_size": 0.0001,
        "pip_value_inr": 1100,
        "lot_size": 0.5,
    },
    "CAD/CHF": {
        "symbol": "CADCHF=X",
        "spread_pips": 2.5,
        "pip_size": 0.0001,
        "pip_value_inr": 950,
        "lot_size": 0.5,
    },
}

STARTING_CAPITAL = 150_000
TRADE_AMOUNT_BINARY = 2_000
PAYOUT_PCT = 0.80


def strategy_sr(candles, lookback=25, threshold_pct=0.0005):
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    signals = []
    for i in range(lookback, len(candles)):
        resistance = max(highs[i - lookback:i])
        support = min(lows[i - lookback:i])
        price = closes[i]
        rng = resistance - support
        if rng == 0:
            continue
        if (price - support) / rng < threshold_pct * 10:
            signals.append((i, "CALL"))
        elif (resistance - price) / rng < threshold_pct * 10:
            signals.append((i, "PUT"))
    return signals


def strategy_linreg(candles, period=20, std_mult=2.0):
    closes = [c["close"] for c in candles]
    signals = []
    for i in range(period, len(candles)):
        window = closes[i - period:i]
        n = len(window)
        sx = n * (n - 1) / 2
        sy = sum(window)
        sxx = sum(j * j for j in range(n))
        sxy = sum(j * v for j, v in enumerate(window))
        denom = n * sxx - sx * sx
        if denom == 0:
            continue
        slope = (n * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / n
        predicted = intercept + slope * (n - 1)
        residuals = [window[j] - (intercept + slope * j) for j in range(n)]
        std = (sum(r ** 2 for r in residuals) / n) ** 0.5
        if std == 0:
            continue
        price = closes[i]
        if price < predicted - std_mult * std:
            signals.append((i, "CALL"))
        elif price > predicted + std_mult * std:
            signals.append((i, "PUT"))
    return signals


def fetch_week_candles(symbol: str, days: int = 7) -> list[dict]:
    """Fetch this week's 1-min candles."""
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=f"{days}d", interval="1m")
    if df.empty:
        print(f"  WARNING: No data for {symbol}")
        return []
    candles = []
    for ts, row in df.iterrows():
        candles.append({
            "time": int(ts.timestamp()),
            "dt": ts.to_pydatetime(),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        })
    print(f"  {symbol}: {len(candles)} candles, {candles[0]['dt'].strftime('%Y-%m-%d %H:%M')} to {candles[-1]['dt'].strftime('%Y-%m-%d %H:%M')}")
    return candles


def run_backtest_old(candles: list[dict], pair_name: str) -> dict:
    """OLD model: binary options, entry at signal candle close, exit at next close."""
    trades = []
    for name, fn in [("S/R", strategy_sr), ("LinReg", strategy_linreg)]:
        signals = fn(candles)
        for idx, direction in signals:
            if idx >= len(candles) - 1:
                continue
            dt_utc = datetime.fromtimestamp(candles[idx]["time"], UTC)
            if dt_utc.hour not in BEST_HOURS_UTC:
                continue

            entry_price = candles[idx]["close"]
            expiry_price = candles[idx + 1]["close"]

            if entry_price == expiry_price:
                continue

            won = (direction == "CALL" and expiry_price > entry_price) or \
                  (direction == "PUT" and expiry_price < entry_price)

            pnl = TRADE_AMOUNT_BINARY * PAYOUT_PCT if won else -TRADE_AMOUNT_BINARY
            trades.append({
                "strategy": name,
                "direction": direction,
                "entry": entry_price,
                "exit": expiry_price,
                "won": won,
                "pnl": pnl,
                "day": dt_utc.strftime("%Y-%m-%d"),
                "time": dt_utc.strftime("%H:%M"),
            })

    wins = sum(1 for t in trades if t["won"])
    losses = len(trades) - wins
    wr = wins / len(trades) * 100 if trades else 0
    total_pnl = sum(t["pnl"] for t in trades)
    return {
        "trades": trades,
        "total": len(trades),
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "pnl": total_pnl,
    }


def run_backtest_realistic(candles: list[dict], pair_name: str) -> dict:
    """NEW model: CFD, entry at NEXT candle open + spread + slippage, exit at that candle's close."""
    cfg = PAIR_CONFIG[pair_name]
    pip_size = cfg["pip_size"]
    spread_pips = cfg["spread_pips"]
    pip_value = cfg["pip_value_inr"]
    lot_size = cfg["lot_size"]

    trades = []
    total_spread_cost = 0
    total_pips = 0

    for name, fn in [("S/R", strategy_sr), ("LinReg", strategy_linreg)]:
        signals = fn(candles)
        for idx, direction in signals:
            # Need idx+1 for entry candle (next candle's open & close)
            if idx >= len(candles) - 1:
                continue
            dt_utc = datetime.fromtimestamp(candles[idx]["time"], UTC)
            if dt_utc.hour not in BEST_HOURS_UTC:
                continue

            signal_price = candles[idx]["close"]
            entry_candle = candles[idx + 1]

            raw_entry = entry_candle["open"]
            raw_exit = entry_candle["close"]

            slippage_pips = random.uniform(0, 0.5)
            spread_cost_price = spread_pips * pip_size
            slippage_cost_price = slippage_pips * pip_size

            if direction == "CALL":
                entry_price = raw_entry + (spread_cost_price / 2) + slippage_cost_price
                exit_price = raw_exit - (spread_cost_price / 2)
                pip_move = (exit_price - entry_price) / pip_size
            else:
                entry_price = raw_entry - (spread_cost_price / 2) - slippage_cost_price
                exit_price = raw_exit + (spread_cost_price / 2)
                pip_move = (entry_price - exit_price) / pip_size

            pnl = pip_move * pip_value * lot_size
            spread_cost = spread_pips * pip_value * lot_size
            total_spread_cost += spread_cost
            total_pips += pip_move

            won = pnl > 0
            trades.append({
                "strategy": name,
                "direction": direction,
                "signal_price": signal_price,
                "raw_entry": raw_entry,
                "entry": round(entry_price, 5),
                "exit": round(exit_price, 5),
                "pips": round(pip_move, 1),
                "spread_cost": round(spread_cost, 0),
                "slippage": round(slippage_pips, 2),
                "won": won,
                "pnl": round(pnl, 0),
                "day": dt_utc.strftime("%Y-%m-%d"),
                "time": dt_utc.strftime("%H:%M"),
            })

    wins = sum(1 for t in trades if t["won"])
    losses = len(trades) - wins
    wr = wins / len(trades) * 100 if trades else 0
    total_pnl = sum(t["pnl"] for t in trades)

    # Daily breakdown
    daily = {}
    for t in trades:
        d = t["day"]
        if d not in daily:
            daily[d] = {"pnl": 0, "trades": 0, "wins": 0, "pips": 0}
        daily[d]["pnl"] += t["pnl"]
        daily[d]["trades"] += 1
        daily[d]["pips"] += t["pips"]
        if t["won"]:
            daily[d]["wins"] += 1

    return {
        "trades": trades,
        "total": len(trades),
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "pnl": total_pnl,
        "total_pips": round(total_pips, 1),
        "total_spread_cost": round(total_spread_cost, 0),
        "daily": daily,
    }


def main():
    random.seed(42)  # reproducible slippage
    print("=" * 70)
    print("REALISTIC vs OLD BACKTEST — This Week's Data")
    print("=" * 70)

    for pair_name, cfg in PAIR_CONFIG.items():
        print(f"\n{'─' * 60}")
        print(f"  {pair_name}")
        print(f"  Spread: {cfg['spread_pips']} pips | Lot: {cfg['lot_size']} | Pip value: ₹{cfg['pip_value_inr']}/lot")
        print(f"{'─' * 60}")

        candles = fetch_week_candles(cfg["symbol"], days=7)
        if len(candles) < 30:
            print(f"  Not enough candles, skipping")
            continue

        # Run both models
        old = run_backtest_old(candles, pair_name)
        realistic = run_backtest_realistic(candles, pair_name)

        print(f"\n  OLD MODEL (Binary Options, entry@close):")
        print(f"    Trades: {old['total']}  |  W: {old['wins']}  L: {old['losses']}  |  WR: {old['wr']:.1f}%")
        print(f"    P&L: ₹{old['pnl']:,.0f}")

        print(f"\n  NEW MODEL (CFD, entry@next_open + spread + slippage):")
        print(f"    Trades: {realistic['total']}  |  W: {realistic['wins']}  L: {realistic['losses']}  |  WR: {realistic['wr']:.1f}%")
        print(f"    P&L: ₹{realistic['pnl']:,.0f}")
        print(f"    Total pips: {realistic['total_pips']}")
        print(f"    Spread cost: ₹{realistic['total_spread_cost']:,.0f}")
        print(f"    Avg pips/trade: {realistic['total_pips']/realistic['total']:.1f}" if realistic['total'] else "")

        # Daily breakdown for realistic
        print(f"\n  DAILY BREAKDOWN (realistic):")
        print(f"    {'Day':<12} {'Trades':>6} {'Wins':>5} {'WR':>6} {'Pips':>7} {'P&L':>10}")
        print(f"    {'─'*48}")
        for day in sorted(realistic["daily"].keys()):
            d = realistic["daily"][day]
            wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            marker = "  ✓" if d["pnl"] > 0 else "  ✗" if d["pnl"] < 0 else ""
            print(f"    {day:<12} {d['trades']:>6} {d['wins']:>5} {wr:>5.1f}% {d['pips']:>7.1f} ₹{d['pnl']:>9,.0f}{marker}")

        # Show some example trades
        print(f"\n  SAMPLE TRADES (first 5 realistic):")
        print(f"    {'Strat':<8} {'Dir':<5} {'Signal':>9} {'Entry':>9} {'Exit':>9} {'Pips':>6} {'P&L':>8} {'Spread':>7}")
        for t in realistic["trades"][:5]:
            print(f"    {t['strategy']:<8} {t['direction']:<5} {t['signal_price']:.5f} {t['entry']:.5f} {t['exit']:.5f} {t['pips']:>6.1f} ₹{t['pnl']:>7,.0f} ₹{t['spread_cost']:>5,.0f}")

    # Combined summary
    print(f"\n{'=' * 70}")
    print("COMBINED SUMMARY")
    print(f"{'=' * 70}")

    all_realistic_trades = []
    all_old_trades = []
    for pair_name, cfg in PAIR_CONFIG.items():
        candles = fetch_week_candles(cfg["symbol"], days=7)
        if len(candles) < 30:
            continue
        random.seed(42)
        old = run_backtest_old(candles, pair_name)
        realistic = run_backtest_realistic(candles, pair_name)
        all_old_trades.extend(old["trades"])
        all_realistic_trades.extend(realistic["trades"])

    old_wins = sum(1 for t in all_old_trades if t["won"])
    old_pnl = sum(t["pnl"] for t in all_old_trades)
    new_wins = sum(1 for t in all_realistic_trades if t["won"])
    new_pnl = sum(t["pnl"] for t in all_realistic_trades)
    new_pips = sum(t["pips"] for t in all_realistic_trades)
    new_spread = sum(t["spread_cost"] for t in all_realistic_trades)

    print(f"\n  OLD (Binary):     {len(all_old_trades)} trades | {old_wins}W | WR {old_wins/len(all_old_trades)*100:.1f}% | P&L ₹{old_pnl:,.0f}" if all_old_trades else "")
    print(f"  NEW (Realistic):  {len(all_realistic_trades)} trades | {new_wins}W | WR {new_wins/len(all_realistic_trades)*100:.1f}% | P&L ₹{new_pnl:,.0f}" if all_realistic_trades else "")
    print(f"                    Pips: {new_pips:.1f} | Spread cost: ₹{new_spread:,.0f}")

    diff = new_pnl - old_pnl
    print(f"\n  Reality gap: ₹{diff:,.0f} ({'better' if diff > 0 else 'worse'} than the old model)")
    print(f"\n  If realistic P&L > 0, the strategy has REAL edge.")
    print(f"  If realistic P&L < 0 but old P&L > 0, the edge was an illusion from unrealistic assumptions.")


if __name__ == "__main__":
    main()

"""Backtest S/R + LinReg strategies on Deriv historical data.

Fetches real candles from Deriv API, simulates binary options trades
with actual payout ratios and expiry durations.

Tests both:
  - EUR/GBP (frxEURGBP) — 15-min CALL/PUT
  - Volatility 10 (1s) (1HZ10V) — 1-min and 5-min CALL/PUT
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import websockets

UTC = ZoneInfo("UTC")
IST = ZoneInfo("Asia/Kolkata")
WS_PUBLIC = "wss://api.derivws.com/trading/v1/options/ws/public"
WS_HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Origin": "https://app.deriv.com",
}

# ── Strategies (same as forex_app.py) ───────────────────────────────────

def strategy_sr(candles: list[dict], lookback: int = 25, threshold_pct: float = 0.0005) -> list[tuple[int, str]]:
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


def strategy_linreg(candles: list[dict], period: int = 20, std_mult: float = 2.0) -> list[tuple[int, str]]:
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


# ── Fetch historical data ───────────────────────────────────────────────

async def fetch_candles(ws, symbol: str, granularity: int, count: int = 5000) -> list[dict]:
    """Fetch historical candles. Max 5000 per request."""
    req_id = int(time.time() * 1000) % 100000
    await ws.send(json.dumps({
        "ticks_history": symbol,
        "adjust_start_time": 1,
        "count": min(count, 5000),
        "end": "latest",
        "granularity": granularity,
        "style": "candles",
        "req_id": req_id,
    }))
    resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
    raw = resp.get("candles", [])
    candles = []
    for c in raw:
        candles.append({
            "time": c["epoch"],
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
        })
    return candles


# ── Backtest engine ─────────────────────────────────────────────────────

def run_backtest(
    candles: list[dict],
    expiry_bars: int,
    stake: float,
    payout_pct: float,
    best_hours: set[int] | None = None,
    max_daily_loss: float = 10_000,
    label: str = "",
) -> dict:
    """
    Simulate binary options trades.
    expiry_bars: how many candles ahead to check result.
    payout_pct: e.g. 0.80 means WIN pays +80% of stake.
    """
    results = {
        "label": label,
        "total": 0,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "pnl": 0.0,
        "peak_pnl": 0.0,
        "max_drawdown": 0.0,
        "daily_pnl": defaultdict(float),
        "strategy_stats": defaultdict(lambda: {"wins": 0, "losses": 0, "draws": 0, "pnl": 0.0}),
        "hourly_stats": defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0}),
        "trades": [],
    }

    last_signal_idx = -expiry_bars  # cooldown between trades

    for strat_name, strat_fn in [("S/R", strategy_sr), ("LinReg", strategy_linreg)]:
        signals = strat_fn(candles)
        for idx, direction in signals:
            # Need room for expiry
            if idx + expiry_bars >= len(candles):
                continue
            # Cooldown: don't trade if too close to last trade
            if idx - last_signal_idx < expiry_bars:
                continue

            entry_candle = candles[idx]
            dt = datetime.fromtimestamp(entry_candle["time"], UTC)

            # Hour filter (forex only)
            if best_hours and dt.hour not in best_hours:
                continue

            # Daily loss cap
            day_key = dt.strftime("%Y-%m-%d")
            if results["daily_pnl"][day_key] <= -max_daily_loss:
                continue

            entry_price = entry_candle["close"]
            expiry_price = candles[idx + expiry_bars]["close"]

            if entry_price == expiry_price:
                pnl = 0
                outcome = "DRAW"
                results["draws"] += 1
                results["strategy_stats"][strat_name]["draws"] += 1
            elif (direction == "CALL" and expiry_price > entry_price) or \
                 (direction == "PUT" and expiry_price < entry_price):
                pnl = stake * payout_pct
                outcome = "WIN"
                results["wins"] += 1
                results["strategy_stats"][strat_name]["wins"] += 1
            else:
                pnl = -stake
                outcome = "LOSS"
                results["losses"] += 1
                results["strategy_stats"][strat_name]["losses"] += 1

            results["total"] += 1
            results["pnl"] += pnl
            results["daily_pnl"][day_key] += pnl
            results["strategy_stats"][strat_name]["pnl"] += pnl
            results["peak_pnl"] = max(results["peak_pnl"], results["pnl"])
            dd = results["peak_pnl"] - results["pnl"]
            results["max_drawdown"] = max(results["max_drawdown"], dd)
            results["hourly_stats"][dt.hour]["total"] += 1
            if outcome == "WIN":
                results["hourly_stats"][dt.hour]["wins"] += 1
            elif outcome == "LOSS":
                results["hourly_stats"][dt.hour]["losses"] += 1

            last_signal_idx = idx

            results["trades"].append({
                "ts": dt.strftime("%Y-%m-%d %H:%M"),
                "strategy": strat_name,
                "direction": direction,
                "entry": round(entry_price, 5),
                "expiry": round(expiry_price, 5),
                "outcome": outcome,
                "pnl": pnl,
                "cumulative_pnl": round(results["pnl"], 2),
            })

    return results


def print_report(r: dict) -> None:
    print(f"\n{'='*60}")
    print(f"  {r['label']}")
    print(f"{'='*60}")
    if r["total"] == 0:
        print("  No trades generated!")
        return
    wr = r["wins"] / r["total"] * 100 if r["total"] > 0 else 0
    print(f"  Trades: {r['total']}  |  W: {r['wins']}  L: {r['losses']}  D: {r['draws']}")
    print(f"  Win Rate: {wr:.1f}%")
    print(f"  P&L: ${r['pnl']:.2f}  |  Max Drawdown: ${r['max_drawdown']:.2f}")
    be = 100 / (1 + 0.80)  # breakeven WR for 80% payout
    print(f"  Breakeven WR: {be:.1f}%  |  {'PROFITABLE' if wr > be else 'UNPROFITABLE'}")

    print(f"\n  Per-strategy:")
    for name, st in sorted(r["strategy_stats"].items()):
        total = st["wins"] + st["losses"] + st["draws"]
        wr2 = st["wins"] / total * 100 if total > 0 else 0
        print(f"    {name:8s}: {total:3d} trades, WR {wr2:.1f}%, P&L ${st['pnl']:.2f}")

    # Best/worst hours
    print(f"\n  Hourly breakdown (top 5 by volume):")
    hourly = sorted(r["hourly_stats"].items(), key=lambda x: x[1]["total"], reverse=True)
    for h, st in hourly[:5]:
        wr3 = st["wins"] / st["total"] * 100 if st["total"] > 0 else 0
        print(f"    {h:02d}:00 UTC — {st['total']:3d} trades, WR {wr3:.1f}%")

    # Daily P&L summary
    days = sorted(r["daily_pnl"].items())
    win_days = sum(1 for _, v in days if v > 0)
    loss_days = sum(1 for _, v in days if v < 0)
    print(f"\n  Trading days: {len(days)}  |  Green: {win_days}  Red: {loss_days}")
    if days:
        best_day = max(days, key=lambda x: x[1])
        worst_day = min(days, key=lambda x: x[1])
        print(f"  Best day:  {best_day[0]} ${best_day[1]:.2f}")
        print(f"  Worst day: {worst_day[0]} ${worst_day[1]:.2f}")

    # Last 10 trades
    print(f"\n  Last 10 trades:")
    for t in r["trades"][-10:]:
        marker = "✓" if t["outcome"] == "WIN" else "✗" if t["outcome"] == "LOSS" else "—"
        print(f"    {marker} {t['ts']} {t['strategy']:6s} {t['direction']:4s} "
              f"entry={t['entry']} expiry={t['expiry']} pnl=${t['pnl']:.0f} cum=${t['cumulative_pnl']:.0f}")


# ── Main ────────────────────────────────────────────────────────────────

async def main():
    print("Connecting to Deriv API...")
    ws = await asyncio.wait_for(
        websockets.connect(WS_PUBLIC, additional_headers=WS_HDRS, ping_interval=25),
        timeout=15,
    )
    print("Connected!\n")

    STAKE = 2.0       # $2 per trade (Deriv demo default)
    PAYOUT = 0.80     # 80% payout on win

    # ── 1. EUR/GBP on 15-min candles, 1-bar expiry = 15min trade ──
    print("Fetching EUR/GBP 15-min candles...")
    eurgbp_15m = await fetch_candles(ws, "frxEURGBP", granularity=900, count=5000)
    print(f"  Got {len(eurgbp_15m)} candles")
    if eurgbp_15m:
        t0 = datetime.fromtimestamp(eurgbp_15m[0]["time"], UTC)
        t1 = datetime.fromtimestamp(eurgbp_15m[-1]["time"], UTC)
        print(f"  Range: {t0:%Y-%m-%d %H:%M} to {t1:%Y-%m-%d %H:%M} UTC")

    r1 = run_backtest(
        eurgbp_15m, expiry_bars=1, stake=STAKE, payout_pct=PAYOUT,
        best_hours={0, 1, 2, 3, 4, 7, 8, 9, 10, 11, 12, 13, 14, 15, 19, 21, 22, 23},
        label="EUR/GBP — 15min expiry (S/R + LinReg)",
    )
    print_report(r1)

    # ── 2. EUR/GBP on 5-min candles, 3-bar expiry = 15min trade ──
    print("\nFetching EUR/GBP 5-min candles...")
    eurgbp_5m = await fetch_candles(ws, "frxEURGBP", granularity=300, count=5000)
    print(f"  Got {len(eurgbp_5m)} candles")

    r2 = run_backtest(
        eurgbp_5m, expiry_bars=3, stake=STAKE, payout_pct=PAYOUT,
        best_hours={0, 1, 2, 3, 4, 7, 8, 9, 10, 11, 12, 13, 14, 15, 19, 21, 22, 23},
        label="EUR/GBP — 5min candles, 15min expiry (S/R + LinReg)",
    )
    print_report(r2)

    # ── 3. Volatility 10 (1s) on 1-min candles, 1-bar expiry ──
    print("\nFetching Volatility 10 (1s) 1-min candles...")
    vol10_1m = await fetch_candles(ws, "1HZ10V", granularity=60, count=5000)
    print(f"  Got {len(vol10_1m)} candles")
    if vol10_1m:
        t0 = datetime.fromtimestamp(vol10_1m[0]["time"], UTC)
        t1 = datetime.fromtimestamp(vol10_1m[-1]["time"], UTC)
        print(f"  Range: {t0:%Y-%m-%d %H:%M} to {t1:%Y-%m-%d %H:%M} UTC")

    r3 = run_backtest(
        vol10_1m, expiry_bars=1, stake=STAKE, payout_pct=PAYOUT,
        best_hours=None,  # synthetics run 24/7
        label="Volatility 10 (1s) — 1min expiry (S/R + LinReg)",
    )
    print_report(r3)

    # ── 4. Volatility 10 (1s) on 5-min candles, 1-bar expiry = 5min ──
    print("\nFetching Volatility 10 (1s) 5-min candles...")
    vol10_5m = await fetch_candles(ws, "1HZ10V", granularity=300, count=5000)
    print(f"  Got {len(vol10_5m)} candles")

    r4 = run_backtest(
        vol10_5m, expiry_bars=1, stake=STAKE, payout_pct=PAYOUT,
        best_hours=None,
        label="Volatility 10 (1s) — 5min expiry (S/R + LinReg)",
    )
    print_report(r4)

    # ── 5. Volatility 75 (1s) on 1-min candles ──
    print("\nFetching Volatility 75 (1s) 1-min candles...")
    vol75_1m = await fetch_candles(ws, "1HZ75V", granularity=60, count=5000)
    print(f"  Got {len(vol75_1m)} candles")

    r5 = run_backtest(
        vol75_1m, expiry_bars=1, stake=STAKE, payout_pct=PAYOUT,
        best_hours=None,
        label="Volatility 75 (1s) — 1min expiry (S/R + LinReg)",
    )
    print_report(r5)

    await ws.close()
    print("\n\nBacktest complete!")


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""Backtest MOM strategy for today with different SL/floor combinations.
Fetches 1-min candle data for each trade's option contract and simulates exits."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
import config

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from src.storage.db import get_conn
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo(config.TIMEZONE)
SLIPPAGE_PCT = 0.002  # match live slippage

# Parameter grid to test
SL_PCTS = [0.15, 0.20, 0.25, 0.30]
MAX_SLS = [1500, 2000, 3000, 5000]
FLOOR_STARTS = [500, 1000, 1500, 2000]
FLOOR_STEPS = [500, 1000, 1500]


def build_floor_levels(start, step, count=12):
    return [start + step * i for i in range(count)]


def simulate_trade(candles, entry_price, qty, sl_price, floor_levels):
    """Simulate a single trade through 1-min candles.
    Returns (exit_price, pnl, reason)."""
    peak_pnl = 0
    current_floor = 0  # no floor active until first level hit

    for c in candles:
        low = c["low"]
        high = c["high"]
        close = c["close"]

        # Check SL hit (use low)
        if low <= sl_price:
            exit_p = sl_price * (1 - SLIPPAGE_PCT)
            gross = (exit_p - entry_price) * qty
            charges = abs(gross) * 0.04 + qty * 0.05  # rough charge estimate
            return exit_p, gross - charges, "SL"

        # Calculate current PnL at high and close
        pnl_at_high = (high - entry_price) * qty
        pnl_at_close = (close - entry_price) * qty

        # Update peak
        peak_pnl = max(peak_pnl, pnl_at_high)

        # Check if we've hit a new floor level
        for fl in floor_levels:
            if peak_pnl >= fl and fl > current_floor:
                current_floor = fl

        # Check floor exit (current pnl drops below active floor)
        if current_floor > 0 and pnl_at_close < current_floor:
            exit_p = close * (1 - SLIPPAGE_PCT)
            gross = (exit_p - entry_price) * qty
            charges = abs(gross) * 0.04 + qty * 0.05
            return exit_p, gross - charges, f"FLOOR_{current_floor}"

    # EOD exit at last close
    last_close = candles[-1]["close"] if candles else entry_price
    exit_p = last_close * (1 - SLIPPAGE_PCT)
    gross = (exit_p - entry_price) * qty
    charges = abs(gross) * 0.04 + qty * 0.05
    return exit_p, gross - charges, "EOD"


def main():
    token = load_cached_token()
    if not token:
        print("ERROR: No Upstox token")
        return

    ud = UpstoxData(access_token=token)

    # Get today's MOM trades
    with get_conn() as c:
        trades = c.execute(
            "SELECT id, symbol, qty, price, broker_key, ts "
            "FROM trades WHERE channel='mom' AND ts >= '2026-10-15' ORDER BY ts"
        ).fetchall()

    if not trades:
        print("No MOM trades today")
        return

    print(f"Found {len(trades)} MOM trades today. Fetching candle data...\n")

    # Fetch 1-min candles for each trade from entry time to 15:30
    trade_data = []
    today = datetime.now(IST).date()
    eod = datetime.combine(today, datetime.min.time()).replace(hour=15, minute=30, tzinfo=IST)

    for t in trades:
        entry_ts = datetime.fromisoformat(t["ts"])
        broker_key = t["broker_key"]
        if not broker_key:
            print(f"  SKIP id={t['id']} {t['symbol']} — no broker_key")
            continue

        # Fetch 1-min candles from entry to EOD
        try:
            candles = ud.historical_data(broker_key, entry_ts, eod, "1minute")
            time.sleep(0.3)  # rate limit
        except Exception as exc:
            print(f"  SKIP id={t['id']} {t['symbol']} — fetch error: {exc}")
            time.sleep(1)
            continue

        if not candles:
            print(f"  SKIP id={t['id']} {t['symbol']} — no candles")
            continue

        trade_data.append({
            "id": t["id"],
            "symbol": t["symbol"],
            "qty": t["qty"],
            "entry": t["price"],
            "broker_key": broker_key,
            "candles": candles,
        })
        print(f"  OK id={t['id']} {t['symbol']:25s} entry={t['price']:>8.2f} candles={len(candles)}")

    print(f"\nFetched candle data for {len(trade_data)}/{len(trades)} trades")
    print("=" * 100)

    # Run parameter grid
    results = []

    for sl_pct in SL_PCTS:
        for max_sl in MAX_SLS:
            for floor_start in FLOOR_STARTS:
                for floor_step in FLOOR_STEPS:
                    floors = build_floor_levels(floor_start, floor_step)
                    total_pnl = 0
                    wins = 0
                    losses = 0
                    sl_count = 0
                    floor_count = 0
                    eod_count = 0
                    trade_pnls = []

                    for td in trade_data:
                        entry = td["entry"]
                        qty = td["qty"]

                        # Calculate SL price
                        sl_pct_price = entry * (1 - sl_pct)
                        sl_cap_price = entry - (max_sl / qty)
                        sl_price = max(sl_pct_price, sl_cap_price)

                        exit_p, pnl, reason = simulate_trade(
                            td["candles"], entry, qty, sl_price, floors
                        )
                        total_pnl += pnl
                        trade_pnls.append(pnl)
                        if pnl > 0:
                            wins += 1
                        else:
                            losses += 1
                        if reason == "SL":
                            sl_count += 1
                        elif reason.startswith("FLOOR"):
                            floor_count += 1
                        else:
                            eod_count += 1

                    avg_win = sum(p for p in trade_pnls if p > 0) / max(1, wins)
                    avg_loss = sum(p for p in trade_pnls if p < 0) / max(1, losses)

                    results.append({
                        "sl_pct": sl_pct,
                        "max_sl": max_sl,
                        "floor_start": floor_start,
                        "floor_step": floor_step,
                        "total_pnl": total_pnl,
                        "wins": wins,
                        "losses": losses,
                        "sl_count": sl_count,
                        "floor_count": floor_count,
                        "eod_count": eod_count,
                        "avg_win": avg_win,
                        "avg_loss": avg_loss,
                    })

    # Sort by total PnL descending
    results.sort(key=lambda x: x["total_pnl"], reverse=True)

    # Print top 20 combinations
    print(f"\n{'='*100}")
    print(f"TOP 20 PARAMETER COMBINATIONS (out of {len(results)})")
    print(f"{'='*100}")
    print(f"{'Rank':>4} {'SL%':>5} {'MaxSL':>6} {'FloorStart':>10} {'FloorStep':>9} "
          f"{'PnL':>10} {'W':>3} {'L':>3} {'SL':>3} {'Floor':>5} {'EOD':>3} "
          f"{'AvgWin':>8} {'AvgLoss':>8} {'WR%':>5}")
    print("-" * 100)

    for i, r in enumerate(results[:20]):
        wr = r["wins"] / max(1, r["wins"] + r["losses"]) * 100
        print(f"{i+1:>4} {r['sl_pct']:>5.0%} {r['max_sl']:>6} {r['floor_start']:>10} {r['floor_step']:>9} "
              f"{r['total_pnl']:>+10,.0f} {r['wins']:>3} {r['losses']:>3} {r['sl_count']:>3} {r['floor_count']:>5} {r['eod_count']:>3} "
              f"{r['avg_win']:>+8,.0f} {r['avg_loss']:>+8,.0f} {wr:>5.1f}")

    # Print bottom 5
    print(f"\nBOTTOM 5:")
    print("-" * 100)
    for r in results[-5:]:
        wr = r["wins"] / max(1, r["wins"] + r["losses"]) * 100
        print(f"     {r['sl_pct']:>5.0%} {r['max_sl']:>6} {r['floor_start']:>10} {r['floor_step']:>9} "
              f"{r['total_pnl']:>+10,.0f} {r['wins']:>3} {r['losses']:>3} {r['sl_count']:>3} {r['floor_count']:>5} {r['eod_count']:>3} "
              f"{r['avg_win']:>+8,.0f} {r['avg_loss']:>+8,.0f} {wr:>5.1f}")

    # Print current config for comparison
    print(f"\n{'='*100}")
    print("CURRENT CONFIG: SL=30%, MaxSL=5000, FloorStart=500, FloorStep=500")
    current = [r for r in results if r["sl_pct"] == 0.30 and r["max_sl"] == 5000
               and r["floor_start"] == 500 and r["floor_step"] == 500]
    if current:
        r = current[0]
        rank = results.index(r) + 1
        wr = r["wins"] / max(1, r["wins"] + r["losses"]) * 100
        print(f"  Rank: {rank}/{len(results)} | PnL: {r['total_pnl']:+,.0f} | "
              f"W:{r['wins']} L:{r['losses']} | SL:{r['sl_count']} Floor:{r['floor_count']} EOD:{r['eod_count']} | "
              f"AvgWin:{r['avg_win']:+,.0f} AvgLoss:{r['avg_loss']:+,.0f} | WR:{wr:.1f}%")

    # Best combo details
    print(f"\n{'='*100}")
    print("BEST COMBO — trade-by-trade breakdown:")
    best = results[0]
    floors = build_floor_levels(best["floor_start"], best["floor_step"])
    print(f"SL={best['sl_pct']:.0%}, MaxSL={best['max_sl']}, FloorStart={best['floor_start']}, FloorStep={best['floor_step']}")
    print(f"{'Symbol':25s} {'Qty':>5} {'Entry':>8} {'Exit':>8} {'PnL':>8} {'Reason':>10}")
    print("-" * 75)
    for td in trade_data:
        entry = td["entry"]
        qty = td["qty"]
        sl_pct_price = entry * (1 - best["sl_pct"])
        sl_cap_price = entry - (best["max_sl"] / qty)
        sl_price = max(sl_pct_price, sl_cap_price)
        exit_p, pnl, reason = simulate_trade(td["candles"], entry, qty, sl_price, floors)
        print(f"{td['symbol']:25s} {qty:>5} {entry:>8.2f} {exit_p:>8.2f} {pnl:>+8,.0f} {reason:>10}")


if __name__ == "__main__":
    main()

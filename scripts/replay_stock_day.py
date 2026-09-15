"""Replay a trading day using 1-min candles to simulate live stock spread entry.

Fetches 1-min candles from 9:15 to 15:30, builds the daily picture at 15:10,
detects signals, and shows what the live executor would have entered with
real option LTP at that moment.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/replay_stock_day.py [--date 2026-09-15]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.broker.upstox_data import UpstoxData
from src.strategy.stock_runner import (
    STOCKS, STRATEGIES, _build_stock_option_master, _monthly_expiry_for,
    calc_charges, calc_ema, calc_rsi, fetch_daily_candles, round_strike,
)
from src.utils.logging import get_logger

log = get_logger("replay")
IST = ZoneInfo("Asia/Kolkata")
LIVE_STRATEGY = "ema20_rsi60"


def fetch_1min_candles(udata: UpstoxData, stock_name: str, ref_date: date) -> list[dict]:
    """Fetch 1-min candles for a stock on a given date."""
    stk = STOCKS[stock_name]
    key = stk["key"]
    from_dt = datetime(ref_date.year, ref_date.month, ref_date.day, 9, 15, tzinfo=IST)
    to_dt = datetime(ref_date.year, ref_date.month, ref_date.day, 15, 30, tzinfo=IST)

    candles = []
    try:
        d = udata._get(f"/v3/historical-candle/{key}/minutes/1/{to_dt.date().isoformat()}/{from_dt.date().isoformat()}")
        candles += d.get("data", {}).get("candles", [])
    except Exception as exc:
        log.warning("Historical 1min failed (%s): %s", stock_name, exc)

    try:
        d = udata._get(f"/v3/historical-candle/intraday/{key}/minutes/1")
        candles += d.get("data", {}).get("candles", [])
    except Exception:
        pass

    rows = [{"date": c[0], "open": c[1], "high": c[2], "low": c[3],
             "close": c[4], "volume": c[5]} for c in candles]
    rows.sort(key=lambda r: r["date"])

    # De-duplicate
    seen = set()
    out = []
    for r in rows:
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        d = r["date"][:10]
        if d == ref_date.isoformat():
            out.append(r)
    return out


def build_daily_ohlc_at(candles_1min: list[dict], until_time: str) -> dict | None:
    """Build a single daily OHLC bar from 1-min candles up to a given time (HH:MM)."""
    filtered = [c for c in candles_1min if c["date"][11:16] <= until_time]
    if not filtered:
        return None
    return {
        "open": filtered[0]["open"],
        "high": max(c["high"] for c in filtered),
        "low": min(c["low"] for c in filtered),
        "close": filtered[-1]["close"],
        "volume": sum(c["volume"] for c in filtered),
        "candle_count": len(filtered),
        "last_time": filtered[-1]["date"][11:16],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="Date to replay (YYYY-MM-DD)")
    args = parser.parse_args()

    ref_date = date.fromisoformat(args.date) if args.date else datetime.now(IST).date()
    params = STRATEGIES[LIVE_STRATEGY]
    expiry = _monthly_expiry_for(ref_date)
    dte = (expiry - ref_date).days

    print(f"\n{'='*70}")
    print(f"  STOCK SPREAD REPLAY — {ref_date} (DTE={dte}, expiry={expiry})")
    print(f"  Strategy: {LIVE_STRATEGY} | RSI bull>{params['rsi_bull']} bear<{params['rsi_bear']}")
    print(f"{'='*70}\n")

    udata = UpstoxData()

    # Check DTE range
    min_dte, max_dte = params["entry_dte_range"]
    if not (min_dte <= dte <= max_dte):
        print(f"  DTE {dte} outside range [{min_dte}, {max_dte}] — no trades today")
        return

    # Build option master once
    print("Loading option master...")
    master = _build_stock_option_master()
    print(f"  Master: {len(master)} entries\n")

    total_pnl = 0
    trades = []

    for stock_name, stk in STOCKS.items():
        step = stk["strike_step"]
        lot_size = stk["lot_size"]

        print(f"{'─'*60}")
        print(f"  {stock_name} (lot={lot_size}, step={step})")
        print(f"{'─'*60}")

        # Fetch 1-min candles for today
        candles_1m = fetch_1min_candles(udata, stock_name, ref_date)
        if not candles_1m:
            print(f"  ✗ No 1-min candles for {ref_date}\n")
            continue

        print(f"  1-min candles: {len(candles_1m)} (09:15 → {candles_1m[-1]['date'][11:16]})")

        # Show key intraday levels
        day_ohlc = build_daily_ohlc_at(candles_1m, "15:30")
        if day_ohlc:
            print(f"  Day OHLC: O={day_ohlc['open']:.1f} H={day_ohlc['high']:.1f} "
                  f"L={day_ohlc['low']:.1f} C={day_ohlc['close']:.1f}")

        # Build daily candle at 15:10 (entry time)
        ohlc_1510 = build_daily_ohlc_at(candles_1m, "15:10")
        if not ohlc_1510:
            print(f"  ✗ No candles until 15:10\n")
            continue

        print(f"  At 15:10: close={ohlc_1510['close']:.1f} ({ohlc_1510['candle_count']} bars)")

        # Fetch historical daily candles for EMA/RSI (need ~40 prior days)
        buffer_start = ref_date - timedelta(days=60)
        daily = fetch_daily_candles(udata, stock_name, buffer_start, ref_date - timedelta(days=1))
        if not daily or len(daily) < 30:
            print(f"  ✗ Insufficient history ({len(daily) if daily else 0} days)\n")
            continue

        # Append today's candle at 15:10 as if it were the daily close
        today_candle = {
            "date": ref_date.isoformat(),
            "open": ohlc_1510["open"],
            "high": ohlc_1510["high"],
            "low": ohlc_1510["low"],
            "close": ohlc_1510["close"],
            "volume": ohlc_1510["volume"],
        }
        daily_with_today = daily + [today_candle]

        # Calculate EMA and RSI
        closes = [c["close"] for c in daily_with_today]
        ema_val = calc_ema(closes, params["ema_period"])
        rsi_val = calc_rsi(closes, params["rsi_period"])

        if ema_val is None or rsi_val is None:
            print(f"  ✗ Cannot calculate EMA/RSI\n")
            continue

        spot = ohlc_1510["close"]
        print(f"  EMA({params['ema_period']})={ema_val:.1f} | RSI({params['rsi_period']})={rsi_val:.1f} | Spot={spot:.1f}")

        # Detect signal
        direction = None
        if spot > ema_val and rsi_val > params["rsi_bull"]:
            direction = "bullish"
        elif spot < ema_val and rsi_val < params["rsi_bear"]:
            direction = "bearish"

        if not direction:
            above_below = "above" if spot > ema_val else "below"
            print(f"  → No signal (spot {above_below} EMA, RSI={rsi_val:.1f})\n")
            continue

        print(f"  → Signal: {direction.upper()}")

        # Calculate strikes
        if direction == "bullish":
            opt_type = "PE"
            sell_strike = round_strike(spot * 0.98, step)
            buy_strike = sell_strike - 2 * step
        else:
            opt_type = "CE"
            sell_strike = round_strike(spot * 1.02, step)
            buy_strike = sell_strike + 2 * step

        print(f"  Strikes: sell={sell_strike} buy={buy_strike} {opt_type}")

        # Resolve instrument keys from master
        sell_key = master.get((stock_name, expiry, sell_strike, opt_type))
        buy_key = master.get((stock_name, expiry, buy_strike, opt_type))

        if not sell_key or not buy_key:
            print(f"  ✗ Instrument keys not found in master")
            # Show nearby available strikes
            avail = sorted([k[2] for k in master
                           if k[0] == stock_name and k[1] == expiry and k[3] == opt_type])
            if avail:
                print(f"    Available {opt_type} strikes: {avail[:10]}...")
            print()
            continue

        # Fetch real option LTP
        try:
            prices = udata.ltp_by_key([sell_key, buy_key])
        except Exception as exc:
            print(f"  ✗ LTP fetch failed: {exc}\n")
            continue

        sell_ltp = prices.get(sell_key)
        buy_ltp = prices.get(buy_key)

        if not sell_ltp or not buy_ltp:
            print(f"  ✗ LTP missing (sell={sell_ltp} buy={buy_ltp})\n")
            continue

        net_credit = sell_ltp - buy_ltp
        print(f"  Option LTP: sell={sell_ltp:.2f} buy={buy_ltp:.2f} → credit={net_credit:.2f}")

        if net_credit <= 0.5:
            print(f"  ✗ Credit too low ({net_credit:.2f} ≤ 0.50)\n")
            continue

        # Calculate targets
        profit_target = net_credit * params["profit_target_pct"]
        stop_loss_spread = net_credit * (1 + params["stop_loss_mult"])
        max_profit = net_credit * lot_size
        max_loss = ((buy_strike - sell_strike if direction == "bearish"
                     else sell_strike - buy_strike) - net_credit) * lot_size
        charges = calc_charges(net_credit, lot_size)

        tag = "BULL PUT" if direction == "bullish" else "BEAR CALL"
        print(f"\n  ╔══════════════════════════════════════════════════╗")
        print(f"  ║  {tag} SPREAD — {stock_name:12s}                   ║")
        print(f"  ╠══════════════════════════════════════════════════╣")
        print(f"  ║  Sell {sell_strike:>7.0f} {opt_type} @ {sell_ltp:>7.2f}               ║")
        print(f"  ║  Buy  {buy_strike:>7.0f} {opt_type} @ {buy_ltp:>7.2f}               ║")
        print(f"  ║  Net credit:  ₹{net_credit:>7.2f}  per share          ║")
        print(f"  ║  Lot size:    {lot_size:>7d}                        ║")
        print(f"  ║  Max profit:  ₹{max_profit:>+8,.0f}  ({params['profit_target_pct']*100:.0f}% tgt)       ║")
        print(f"  ║  Max loss:    ₹{-max_loss:>+8,.0f}  ({params['stop_loss_mult']}x SL)        ║")
        print(f"  ║  Charges:     ₹{charges:>8,.0f}                     ║")
        print(f"  ║  Expiry:      {expiry}  (DTE={dte})          ║")
        print(f"  ╚══════════════════════════════════════════════════╝")

        # Track minute-by-minute spot movement after entry (15:10 → 15:30)
        post_entry = [c for c in candles_1m if c["date"][11:16] > "15:10"]
        if post_entry:
            print(f"\n  Post-entry spot movement (15:10 → 15:30):")
            entry_spot = spot
            for c in post_entry:
                t = c["date"][11:16]
                cs = c["close"]
                chg = cs - entry_spot
                pct = (chg / entry_spot) * 100
                bar = "█" * min(int(abs(pct) * 50), 30)
                arrow = "↑" if chg >= 0 else "↓"
                if t in ("15:15", "15:20", "15:25", "15:29", "15:30") or abs(pct) > 0.3:
                    print(f"    {t}  {cs:>8.1f}  {arrow} {chg:>+6.1f} ({pct:>+.2f}%)  {bar}")
            final_spot = post_entry[-1]["close"]
            print(f"    Final: {final_spot:.1f} (moved {final_spot - entry_spot:+.1f} from entry)")

        trades.append({
            "stock": stock_name, "direction": direction, "tag": tag,
            "sell_strike": sell_strike, "buy_strike": buy_strike,
            "opt_type": opt_type, "sell_ltp": sell_ltp, "buy_ltp": buy_ltp,
            "net_credit": net_credit, "lot_size": lot_size,
            "max_profit": max_profit, "charges": charges,
            "spot": spot, "ema": ema_val, "rsi": rsi_val,
        })
        total_pnl += max_profit - charges
        print()

    # Summary
    print(f"\n{'='*70}")
    print(f"  SUMMARY — {ref_date}")
    print(f"{'='*70}")
    if not trades:
        print("  No trades entered today.")
    else:
        print(f"  {'Stock':<12s} {'Type':<12s} {'Sell':>6s} {'Buy':>6s} {'Credit':>8s} {'MaxProfit':>10s}")
        print(f"  {'─'*12} {'─'*12} {'─'*6} {'─'*6} {'─'*8} {'─'*10}")
        for t in trades:
            print(f"  {t['stock']:<12s} {t['tag']:<12s} {t['sell_strike']:>6.0f} "
                  f"{t['buy_strike']:>6.0f} {t['net_credit']:>8.2f} "
                  f"₹{t['max_profit'] - t['charges']:>+9,.0f}")
        print(f"  {'─'*12} {'─'*12} {'─'*6} {'─'*6} {'─'*8} {'─'*10}")
        print(f"  {'TOTAL':<12s} {'':<12s} {'':>6s} {'':>6s} {'':>8s} "
              f"₹{total_pnl:>+9,.0f}")
        print(f"\n  Trades entered: {len(trades)}")
        print(f"  All premiums: REAL (Upstox LTP)")
        print(f"  Entry time: 15:10 IST")
        print(f"  Expiry: {expiry} (DTE={dte})")
    print()


if __name__ == "__main__":
    main()

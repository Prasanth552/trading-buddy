"""Backtest the live trading strategy on historical Kite candles.

Replays past 15-min candles for the watchlist through the SAME code the live bot
uses — ``indicators.build_snapshot`` + ``engine.evaluate`` — then simulates each
trade's outcome under the real exit rules (₹ profit-target vs ATR/₹ stop, intraday
square-off). This tells you whether the strategy has a positive edge BEFORE
trusting it with the configured risk.

Run on the server (needs a valid Kite session):
    .venv/bin/python -m src.backtest --days 90
    .venv/bin/python -m src.backtest --symbol "NSE:NIFTY 50" --days 120

Honest caveats (read these):
  * News veto is NOT applied (historical news isn't available) — live may take
    slightly fewer trades than the backtest shows.
  * P&L is modelled at the INDEX level: a trade risks ``MAX_RISK_PER_TRADE`` to
    the stop and banks ``PROFIT_TARGET_RUPEES`` at the rupee target, scaling
    linearly with the favourable index move (the option premium ≈ index×delta).
    This faithfully captures the *edge* and the reward:risk, not exact option
    fills (slippage, IV, theta are not modelled).
  * Past performance does not guarantee future results.
"""
from __future__ import annotations

import argparse
from datetime import timedelta
from typing import Any

import config
from src.utils import market_calendar as mc
from src.utils.logging import get_logger

log = get_logger("backtest")

# How many trailing 15-min bars to feed the snapshot (mirrors the live ~5-day
# fetch so EMA/VWAP/ADX/ATR are computed over the same window).
WINDOW_BARS = 130

# Per-run caches so --sweep doesn't re-download or re-compute each combo.
_HIST_CACHE: dict[tuple, Any] = {}
_SIG_CACHE: dict[tuple, Any] = {}   # (symbol,days,interval) -> (df, {bar_i: signal})


def _fetch_history(client: Any, token: int, days: int, interval: str):
    """Fetch ``days`` of candles in <=60-day chunks (Kite per-request limit)."""
    import pandas as pd
    from src.data import market_data

    to_dt = mc.now_ist()
    frames = []
    remaining = days
    cursor_to = to_dt
    while remaining > 0:
        span = min(60, remaining)
        cursor_from = cursor_to - timedelta(days=span)
        candles = client.historical_data(token, cursor_from, cursor_to, interval)
        if candles:
            frames.append(market_data.candles_to_df(candles))
        cursor_to = cursor_from
        remaining -= span
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df


def _simulate_trade(df, i: int, sig, n: int) -> tuple[str, float, int]:
    """Walk forward from bar ``i`` to resolve one trade.

    Returns (outcome, pnl₹, exit_bar_index). outcome ∈ {'profit','stop','eod'}.
    Intraday only: squared off at the last bar of the entry day if neither level
    is hit.
    """
    entry = float(sig.entry)
    stop = float(sig.stop)
    is_long = sig.direction == "long"
    R = abs(entry - stop)
    if R <= 0:
        return "skip", 0.0, i

    risk_rs = float(config.MAX_RISK_PER_TRADE)
    # Take-profit level + payoff. With a rupee target set, TP sits at TARGET/RISK
    # of R favourable and banks ₹TARGET; with it disabled (0) we let the winner
    # run to the ATR target (sig.target) and bank its R-multiple in rupees.
    if config.PROFIT_TARGET_RUPEES > 0:
        prof_move = R * (config.PROFIT_TARGET_RUPEES / risk_rs)
        tp_level = entry + prof_move if is_long else entry - prof_move
        tp_payoff = float(config.PROFIT_TARGET_RUPEES)
    else:
        tp_level = float(sig.target)
        tp_payoff = (abs(sig.target - entry) / R) * risk_rs

    entry_day = df.index[i].date()
    j = i + 1
    while j < n and df.index[j].date() == entry_day:
        hi = float(df["high"].iloc[j]); lo = float(df["low"].iloc[j])
        if is_long:
            stop_hit, prof_hit = lo <= stop, hi >= tp_level
        else:
            stop_hit, prof_hit = hi >= stop, lo <= tp_level
        if stop_hit:                   # worst-case when a bar spans both
            return "stop", -risk_rs, j
        if prof_hit:
            return "profit", round(tp_payoff, 2), j
        j += 1

    # Square off intraday at the last bar of the entry day.
    last = j - 1 if j - 1 >= i + 1 else i
    exit_px = float(df["close"].iloc[last])
    move = (exit_px - entry) if is_long else (entry - exit_px)
    pnl = (move / R) * float(config.MAX_RISK_PER_TRADE)
    return "eod", round(pnl, 2), last


def backtest_symbol(client: Any, symbol: str, days: int, interval: str) -> dict[str, Any]:
    """Run the strategy over one symbol's history; return a stats dict."""
    from src.data import market_data, indicators
    from src.signals import engine

    tokens = market_data.resolve_tokens(client, [symbol])
    token = tokens.get(symbol)
    if token is None:
        return {"symbol": symbol, "error": "no instrument token"}

    ck = (symbol, days, interval)
    df = _HIST_CACHE.get(ck)
    if df is None:
        df = _fetch_history(client, token, days, interval)
        _HIST_CACHE[ck] = df
    n = len(df)
    if n < WINDOW_BARS + 5:
        return {"symbol": symbol, "error": f"not enough data ({n} bars)"}

    # Expensive step (snapshot + evaluate per bar) is config-independent for the
    # swept params, so compute the signals once and cache them.
    sigs = _SIG_CACHE.get(ck)
    if sigs is None:
        neutral = {"net": "neutral", "has_high_bull": False, "has_high_bear": False}
        sigs = {}
        for k in range(WINDOW_BARS, n - 1):
            window = df.iloc[k - WINDOW_BARS:k + 1]
            snap = indicators.build_snapshot(
                symbol, window, prev_day=None, ltp=float(df["close"].iloc[k]))
            s = engine.evaluate(snap, news=neutral)
            if s is not None:
                sigs[k] = s
        _SIG_CACHE[ck] = sigs

    trades: list[dict[str, Any]] = []
    i = WINDOW_BARS
    day_count: dict[Any, int] = {}
    start_t = _parse_hhmm(getattr(config, "ENTRY_START", "09:15"))
    end_t = _parse_hhmm(getattr(config, "ENTRY_END", "15:30"))
    per_sym_cap = getattr(config, "MAX_TRADES_PER_SYMBOL_PER_DAY", config.MAX_TRADES_PER_DAY)
    while i < n - 1:
        sig = sigs.get(i)
        if sig is None:
            i += 1
            continue
        ts = df.index[i]
        day = ts.date()
        # Session-time filter: skip the noisy open/close auctions.
        if not (start_t <= ts.time() <= end_t):
            i += 1
            continue
        if day_count.get(day, 0) >= per_sym_cap:
            i += 1
            continue
        outcome, pnl, exit_i = _simulate_trade(df, i, sig, n)
        if outcome == "skip":
            i += 1
            continue
        day_count[day] = day_count.get(day, 0) + 1
        trades.append({"ts": ts, "dir": sig.direction, "outcome": outcome, "pnl": pnl})
        # One position per symbol at a time: resume only AFTER this trade exits.
        i = max(i + 1, exit_i + 1)

    return _summarise(symbol, trades, n)


def _parse_hhmm(s: str):
    from datetime import time as _t
    h, m = s.split(":")
    return _t(int(h), int(m))


def _summarise(symbol: str, trades: list[dict[str, Any]], bars: int) -> dict[str, Any]:
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    total = round(sum(t["pnl"] for t in trades), 2)

    # Max drawdown on the cumulative equity curve.
    eq = 0.0; peak = 0.0; mdd = 0.0
    for t in trades:
        eq += t["pnl"]; peak = max(peak, eq); mdd = min(mdd, eq - peak)

    n = len(trades)
    return {
        "symbol": symbol, "bars": bars, "trades": n,
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(100 * len(wins) / n, 1) if n else 0.0,
        "total_pnl": total,
        "avg_pnl": round(total / n, 2) if n else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else float("inf"),
        "max_drawdown": round(mdd, 2),
        "by_outcome": {
            o: sum(1 for t in trades if t["outcome"] == o)
            for o in ("profit", "stop", "eod")
        },
    }


def _print_report(results: list[dict[str, Any]], days: int) -> None:
    print("\n" + "=" * 64)
    print(f" BACKTEST — last {days} days · mode={config.STRATEGY_MODE} · "
          f"target ₹{config.PROFIT_TARGET_RUPEES:.0f} / risk ₹{config.MAX_RISK_PER_TRADE}")
    print("=" * 64)
    agg_total = 0.0; agg_trades = 0; agg_wins = 0; agg_losses = 0
    for r in results:
        print(f"\n● {r['symbol']}")
        if r.get("error"):
            print(f"    (skipped: {r['error']})")
            continue
        print(f"    trades {r['trades']}  ·  win-rate {r['win_rate']}%  "
              f"({r['wins']}W / {r['losses']}L)")
        print(f"    outcomes: ₹target {r['by_outcome']['profit']} · "
              f"stop {r['by_outcome']['stop']} · square-off {r['by_outcome']['eod']}")
        print(f"    total P&L ₹{r['total_pnl']:,.0f}  ·  avg/trade ₹{r['avg_pnl']:,.0f}")
        print(f"    profit factor {r['profit_factor']}  ·  max drawdown ₹{r['max_drawdown']:,.0f}")
        agg_total += r["total_pnl"]; agg_trades += r["trades"]
        agg_wins += r["wins"]; agg_losses += r["losses"]
    print("\n" + "-" * 64)
    wr = round(100 * agg_wins / (agg_wins + agg_losses), 1) if (agg_wins + agg_losses) else 0.0
    print(f" TOTAL: {agg_trades} trades · win-rate {wr}% · net P&L ₹{agg_total:,.0f}")
    # Break-even win rate for the configured reward:risk.
    rr = config.PROFIT_TARGET_RUPEES / float(config.MAX_RISK_PER_TRADE)
    be = round(100 * 1 / (1 + rr), 1)
    print(f" Break-even win-rate for ₹{config.PROFIT_TARGET_RUPEES:.0f}:₹{config.MAX_RISK_PER_TRADE} "
          f"= {be}%  →  strategy is {'PROFITABLE ✅' if wr >= be else 'LOSING ❌'} vs break-even")
    print("-" * 64)
    print(" Caveat: news veto not modelled; index-level P&L (no slippage/theta).\n")


def _run_all(client: Any, symbols: list[str], days: int, interval: str) -> list[dict[str, Any]]:
    out = []
    for sym in symbols:
        try:
            out.append(backtest_symbol(client, sym, days, interval))
        except Exception as exc:  # noqa: BLE001
            out.append({"symbol": sym, "error": str(exc)})
    return out


def _sweep(client: Any, symbols: list[str], days: int, interval: str) -> None:
    """Try several (profit-target, risk) combinations and rank them by net P&L.

    profit_target = 0 means "let winners run to the ATR target" (no rupee cap).
    Mutates config per combo (restored after).
    """
    combos = [
        # (PROFIT_TARGET_RUPEES, MAX_RISK_PER_TRADE)
        (1500, 10000), (1500, 5000), (1500, 3000), (2000, 3000),
        (2000, 4000), (3000, 3000), (0, 3000), (0, 5000),
    ]
    orig = (config.PROFIT_TARGET_RUPEES, config.MAX_RISK_PER_TRADE)
    # Cache per-symbol trade *signals* are re-simulated each combo (cheap vs fetch).
    print(f"\n{'target':>8} {'risk':>7} {'trades':>7} {'win%':>6} {'net P&L':>14} {'PF':>6}")
    print("-" * 52)
    best = None
    for tgt, risk in combos:
        config.PROFIT_TARGET_RUPEES = float(tgt)
        config.MAX_RISK_PER_TRADE = int(risk)
        results = _run_all(client, symbols, days, interval)
        tot = sum(r.get("total_pnl", 0) for r in results)
        tr = sum(r.get("trades", 0) for r in results)
        w = sum(r.get("wins", 0) for r in results)
        l = sum(r.get("losses", 0) for r in results)
        wr = round(100 * w / (w + l), 1) if (w + l) else 0.0
        tlabel = "ATR" if tgt == 0 else f"₹{tgt}"
        print(f"{tlabel:>8} {risk:>7} {tr:>7} {wr:>6} {tot:>14,.0f}")
        if best is None or tot > best[0]:
            best = (tot, tgt, risk, wr, tr)
    config.PROFIT_TARGET_RUPEES, config.MAX_RISK_PER_TRADE = orig
    print("-" * 52)
    if best:
        tl = "ATR target (let winners run)" if best[1] == 0 else f"₹{best[1]} target"
        print(f" BEST: {tl} · ₹{best[2]} risk → net ₹{best[0]:,.0f} "
              f"({best[3]}% win, {best[4]} trades)")
    print(" (Index-level model; news/slippage/theta not included.)\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest the trading strategy on Kite history.")
    ap.add_argument("--symbol", action="append",
                    help="Symbol to test (repeatable). Default: full WATCHLIST.")
    ap.add_argument("--days", type=int, default=90, help="Lookback in days (default 90).")
    ap.add_argument("--interval", default=config.KITE_INTERVALS["15min"],
                    help="Kite interval (default 15minute).")
    ap.add_argument("--sweep", action="store_true",
                    help="Try several profit-target/risk combos and rank by net P&L.")
    args = ap.parse_args()

    from src.broker.session import ensure_session
    from src.broker.kite_client import KiteClientError
    try:
        client = ensure_session()
    except KiteClientError as exc:
        print(f"No Kite session: {exc}\nRun `python main.py --login` first.")
        return 1

    symbols = args.symbol or list(config.WATCHLIST)
    if args.sweep:
        print(f"… fetching {args.days}d history and sweeping settings …")
        _sweep(client, symbols, args.days, args.interval)
        return 0
    results = []
    for sym in symbols:
        print(f"… backtesting {sym} ({args.days}d) …")
        try:
            results.append(backtest_symbol(client, sym, args.days, args.interval))
        except Exception as exc:  # noqa: BLE001
            results.append({"symbol": sym, "error": str(exc)})
    _print_report(results, args.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

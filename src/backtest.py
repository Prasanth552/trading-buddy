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
_SNAP_CACHE: dict[tuple, Any] = {}  # ck -> ({bar_i: snapshot}, st_arr, atr_arr)


def _fetch_history(client: Any, token: int, days: int, interval: str, offset: int = 0):
    """Fetch ``days`` of candles ending ``offset`` days ago, in <=60-day chunks."""
    import pandas as pd
    from src.data import market_data

    to_dt = mc.now_ist() - timedelta(days=offset)
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


def _simulate_trade(df, i: int, sig, n: int,
                    st_arr=None, atr_arr=None) -> tuple[str, float, int]:
    """Walk forward from bar ``i`` to resolve one trade under ``config.EXIT_MODE``.

    Returns (outcome, pnl₹, exit_bar_index). P&L scales with the favourable move
    relative to the initial risk R (a full initial-stop loss = -MAX_RISK).
    Intraday only: squared off at the entry day's last bar.
    """
    entry = float(sig.entry)
    stop0 = float(sig.stop)
    is_long = sig.direction == "long"
    R = abs(entry - stop0)
    if R <= 0:
        return "skip", 0.0, i

    risk_rs = float(config.MAX_RISK_PER_TRADE)
    hi_arr = df["high"].to_numpy(); lo_arr = df["low"].to_numpy(); cl_arr = df["close"].to_numpy()
    dates = df.index
    entry_day = dates[i].date()
    mode = getattr(config, "EXIT_MODE", "target")

    def _pnl(px: float) -> float:
        move = (px - entry) if is_long else (entry - px)
        return round((move / R) * risk_rs, 2)

    # --- Fixed target + initial stop (current live behaviour) --------------
    if mode == "target":
        if config.PROFIT_TARGET_RUPEES > 0:
            prof_move = R * (config.PROFIT_TARGET_RUPEES / risk_rs)
            tp_level = entry + prof_move if is_long else entry - prof_move
            tp_payoff = float(config.PROFIT_TARGET_RUPEES)
        else:
            tp_level = float(sig.target)
            tp_payoff = (abs(sig.target - entry) / R) * risk_rs
        j = i + 1
        while j < n and dates[j].date() == entry_day:
            hi = hi_arr[j]; lo = lo_arr[j]
            if is_long:
                stop_hit, prof_hit = lo <= stop0, hi >= tp_level
            else:
                stop_hit, prof_hit = hi >= stop0, lo <= tp_level
            if stop_hit:
                return "stop", -risk_rs, j
            if prof_hit:
                return "profit", round(tp_payoff, 2), j
            j += 1
        last = max(i, j - 1)
        return "eod", _pnl(cl_arr[last]), last

    # --- Trailing exits (let winners run) ---------------------------------
    cur_stop = stop0
    peak = entry
    j = i + 1
    while j < n and dates[j].date() == entry_day:
        hi = hi_arr[j]; lo = lo_arr[j]
        # 1) stop (initial or trailed) — conservative, checked first.
        if is_long and lo <= cur_stop:
            return "stop", _pnl(cur_stop), j
        if (not is_long) and hi >= cur_stop:
            return "stop", _pnl(cur_stop), j
        # 2) Supertrend flip exit.
        if mode == "trail_st" and st_arr is not None:
            d = st_arr[j]
            if (is_long and d < 0) or ((not is_long) and d > 0):
                return "flip", _pnl(cl_arr[j]), j
        # 3) Chandelier ATR trail update.
        if mode == "trail_atr" and atr_arr is not None:
            a = float(atr_arr[j])
            if is_long:
                peak = max(peak, hi); cur_stop = max(cur_stop, peak - config.ATR_TRAIL_MULT * a)
            else:
                peak = min(peak, lo); cur_stop = min(cur_stop, peak + config.ATR_TRAIL_MULT * a)
        j += 1
    last = max(i, j - 1)
    return "eod", _pnl(cl_arr[last]), last


def backtest_symbol(client: Any, symbol: str, days: int, interval: str,
                    offset: int = 0) -> dict[str, Any]:
    """Run the strategy over one symbol's history; return a stats dict."""
    from src.data import market_data, indicators
    from src.signals import engine

    tokens = market_data.resolve_tokens(client, [symbol])
    token = tokens.get(symbol)
    if token is None:
        return {"symbol": symbol, "error": "no instrument token"}

    ck = (symbol, days, interval, offset)
    df = _HIST_CACHE.get(ck)
    if df is None:
        df = _fetch_history(client, token, days, interval, offset)
        _HIST_CACHE[ck] = df
    n = len(df)
    if n < WINDOW_BARS + 5:
        return {"symbol": symbol, "error": f"not enough data ({n} bars)"}

    # The EXPENSIVE step is build_snapshot per bar — and it does NOT depend on the
    # swept entry filters (ADX/EMA) or exit params. So cache the per-bar snapshots
    # (+ Supertrend/ATR arrays) once, and only re-run the cheap engine.evaluate
    # each call. This makes --filter-sweep fast.
    cached = _SNAP_CACHE.get(ck)
    if cached is None:
        print(f"   · building indicators for {symbol} "
              f"({'recent' if not offset else f'{offset}d-ago'}, {n} bars) …", flush=True)
        snaps = {}
        for k in range(WINDOW_BARS, n - 1):
            window = df.iloc[k - WINDOW_BARS:k + 1]
            snaps[k] = indicators.build_snapshot(
                symbol, window, prev_day=None, ltp=float(df["close"].iloc[k]))
        st_arr = indicators.supertrend(
            df, config.SUPERTREND_ATR_PERIOD, config.SUPERTREND_MULTIPLIER).to_numpy()
        atr_arr = indicators.atr(df, config.ATR_PERIOD).to_numpy()
        cached = (snaps, st_arr, atr_arr)
        _SNAP_CACHE[ck] = cached
    snaps, st_arr, atr_arr = cached

    # Cheap: evaluate the cached snapshots under the current filter config.
    neutral = {"net": "neutral", "has_high_bull": False, "has_high_bear": False}
    sigs = {}
    for k, snap in snaps.items():
        s = engine.evaluate(snap, news=neutral)
        if s is not None:
            sigs[k] = s

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
        outcome, pnl, exit_i = _simulate_trade(df, i, sig, n, st_arr, atr_arr)
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
            for o in ("profit", "flip", "stop", "eod")
        },
    }


def _print_report(results: list[dict[str, Any]], days: int, offset: int = 0) -> None:
    win = f"last {days}d" if not offset else f"days {offset}–{offset + days} ago (out-of-sample)"
    print("\n" + "=" * 64)
    print(f" BACKTEST — {win} · exit={getattr(config, 'EXIT_MODE', 'target')} · "
          f"target ₹{config.PROFIT_TARGET_RUPEES:.0f} / risk ₹{config.MAX_RISK_PER_TRADE}")
    print("=" * 64)
    agg_total = 0.0; agg_trades = 0; agg_wins = 0; agg_losses = 0
    for r in results:
        print(f"\n● {r['symbol']}")
        if r.get("error"):
            print(f"    (skipped: {r['error']})")
            continue
        bo = r["by_outcome"]
        print(f"    trades {r['trades']}  ·  win-rate {r['win_rate']}%  "
              f"({r['wins']}W / {r['losses']}L)")
        print(f"    outcomes: target {bo['profit']} · flip {bo['flip']} · "
              f"stop {bo['stop']} · square-off {bo['eod']}")
        print(f"    total P&L ₹{r['total_pnl']:,.0f}  ·  avg/trade ₹{r['avg_pnl']:,.0f}")
        print(f"    profit factor {r['profit_factor']}  ·  max drawdown ₹{r['max_drawdown']:,.0f}")
        agg_total += r["total_pnl"]; agg_trades += r["trades"]
        agg_wins += r["wins"]; agg_losses += r["losses"]
    print("\n" + "-" * 64)
    wr = round(100 * agg_wins / (agg_wins + agg_losses), 1) if (agg_wins + agg_losses) else 0.0
    verdict = "PROFITABLE ✅" if agg_total > 0 else "LOSING ❌"
    print(f" TOTAL: {agg_trades} trades · win-rate {wr}% · net P&L ₹{agg_total:,.0f}  →  {verdict}")
    print("-" * 64)
    print(" Caveat: news veto not modelled; index-level P&L (no slippage/theta).\n")


def _run_all(client: Any, symbols: list[str], days: int, interval: str,
             offset: int = 0) -> list[dict[str, Any]]:
    out = []
    for sym in symbols:
        try:
            out.append(backtest_symbol(client, sym, days, interval, offset))
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


def _filter_sweep(client: Any, symbols: list[str], days: int, interval: str) -> None:
    """Vary the entry filters (ADX floor, EMA-distance) and show net P&L both
    in-sample (recent) and out-of-sample (prior window), using the live exit mode.

    Re-generates signals each combo (filters change engine.evaluate), so the
    signal cache is cleared per combo; history stays cached.
    """
    combos = [
        # (ADX_MIN_TREND, EMA_TREND_MIN_PCT)
        (20, 0.0), (20, 0.0005), (25, 0.0), (25, 0.0005),
        (25, 0.0010), (30, 0.0005), (30, 0.0010),
    ]
    orig = (config.ADX_MIN_TREND, config.EMA_TREND_MIN_PCT)
    print(f"\n exit={getattr(config,'EXIT_MODE','target')} · risk ₹{config.MAX_RISK_PER_TRADE}")
    print(f"\n{'ADX':>5} {'EMA%':>7} {'in-sample ₹':>14} {'out-of-sample ₹':>16} {'in#':>5} {'oos#':>5}")
    print("-" * 56)
    best = None
    for adx, ema in combos:
        config.ADX_MIN_TREND = float(adx)
        config.EMA_TREND_MIN_PCT = float(ema)
        ins = _run_all(client, symbols, days, interval, offset=0)
        oos = _run_all(client, symbols, days, interval, offset=days)
        ins_tot = sum(r.get("total_pnl", 0) for r in ins)
        oos_tot = sum(r.get("total_pnl", 0) for r in oos)
        ins_n = sum(r.get("trades", 0) for r in ins)
        oos_n = sum(r.get("trades", 0) for r in oos)
        print(f"{adx:>5} {ema*100:>6.2f}% {ins_tot:>14,.0f} {oos_tot:>16,.0f} {ins_n:>5} {oos_n:>5}")
        # Rank by the worst of the two periods (robustness), then by sum.
        score = min(ins_tot, oos_tot)
        if best is None or score > best[0]:
            best = (score, adx, ema, ins_tot, oos_tot)
    config.ADX_MIN_TREND, config.EMA_TREND_MIN_PCT = orig
    print("-" * 56)
    if best:
        print(f" MOST ROBUST: ADX≥{best[1]} · EMA-dist {best[2]*100:.2f}% "
              f"→ in-sample ₹{best[3]:,.0f}, out-of-sample ₹{best[4]:,.0f}")
    print(" (Ranked by the worse of the two periods. News/slippage not modelled.)\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest the trading strategy on Kite history.")
    ap.add_argument("--symbol", action="append",
                    help="Symbol to test (repeatable). Default: full WATCHLIST.")
    ap.add_argument("--days", type=int, default=90, help="Lookback in days (default 90).")
    ap.add_argument("--interval", default=config.KITE_INTERVALS["15min"],
                    help="Kite interval (default 15minute).")
    ap.add_argument("--sweep", action="store_true",
                    help="Try several profit-target/risk combos and rank by net P&L.")
    ap.add_argument("--filter-sweep", action="store_true",
                    help="Vary ADX/EMA-distance entry filters; show in- & out-of-sample P&L.")
    ap.add_argument("--exit", dest="exit_mode",
                    choices=["target", "trail_st", "trail_atr"],
                    help="Override EXIT_MODE: fixed target | Supertrend-flip trail | ATR trail.")
    ap.add_argument("--offset", type=int, default=0,
                    help="End the window N days ago (for out-of-sample testing).")
    args = ap.parse_args()

    if args.exit_mode:
        config.EXIT_MODE = args.exit_mode

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
    if args.filter_sweep:
        print(f"… fetching {args.days}d (×2 windows) and sweeping entry filters …")
        _filter_sweep(client, symbols, args.days, args.interval)
        return 0
    print(f"… backtesting {len(symbols)} symbol(s) · {args.days}d "
          f"· exit={config.EXIT_MODE} · offset={args.offset}d …")
    results = _run_all(client, symbols, args.days, args.interval, args.offset)
    _print_report(results, args.days, args.offset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

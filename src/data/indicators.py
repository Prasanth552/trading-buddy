"""Technical indicators for Trading Buddy.

Pure functions over pandas OHLC data — no I/O, no broker calls — so they can be
unit-tested offline with synthetic candles.

Provides:
  - classic_pivots()      : PP, R1/R2/R3, S1/S2/S3 from a prior-period OHLC
  - rsi()                 : Wilder's RSI
  - detect_patterns()     : doji, hammer, bullish/bearish engulfing on the last bar
  - build_snapshot()      : assemble a per-symbol technical snapshot dict
  - format_snapshot()     : render a snapshot as readable text

Expected OHLC DataFrame columns (lowercase): open, high, low, close, volume.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

import config

OHLC_COLS = ("open", "high", "low", "close")


# --------------------------------------------------------------------------
# Pivots
# --------------------------------------------------------------------------
def classic_pivots(high: float, low: float, close: float) -> dict[str, float]:
    """Classic floor-trader pivots from a prior period's H/L/C.

    Typically fed the *previous trading day's* high/low/close to get the
    current day's intraday pivot levels.
    """
    pp = (high + low + close) / 3.0
    rng = high - low
    return {
        "PP": pp,
        "R1": 2 * pp - low,
        "S1": 2 * pp - high,
        "R2": pp + rng,
        "S2": pp - rng,
        "R3": high + 2 * (pp - low),
        "S3": low - 2 * (high - pp),
    }


# --------------------------------------------------------------------------
# RSI (Wilder's)
# --------------------------------------------------------------------------
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI as a Series aligned to ``close``.

    Implements Wilder's original smoothing: the first average gain/loss is the
    simple mean of the first ``period`` changes, then each subsequent value is
    ``(prev * (period - 1) + current) / period``. The first defined RSI sits at
    index ``period``; earlier values are NaN. This matches the textbook
    reference (e.g. Wilder's worked example RSI ~= 70.53).
    """
    if period < 1:
        raise ValueError("RSI period must be >= 1")
    close = close.astype("float64")
    delta = close.diff().to_numpy()
    n = len(close)
    out = np.full(n, np.nan)
    if n <= period:
        return pd.Series(out, index=close.index)

    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    # delta[0] is NaN -> first `period` changes are indices 1..period.
    avg_gain = np.nanmean(gain[1 : period + 1])
    avg_loss = np.nanmean(loss[1 : period + 1])

    def _rsi(ag: float, al: float) -> float:
        if al == 0 and ag == 0:
            return 50.0  # flat -> neutral
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        out[i] = _rsi(avg_gain, avg_loss)

    return pd.Series(out, index=close.index)


def rsi_state(value: float) -> str:
    """Classify an RSI value relative to configured thresholds."""
    if value >= config.RSI_OVERBOUGHT:
        return "overbought"
    if value <= config.RSI_OVERSOLD:
        return "oversold"
    return "neutral"


# --------------------------------------------------------------------------
# Candlestick patterns
# --------------------------------------------------------------------------
def _body(o: float, c: float) -> float:
    return abs(c - o)


def detect_patterns(df: pd.DataFrame, doji_frac: float = 0.1) -> list[str]:
    """Detect simple candlestick patterns on the *last* bar of ``df``.

    Needs at least 2 rows for engulfing patterns. Returns a list of pattern
    names (possibly empty).
    """
    if df is None or len(df) == 0:
        return []
    df = df[list(OHLC_COLS)].astype("float64")
    patterns: list[str] = []

    o = df["open"].iloc[-1]
    h = df["high"].iloc[-1]
    low_ = df["low"].iloc[-1]
    c = df["close"].iloc[-1]

    rng = h - low_
    body = _body(o, c)
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - low_

    # Doji: very small body relative to range.
    if rng > 0 and body <= doji_frac * rng:
        patterns.append("doji")

    # Hammer: small body in the upper third, long lower shadow, little upper shadow.
    if body > 0 and lower_shadow >= 2 * body and upper_shadow <= body:
        patterns.append("hammer")

    # Shooting star: mirror of hammer (long upper shadow).
    if body > 0 and upper_shadow >= 2 * body and lower_shadow <= body:
        patterns.append("shooting_star")

    # Engulfing needs a previous candle.
    if len(df) >= 2:
        po = df["open"].iloc[-2]
        pc = df["close"].iloc[-2]
        prev_bear = pc < po
        prev_bull = pc > po
        cur_bull = c > o
        cur_bear = c < o
        # Bullish engulfing: prev red, current green body engulfs prev body.
        if prev_bear and cur_bull and o <= pc and c >= po:
            patterns.append("bullish_engulfing")
        # Bearish engulfing: prev green, current red body engulfs prev body.
        if prev_bull and cur_bear and o >= pc and c <= po:
            patterns.append("bearish_engulfing")

    return patterns


# --------------------------------------------------------------------------
# Snapshot assembly
# --------------------------------------------------------------------------
def _nearest_pivot(price: float, pivots: dict[str, float]) -> tuple[str, float]:
    """Return (level_name, distance) of the pivot nearest to ``price``."""
    name, dist = min(
        ((k, abs(price - v)) for k, v in pivots.items()), key=lambda kv: kv[1]
    )
    return name, dist


def build_snapshot(
    symbol: str,
    intraday: pd.DataFrame,
    prev_day: dict[str, float] | None,
    ltp: float | None = None,
) -> dict[str, Any]:
    """Assemble a technical snapshot for one symbol.

    Args:
        symbol: e.g. "NSE:NIFTY 50".
        intraday: OHLC DataFrame on the primary timeframe (15min), oldest first.
        prev_day: previous day's {high, low, close} for pivots (or None).
        ltp: last traded price; falls back to last intraday close.
    """
    df = intraday[list(OHLC_COLS)].astype("float64") if len(intraday) else intraday
    last_close = float(df["close"].iloc[-1]) if len(df) else float("nan")
    price = float(ltp) if ltp is not None else last_close

    fast = rsi(df["close"], config.RSI_FAST_PERIOD) if len(df) else pd.Series(dtype=float)
    slow = rsi(df["close"], config.RSI_SLOW_PERIOD) if len(df) else pd.Series(dtype=float)
    rsi_fast = float(fast.iloc[-1]) if len(fast.dropna()) else float("nan")
    rsi_slow = float(slow.iloc[-1]) if len(slow.dropna()) else float("nan")

    pivots = (
        classic_pivots(prev_day["high"], prev_day["low"], prev_day["close"])
        if prev_day
        else {}
    )
    near_name, near_dist = _nearest_pivot(price, pivots) if pivots else ("n/a", float("nan"))

    trend = "n/a"
    if pivots and not np.isnan(price):
        trend = "above PP (bullish bias)" if price > pivots["PP"] else "below PP (bearish bias)"

    last_bar = (
        {k: float(df[k].iloc[-1]) for k in OHLC_COLS} if len(df) else {}
    )

    return {
        "symbol": symbol,
        "ltp": price,
        "bars": int(len(df)),
        "last_bar": last_bar,
        "trend": trend,
        "pivots": pivots,
        "nearest_pivot": near_name,
        "nearest_pivot_dist": near_dist,
        "rsi_fast": rsi_fast,
        "rsi_fast_state": rsi_state(rsi_fast) if not np.isnan(rsi_fast) else "n/a",
        "rsi_slow": rsi_slow,
        "rsi_slow_state": rsi_state(rsi_slow) if not np.isnan(rsi_slow) else "n/a",
        "patterns": detect_patterns(df),
    }


def format_snapshot(s: dict[str, Any]) -> str:
    """Render a snapshot dict (from build_snapshot) as readable text."""
    def f(x: Any) -> str:
        return f"{x:,.2f}" if isinstance(x, (int, float)) and not (
            isinstance(x, float) and np.isnan(x)
        ) else "n/a"

    piv = s.get("pivots") or {}
    piv_line = "  ".join(f"{k}={f(v)}" for k, v in piv.items()) if piv else "n/a"
    patterns = ", ".join(s["patterns"]) if s["patterns"] else "none"
    lines = [
        f"--- {s['symbol']} ---",
        f"  LTP        : {f(s['ltp'])}   ({s['bars']} bars)",
        f"  Trend      : {s['trend']}",
        f"  Pivots     : {piv_line}",
        f"  Nearest    : {s['nearest_pivot']} (dist {f(s['nearest_pivot_dist'])})",
        f"  RSI fast({config.RSI_FAST_PERIOD}): {f(s['rsi_fast'])} [{s['rsi_fast_state']}]",
        f"  RSI slow({config.RSI_SLOW_PERIOD}): {f(s['rsi_slow'])} [{s['rsi_slow_state']}]",
        f"  Patterns   : {patterns}",
    ]
    return "\n".join(lines)

"""Offline tests for src.data.indicators — no broker/credentials needed.

Run:  python -m tests.test_indicators
Exits 0 on success, 1 on any failed assertion.
"""
from __future__ import annotations

import math
import sys

import numpy as np
import pandas as pd

from src.data import indicators


def _ohlc(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """Build an OHLC DataFrame from (open, high, low, close) tuples."""
    idx = pd.date_range("2026-06-12 09:15", periods=len(rows), freq="15min")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1000
    return df


def check(name: str, cond: bool) -> None:
    print(f"  [{'OK ' if cond else 'BAD'}] {name}")
    if not cond:
        check.failed += 1  # type: ignore[attr-defined]


check.failed = 0  # type: ignore[attr-defined]


def test_pivots() -> None:
    print("classic_pivots:")
    p = indicators.classic_pivots(high=110, low=90, close=100)
    # PP = (110+90+100)/3 = 100
    check("PP == 100", math.isclose(p["PP"], 100.0))
    check("R1 == 110", math.isclose(p["R1"], 110.0))   # 2*100 - 90
    check("S1 == 90", math.isclose(p["S1"], 90.0))     # 2*100 - 110
    check("R2 == 120", math.isclose(p["R2"], 120.0))   # 100 + (110-90)
    check("S2 == 80", math.isclose(p["S2"], 80.0))     # 100 - (110-90)


def test_rsi_known() -> None:
    print("rsi (Wilder, classic 14-period reference):")
    # Wilder's original worked example — RSI should be ~70.53 on this series.
    closes = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
        45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
    ]
    s = pd.Series(closes)
    r = indicators.rsi(s, period=14)
    val = r.iloc[14]  # first defined RSI after 14 deltas
    check(f"RSI[14] ~= 70.46 (got {val:.2f})", abs(val - 70.46) < 0.5)
    check("RSI bounded 0..100", bool((r.dropna().between(0, 100)).all()))


def test_rsi_extremes() -> None:
    print("rsi extremes:")
    up = pd.Series(np.arange(1, 40, dtype=float))      # monotonic up
    down = pd.Series(np.arange(40, 1, -1, dtype=float))  # monotonic down
    check("all-up RSI == 100", math.isclose(indicators.rsi(up, 14).iloc[-1], 100.0))
    check("all-down RSI == 0", math.isclose(indicators.rsi(down, 14).iloc[-1], 0.0))


def test_patterns() -> None:
    print("detect_patterns:")
    # Doji: open ~ close, with range.
    doji = _ohlc([(100, 105, 95, 100.2)])
    check("doji detected", "doji" in indicators.detect_patterns(doji))

    # Hammer: small body at top, long lower shadow.
    hammer = _ohlc([(100, 101, 90, 100.5)])
    check("hammer detected", "hammer" in indicators.detect_patterns(hammer))

    # Bullish engulfing: red then bigger green.
    bull = _ohlc([(100, 101, 98, 99), (98.5, 103, 98, 102)])
    check("bullish_engulfing detected",
          "bullish_engulfing" in indicators.detect_patterns(bull))

    # Bearish engulfing: green then bigger red.
    bear = _ohlc([(100, 102, 99, 101.5), (102, 103, 98, 99)])
    check("bearish_engulfing detected",
          "bearish_engulfing" in indicators.detect_patterns(bear))

    # Plain trend candle: no pattern.
    plain = _ohlc([(100, 102, 99.8, 101.8)])
    check("plain candle -> no doji/hammer",
          not ({"doji", "hammer"} & set(indicators.detect_patterns(plain))))


def test_snapshot() -> None:
    print("build_snapshot / format_snapshot:")
    rows = [(100 + i * 0.1, 100 + i * 0.1 + 1, 100 + i * 0.1 - 1, 100 + i * 0.1 + 0.5)
            for i in range(40)]
    df = _ohlc(rows)
    prev = {"high": 110.0, "low": 90.0, "close": 100.0}
    snap = indicators.build_snapshot("NSE:NIFTY 50", df, prev, ltp=104.0)
    check("snapshot has pivots", snap["pivots"].get("PP") == 100.0)
    check("snapshot trend bullish (ltp>PP)", "bullish" in snap["trend"])
    check("rsi_fast is finite", not math.isnan(snap["rsi_fast"]))
    text = indicators.format_snapshot(snap)
    check("format includes symbol", "NSE:NIFTY 50" in text)
    print("\n--- sample formatted snapshot ---")
    print(text)
    print("---------------------------------")


def main() -> int:
    print("\n=== indicator tests (offline) ===")
    test_pivots()
    test_rsi_known()
    test_rsi_extremes()
    test_patterns()
    test_snapshot()
    failed = check.failed  # type: ignore[attr-defined]
    print(f"\nResult: {'PASSED' if failed == 0 else f'FAILED ({failed} checks)'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

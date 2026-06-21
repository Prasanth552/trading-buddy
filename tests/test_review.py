"""Offline tests for src.review.performance.compute_stats. No LLM/network.

Run: python -m tests.test_review
"""
from __future__ import annotations

import json
import sys

from src.review import performance


def check(name: str, cond: bool) -> None:
    print(f"  [{'OK ' if cond else 'BAD'}] {name}")
    if not cond:
        check.failed += 1  # type: ignore[attr-defined]


check.failed = 0  # type: ignore[attr-defined]


def _row(symbol, pnl, news, rsi_state, patterns):
    return {
        "symbol": symbol, "pnl": pnl, "status": "CLOSED_TARGET" if pnl > 0 else "CLOSED_STOP",
        "context": json.dumps({"news_net": news, "rsi_fast_state": rsi_state, "patterns": patterns}),
    }


def test_stats() -> None:
    print("compute_stats:")
    rows = [
        _row("NSE:NIFTY 50", 450, "bullish", "neutral", ["hammer"]),
        _row("NSE:NIFTY 50", -300, "neutral", "overbought", []),
        _row("BSE:SENSEX", 600, "bullish", "oversold", ["bullish_engulfing"]),
        _row("BSE:SENSEX", -200, "bearish", "overbought", []),
    ]
    s = performance.compute_stats(rows)
    check("n == 4", s["n"] == 4)
    check("2 wins / 2 losses", s["wins"] == 2 and s["losses"] == 2)
    check("win rate 0.5", s["win_rate"] == 0.5)
    check("total pnl = 550", s["total_pnl"] == 550)
    check("avg win = 525", s["avg_win"] == 525.0)
    check("avg loss = -250", s["avg_loss"] == -250.0)
    check("expectancy = 137.5", s["expectancy"] == 137.5)
    check("by_news bullish 2W/0L", s["by_news"]["bullish"] == {"wins": 2, "losses": 0, "pnl": 1050.0})
    check("by_pattern: pattern bucket 2W", s["by_pattern"]["pattern"]["wins"] == 2)
    check("by_pattern: no_pattern 0W/2L",
          s["by_pattern"]["no_pattern"]["wins"] == 0 and s["by_pattern"]["no_pattern"]["losses"] == 2)
    check("by_rsi overbought all losses", s["by_rsi_fast"]["overbought"]["wins"] == 0)


def test_empty() -> None:
    print("compute_stats — empty:")
    s = performance.compute_stats([])
    check("n == 0", s["n"] == 0)
    check("win_rate 0", s["win_rate"] == 0.0)
    check("format says no trades", "No closed trades" in performance.format_stats_text(s))


def main() -> int:
    print("\n=== review/performance tests (offline) ===")
    test_stats()
    test_empty()
    failed = check.failed  # type: ignore[attr-defined]
    print(f"\nResult: {'PASSED' if failed == 0 else f'FAILED ({failed} checks)'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

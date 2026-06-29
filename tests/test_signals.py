"""Offline tests for the signal engine — no broker, no network.

Feeds synthetic snapshots (the dict shape produced by indicators.build_snapshot)
through engine.evaluate() and checks each low-risk rule.

Run:  python -m tests.test_signals
"""
from __future__ import annotations

import sys

import config
from src.signals import engine

# These tests validate the legacy pivot mean-reversion path; pin the mode so
# they don't dispatch to the new trend strategy. MODE=LIVE so the news-conflict
# gate is active (it is intentionally skipped in PAPER/sandbox).
config.STRATEGY_MODE = "meanrev"
config.MODE = "LIVE"


def check(name: str, cond: bool) -> None:
    print(f"  [{'OK ' if cond else 'BAD'}] {name}")
    if not cond:
        check.failed += 1  # type: ignore[attr-defined]


check.failed = 0  # type: ignore[attr-defined]


def snap(ltp, rsi_fast, patterns=None, pp=100.0):
    """Build a snapshot with pivots spaced ~2% apart around pp=100."""
    pivots = {"PP": pp, "R1": 102.0, "R2": 104.0, "S1": 98.0, "S2": 96.0,
              "R3": 106.0, "S3": 94.0}
    return {
        "symbol": "NSE:NIFTY 50", "ltp": ltp, "pivots": pivots,
        "rsi_fast": rsi_fast, "rsi_slow": rsi_fast, "patterns": patterns or [],
    }


NEUTRAL = {"net": "neutral", "has_high_bull": False, "has_high_bear": False}


def test_long_at_support() -> None:
    print("long near support, RSI not overbought, neutral news:")
    # LTP just above S1=98 (within 0.4% of 98.1 -> threshold ~0.39), RSI 35.
    s = engine.evaluate(snap(98.1, 35.0, ["hammer"]), NEUTRAL)
    check("signal raised", s is not None)
    check("direction long", s and s.direction == "long")
    check("stop below support", s and s.stop < 98.0)
    check("target above entry", s and s.target > s.entry)
    check("RR matches config", s and abs(
        (s.target - s.entry) - config.SIGNAL_RR_RATIO * (s.entry - s.stop)) < 0.01)
    check("qty = MIN_LOT_SIZE", s and s.qty == config.MIN_LOT_SIZE)
    check("max_risk = budget", s and s.max_risk == float(config.MAX_RISK_PER_TRADE))


def test_long_blocked_overbought() -> None:
    print("long blocked when RSI overbought:")
    s = engine.evaluate(snap(98.1, 85.0, ["hammer"]), NEUTRAL)
    check("no signal (overbought)", s is None)


def test_short_at_resistance() -> None:
    print("short near resistance, RSI not oversold:")
    # LTP just below R1=102 (within threshold), RSI 65.
    s = engine.evaluate(snap(101.9, 65.0, ["shooting_star"]), NEUTRAL)
    check("signal raised", s is not None)
    check("direction short", s and s.direction == "short")
    check("stop above resistance", s and s.stop > 102.0)
    check("target below entry", s and s.target < s.entry)


def test_short_blocked_oversold() -> None:
    print("short blocked when RSI oversold:")
    s = engine.evaluate(snap(101.9, 20.0), NEUTRAL)
    check("no signal (oversold)", s is None)


def test_no_setup_midrange() -> None:
    print("no signal mid-range (not near any pivot):")
    # Build a snapshot whose LTP sits far (>1.5%) from every pivot.
    far = {"symbol": "NSE:NIFTY 50", "ltp": 150.0,
           "pivots": {"PP": 100.0, "R1": 102.0, "S1": 98.0},
           "rsi_fast": 50.0, "rsi_slow": 50.0, "patterns": []}
    s = engine.evaluate(far, NEUTRAL)
    check("no signal mid-range", s is None)


def test_news_conflict_blocks_long() -> None:
    print("high-confidence bearish news blocks a long:")
    bearish = {"net": "bearish", "has_high_bull": False, "has_high_bear": True}
    s = engine.evaluate(snap(98.1, 35.0, ["hammer"]), bearish)
    check("long blocked by bearish news", s is None)


def test_mild_news_does_not_block() -> None:
    print("mild net sentiment no longer blocks a clean setup:")
    mild_bear = {"net": "bearish", "has_high_bull": False, "has_high_bear": False}
    s = engine.evaluate(snap(98.1, 35.0, ["hammer"]), mild_bear)
    check("mild bearish net still allows the long", s is not None and s.direction == "long")
    strong_bear = {"net": "bearish", "has_high_bull": False, "has_high_bear": True}
    s2 = engine.evaluate(snap(98.1, 35.0, ["hammer"]), strong_bear)
    check("high-confidence bearish still blocks the long", s2 is None)


def test_news_confidence_boost() -> None:
    print("aligned news + pattern -> high confidence in rationale:")
    bullish = {"net": "bullish", "has_high_bull": True, "has_high_bear": False}
    s = engine.evaluate(snap(98.1, 35.0, ["hammer"]), bullish)
    check("long raised with bullish news", s is not None and s.direction == "long")
    check("rationale notes high confidence", s and "Confidence high" in s.rationale)


def test_aggregate_news() -> None:
    print("aggregate_news_sentiment weighting:")
    rows = [
        {"sentiment": "bullish", "confidence": "high"},
        {"sentiment": "bullish", "confidence": "low"},
        {"sentiment": "bearish", "confidence": "medium"},
    ]
    agg = engine.aggregate_news_sentiment(rows)
    check("net bullish (3+1 vs 2)", agg["net"] == "bullish")
    check("has_high_bull true", agg["has_high_bull"] is True)
    check("has_high_bear false", agg["has_high_bear"] is False)
    check("empty -> neutral", engine.aggregate_news_sentiment([])["net"] == "neutral")


def main() -> int:
    print("\n=== signal engine tests (offline) ===")
    test_long_at_support()
    test_long_blocked_overbought()
    test_short_at_resistance()
    test_short_blocked_oversold()
    test_no_setup_midrange()
    test_news_conflict_blocks_long()
    test_mild_news_does_not_block()
    test_news_confidence_boost()
    test_aggregate_news()
    failed = check.failed  # type: ignore[attr-defined]
    print(f"\nResult: {'PASSED' if failed == 0 else f'FAILED ({failed} checks)'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

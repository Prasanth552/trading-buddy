"""Offline tests for the trend-following signal path (engine._evaluate_trend).

Feeds synthetic snapshots (the shape produced by indicators.build_snapshot) and
checks the EMA/VWAP/Supertrend/ADX/RSI rules and ATR-based stops/targets.

Run:  python -m tests.test_signals_trend
"""
from __future__ import annotations

import sys

import config
from src.signals import engine

config.STRATEGY_MODE = "trend"

NEUTRAL = {"net": "neutral", "has_high_bull": False, "has_high_bear": False}


def check(name: str, cond: bool) -> None:
    print(f"  [{'OK ' if cond else 'BAD'}] {name}")
    if not cond:
        check.failed += 1  # type: ignore[attr-defined]


check.failed = 0  # type: ignore[attr-defined]


def snap(*, ltp, ema_fast, ema_slow, vwap, st_dir, adx, rsi, atr=50.0, patterns=None):
    return {
        "symbol": "NSE:NIFTY 50", "ltp": ltp,
        "ema_fast": ema_fast, "ema_slow": ema_slow, "vwap": vwap,
        "supertrend_dir": st_dir, "adx": adx, "rsi_fast": rsi, "atr": atr,
        "patterns": patterns or [],
    }


def test_long_in_uptrend() -> None:
    print("strong uptrend -> long:")
    s = engine.evaluate(snap(ltp=24400, ema_fast=24380, ema_slow=24350,
                             vwap=24360, st_dir=1, adx=30, rsi=62), NEUTRAL)
    check("signal raised", s is not None)
    check("direction long", s and s.direction == "long")
    check("stop = entry - 1*ATR", s and abs(s.stop - (24400 - config.ATR_STOP_MULT * 50)) < 0.01)
    check("target = entry + 2*ATR", s and abs(s.target - (24400 + config.ATR_TARGET_MULT * 50)) < 0.01)


def test_short_in_downtrend() -> None:
    print("strong downtrend -> short:")
    s = engine.evaluate(snap(ltp=24000, ema_fast=24030, ema_slow=24060,
                             vwap=24050, st_dir=-1, adx=30, rsi=38), NEUTRAL)
    check("direction short", s and s.direction == "short")
    check("stop above entry", s and s.stop > s.entry)
    check("target below entry", s and s.target < s.entry)


def test_no_trade_when_choppy() -> None:
    print("ADX below threshold (chop) -> no trade:")
    s = engine.evaluate(snap(ltp=24400, ema_fast=24380, ema_slow=24350,
                             vwap=24360, st_dir=1, adx=12, rsi=62), NEUTRAL)
    check("no signal in chop", s is None)


def test_no_long_against_supertrend() -> None:
    print("EMA bullish but Supertrend down -> no long (conflict):")
    s = engine.evaluate(snap(ltp=24400, ema_fast=24380, ema_slow=24350,
                             vwap=24360, st_dir=-1, adx=30, rsi=62), NEUTRAL)
    check("no conflicting long", s is None)


def test_no_long_below_vwap() -> None:
    print("price below VWAP -> no long even if EMA/ST bullish:")
    s = engine.evaluate(snap(ltp=24340, ema_fast=24380, ema_slow=24350,
                             vwap=24360, st_dir=1, adx=30, rsi=62), NEUTRAL)
    check("no long below VWAP", s is None)


def test_blowoff_rsi_blocks_long() -> None:
    print("extreme RSI blow-off blocks the long:")
    s = engine.evaluate(snap(ltp=24400, ema_fast=24380, ema_slow=24350,
                             vwap=24360, st_dir=1, adx=30, rsi=92), NEUTRAL)
    check("no long at RSI 92", s is None)


def test_news_vetoes_long() -> None:
    print("strong bearish net news vetoes an uptrend long:")
    bearish = {"net": "bearish", "has_high_bull": False, "has_high_bear": True}
    s = engine.evaluate(snap(ltp=24400, ema_fast=24380, ema_slow=24350,
                             vwap=24360, st_dir=1, adx=30, rsi=62), bearish)
    check("long vetoed by bearish news", s is None)


def main() -> int:
    for fn in [test_long_in_uptrend, test_short_in_downtrend, test_no_trade_when_choppy,
               test_no_long_against_supertrend, test_no_long_below_vwap,
               test_blowoff_rsi_blocks_long, test_news_vetoes_long]:
        fn()
    print(f"\n{'PASS' if not check.failed else 'FAIL'} — {check.failed} failed check(s).")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())

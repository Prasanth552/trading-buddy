"""Offline tests for Telegram formatting + 8-section analysis prompt building.

No network, no anthropic SDK, no Telegram token. Run: python -m tests.test_telegram
"""
from __future__ import annotations

import sys

import config
from src.notify import analysis, telegram_bot


def check(name: str, cond: bool) -> None:
    print(f"  [{'OK ' if cond else 'BAD'}] {name}")
    if not cond:
        check.failed += 1  # type: ignore[attr-defined]


check.failed = 0  # type: ignore[attr-defined]


def test_chunk_message() -> None:
    print("telegram_bot.chunk_message:")
    short = "hello"
    check("short stays single", telegram_bot.chunk_message(short) == [short])

    long_text = "\n".join(f"line {i}" for i in range(2000))  # > 4096 chars
    chunks = telegram_bot.chunk_message(long_text)
    check("long text is split", len(chunks) > 1)
    check("all chunks within limit", all(len(c) <= 4096 for c in chunks))
    check("reassembles to original", "".join(chunks) == long_text)

    huge_line = "x" * 9000
    hc = telegram_bot.chunk_message(huge_line)
    check("oversized single line hard-split", all(len(c) <= 4096 for c in hc))
    check("hard-split reassembles", "".join(hc) == huge_line)


def test_format_signal_alert() -> None:
    print("messages.signal_alert (English + Tamil):")
    sig = {"symbol": "NSE:NIFTY 50", "direction": "long", "entry": 23600,
           "stop": 23550, "target": 23675, "qty": 1, "max_risk": 500.0,
           "rationale": "LONG near support."}
    old = config.ALERT_LANGUAGE
    try:
        config.ALERT_LANGUAGE = "english"
        en = telegram_bot.format_signal_alert(sig)
        check("EN has TRADE ALERT", "TRADE ALERT" in en)
        check("EN long shows LONG", "LONG" in en)
        check("EN numbers present", all(str(v) in en for v in (23600, 23550, 23675)))

        config.ALERT_LANGUAGE = "tamil"
        ta = telegram_bot.format_signal_alert(sig)
        check("TA has Tamil text", "வர்த்தக" in ta)
        check("TA numbers present", all(str(v) in ta for v in (23600, 23550, 23675)))
        check("TA long marker", "வாங்கும்" in ta)
        ta_short = telegram_bot.format_signal_alert({**sig, "direction": "short"})
        check("TA short marker", "விற்கும்" in ta_short)
    finally:
        config.ALERT_LANGUAGE = old


def test_analysis_prompt() -> None:
    print("analysis.build_analysis_prompt:")
    snap = {
        "symbol": "NSE:NIFTY 50", "ltp": 23622.9, "trend": "above PP (bullish bias)",
        "pivots": {"PP": 23527.38, "R1": 23740.87, "S1": 23409.42},
        "rsi_fast": 86.1, "rsi_fast_state": "overbought",
        "rsi_slow": 79.06, "rsi_slow_state": "overbought",
        "patterns": ["doji"],
    }
    news = {"net": "bullish", "bull": 16, "bear": 3}
    prompt = analysis.build_analysis_prompt(snap, news_view=news, signal=None)
    check("prompt includes symbol", "NSE:NIFTY 50" in prompt)
    check("prompt includes pivots", "23,527.38" in prompt or "23527.38" in prompt)
    check("prompt includes news sentiment", "bullish" in prompt)
    check("no-signal note present", "no low-risk setup" in prompt.lower())

    sig = {"direction": "long", "entry": 23622.9, "stop": 23400, "target": 23900}
    p2 = analysis.build_analysis_prompt(snap, news_view=news, signal=sig)
    check("signal note present when provided", "Signal engine: long" in p2)

    check("8 section names defined", len(analysis.SECTIONS) == 8)
    check("Confidence Level is a section", "Confidence Level" in analysis.SECTIONS)


def main() -> int:
    print("\n=== telegram / analysis tests (offline) ===")
    test_chunk_message()
    test_format_signal_alert()
    test_analysis_prompt()
    failed = check.failed  # type: ignore[attr-defined]
    print(f"\nResult: {'PASSED' if failed == 0 else f'FAILED ({failed} checks)'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

"""Offline tests for the news engine — no network, no anthropic SDK.

Covers:
  - feeds.parse_feed_content / normalize / relevance / dedupe (static RSS string)
  - analyzer.build_user_prompt + tag_item with an injected fake LLM

Run:  python -m tests.test_news
"""
from __future__ import annotations

import sys

from src.news import analyzer, feeds

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Test Feed</title>
  <item>
    <title>Nifty 50 hits record high as IT stocks rally</title>
    <link>https://example.com/a</link>
    <description>Benchmark index surged led by Infosys and TCS.</description>
    <pubDate>Mon, 08 Jun 2026 09:30:00 +0530</pubDate>
  </item>
  <item>
    <title>Local bakery wins neighbourhood award</title>
    <link>https://example.com/b</link>
    <description>A community celebration of fresh bread.</description>
    <pubDate>Mon, 08 Jun 2026 08:00:00 +0530</pubDate>
  </item>
  <item>
    <title>RBI holds repo rate steady, signals caution on inflation</title>
    <link>https://example.com/c</link>
    <description>Monetary policy committee keeps rates unchanged.</description>
    <pubDate>Mon, 08 Jun 2026 10:00:00 +0530</pubDate>
  </item>
</channel></rss>"""


def check(name: str, cond: bool) -> None:
    print(f"  [{'OK ' if cond else 'BAD'}] {name}")
    if not cond:
        check.failed += 1  # type: ignore[attr-defined]


check.failed = 0  # type: ignore[attr-defined]


class FakeLLM:
    """Stand-in for LLMClient — returns deterministic tags based on keywords."""

    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, system, user, schema, model=None, max_tokens=200):
        self.calls += 1
        low = user.lower()
        if "nifty" in low or "it stocks" in low:
            return {"relevant": True, "symbol": "NIFTY", "sentiment": "bullish", "confidence": "high"}
        if "rbi" in low or "repo rate" in low:
            return {"relevant": True, "symbol": "macro", "sentiment": "neutral", "confidence": "medium"}
        return {"relevant": False, "symbol": "", "sentiment": "neutral", "confidence": "low"}


def test_feed_parsing() -> None:
    print("feeds.parse_feed_content / normalize:")
    items = feeds.parse_feed_content(SAMPLE_RSS, source="test")
    check("parsed 3 items", len(items) == 3)
    first = items[0]
    check("headline captured", "Nifty 50 hits record high" in first["headline"])
    check("url captured", first["url"] == "https://example.com/a")
    check("ts converted to IST iso", first["ts"].endswith("+05:30"))
    check("tags start empty", first["symbol"] is None and first["sentiment"] is None)


def test_relevance_and_dedupe() -> None:
    print("feeds.is_relevant / dedupe:")
    items = feeds.parse_feed_content(SAMPLE_RSS, source="test")
    relevant = [i for i in items if feeds.is_relevant(i)]
    check("2 of 3 are relevant (bakery dropped)", len(relevant) == 2)

    dupes = items + [items[0]]
    check("dedupe removes the repeat", len(feeds.dedupe(dupes)) == 3)


def test_analyzer_with_fake_llm() -> None:
    print("analyzer.build_user_prompt / tag_item:")
    items = feeds.parse_feed_content(SAMPLE_RSS, source="test")
    prompt = analyzer.build_user_prompt(items[0])
    check("prompt includes headline", "Nifty 50" in prompt)
    check("prompt includes source", "Source: test" in prompt)

    fake = FakeLLM()
    tagged = analyzer.tag_item(items[0], llm=fake)
    check("nifty item -> bullish", tagged["sentiment"] == "bullish")
    check("nifty item -> symbol NIFTY", tagged["symbol"] == "NIFTY")
    check("nifty item -> relevant", tagged["_relevant"] is True)

    bakery = analyzer.tag_item(items[1], llm=fake)
    check("bakery -> not relevant", bakery["_relevant"] is False)
    check("fake llm called per item", fake.calls == 2)


def test_schema_shape() -> None:
    print("analyzer.TAG_SCHEMA (structured-output rules):")
    s = analyzer.TAG_SCHEMA
    check("additionalProperties is False", s.get("additionalProperties") is False)
    check("all properties are required",
          set(s["required"]) == set(s["properties"].keys()))
    check("sentiment is an enum",
          s["properties"]["sentiment"].get("enum") == ["bullish", "bearish", "neutral"])


def main() -> int:
    print("\n=== news engine tests (offline) ===")
    test_feed_parsing()
    test_relevance_and_dedupe()
    test_analyzer_with_fake_llm()
    test_schema_shape()
    failed = check.failed  # type: ignore[attr-defined]
    print(f"\nResult: {'PASSED' if failed == 0 else f'FAILED ({failed} checks)'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

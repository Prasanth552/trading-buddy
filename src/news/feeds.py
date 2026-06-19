"""RSS + announcement polling for Trading Buddy.

Pulls items from the configured feeds (config.NEWS_FEEDS), normalizes them to a
common dict shape, filters for market relevance, and dedupes. Storage-level
dedupe is handled by the UNIQUE(headline, url) constraint in news_items.

Functions are split so the parsing/normalization/relevance logic can be tested
offline against a static RSS string (no network).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import feedparser

import config
from src.utils.logging import get_logger

log = get_logger("feeds")

IST = ZoneInfo(config.TIMEZONE)

# Lightweight relevance filter for NSE/BSE-relevant news. Tune as needed.
RELEVANCE_KEYWORDS: tuple[str, ...] = (
    "nifty", "sensex", "bank nifty", "banknifty", "nse", "bse", "sebi", "rbi",
    "repo rate", "inflation", "cpi", "wpi", "gdp", "rupee", "fii", "dii",
    "earnings", "results", "ipo", "stock", "share", "market", "index",
    "fed", "crude", "oil", "dollar", "us fed", "fomc", "bond yield",
    "auto", "it ", "pharma", "metal", "fmcg", "psu", "psb", "midcap",
)


def _published_to_ist_iso(entry: dict[str, Any]) -> str:
    """Best-effort published timestamp -> IST ISO string."""
    for key in ("published_parsed", "updated_parsed"):
        tm = entry.get(key)
        if tm:
            dt = datetime(*tm[:6], tzinfo=timezone.utc)
            return dt.astimezone(IST).isoformat(timespec="seconds")
    return datetime.now(IST).isoformat(timespec="seconds")


def normalize_entry(entry: dict[str, Any], source: str) -> dict[str, Any]:
    """Convert a feedparser entry (or plain dict) to a news_items-shaped dict."""
    headline = (entry.get("title") or "").strip()
    url = (entry.get("link") or "").strip()
    summary = (entry.get("summary") or entry.get("description") or "").strip()
    return {
        "ts": _published_to_ist_iso(entry),
        "source": source,
        "headline": headline,
        "url": url,
        "symbol": None,
        "sentiment": None,
        "confidence": None,
        "raw_summary": summary[:1000],
    }


def is_relevant(item: dict[str, Any]) -> bool:
    """True if the headline/summary mentions a market-relevant keyword."""
    text = f"{item.get('headline', '')} {item.get('raw_summary', '')}".lower()
    return any(kw in text for kw in RELEVANCE_KEYWORDS)


def parse_feed_content(content: str | bytes, source: str) -> list[dict[str, Any]]:
    """Parse a raw RSS/Atom string (offline-testable) into normalized items."""
    parsed = feedparser.parse(content)
    return [normalize_entry(e, source) for e in parsed.entries]


def _source_name(url: str) -> str:
    """Derive a short source label from a feed URL host."""
    try:
        host = url.split("//", 1)[1].split("/", 1)[0]
        return host.replace("www.", "")
    except (IndexError, AttributeError):
        return url


def fetch_feed(url: str) -> list[dict[str, Any]]:
    """Fetch and normalize a single feed URL (network)."""
    source = _source_name(url)
    parsed = feedparser.parse(url)
    if parsed.bozo:
        log.warning("Feed parse issue for %s: %s", url, parsed.get("bozo_exception"))
    return [normalize_entry(e, source) for e in parsed.entries]


def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop in-batch duplicates by (headline, url)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        key = (it.get("headline", ""), it.get("url", ""))
        if key in seen or not it.get("headline"):
            continue
        seen.add(key)
        out.append(it)
    return out


def poll(
    feeds: list[str] | None = None,
    relevant_only: bool = True,
    max_per_feed: int = 25,
) -> list[dict[str, Any]]:
    """Poll all configured feeds; return normalized, deduped, relevant items."""
    feeds = feeds if feeds is not None else config.NEWS_FEEDS
    collected: list[dict[str, Any]] = []
    for url in feeds:
        try:
            items = fetch_feed(url)[:max_per_feed]
            collected.extend(items)
            log.info("Fetched %d items from %s", len(items), _source_name(url))
        except Exception as exc:  # noqa: BLE001 - one bad feed shouldn't stop the rest
            log.error("Failed to fetch %s: %s", url, exc)
    collected = dedupe(collected)
    if relevant_only:
        collected = [i for i in collected if is_relevant(i)]
    return collected

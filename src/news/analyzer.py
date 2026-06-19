"""LLM news tagging for Trading Buddy.

Each news item is sent to the fast model (Haiku) and tagged with:
  - symbol/sector affected (free text, e.g. "NIFTY", "Banking", "IT", "macro")
  - sentiment   : bullish | bearish | neutral
  - confidence  : low | medium | high
  - relevant    : whether it has any clear market relevance at all

Schema/prompt construction is split from the network call so it can be
unit-tested offline without the anthropic SDK installed.
"""
from __future__ import annotations

from typing import Any

from src.storage import db
from src.utils.logging import get_logger

import config

log = get_logger("analyzer")

# Structured-output schema (json_schema). Note: all objects need
# additionalProperties:false and every property listed in `required`.
TAG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevant": {
            "type": "boolean",
            "description": "True only if the item has clear NSE/BSE market relevance.",
        },
        "symbol": {
            "type": "string",
            "description": "Affected index/stock/sector or 'macro'; '' if none.",
        },
        "sentiment": {
            "type": "string",
            "enum": ["bullish", "bearish", "neutral"],
        },
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
        },
    },
    "required": ["relevant", "symbol", "sentiment", "confidence"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You are a financial news classifier for NSE/BSE India (NIFTY 50, Bank NIFTY, "
    "SENSEX, sectors, and macro). For the given headline + summary, decide whether "
    "it has clear market relevance, the most affected symbol/sector (or 'macro' for "
    "broad economic news, '' if none), the sentiment for that symbol, and your "
    "confidence. Be conservative: if there is no clear market impact, set relevant "
    "to false, sentiment to neutral, confidence to low. Respond only via the schema."
)


def build_user_prompt(item: dict[str, Any]) -> str:
    """Construct the per-item user prompt (pure — offline-testable)."""
    headline = item.get("headline", "") or ""
    summary = (item.get("raw_summary", "") or "")[:600]
    source = item.get("source", "") or "?"
    return (
        f"Source: {source}\n"
        f"Headline: {headline}\n"
        f"Summary: {summary}\n\n"
        "Classify this item."
    )


def tag_item(item: dict[str, Any], llm: Any | None = None) -> dict[str, Any]:
    """Tag one news item via the LLM; returns the item augmented with tags.

    ``llm`` may be injected (e.g. a fake in tests). If None, a real LLMClient
    is created lazily so importing this module doesn't require the anthropic SDK.
    """
    if llm is None:
        from src.llm.client import LLMClient  # lazy import
        llm = LLMClient()

    try:
        tags = llm.complete_json(
            system=SYSTEM_PROMPT,
            user=build_user_prompt(item),
            schema=TAG_SCHEMA,
            model=config.LLM_FAST_MODEL,
            max_tokens=200,
        )
    except Exception as exc:  # noqa: BLE001 - one bad item shouldn't stop the batch
        log.error("Tagging failed for %r: %s", item.get("headline", "")[:60], exc)
        tags = {"relevant": False, "symbol": "", "sentiment": "neutral", "confidence": "low"}

    return {
        **item,
        "symbol": tags.get("symbol") or None,
        "sentiment": tags.get("sentiment"),
        "confidence": tags.get("confidence"),
        "_relevant": bool(tags.get("relevant")),
    }


def analyze_and_store(
    items: list[dict[str, Any]],
    llm: Any | None = None,
    max_items: int = 30,
    store_irrelevant: bool = False,
) -> list[dict[str, Any]]:
    """Tag a batch of items and persist them to ``news_items``.

    Returns the tagged items that were stored (new, non-duplicate). Duplicates
    are skipped by the UNIQUE(headline, url) constraint in the DB layer.
    """
    # Skip items already in the DB BEFORE tagging — avoids wasting LLM calls
    # re-tagging the same headlines on every polling cycle.
    fresh = [it for it in items if not db.news_exists(it.get("headline", ""), it.get("url"))]
    skipped = len(items) - len(fresh)
    if skipped:
        log.info("Skipped %d already-stored item(s) before tagging.", skipped)

    stored: list[dict[str, Any]] = []
    for item in fresh[:max_items]:
        tagged = tag_item(item, llm=llm)
        if not tagged["_relevant"] and not store_irrelevant:
            continue
        row_id = db.insert_news_item(tagged)
        if row_id is not None:
            tagged["id"] = row_id
            stored.append(tagged)
            log.info(
                "Tagged [%s/%s/%s] %s",
                tagged.get("symbol"), tagged.get("sentiment"),
                tagged.get("confidence"), tagged.get("headline", "")[:70],
            )
    return stored

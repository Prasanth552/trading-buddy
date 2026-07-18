"""Anthropic LLM wrapper for Trading Buddy.

Two models (see config):
  - LLM_FAST_MODEL  (Haiku) for news tagging / routine classification
  - LLM_SMART_MODEL (Sonnet) for trade-decision reasoning

The SDK auto-retries 429/5xx with exponential backoff. We keep calls small and
use structured outputs (output_config.format) for classification so responses
are valid JSON without fragile text parsing.
"""
from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv

import config
from src.utils.logging import get_logger

load_dotenv()
log = get_logger("llm")


class LLMError(RuntimeError):
    """Configuration / call problems in the LLM client."""


class LLMClient:
    """Thin wrapper over the anthropic SDK (lazy import)."""

    def __init__(self, api_key: str | None = None, max_retries: int = 3) -> None:
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY missing — set it in .env")
        try:
            import anthropic  # imported lazily so Phase 0/1 don't need it
        except ImportError as exc:  # pragma: no cover
            raise LLMError(
                "anthropic not installed — run: pip install -r requirements.txt"
            ) from exc
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=self.api_key, max_retries=max_retries)

    def complete_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: str | None = None,
        max_tokens: int = 400,
    ) -> dict[str, Any]:
        """Return a JSON object constrained to ``schema`` via structured outputs."""
        model = model or config.LLM_FAST_MODEL
        resp = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(text)

    def complete_text(
        self,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        """Plain text completion (used by the signal/decision step in Phase 3)."""
        model = model or config.LLM_SMART_MODEL
        resp = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return next((b.text for b in resp.content if b.type == "text"), "")

    def analyze_image(
        self,
        system: str,
        image_b64: str,
        media_type: str,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        """Send an image (base64) + text prompt and return the analysis."""
        model = model or config.LLM_SMART_MODEL
        resp = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type, "data": image_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return next((b.text for b in resp.content if b.type == "text"), "")

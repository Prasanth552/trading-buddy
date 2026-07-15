"""Upstox execution client — places (sandbox/dummy) option orders.

Plan: market data + analysis stay on Kite; the actual trade is placed on Upstox.
The Upstox **sandbox** uses a separate 30-day access token (UPSTOX_SANDBOX_TOKEN),
so there is no daily login on the execution side.

Order API (v3):  POST {UPSTOX_ORDER_URL}
  headers: Authorization: Bearer <token>, Content-Type/Accept: application/json
  body:    instrument_token ("NSE_FO|43919"), quantity, product, validity,
           order_type, transaction_type, price, trigger_price, ...

Instrument mapping (pick_upstox_option) is pure and offline-testable; the HTTP
calls are thin and must be confirmed against the live sandbox on first use.
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

import config
from src.utils.logging import get_logger

log = get_logger("upstox")
IST = ZoneInfo(config.TIMEZONE)


class UpstoxError(RuntimeError):
    """Configuration / API problems in the Upstox client."""


def _expiry_to_date(value: Any) -> date | None:
    """Coerce an Upstox expiry (epoch ms | ISO string | date) to an IST date."""
    if value in (None, "", 0):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # Upstox publishes expiry as epoch milliseconds.
        return datetime.fromtimestamp(value / 1000, tz=IST).date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def pick_upstox_option(
    instruments: list[dict[str, Any]],
    name: str,
    expiry: date,
    strike: float,
    option_type: str,
    segment: str,
) -> dict[str, Any] | None:
    """Find the Upstox instrument matching an option by its real attributes.

    Matches on segment + underlying name + instrument_type (CE/PE) + strike + expiry,
    so it works regardless of broker-specific tradingsymbol formatting.
    """
    for inst in instruments:
        if inst.get("segment") != segment:
            continue
        if inst.get("name") != name:
            continue
        if inst.get("instrument_type") != option_type:
            continue
        if abs(float(inst.get("strike_price", -1)) - float(strike)) > 0.001:
            continue
        if _expiry_to_date(inst.get("expiry")) != expiry:
            continue
        return inst
    return None


class UpstoxClient:
    """Thin Upstox order client (sandbox token-based)."""

    def __init__(self, sandbox_token: str | None = None) -> None:
        # Prefer the fresh daily OAuth token (refreshed by the data login) over
        # the separate sandbox token, which expires unpredictably (UDAPI100050
        # "Invalid token" on Jul 15). Fall back to UPSTOX_SANDBOX_TOKEN.
        self.token = sandbox_token
        if not self.token:
            try:
                from src.broker.upstox_data import load_cached_token
                self.token = load_cached_token()
            except Exception:  # noqa: BLE001
                self.token = None
        if not self.token:
            self.token = os.getenv("UPSTOX_SANDBOX_TOKEN")
        if not self.token:
            raise UpstoxError(
                "No Upstox token — run `python -m src.broker.upstox_data` to log in, "
                "or set UPSTOX_SANDBOX_TOKEN in .env."
            )
        self._instruments: list[dict[str, Any]] | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # --- instruments -------------------------------------------------------
    def load_instruments(self, url: str = config.UPSTOX_INSTRUMENTS_URL) -> list[dict[str, Any]]:
        """Download + decompress the Upstox instrument master (cached per client)."""
        if self._instruments is None:
            log.info("Downloading Upstox instrument master...")
            raw = requests.get(url, timeout=60).content
            self._instruments = json.loads(gzip.decompress(raw))
            log.info("Loaded %d Upstox instruments.", len(self._instruments))
        return self._instruments

    def resolve_option(
        self, index_symbol: str, expiry: date, strike: float, option_type: str,
    ) -> dict[str, Any] | None:
        """Resolve the Upstox instrument for an index option."""
        spec = config.UPSTOX_OPTION_SEGMENTS.get(index_symbol)
        if not spec:
            log.warning("No Upstox segment mapping for %s", index_symbol)
            return None
        return pick_upstox_option(
            self.load_instruments(), spec["name"], expiry, strike, option_type, spec["segment"])

    # --- orders ------------------------------------------------------------
    def place_order(
        self,
        instrument_token: str,
        quantity: int,
        transaction_type: str,
        order_type: str = config.UPSTOX_ENTRY_ORDER_TYPE,
        price: float = 0,
        trigger_price: float = 0,
        product: str = config.UPSTOX_PRODUCT,
        tag: str = "trading-buddy",
    ) -> dict[str, Any]:
        """Place one order via the Upstox sandbox; returns the parsed response."""
        body = {
            "quantity": quantity,
            "product": product,
            "validity": config.UPSTOX_VALIDITY,
            "price": price,
            "instrument_token": instrument_token,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": trigger_price,
            "is_amo": False,
            "tag": tag,
        }
        log.info("UPSTOX ORDER (pre-submit) %s %s x%d", transaction_type, instrument_token, quantity)
        resp = requests.post(config.UPSTOX_ORDER_URL, json=body, headers=self._headers(), timeout=15)
        data = resp.json()
        if data.get("status") != "success":
            raise UpstoxError(f"Upstox order failed: {data}")
        order_ids = data.get("data", {}).get("order_ids", [])
        log.info("UPSTOX ORDER ok ids=%s", order_ids)
        return {"order_ids": order_ids, "raw": data}

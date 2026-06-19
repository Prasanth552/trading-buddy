"""Zerodha Kite client — auth, market data, and (later) orders.

Thin wrapper over ``kiteconnect.KiteConnect``. Credentials come from the
environment (loaded from ``.env``). Order placement is added in Phase 5.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

from src.utils.logging import get_logger

load_dotenv()
log = get_logger("kite")


class KiteClientError(RuntimeError):
    """Raised for configuration / auth problems in the Kite client."""


class KiteClient:
    """Wrapper around KiteConnect with lazy import so Phase 0 stays import-light."""

    def __init__(self, api_key: str | None = None, api_secret: str | None = None) -> None:
        self.api_key = api_key or os.getenv("KITE_API_KEY")
        self.api_secret = api_secret or os.getenv("KITE_API_SECRET")
        if not self.api_key:
            raise KiteClientError("KITE_API_KEY missing — set it in .env")
        try:
            from kiteconnect import KiteConnect  # imported lazily
        except ImportError as exc:  # pragma: no cover
            raise KiteClientError(
                "kiteconnect not installed — run: pip install -r requirements.txt"
            ) from exc
        self.kite = KiteConnect(api_key=self.api_key)

    # --- Auth ---------------------------------------------------------------
    def login_url(self) -> str:
        """URL the user opens to log in and obtain a request_token."""
        return self.kite.login_url()

    def generate_session(self, request_token: str) -> str:
        """Exchange a request_token for the day's access_token and set it."""
        if not self.api_secret:
            raise KiteClientError("KITE_API_SECRET missing — set it in .env")
        data = self.kite.generate_session(request_token, api_secret=self.api_secret)
        token = data["access_token"]
        self.kite.set_access_token(token)
        log.info("Kite session established for %s", data.get("user_id", "?"))
        return token

    def set_access_token(self, access_token: str) -> None:
        self.kite.set_access_token(access_token)

    def profile(self) -> dict[str, Any]:
        return self.kite.profile()

    # --- Market data --------------------------------------------------------
    def ltp(self, symbols: list[str]) -> dict[str, Any]:
        """Last traded price + instrument_token for ``EXCHANGE:SYMBOL`` strings."""
        return self.kite.ltp(symbols)

    def quote(self, symbols: list[str]) -> dict[str, Any]:
        return self.kite.quote(symbols)

    def historical_data(
        self,
        instrument_token: int,
        from_dt: datetime,
        to_dt: datetime,
        interval: str,
        continuous: bool = False,
        oi: bool = False,
    ) -> list[dict[str, Any]]:
        """Historical OHLC candles for an instrument token."""
        return self.kite.historical_data(
            instrument_token, from_dt, to_dt, interval, continuous=continuous, oi=oi
        )

    def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        return self.kite.instruments(exchange) if exchange else self.kite.instruments()

    # --- Account (used by later phases) ------------------------------------
    def positions(self) -> dict[str, Any]:
        return self.kite.positions()

    def orders(self) -> list[dict[str, Any]]:
        return self.kite.orders()

"""Alice Blue broker client — independent from Upstox.

Auth: API key + secret, daily session refresh. No TOTP needed.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterator
from zoneinfo import ZoneInfo

log = logging.getLogger("trading_buddy.alice_blue")

IST = ZoneInfo("Asia/Kolkata")

# ── credentials (from env) ──────────────────────────────────────────
AB_CLIENT_ID = os.getenv("AB_CLIENT_ID", "")
AB_API_KEY = os.getenv("AB_API_KEY", "")
AB_SECRET_KEY = os.getenv("AB_SECRET_KEY", "")
AB_SIMULATE = os.getenv("AB_SIMULATE_ORDERS", "true").lower() in ("1", "true", "yes")

# ── DB ──────────────────────────────────────────────────────────────
AB_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "alice_blue.db")

AB_SCHEMA = """
CREATE TABLE IF NOT EXISTS ab_trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL,
    qty            INTEGER,
    price          REAL,
    order_id       TEXT,
    status         TEXT DEFAULT 'OPEN',
    exit_price     REAL,
    pnl            REAL,
    stop_price     REAL,
    target_price   REAL,
    peak_pnl       REAL DEFAULT 0,
    channel        TEXT DEFAULT 'ch2',
    exit_reason    TEXT,
    broker_key     TEXT,
    instrument     TEXT
);

CREATE TABLE IF NOT EXISTS ab_daily (
    date TEXT PRIMARY KEY,
    pnl  REAL DEFAULT 0,
    trades INTEGER DEFAULT 0
);
"""


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    path = os.path.abspath(AB_DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(AB_SCHEMA)
    log.info("Alice Blue DB initialized at %s", os.path.abspath(AB_DB_PATH))


class AliceBlueClient:
    """Wrapper around pya3 for order placement and data."""

    def __init__(self):
        self._alice = None
        self._session_date: str | None = None

    def login(self) -> bool:
        try:
            from pya3 import Aliceblue
            self._alice = Aliceblue(user_id=AB_CLIENT_ID, api_key=AB_API_KEY)
            session_id = self._alice.get_session_id()
            if session_id:
                self._session_date = datetime.now(IST).strftime("%Y-%m-%d")
                log.info("Alice Blue login OK — session for %s", self._session_date)
                return True
            log.error("Alice Blue login failed — no session_id returned")
            return False
        except Exception as exc:
            log.error("Alice Blue login error: %s", exc)
            return False

    def ensure_session(self):
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if self._session_date != today or self._alice is None:
            log.info("Alice Blue session expired/missing, logging in...")
            self.login()

    def get_instrument(self, symbol: str, expiry: str, strike: float,
                       is_ce: bool) -> Any:
        self.ensure_session()
        if not self._alice:
            return None
        try:
            return self._alice.get_instrument_for_fno(
                exch="NFO", symbol=symbol, expiry=expiry,
                is_fut=False, strike=strike, is_CE=is_ce,
            )
        except Exception as exc:
            log.error("get_instrument failed: %s", exc)
            return None

    def get_ltp(self, instrument) -> float | None:
        self.ensure_session()
        if not self._alice or not instrument:
            return None
        try:
            info = self._alice.get_scrip_info(instrument)
            if info and "last_trade_price" in info:
                return float(info["last_trade_price"])
            if info and "ltp" in info:
                return float(info["ltp"])
        except Exception as exc:
            log.error("get_ltp failed: %s", exc)
        return None

    def place_order(self, instrument, qty: int, side: str = "BUY",
                    order_type: str = "MARKET") -> dict:
        self.ensure_session()
        if AB_SIMULATE:
            log.info("[SIMULATE] Alice Blue %s %s x%d", side, instrument, qty)
            return {"status": "simulated", "order_id": f"SIM_{int(time.time())}"}
        if not self._alice:
            return {"status": "error", "reason": "not logged in"}
        try:
            from pya3 import TransactionType, OrderType, ProductType
            txn = TransactionType.Buy if side == "BUY" else TransactionType.Sell
            ot = OrderType.Market if order_type == "MARKET" else OrderType.Limit
            result = self._alice.place_order(
                transaction_type=txn,
                instrument=instrument,
                quantity=qty,
                order_type=ot,
                product_type=ProductType.Intraday,
            )
            log.info("Alice Blue order placed: %s", result)
            return {"status": "placed", "order_id": str(result)}
        except Exception as exc:
            log.error("Alice Blue place_order error: %s", exc)
            return {"status": "error", "reason": str(exc)}

    def exit_order(self, instrument, qty: int) -> dict:
        return self.place_order(instrument, qty, side="SELL")

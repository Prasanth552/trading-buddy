"""SQLite storage layer for Trading Buddy.

Schema (see build spec §7):
  news_items, signals, trades, daily_state, app_log

Use ``init_db()`` once at startup to create tables. All helpers open a
short-lived connection; SQLite handles this fine for our low write volume.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    source      TEXT,
    headline    TEXT NOT NULL,
    url         TEXT,
    symbol      TEXT,
    sentiment   TEXT,
    confidence  TEXT,
    raw_summary TEXT,
    UNIQUE(headline, url)
);

CREATE TABLE IF NOT EXISTS signals (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry     REAL,
    stop      REAL,
    target    REAL,
    qty       INTEGER,
    max_risk  REAL,
    rationale TEXT,
    status    TEXT DEFAULT 'new'
);

CREATE TABLE IF NOT EXISTS trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id  INTEGER,
    ts         TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL,
    qty        INTEGER,
    price      REAL,
    order_id   TEXT,
    mode       TEXT,
    status     TEXT,
    exit_price REAL,
    pnl        REAL,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS daily_state (
    date                TEXT PRIMARY KEY,
    trades_count        INTEGER DEFAULT 0,
    realised_pnl        REAL DEFAULT 0,
    kill_switch_tripped INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT,
    message TEXT
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Context-managed connection that commits on success, rolls back on error."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create all tables if they do not already exist, and run light migrations."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Migration: option-premium stop/target + broker instrument key on the row.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
        for col in ("stop_price", "target_price"):
            if col not in cols:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
        if "broker_key" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN broker_key TEXT")


def table_names() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        ).fetchall()
    return [r["name"] for r in rows]


# --- Lightweight insert helpers (extended in later phases) -----------------

def insert_app_log(ts: str, level: str, message: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_log (ts, level, message) VALUES (?, ?, ?)",
            (ts, level, message),
        )


def news_exists(headline: str, url: str | None) -> bool:
    """True if a news item with this (headline, url) is already stored."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM news_items WHERE headline = ? AND IFNULL(url,'') = IFNULL(?,'') LIMIT 1",
            (headline, url),
        ).fetchone() is not None


def insert_news_item(item: dict[str, Any]) -> int | None:
    """Insert a news item; returns row id, or None if a duplicate."""
    with get_conn() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO news_items
                   (ts, source, headline, url, symbol, sentiment, confidence, raw_summary)
                   VALUES (:ts, :source, :headline, :url, :symbol, :sentiment,
                           :confidence, :raw_summary)""",
                {k: item.get(k) for k in
                 ("ts", "source", "headline", "url", "symbol",
                  "sentiment", "confidence", "raw_summary")},
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # duplicate headline/url


def insert_signal(signal: dict[str, Any]) -> int:
    """Insert a signal row; returns its id."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO signals
               (ts, symbol, direction, entry, stop, target, qty, max_risk, rationale, status)
               VALUES (:ts, :symbol, :direction, :entry, :stop, :target,
                       :qty, :max_risk, :rationale, :status)""",
            {k: signal.get(k) for k in
             ("ts", "symbol", "direction", "entry", "stop", "target",
              "qty", "max_risk", "rationale", "status")},
        )
        return cur.lastrowid


def recent_news_for_keyword(keyword: str, since_iso: str) -> list[sqlite3.Row]:
    """News items whose symbol matches ``keyword`` (or 'macro') since ``since_iso``."""
    like = f"%{keyword}%"
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM news_items
               WHERE ts >= ? AND (symbol LIKE ? OR symbol = 'macro')
               ORDER BY ts DESC""",
            (since_iso, like),
        ).fetchall()


def insert_trade(trade: dict[str, Any]) -> int:
    """Insert a trade row (entry or stop leg); returns its id."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (signal_id, ts, symbol, side, qty, price, order_id, mode, status,
                exit_price, pnl, stop_price, target_price, broker_key)
               VALUES (:signal_id, :ts, :symbol, :side, :qty, :price, :order_id,
                       :mode, :status, :exit_price, :pnl, :stop_price, :target_price,
                       :broker_key)""",
            {k: trade.get(k) for k in
             ("signal_id", "ts", "symbol", "side", "qty", "price", "order_id",
              "mode", "status", "exit_price", "pnl", "stop_price", "target_price",
              "broker_key")},
        )
        return cur.lastrowid


def get_open_positions(mode: str = "PAPER") -> list[sqlite3.Row]:
    """Open long-option positions for a mode (entry BUY rows with a stop set)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM trades WHERE mode=? AND side='BUY' "
            "AND status='OPEN' AND stop_price IS NOT NULL ORDER BY id",
            (mode,),
        ).fetchall()


def get_open_paper_positions() -> list[sqlite3.Row]:
    """Open PAPER long-option positions (entry BUY rows with a stop set)."""
    return get_open_positions("PAPER")


def close_position(trade_id: int, exit_price: float, pnl: float, status: str) -> None:
    """Close a position row: record exit price, P&L, and final status."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE trades SET exit_price=?, pnl=?, status=? WHERE id=?",
            (exit_price, pnl, status, trade_id),
        )
        # Mark any sibling protective-stop legs for the same signal as cancelled.
        row = conn.execute("SELECT signal_id FROM trades WHERE id=?", (trade_id,)).fetchone()
        if row and row["signal_id"] is not None:
            conn.execute(
                "UPDATE trades SET status='CANCELLED' WHERE signal_id=? AND side='SELL' "
                "AND status='TRIGGER_PENDING'", (row["signal_id"],))


def bump_trades_count(date_iso: str, by: int = 1) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO daily_state (date) VALUES (?)", (date_iso,))
        conn.execute(
            "UPDATE daily_state SET trades_count = trades_count + ? WHERE date = ?",
            (by, date_iso),
        )


def add_realised_pnl(date_iso: str, amount: float) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO daily_state (date) VALUES (?)", (date_iso,))
        conn.execute(
            "UPDATE daily_state SET realised_pnl = realised_pnl + ? WHERE date = ?",
            (amount, date_iso),
        )


def get_or_create_daily_state(date_iso: str) -> sqlite3.Row:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO daily_state (date) VALUES (?)", (date_iso,)
        )
        return conn.execute(
            "SELECT * FROM daily_state WHERE date = ?", (date_iso,)
        ).fetchone()

"""Market data retrieval (Kite) -> pandas OHLC + technical snapshots.

Resolves instrument tokens for the configured watchlist, pulls live LTP and
historical candles, and assembles per-symbol technical snapshots using
``src.data.indicators``.

All datetimes are IST (Asia/Kolkata).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pandas as pd

import config
from src.broker.kite_client import KiteClient
from src.data import indicators
from src.utils import market_calendar as mc
from src.utils.logging import get_logger

log = get_logger("market_data")

_OHLC_FIELDS = ("open", "high", "low", "close", "volume")


def candles_to_df(candles: list[dict[str, Any]]) -> pd.DataFrame:
    """Convert Kite historical_data() output to a tidy OHLC DataFrame (oldest first)."""
    if not candles:
        return pd.DataFrame(columns=["date", *_OHLC_FIELDS]).set_index("date")
    df = pd.DataFrame(candles)
    df = df.rename(columns={"date": "date"})
    keep = ["date", *[c for c in _OHLC_FIELDS if c in df.columns]]
    df = df[keep].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df


def resolve_tokens(client: KiteClient, symbols: list[str]) -> dict[str, Any]:
    """Map ``EXCHANGE:SYMBOL`` strings to instrument tokens via the LTP endpoint.

    Tokens are kept as-is: Kite returns ints, Upstox returns string keys like
    ``NSE_INDEX|Nifty 50`` — both are passed straight back to historical_data().
    """
    data = client.ltp(symbols)
    tokens: dict[str, Any] = {}
    for sym in symbols:
        info = data.get(sym)
        if info and info.get("instrument_token") is not None:
            tokens[sym] = info["instrument_token"]
        else:
            log.warning("Could not resolve instrument token for %s", sym)
    return tokens


def fetch_ohlc(
    client: KiteClient,
    instrument_token: int,
    timeframe: str = config.PRIMARY_TIMEFRAME,
    days: int = 5,
) -> pd.DataFrame:
    """Fetch recent OHLC candles for an instrument on the given timeframe."""
    interval = config.KITE_INTERVALS.get(timeframe, timeframe)
    to_dt = mc.now_ist()
    from_dt = to_dt - timedelta(days=days)
    candles = client.historical_data(instrument_token, from_dt, to_dt, interval)
    return candles_to_df(candles)


def previous_day_ohlc(client: KiteClient, instrument_token: int) -> dict[str, float] | None:
    """Previous *trading day's* daily OHLC (for classic pivots)."""
    to_dt = mc.now_ist()
    from_dt = to_dt - timedelta(days=10)  # cushion for weekends/holidays
    candles = client.historical_data(instrument_token, from_dt, to_dt, "day")
    df = candles_to_df(candles)
    if len(df) < 2:
        return None
    # Last fully-formed prior session is the second-to-last row if today is present,
    # else the last row. Use the most recent row strictly before today's date.
    today = mc.now_ist().date()
    prior = df[df.index.date < today]
    row = prior.iloc[-1] if len(prior) else df.iloc[-2]
    return {"high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"])}


def snapshot_for_symbol(
    client: KiteClient, symbol: str, token: int
) -> dict[str, Any]:
    """Build a full technical snapshot for one symbol."""
    intraday = fetch_ohlc(client, token, config.PRIMARY_TIMEFRAME, days=5)
    prev_day = previous_day_ohlc(client, token)
    ltp_data = client.ltp([symbol]).get(symbol, {})
    ltp = ltp_data.get("last_price")
    return indicators.build_snapshot(symbol, intraday, prev_day, ltp=ltp)


def snapshot_watchlist(
    client: KiteClient, symbols: list[str] | None = None
) -> list[dict[str, Any]]:
    """Build technical snapshots for every configured symbol."""
    symbols = symbols or [*config.WATCHLIST, *config.WATCH_ONLY]
    tokens = resolve_tokens(client, symbols)
    out: list[dict[str, Any]] = []
    for sym in symbols:
        token = tokens.get(sym)
        if token is None:
            log.warning("Skipping %s — no instrument token", sym)
            continue
        try:
            out.append(snapshot_for_symbol(client, sym, token))
        except Exception as exc:  # noqa: BLE001 - one bad symbol shouldn't kill the loop
            log.error("Snapshot failed for %s: %s", sym, exc)
    return out

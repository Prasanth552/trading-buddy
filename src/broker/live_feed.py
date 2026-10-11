"""Upstox WebSocket v3 live market data streamer.

Connects to the Upstox Market Data Feed v3 WebSocket and streams real-time
LTP (last traded price) for subscribed instruments. Data is stored in an
in-memory dict for zero-latency reads by the dashboard.

Usage (standalone test):
    PYTHONPATH=. .venv/bin/python3 -m src.broker.live_feed

Usage (from dashboard):
    from src.broker.live_feed import LiveFeed
    feed = LiveFeed(access_token)
    await feed.start()
    feed.subscribe(["NSE_FO|12345", ...])
    price = feed.get_ltp("NSE_FO|12345")  # instant, from memory
"""
from __future__ import annotations

import asyncio
import json
import ssl
import time
from typing import Any

import requests
import websockets

from src.utils.logging import get_logger

log = get_logger("live_feed")

# Lazy-import the compiled protobuf module
_pb2 = None

def _get_pb2():
    global _pb2
    if _pb2 is None:
        from src.broker import MarketDataFeedV3_pb2
        _pb2 = MarketDataFeedV3_pb2
    return _pb2


class LiveFeed:
    """Persistent WebSocket streamer for real-time market data."""

    AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"

    def __init__(self, access_token: str) -> None:
        self._token = access_token
        self._ws: Any = None
        self._running = False
        self._subscribed: set[str] = set()
        # instrument_key -> {ltp, ltt, ltq, cp, ts}
        self._ltp_cache: dict[str, dict] = {}
        self._connected = False
        self._last_tick_ts: float = 0.0
        self._reconnect_delay = 1.0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def subscribed_count(self) -> int:
        return len(self._subscribed)

    @property
    def last_tick_age(self) -> float:
        if self._last_tick_ts == 0:
            return float("inf")
        return time.time() - self._last_tick_ts

    def get_ltp(self, instrument_key: str) -> float | None:
        entry = self._ltp_cache.get(instrument_key)
        return entry["ltp"] if entry else None

    def get_all_ltp(self) -> dict[str, dict]:
        return dict(self._ltp_cache)

    def _authorize(self) -> str:
        resp = requests.get(
            self.AUTHORIZE_URL,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            timeout=10,
        )
        data = resp.json()
        if resp.status_code != 200 or data.get("status") == "error":
            raise RuntimeError(f"WS authorize failed: {data}")
        return data["data"]["authorized_redirect_uri"]

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        asyncio.create_task(self._connect_loop())
        log.info("LiveFeed started")

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        self._connected = False
        log.info("LiveFeed stopped")

    def subscribe(self, instrument_keys: list[str]) -> None:
        new_keys = set(instrument_keys) - self._subscribed
        if not new_keys:
            return
        self._subscribed.update(new_keys)
        if self._ws and self._connected:
            asyncio.ensure_future(self._send_subscribe(list(new_keys)))

    def unsubscribe(self, instrument_keys: list[str]) -> None:
        remove_keys = set(instrument_keys) & self._subscribed
        if not remove_keys:
            return
        self._subscribed -= remove_keys
        for k in remove_keys:
            self._ltp_cache.pop(k, None)
        if self._ws and self._connected:
            asyncio.ensure_future(self._send_unsubscribe(list(remove_keys)))

    async def _send_subscribe(self, keys: list[str]) -> None:
        if not self._ws:
            return
        msg = {"guid": "sub", "method": "sub", "data": {"mode": "ltpc", "instrumentKeys": keys}}
        try:
            await self._ws.send(json.dumps(msg).encode("utf-8"))
            log.info("Subscribed %d instruments (total %d)", len(keys), len(self._subscribed))
        except Exception as e:
            log.warning("Subscribe send failed: %s", e)

    async def _send_unsubscribe(self, keys: list[str]) -> None:
        if not self._ws:
            return
        msg = {"guid": "unsub", "method": "unsub", "data": {"mode": "ltpc", "instrumentKeys": keys}}
        try:
            await self._ws.send(json.dumps(msg).encode("utf-8"))
            log.info("Unsubscribed %d instruments", len(keys))
        except Exception as e:
            log.warning("Unsubscribe send failed: %s", e)

    async def _connect_loop(self) -> None:
        while self._running:
            try:
                ws_url = self._authorize()
                log.info("Connecting to Upstox WS feed...")

                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

                async with websockets.connect(ws_url, ssl=ssl_ctx) as ws:
                    self._ws = ws
                    self._connected = True
                    self._reconnect_delay = 1.0
                    log.info("WebSocket connected")

                    await asyncio.sleep(0.5)
                    if self._subscribed:
                        await self._send_subscribe(list(self._subscribed))

                    async for message in ws:
                        if isinstance(message, bytes):
                            self._process_message(message)
                        # text messages are heartbeats, ignore

            except Exception as e:
                log.warning("WebSocket error: %s — reconnecting in %.0fs", e, self._reconnect_delay)
            finally:
                self._connected = False
                self._ws = None

            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    def _process_message(self, raw: bytes) -> None:
        try:
            pb2 = _get_pb2()
            resp = pb2.FeedResponse()
            resp.ParseFromString(raw)

            for inst_key, feed in resp.feeds.items():
                ltpc = None
                if feed.HasField("ltpc"):
                    ltpc = feed.ltpc
                elif feed.HasField("fullFeed"):
                    ff = feed.fullFeed
                    if ff.HasField("marketFF") and ff.marketFF.HasField("ltpc"):
                        ltpc = ff.marketFF.ltpc
                    elif ff.HasField("indexFF") and ff.indexFF.HasField("ltpc"):
                        ltpc = ff.indexFF.ltpc

                if ltpc and ltpc.ltp > 0:
                    self._ltp_cache[inst_key] = {
                        "ltp": ltpc.ltp,
                        "ltt": ltpc.ltt,
                        "ltq": ltpc.ltq,
                        "cp": ltpc.cp,
                        "ts": time.time(),
                    }
                    self._last_tick_ts = time.time()
        except Exception as e:
            log.debug("Protobuf decode error: %s", e)


# --- Singleton access for the dashboard ---
_feed_instance: LiveFeed | None = None


def get_feed() -> LiveFeed | None:
    return _feed_instance


async def start_feed(access_token: str) -> LiveFeed:
    global _feed_instance
    if _feed_instance and _feed_instance.connected:
        return _feed_instance
    _feed_instance = LiveFeed(access_token)
    await _feed_instance.start()
    return _feed_instance


# --- Standalone test ---
if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from src.broker.upstox_data import load_cached_token

    async def _test():
        token = load_cached_token()
        if not token:
            print("No cached token — run auto login first")
            return
        feed = LiveFeed(token)
        await feed.start()
        feed.subscribe(["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"])
        for _ in range(30):
            await asyncio.sleep(1)
            for k, v in feed.get_all_ltp().items():
                print(f"  {k}: ₹{v['ltp']:,.2f}  (age {time.time()-v['ts']:.1f}s)")
        await feed.stop()

    asyncio.run(_test())

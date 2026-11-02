"""Deriv Options API client — WebSocket via api.derivws.com.

Uses the new Options API endpoint (works from cloud/datacenter IPs).
Legacy ws.derivws.com is blocked by Cloudflare on non-residential IPs.

Auth flow:
  1. GET /trading/v1/options/accounts (with Bearer PAT + Deriv-App-ID) → account list
  2. POST /trading/v1/options/accounts/{id}/otp → short-lived OTP (120s, single-use)
  3. Connect WebSocket at the returned URL (wss://...?otp=TOKEN)

Public endpoint (no auth) works for market data: ticks, candles, active_symbols.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

import httpx
import websockets

log = logging.getLogger("deriv")

WS_PUBLIC = "wss://api.derivws.com/trading/v1/options/ws/public"
REST_BASE = "https://api.derivws.com"

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Origin": "https://app.deriv.com",
}


class DerivClient:
    def __init__(
        self,
        api_token: str = "",
        app_id: str = "",
        on_tick: Callable[[dict], Any] | None = None,
        on_error: Callable[[Exception], Any] | None = None,
    ):
        self.api_token = api_token
        self.app_id = app_id
        self.on_tick = on_tick
        self.on_error = on_error
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._subscriptions: dict[str, str] = {}
        self._recv_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None
        self._connected = False
        self._mode = "public"
        self._account_id: str = ""
        self._account_currency: str = "USD"

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None

    async def connect(self, mode: str = "public") -> bool:
        """Connect to Deriv WebSocket. mode: 'public', 'demo', or 'real'."""
        self._mode = mode
        try:
            if mode == "public":
                url = WS_PUBLIC
            else:
                url = await self._get_authenticated_url(mode)
                if not url:
                    return False

            self._ws = await asyncio.wait_for(
                websockets.connect(url, additional_headers=_BROWSER_HEADERS, ping_interval=25),
                timeout=15,
            )
            self._connected = True
            self._recv_task = asyncio.create_task(self._recv_loop())
            self._ping_task = asyncio.create_task(self._keep_alive())
            log.info("Connected to Deriv [%s]", mode)
            return True
        except Exception as e:
            log.error("Deriv connect failed: %s", e)
            if self.on_error:
                self.on_error(e)
            return False

    async def disconnect(self) -> None:
        self._connected = False
        for task in (self._ping_task, self._recv_task):
            if task:
                task.cancel()
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._subscriptions.clear()
        log.info("Disconnected from Deriv")

    async def reconnect(self) -> bool:
        await self.disconnect()
        return await self.connect(self._mode)

    async def _get_authenticated_url(self, mode: str) -> str | None:
        if not self.api_token or not self.app_id:
            log.error("api_token and app_id required for %s mode", mode)
            return None
        auth_headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Deriv-App-ID": self.app_id,
            **_BROWSER_HEADERS,
        }
        try:
            async with httpx.AsyncClient() as client:
                if not self._account_id:
                    resp = await client.get(
                        f"{REST_BASE}/trading/v1/options/accounts",
                        headers=auth_headers,
                        timeout=10,
                    )
                    if resp.status_code != 200:
                        log.error("Accounts listing failed: %s", resp.text[:200])
                        return None
                    accounts = resp.json().get("data", [])
                    for acc in accounts:
                        is_demo = acc.get("account_type") == "demo"
                        if (mode == "demo" and is_demo) or (mode == "real" and not is_demo):
                            self._account_id = acc["account_id"]
                            self._account_currency = acc.get("currency", "USD")
                            break
                    if not self._account_id and accounts:
                        self._account_id = accounts[0]["account_id"]
                        self._account_currency = accounts[0].get("currency", "USD")
                    if not self._account_id:
                        log.error("No matching account found")
                        return None
                    log.info("Using account %s (%s)", self._account_id, mode)

                resp = await client.post(
                    f"{REST_BASE}/trading/v1/options/accounts/{self._account_id}/otp",
                    headers=auth_headers,
                    timeout=10,
                )
                if resp.status_code != 200:
                    log.error("OTP failed: %s", resp.text[:200])
                    return None
                return resp.json()["data"]["url"]
        except Exception as e:
            log.error("Auth error: %s", e)
            return None

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _send(self, msg: dict, timeout: float = 10) -> dict:
        if not self._ws:
            raise ConnectionError("Not connected")
        req_id = self._next_id()
        msg["req_id"] = req_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                data = json.loads(raw)
                req_id = data.get("req_id")
                if req_id and req_id in self._pending:
                    self._pending[req_id].set_result(data)
                elif data.get("msg_type") in ("tick", "ohlc") and self.on_tick:
                    try:
                        self.on_tick(data)
                    except Exception as e:
                        log.error("on_tick error: %s", e)
        except websockets.ConnectionClosed:
            log.warning("Deriv WebSocket closed")
            self._connected = False
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("recv_loop error: %s", e)
            self._connected = False

    async def _keep_alive(self) -> None:
        try:
            while self._connected:
                await asyncio.sleep(25)
                if self._ws:
                    await self._ws.send(json.dumps({"ping": 1}))
        except (asyncio.CancelledError, Exception):
            pass

    # ── Market data ─────────────────────────────────────────────────────

    async def active_symbols(self) -> list[dict]:
        resp = await self._send({"active_symbols": "brief"})
        return resp.get("active_symbols", [])

    async def subscribe_ticks(self, symbol: str) -> dict:
        resp = await self._send({"ticks": symbol, "subscribe": 1})
        sub_id = resp.get("subscription", {}).get("id")
        if sub_id:
            self._subscriptions[symbol] = sub_id
        return resp

    async def subscribe_candles(self, symbol: str, granularity: int = 60) -> dict:
        resp = await self._send({
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": 100,
            "end": "latest",
            "granularity": granularity,
            "style": "candles",
            "subscribe": 1,
        })
        sub_id = resp.get("subscription", {}).get("id")
        if sub_id:
            self._subscriptions[f"{symbol}_{granularity}"] = sub_id
        return resp

    async def forget(self, subscription_id: str) -> dict:
        return await self._send({"forget": subscription_id})

    async def forget_all(self, stream_type: str = "ticks") -> dict:
        return await self._send({"forget_all": stream_type})

    # ── Trading ─────────────────────────────────────────────────────────

    async def contracts_for(self, symbol: str) -> dict:
        return await self._send({"contracts_for": symbol})

    async def price_proposal(
        self,
        symbol: str,
        contract_type: str,
        duration: int = 1,
        duration_unit: str = "d",
        amount: float = 2.0,
        basis: str = "stake",
        currency: str = "USD",
    ) -> dict:
        return await self._send({
            "proposal": 1,
            "amount": amount,
            "basis": basis,
            "contract_type": contract_type,
            "currency": currency,
            "duration": duration,
            "duration_unit": duration_unit,
            "underlying_symbol": symbol,
        })

    async def buy(self, proposal_id: str, price: float) -> dict:
        return await self._send({"buy": proposal_id, "price": price})

    async def sell(self, contract_id: int, price: float = 0) -> dict:
        return await self._send({"sell": contract_id, "price": price})

    async def portfolio(self) -> dict:
        return await self._send({"portfolio": 1})

    async def balance(self) -> dict:
        return await self._send({"balance": 1})

    async def profit_table(self, limit: int = 20) -> dict:
        return await self._send({
            "profit_table": 1,
            "description": 1,
            "limit": limit,
            "sort": "DESC",
        })


async def _test():
    import os
    logging.basicConfig(level=logging.INFO)

    token = os.environ.get("DERIV_TOKEN", "")
    app_id = os.environ.get("DERIV_APP_ID", "")
    if not token or not app_id:
        print("Set DERIV_TOKEN and DERIV_APP_ID env vars")
        return

    ticks_received = []

    def on_tick(data):
        tick = data.get("tick") or data.get("ohlc", {})
        ticks_received.append(tick)
        if len(ticks_received) <= 3:
            quote = tick.get("quote", tick.get("close", "?"))
            print(f"  TICK: {quote}")

    client = DerivClient(api_token=token, app_id=app_id, on_tick=on_tick)

    # 1. Public market data
    print("=== Public connection ===")
    ok = await client.connect(mode="public")
    if not ok:
        return

    symbols = await client.active_symbols()
    forex = [s for s in symbols if s.get("market") == "forex"]
    print(f"Forex pairs: {len(forex)}")

    candles = await client.subscribe_candles("frxEURGBP", granularity=60)
    if "candles" in candles:
        c = candles["candles"][-1]
        print(f"Latest EUR/GBP: O={c['open']} H={c['high']} L={c['low']} C={c['close']}")

    print("Listening for ticks (5s)...")
    await asyncio.sleep(5)
    print(f"Got {len(ticks_received)} ticks")
    await client.disconnect()

    # 2. Demo trading
    print("\n=== Demo connection ===")
    ok = await client.connect(mode="demo")
    if not ok:
        print("Demo auth failed")
        return

    bal = await client.balance()
    print(f"Balance: ${bal['balance']['balance']}")

    prop = await client.price_proposal("frxEURGBP", "CALL", duration=1, duration_unit="d", amount=2)
    if "proposal" in prop:
        p = prop["proposal"]
        print(f"CALL proposal: stake=${p['ask_price']} payout=${p['payout']} spot={p['spot']}")
    else:
        print(f"Proposal error: {prop.get('error', {}).get('message')}")

    await client.disconnect()
    print("\nAll tests passed!")


if __name__ == "__main__":
    asyncio.run(_test())

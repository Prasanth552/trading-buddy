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
        inst_name = inst.get("name") or ""
        # Index options: name == "NIFTY", "BANKNIFTY" etc.
        # Stock options: name is full company name ("HDFC BANK LTD"),
        # but trading_symbol starts with the short symbol ("HDFCBANK").
        tsym = (inst.get("trading_symbol") or "").upper()
        if inst_name != name and not tsym.startswith(name.upper() + " "):
            continue
        if inst.get("instrument_type") != option_type:
            continue
        if abs(float(inst.get("strike_price", -1)) - float(strike)) > 0.001:
            continue
        if _expiry_to_date(inst.get("expiry")) != expiry:
            continue
        return inst
    return None


_FO_SEGMENTS = {"NSE_FO", "BSE_FO"}
_fo_cache_path: str | None = None


def load_fo_instruments(url: str | None = None) -> list[dict[str, Any]]:
    """Load only F&O instruments, caching to disk so multiple processes share one download.

    First process of the day downloads the full master, filters to NSE_FO/BSE_FO,
    writes the filtered list to data/instruments_fo_<date>.json (~5-8K instruments
    instead of 136K). Subsequent processes read from that file — no download, no
    160MB in-memory parse.
    """
    import config as _cfg
    today = datetime.now(IST).strftime("%Y-%m-%d")
    cache = os.path.join(_cfg.DATA_DIR, f"instruments_fo_{today}.json")

    if os.path.exists(cache):
        with open(cache) as f:
            instruments = json.load(f)
        log.info("Loaded %d F&O instruments from cache %s", len(instruments), cache)
        return instruments

    url = url or _cfg.UPSTOX_INSTRUMENTS_URL
    log.info("Downloading Upstox instrument master (F&O filter)...")
    raw = requests.get(url, timeout=120).content
    all_instruments = json.loads(gzip.decompress(raw))
    fo = [i for i in all_instruments if i.get("segment") in _FO_SEGMENTS]
    log.info("Filtered %d -> %d F&O instruments", len(all_instruments), len(fo))
    del all_instruments

    with open(cache, "w") as f:
        json.dump(fo, f, separators=(",", ":"))
    # clean up old cache files
    for name in os.listdir(_cfg.DATA_DIR):
        if name.startswith("instruments_fo_") and name.endswith(".json") and name != os.path.basename(cache):
            try:
                os.remove(os.path.join(_cfg.DATA_DIR, name))
            except OSError:
                pass
    return fo


class UpstoxClient:
    """Thin Upstox order client (sandbox token-based)."""

    _shared_instruments: list[dict[str, Any]] | None = None
    _shared_instruments_date: str | None = None

    def __init__(self, sandbox_token: str | None = None) -> None:
        self.token = sandbox_token
        if not self.token:
            self.token = self._load_today_token()
        if not self.token:
            self.token = os.getenv("UPSTOX_SANDBOX_TOKEN")
        if not self.token and not getattr(config, "UPSTOX_SIMULATE_ORDERS", False):
            raise UpstoxError(
                "No Upstox token — run `python -m src.broker.upstox_data` to log in, "
                "or set UPSTOX_SANDBOX_TOKEN in .env."
            )
        today = datetime.now().strftime("%Y-%m-%d")
        if UpstoxClient._shared_instruments is not None and UpstoxClient._shared_instruments_date == today:
            self._instruments = UpstoxClient._shared_instruments
        else:
            self._instruments: list[dict[str, Any]] | None = None

    @staticmethod
    def _load_today_token() -> str | None:
        """Read today's OAuth token from the session file directly."""
        import json as _json
        session_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "data", ".upstox_session.json"
        )
        try:
            with open(session_path) as f:
                data = _json.load(f)
            if data.get("date") != datetime.now().strftime("%Y-%m-%d"):
                return None
            return data.get("access_token") or None
        except Exception:  # noqa: BLE001
            return None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # --- instruments -------------------------------------------------------
    def load_instruments(self, url: str = config.UPSTOX_INSTRUMENTS_URL) -> list[dict[str, Any]]:
        """Download + decompress the Upstox instrument master (cached across instances per day)."""
        if self._instruments is None:
            log.info("Downloading Upstox instrument master...")
            raw = requests.get(url, timeout=60).content
            self._instruments = json.loads(gzip.decompress(raw))
            log.info("Loaded %d Upstox instruments.", len(self._instruments))
            UpstoxClient._shared_instruments = self._instruments
            UpstoxClient._shared_instruments_date = datetime.now().strftime("%Y-%m-%d")
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
        # Paper simulation: skip the sandbox HTTP call (whose token expires and
        # breaks trading) and return a simulated fill. P&L still uses REAL premium
        # prices from the data feed, so the whole hedge flow works token-free.
        if getattr(config, "UPSTOX_SIMULATE_ORDERS", False):
            import time as _t
            oid = f"SIM-{int(_t.time()*1000)}"
            log.info("UPSTOX ORDER simulated ok id=%s", oid)
            return {"order_ids": [oid], "raw": {"simulated": True}}
        resp = requests.post(config.UPSTOX_ORDER_URL, json=body, headers=self._headers(), timeout=15)
        data = resp.json()
        if data.get("status") != "success":
            raise UpstoxError(f"Upstox order failed: {data}")
        resp_data = data.get("data", {})
        # Upstox v3 returns "order_id" (singular string), v2 returned "order_ids" (array)
        order_ids = resp_data.get("order_ids", [])
        if not order_ids:
            single_id = resp_data.get("order_id")
            if single_id:
                order_ids = [single_id]
        log.info("UPSTOX ORDER ok ids=%s", order_ids)
        if not order_ids:
            raise UpstoxError(f"Upstox returned success but no order id: {data}")
        return {"order_ids": order_ids, "raw": data}

    # --- GTT orders -----------------------------------------------------------
    GTT_BASE = "https://api.upstox.com/v3/order/gtt"

    def place_gtt_sl(
        self,
        instrument_token: str,
        quantity: int,
        trigger_price: float,
        product: str = "I",
    ) -> str | None:
        """Place a GTT SELL stop-loss order. Returns the gtt_order_id or None."""
        if getattr(config, "UPSTOX_SIMULATE_ORDERS", False):
            import time as _t
            gid = f"GTT-SIM-{int(_t.time()*1000)}"
            log.info("GTT SL simulated: %s trigger=%.2f", instrument_token, trigger_price)
            return gid
        body = {
            "type": "SINGLE",
            "quantity": quantity,
            "product": product,
            "instrument_token": instrument_token,
            "transaction_type": "SELL",
            "rules": [
                {
                    "strategy": "ENTRY",
                    "trigger_type": "BELOW",
                    "trigger_price": trigger_price,
                },
            ],
        }
        try:
            resp = requests.post(f"{self.GTT_BASE}/place", json=body, headers=self._headers(), timeout=15)
            data = resp.json()
            if data.get("status") != "success":
                log.error("GTT SL place failed: %s", data)
                return None
            resp_data = data.get("data", {})
            gtt_ids = resp_data.get("gtt_order_ids")
            if isinstance(gtt_ids, list) and gtt_ids:
                gtt_id = gtt_ids[0]
            else:
                gtt_id = (resp_data.get("gtt_order_id")
                          or resp_data.get("order_id")
                          or resp_data.get("id")
                          or resp_data.get("gttOrderId"))
            log.info("GTT SL placed: id=%s token=%s trigger=%.2f full_response=%s",
                     gtt_id, instrument_token, trigger_price, data)
            return gtt_id
        except Exception as exc:
            log.error("GTT SL place error: %s", exc)
            return None

    def modify_gtt_sl(self, gtt_order_id: str, trigger_price: float, quantity: int) -> bool:
        """Modify an existing GTT SL order's trigger price."""
        if getattr(config, "UPSTOX_SIMULATE_ORDERS", False):
            log.info("GTT SL modify simulated: %s → %.2f", gtt_order_id, trigger_price)
            return True
        body = {
            "type": "SINGLE",
            "gtt_order_id": gtt_order_id,
            "quantity": quantity,
            "rules": [{
                "strategy": "ENTRY",
                "trigger_type": "BELOW",
                "trigger_price": trigger_price,
            }],
        }
        try:
            resp = requests.put(f"{self.GTT_BASE}/modify", json=body, headers=self._headers(), timeout=15)
            data = resp.json()
            if data.get("status") != "success":
                log.error("GTT SL modify failed: %s", data)
                return False
            log.info("GTT SL modified: %s → trigger=%.2f", gtt_order_id, trigger_price)
            return True
        except Exception as exc:
            log.error("GTT SL modify error: %s", exc)
            return False

    def cancel_gtt(self, gtt_order_id: str) -> bool:
        """Cancel an existing GTT order."""
        if getattr(config, "UPSTOX_SIMULATE_ORDERS", False):
            log.info("GTT cancel simulated: %s", gtt_order_id)
            return True
        try:
            resp = requests.delete(
                f"{self.GTT_BASE}/cancel",
                json={"gtt_order_id": gtt_order_id},
                headers=self._headers(), timeout=15,
            )
            data = resp.json()
            if data.get("status") != "success":
                log.error("GTT cancel failed: %s", data)
                return False
            log.info("GTT cancelled: %s", gtt_order_id)
            return True
        except Exception as exc:
            log.error("GTT cancel error: %s", exc)
            return False

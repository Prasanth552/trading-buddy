"""Upstox market-data client — a drop-in replacement for KiteClient's data surface.

Implements the same methods the rest of the app calls on the Kite client:
  - ltp(symbols)                      (accepts "NSE:NIFTY 50", "NFO:XYZ", raw keys)
  - historical_data(key, from, to, interval)
  - instruments(exchange)             (Kite-shaped dicts from the Upstox master)
  - profile()                         (token validation)

Auth: Upstox OAuth code flow. Tokens expire daily (~03:30 IST). Run the login
once each morning:
    .venv/bin/python -m src.broker.upstox_data
(open the printed URL, log in, paste the `code` from the redirect URL)
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

log = get_logger("upstox_data")
IST = ZoneInfo(config.TIMEZONE)
SESSION_FILE = os.path.join(config.DATA_DIR, ".upstox_session.json")


class UpstoxDataError(RuntimeError):
    """Auth/data problems in the Upstox data client."""


def _today_iso() -> str:
    return datetime.now(IST).date().isoformat()


def load_cached_token() -> str | None:
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("date") != _today_iso():
        return None
    return data.get("access_token")


def save_token(token: str) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(SESSION_FILE, "w", encoding="utf-8") as fh:
        json.dump({"date": _today_iso(), "access_token": token}, fh)
    log.info("Saved Upstox data token for %s", _today_iso())


def _expiry_to_date(value: Any) -> date | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=IST).date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


class UpstoxData:
    """KiteClient-compatible data client backed by Upstox's free API."""

    _SEGMENT_FOR_EXCHANGE = {"NFO": "NSE_FO", "BFO": "BSE_FO"}

    def __init__(self, access_token: str | None = None) -> None:
        self.token = access_token or os.getenv("UPSTOX_ACCESS_TOKEN") or load_cached_token()
        if not self.token:
            raise UpstoxDataError(
                "No Upstox data token for today — run: "
                ".venv/bin/python -m src.broker.upstox_data")
        self._master: list[dict[str, Any]] | None = None
        self._by_symbol: dict[str, str] = {}   # "SEGMENT:TRADINGSYMBOL" -> instrument_key

    # --- auth/infra ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = requests.get(config.UPSTOX_API_BASE + path, params=params,
                         headers=self._headers(), timeout=20)
        data = r.json()
        if r.status_code != 200 or data.get("status") == "error":
            raise UpstoxDataError(f"Upstox GET {path} failed: {data}")
        return data

    def profile(self) -> dict[str, Any]:
        return self._get("/v2/user/profile").get("data", {})

    # --- instrument master ---------------------------------------------------
    def _load_master(self) -> list[dict[str, Any]]:
        if self._master is None:
            log.info("Downloading Upstox instrument master...")
            raw = requests.get(config.UPSTOX_INSTRUMENTS_URL, timeout=60).content
            self._master = json.loads(gzip.decompress(raw))
            for inst in self._master:
                seg = inst.get("segment", "")
                tsym = inst.get("trading_symbol") or inst.get("tradingsymbol") or ""
                if seg and tsym:
                    self._by_symbol[f"{seg}:{tsym.replace(' ', '')}"] = inst["instrument_key"]
            log.info("Loaded %d Upstox instruments.", len(self._master))
        return self._master

    def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        """Kite-shaped instrument dicts for pick_atm_option()."""
        seg = self._SEGMENT_FOR_EXCHANGE.get(exchange or "", exchange)
        out = []
        for inst in self._load_master():
            if seg and inst.get("segment") != seg:
                continue
            out.append({
                "name": inst.get("name") or inst.get("asset_symbol"),
                "instrument_type": inst.get("instrument_type"),
                "expiry": _expiry_to_date(inst.get("expiry")),
                "strike": float(inst.get("strike_price") or 0),
                "tradingsymbol": (inst.get("trading_symbol")
                                  or inst.get("tradingsymbol") or "").replace(" ", ""),
                "lot_size": int(inst.get("lot_size") or 0),
                "exchange": exchange,
                "instrument_key": inst.get("instrument_key"),
            })
        return out

    # --- symbol -> instrument_key resolution ---------------------------------
    def _key_for(self, symbol: str) -> str | None:
        if "|" in symbol:  # already an instrument key
            return symbol
        if symbol in config.UPSTOX_INDEX_KEYS:
            return config.UPSTOX_INDEX_KEYS[symbol]
        if ":" in symbol:
            exch, tsym = symbol.split(":", 1)
            seg = self._SEGMENT_FOR_EXCHANGE.get(exch)
            if seg:
                self._load_master()
                return self._by_symbol.get(f"{seg}:{tsym.replace(' ', '')}")
        return None

    # --- LTP (kite-shaped) ----------------------------------------------------
    def ltp(self, symbols: list[str]) -> dict[str, Any]:
        keys: dict[str, str] = {}
        for s in symbols:
            k = self._key_for(s)
            if k:
                keys[k] = s
        if not keys:
            return {}
        data = self._get("/v2/market-quote/ltp",
                         params={"instrument_key": ",".join(keys)}).get("data", {})
        out: dict[str, Any] = {}
        for item in data.values():
            k = item.get("instrument_token")
            orig = keys.get(k)
            if orig is not None:
                out[orig] = {"last_price": item.get("last_price"),
                             "instrument_token": k}
        return out

    # --- historical candles (kite-shaped) -------------------------------------
    def historical_data(self, instrument_token: Any, from_dt: datetime,
                        to_dt: datetime, interval: str, **_: Any) -> list[dict[str, Any]]:
        unit, iv = config.UPSTOX_INTERVALS.get(interval, ("minutes", 15))
        key = str(instrument_token)
        frm = from_dt.date().isoformat() if hasattr(from_dt, "date") else str(from_dt)[:10]
        to = to_dt.date().isoformat() if hasattr(to_dt, "date") else str(to_dt)[:10]
        candles: list[list] = []
        try:
            d = self._get(f"/v3/historical-candle/{key}/{unit}/{iv}/{to}/{frm}")
            candles += d.get("data", {}).get("candles", [])
        except UpstoxDataError as exc:
            log.warning("Historical fetch failed (%s): %s", key, exc)
        # Today's bars come from the intraday endpoint.
        try:
            d = self._get(f"/v3/historical-candle/intraday/{key}/{unit}/{iv}")
            candles += d.get("data", {}).get("candles", [])
        except UpstoxDataError:
            pass
        rows = [{"date": c[0], "open": c[1], "high": c[2], "low": c[3],
                 "close": c[4], "volume": c[5]} for c in candles]
        rows.sort(key=lambda r: r["date"])
        # De-duplicate on timestamp (historical + intraday can overlap).
        seen: set = set()
        out = []
        for r in rows:
            if r["date"] in seen:
                continue
            seen.add(r["date"])
            out.append(r)
        return out


# --- session management (mirrors src.broker.session for Kite) ---------------
def ensure_upstox_data_session() -> UpstoxData:
    """Return a validated UpstoxData client for today, or raise with next steps."""
    client = UpstoxData()
    try:
        client.profile()
        return client
    except Exception as exc:  # noqa: BLE001
        raise UpstoxDataError(
            f"Upstox data token invalid ({exc}) — run: "
            ".venv/bin/python -m src.broker.upstox_data") from exc


def interactive_login() -> None:
    """Daily login: print the auth URL, exchange the pasted code for a token."""
    api_key = os.getenv("UPSTOX_API_KEY")
    secret = os.getenv("UPSTOX_API_SECRET")
    if not (api_key and secret):
        raise UpstoxDataError("Set UPSTOX_API_KEY and UPSTOX_API_SECRET in .env "
                              "(create a free app at account.upstox.com → Apps).")
    url = (f"{config.UPSTOX_API_BASE}/v2/login/authorization/dialog"
           f"?response_type=code&client_id={api_key}"
           f"&redirect_uri={config.UPSTOX_REDIRECT_URI}")
    print("\n=== Upstox daily login ===")
    print("1. Open this URL in your browser and log in:\n")
    print("   " + url)
    print(f"\n2. You'll be redirected to {config.UPSTOX_REDIRECT_URI}/?code=XXXX")
    print("   (page won't load — that's fine). Copy the `code` value.\n")
    code = input("code: ").strip()
    r = requests.post(f"{config.UPSTOX_API_BASE}/v2/login/authorization/token",
                      data={"code": code, "client_id": api_key,
                            "client_secret": secret,
                            "redirect_uri": config.UPSTOX_REDIRECT_URI,
                            "grant_type": "authorization_code"},
                      headers={"Accept": "application/json",
                               "Content-Type": "application/x-www-form-urlencoded"},
                      timeout=20)
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise UpstoxDataError(f"Token exchange failed: {data}")
    save_token(token)
    print("✅ Upstox data login OK — token saved for today.")


# --- automated daily login (best-effort, undocumented endpoints) ------------
_LOGIN_SVC = "https://service.upstox.com/login/open/v6"
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")


def _dbg(step: str, r) -> dict:
    body: Any
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = (r.text or "")[:400]
    log.info("Upstox login [%s] HTTP %s: %r", step, r.status_code, body)
    return body if isinstance(body, dict) else {}


def automated_login() -> "UpstoxData":
    """Headless daily login via mobile + TOTP + PIN. Best-effort; logs each step.

    OPT-IN: needs UPSTOX_API_KEY/SECRET + UPSTOX_MOBILE/PIN/TOTP_SECRET in .env.
    Endpoints are undocumented and may change — falls back to interactive_login.
    """
    import pyotp

    api_key = os.getenv("UPSTOX_API_KEY")
    secret = os.getenv("UPSTOX_API_SECRET")
    mobile = (os.getenv("UPSTOX_MOBILE") or "").strip()
    pin = (os.getenv("UPSTOX_PIN") or "").strip()
    totp_secret = (os.getenv("UPSTOX_TOTP_SECRET") or "").replace(" ", "").strip()
    if not all((api_key, secret, mobile, pin, totp_secret)):
        raise UpstoxDataError("Automated login needs UPSTOX_API_KEY/SECRET/MOBILE/"
                              "PIN/TOTP_SECRET in .env.")

    s = requests.Session()
    s.headers.update({"User-Agent": _UA, "Content-Type": "application/json",
                      "Accept": "application/json", "Origin": "https://login.upstox.com",
                      "Referer": "https://login.upstox.com/"})

    # Step 1 — 1FA: submit mobile, get a validation/session id.
    r = s.post(f"{_LOGIN_SVC}/auth/1fa",
               json={"data": {"mobileNumber": mobile, "userId": "", "otpType": "totp"}},
               timeout=20)
    d1 = _dbg("1fa", r)
    val_id = (d1.get("data") or {}).get("validationToken") or \
             (d1.get("data") or {}).get("valId") or (d1.get("data") or {}).get("userId")

    # Step 2 — TOTP verify.
    otp = pyotp.TOTP(totp_secret).now()
    r = s.post(f"{_LOGIN_SVC}/auth/1fa/totp/verify",
               json={"data": {"otp": otp, "validationToken": val_id}}, timeout=20)
    d2 = _dbg("totp-verify", r)

    # Step 3 — PIN (2FA).
    r = s.post(f"{_LOGIN_SVC}/auth/2fa",
               json={"data": {"twoFAMethod": "SECURE_PIN", "inputText": pin,
                              "validationToken": val_id}}, timeout=20)
    d3 = _dbg("pin", r)

    # Step 4 — authorize -> capture ?code= from the redirect chain.
    from urllib.parse import urljoin
    import re
    auth_url = (f"{config.UPSTOX_API_BASE}/v2/login/authorization/dialog"
                f"?response_type=code&client_id={api_key}"
                f"&redirect_uri={config.UPSTOX_REDIRECT_URI}")
    code: str | None = None
    url = auth_url
    for _hop in range(8):
        try:
            rr = s.get(url, timeout=20, allow_redirects=False)
        except requests.exceptions.RequestException as exc:
            m = re.search(r"[?&]code=([^&]+)", str(exc))
            code = m.group(1) if m else None
            break
        loc = rr.headers.get("Location", "") or ""
        m = re.search(r"[?&]code=([^&]+)", loc)
        if m:
            code = m.group(1)
            break
        if rr.status_code in (301, 302, 303, 307, 308) and loc:
            url = urljoin(url, loc)
            continue
        _dbg(f"authorize-deadend@{url}", rr)
        break
    if not code:
        raise UpstoxDataError("Automated login: could not capture authorization code "
                              "(see the logged steps above for where it stopped).")

    # Step 5 — exchange code for access token.
    tr = requests.post(f"{config.UPSTOX_API_BASE}/v2/login/authorization/token",
                       data={"code": code, "client_id": api_key, "client_secret": secret,
                             "redirect_uri": config.UPSTOX_REDIRECT_URI,
                             "grant_type": "authorization_code"},
                       headers={"Accept": "application/json",
                                "Content-Type": "application/x-www-form-urlencoded"},
                       timeout=20)
    token = tr.json().get("access_token")
    if not token:
        raise UpstoxDataError(f"Token exchange failed: {tr.json()}")
    save_token(token)
    log.info("Automated Upstox data login OK.")
    return UpstoxData(access_token=token)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    import sys
    if "--auto" in sys.argv:
        automated_login()
        print("✅ automated login OK")
    else:
        interactive_login()

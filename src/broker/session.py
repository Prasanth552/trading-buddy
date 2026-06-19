"""Daily Kite access-token management.

Kite access tokens are valid for one trading day. This module:
  - caches the day's token in ``data/.kite_session.json`` (git-ignored),
  - validates a cached token via a lightweight ``profile()`` call,
  - supports a manual login flow (open URL -> paste request_token),
  - supports best-effort automated TOTP login (Ongoing/reliability phase).

Security: the session file holds the access token — keep it git-ignored
(see .gitignore: ``data/*.json``).
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from src.broker.kite_client import KiteClient, KiteClientError
from src.utils import market_calendar as mc
from src.utils.logging import get_logger

import config

log = get_logger("session")

SESSION_FILE = os.path.join(config.DATA_DIR, ".kite_session.json")


def _today_iso() -> str:
    return mc.now_ist().date().isoformat()


def load_cached_token() -> str | None:
    """Return today's cached access token, or None if absent/stale."""
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("date") != _today_iso():
        return None
    return data.get("access_token")


def save_token(access_token: str) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(SESSION_FILE, "w", encoding="utf-8") as fh:
        json.dump({"date": _today_iso(), "access_token": access_token}, fh)
    log.info("Saved Kite session token for %s", _today_iso())


def _is_valid(client: KiteClient) -> bool:
    try:
        client.profile()
        return True
    except Exception as exc:  # noqa: BLE001 - any failure means re-auth
        log.warning("Cached token invalid: %s", exc)
        return False


def ensure_session(
    client: KiteClient | None = None,
    request_token: str | None = None,
) -> KiteClient:
    """Return a KiteClient with a valid access token for today.

    Resolution order:
      1. Reuse cached token if it validates.
      2. If ``request_token`` is supplied, exchange it for an access token.
      3. Otherwise raise — caller should run the interactive login (--login).
    """
    client = client or KiteClient()

    cached = load_cached_token()
    if cached:
        client.set_access_token(cached)
        if _is_valid(client):
            log.info("Reusing cached Kite session.")
            return client

    if request_token:
        token = client.generate_session(request_token)
        save_token(token)
        return client

    raise KiteClientError(
        "No valid Kite session. Run `python main.py --login` to authenticate."
    )


def interactive_login(client: KiteClient | None = None) -> KiteClient:
    """Manual login: print the login URL, read the request_token from stdin."""
    client = client or KiteClient()
    print("\n=== Kite login ===")
    print("1. Open this URL in your browser and log in:\n")
    print("   " + client.login_url())
    print("\n2. After login your browser redirects to https://127.0.0.1/?request_token=...")
    print("   It will show a connection error — that's fine. Copy the request_token")
    print("   value from the address bar and paste it below.\n")
    request_token = input("request_token: ").strip()
    if not request_token:
        raise KiteClientError("Empty request_token.")
    token = client.generate_session(request_token)
    save_token(token)
    print("Login OK — session cached for today.")
    return client


def automated_login(client: KiteClient | None = None) -> KiteClient:
    """Best-effort automated daily login using TOTP.

    OPT-IN: requires KITE_USER_ID, KITE_PASSWORD, and KITE_TOTP_SECRET in .env.
    If any is missing this raises and the caller falls back to manual --login.

    WARNING: this drives Kite's web login endpoints (undocumented) and can break
    if Zerodha changes them. It stores your Zerodha password in .env — only enable
    it if you accept that. It cannot be exercised without real credentials.
    """
    user_id = os.getenv("KITE_USER_ID")
    password = os.getenv("KITE_PASSWORD")
    totp_secret = os.getenv("KITE_TOTP_SECRET")
    if not (user_id and password and totp_secret):
        raise KiteClientError(
            "Automated login needs KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET in .env."
        )

    import re
    import requests  # local import; only needed on this path
    import pyotp

    client = client or KiteClient()
    sess = requests.Session()

    # 1. Password login -> request_id.
    r1 = sess.post("https://kite.zerodha.com/api/login",
                   data={"user_id": user_id, "password": password}, timeout=15)
    r1.raise_for_status()
    request_id = r1.json()["data"]["request_id"]

    # 2. TOTP 2FA. Sanitize the key (copied TOTP keys often contain spaces).
    otp = pyotp.TOTP(totp_secret.replace(" ", "").strip()).now()
    r2 = sess.post("https://kite.zerodha.com/api/twofa",
                   data={"user_id": user_id, "request_id": request_id,
                         "twofa_value": otp, "twofa_type": "totp"}, timeout=15)
    r2.raise_for_status()

    # 3. Hit the connect login URL; the redirect to the (unreachable) redirect URL
    #    carries ?request_token=... — capture it from the resulting connection error.
    request_token: str | None = None
    try:
        sess.get(client.login_url(), timeout=15)
    except requests.exceptions.RequestException as exc:
        m = re.search(r"request_token=([A-Za-z0-9]+)", str(exc))
        if m:
            request_token = m.group(1)
    if not request_token:
        raise KiteClientError("Automated login failed to capture request_token.")

    token = client.generate_session(request_token)
    save_token(token)
    log.info("Automated Kite login OK.")
    return client

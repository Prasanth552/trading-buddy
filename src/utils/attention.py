"""'Needs attention' checks — surfaces the few things that may need a human.

Used by the dashboard (/api/attention) so token expiry, login status, the kill
switch, and recent errors are all visible in one place. Each item is
{level: ok|warn|error, msg}.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone

import config
from src.notify import state
from src.utils import market_calendar as mc


def _jwt_exp(token: str) -> int | None:
    """Read the 'exp' (epoch seconds) claim from a JWT without verifying it."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:  # noqa: BLE001
        return None


def _recent_errors(max_lines: int = 400, keep: int = 3) -> list[str]:
    path = os.path.join(config.LOG_DIR, f"trading_buddy_{mc.now_ist():%Y-%m-%d}.log")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-max_lines:]
    except OSError:
        return []
    errs = [ln.strip() for ln in lines if "| ERROR" in ln]
    return errs[-keep:]


def _kite_logged_in_today() -> bool:
    from src.broker.session import load_cached_token
    try:
        return bool(load_cached_token())
    except Exception:  # noqa: BLE001
        return False


def attention_items() -> list[dict[str, str]]:
    """Return the list of attention items (most severe first)."""
    items: list[dict[str, str]] = []
    now = mc.now_ist()

    # 1. Upstox sandbox token expiry.
    tok = os.getenv("UPSTOX_SANDBOX_TOKEN")
    if config.EXECUTION_BROKER == "upstox":
        if not tok:
            items.append({"level": "error", "msg": "Upstox token not set — trades can't be placed."})
        else:
            exp = _jwt_exp(tok)
            if exp:
                exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
                days = (exp_dt - datetime.now(timezone.utc)).days
                if days < 0:
                    items.append({"level": "error",
                                  "msg": "Upstox token EXPIRED — regenerate it in the Upstox portal and update .env."})
                elif days <= 5:
                    items.append({"level": "warn",
                                  "msg": f"Upstox token expires in {days} day(s) — regenerate it soon."})
                else:
                    items.append({"level": "ok",
                                  "msg": f"Upstox token valid ({days} days left, expires {exp_dt:%d %b %Y})."})

    # 2. Kite login status.
    if _kite_logged_in_today():
        items.append({"level": "ok", "msg": "Kite logged in for today."})
    elif mc.is_trading_day():
        items.append({"level": "warn", "msg": "Kite not logged in yet — auto-login runs at the next cycle / pre-open."})

    # 3. Kill switch / paused.
    from src.storage import db
    ds = dict(db.get_or_create_daily_state(now.date().isoformat()))
    if ds.get("kill_switch_tripped"):
        items.append({"level": "error", "msg": "Kill switch TRIPPED — trading halted for today (daily loss limit)."})
    if state.is_paused():
        items.append({"level": "warn", "msg": "Trading is PAUSED — resume from the buttons above to continue."})

    # 4. Recent errors in today's log.
    for err in _recent_errors():
        items.append({"level": "error", "msg": "Log: " + err[-160:]})

    if not any(i["level"] in ("warn", "error") for i in items):
        items.append({"level": "ok", "msg": "All good — nothing needs your attention."})

    order = {"error": 0, "warn": 1, "ok": 2}
    items.sort(key=lambda i: order.get(i["level"], 3))
    return items

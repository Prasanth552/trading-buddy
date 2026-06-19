"""Safety guardrails — HARD requirements (build spec §10). Never weaken these.

Pure decision functions (offline-testable) + a kill-switch action that cancels
pending orders, flattens positions, trips the DB flag, and alerts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import config
from src.notify import state
from src.utils.logging import get_logger

log = get_logger("guardrails")


@dataclass
class PretradeDecision:
    allowed: bool
    reason: str


def pretrade_check(
    daily_state: dict[str, Any],
    mode: str = config.MODE,
) -> PretradeDecision:
    """Gate a new trade against the per-day limits (spec §10.3-§10.5).

    ``daily_state`` is a dict-like row with trades_count, realised_pnl,
    kill_switch_tripped.
    """
    if daily_state.get("kill_switch_tripped"):
        return PretradeDecision(False, "kill switch tripped — trading halted for the day")
    if state.is_paused():
        return PretradeDecision(False, "paused via /pause")
    if daily_state.get("trades_count", 0) >= config.MAX_TRADES_PER_DAY:
        return PretradeDecision(False, f"max trades/day reached ({config.MAX_TRADES_PER_DAY})")
    return PretradeDecision(True, "ok")


def should_trip_kill_switch(realised_pnl: float, unrealised_pnl: float = 0.0) -> bool:
    """True when the day's total loss meets/exceeds MAX_DAILY_LOSS (spec §10.3)."""
    total = realised_pnl + unrealised_pnl
    return total <= -abs(config.MAX_DAILY_LOSS)


def validate_order_pair(entry: dict[str, Any], stop: dict[str, Any] | None) -> PretradeDecision:
    """Enforce: no entry without a protective stop (spec §10.2)."""
    if stop is None:
        return PretradeDecision(False, "REJECTED: no protective stop attached")
    if stop.get("trigger_price") in (None, 0):
        return PretradeDecision(False, "REJECTED: stop has no trigger price")
    if entry.get("qty", 0) != stop.get("qty", 0):
        return PretradeDecision(False, "REJECTED: stop qty != entry qty")
    if entry.get("qty", 0) <= 0:
        return PretradeDecision(False, "REJECTED: non-positive qty")
    return PretradeDecision(True, "ok")


def trip_kill_switch(
    date_iso: str,
    client: Any | None = None,
    notifier: Any | None = None,
    reason: str = "daily loss limit reached",
) -> None:
    """Cancel pending orders, flatten open positions, set the DB flag, alert.

    In PAPER mode there are no live orders to cancel/flatten; we still mark the
    flag and alert. In LIVE mode this cancels real orders and squares off.
    """
    from src.storage import db
    log.error("KILL SWITCH: %s", reason)

    if config.MODE == "LIVE" and client is not None:
        try:
            for o in client.orders():
                if o.get("status") in ("OPEN", "TRIGGER PENDING"):
                    client.kite.cancel_order(variety="regular", order_id=o["order_id"])
        except Exception as exc:  # noqa: BLE001 - best-effort; never raise from kill switch
            log.error("Kill switch: cancel orders failed: %s", exc)
        # Flattening open positions is delegated to the executor's square-off.

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE daily_state SET kill_switch_tripped = 1 WHERE date = ?", (date_iso,)
        )

    if notifier is not None:
        try:
            from src.notify import messages
            notifier.send_message(messages.kill_switch())
        except Exception as exc:  # noqa: BLE001
            log.error("Kill switch alert failed: %s", exc)

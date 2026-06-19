"""Weekly ATM option resolution for the index watchlist.

Selection logic (pick_atm_option) is pure and offline-testable; the Kite fetch
(resolve_weekly_atm_option) wraps it around a cached instruments dump.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import config
from src.utils.logging import get_logger

log = get_logger("instruments")


def _to_date(value: Any) -> date | None:
    """Coerce a Kite ``expiry`` (date | datetime | 'YYYY-MM-DD' | '') to a date."""
    if isinstance(value, date):
        return value
    if hasattr(value, "date"):
        return value.date()
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def atm_strike(index_ltp: float, strike_step: int) -> int:
    """Nearest strike to the index LTP."""
    return int(round(index_ltp / strike_step) * strike_step)


def pick_atm_option(
    instruments: list[dict[str, Any]],
    name: str,
    index_ltp: float,
    direction: str,
    today: date,
    strike_step: int,
) -> dict[str, Any] | None:
    """Pick the nearest-expiry ATM CE (long) / PE (short) for ``name``.

    ``instruments`` is a list of Kite instrument dicts (one exchange's dump).
    Returns the chosen instrument dict, or None if nothing matches.
    """
    opt_type = "CE" if direction == "long" else "PE"
    target = atm_strike(index_ltp, strike_step)

    candidates = []
    for inst in instruments:
        if inst.get("name") != name:
            continue
        if inst.get("instrument_type") != opt_type:
            continue
        exp = _to_date(inst.get("expiry"))
        if exp is None or exp < today:
            continue
        candidates.append((exp, inst))
    if not candidates:
        return None

    nearest_expiry = min(exp for exp, _ in candidates)
    same_expiry = [inst for exp, inst in candidates if exp == nearest_expiry]
    # Closest strike to ATM.
    return min(same_expiry, key=lambda i: abs(float(i.get("strike", 0)) - target))


# In-process cache of per-exchange instrument dumps (large; fetch once per run).
_INSTRUMENT_CACHE: dict[str, list[dict[str, Any]]] = {}


def resolve_weekly_atm_option(
    client: Any,
    index_symbol: str,
    direction: str,
    index_ltp: float,
    today: date | None = None,
) -> dict[str, Any] | None:
    """Resolve the live weekly ATM option for an index signal via Kite."""
    from src.utils import market_calendar as mc
    today = today or mc.now_ist().date()
    spec = config.OPTION_SPECS.get(index_symbol)
    if not spec:
        log.warning("No option spec for %s", index_symbol)
        return None

    exch = spec["exchange"]
    if exch not in _INSTRUMENT_CACHE:
        _INSTRUMENT_CACHE[exch] = client.instruments(exch)
    instruments = _INSTRUMENT_CACHE[exch]

    chosen = pick_atm_option(
        instruments, spec["name"], index_ltp, direction, today, spec["strike_step"]
    )
    if chosen is None:
        log.warning("No %s option found for %s", direction, index_symbol)
    return chosen

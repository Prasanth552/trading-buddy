"""NSE trading-day / market-hours helpers (Asia/Kolkata).

Market hours: 09:15-15:30 IST, Mon-Fri, excluding NSE holidays.

The 2026 holiday list below is a best-effort starting point and MUST be
verified against the official NSE circular before live trading. Update
``NSE_HOLIDAYS_2026`` as the exchange publishes / revises dates.
"""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import config

IST = ZoneInfo(config.TIMEZONE)

# NSE trading holidays — VERIFY against the official NSE circular.
# Format: ISO date string -> description.
NSE_HOLIDAYS_2026: dict[str, str] = {
    "2026-01-26": "Republic Day",
    "2026-03-04": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramzan Id)",
    "2026-03-31": "Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-27": "Bakri Id",
    "2026-08-15": "Independence Day",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-09": "Diwali Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
}

# Merge per-year tables here as they are added.
HOLIDAYS: dict[str, str] = {**NSE_HOLIDAYS_2026}


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


MARKET_OPEN_T: time = _parse_hhmm(config.MARKET_OPEN)
MARKET_CLOSE_T: time = _parse_hhmm(config.MARKET_CLOSE)


def now_ist() -> datetime:
    """Current timezone-aware datetime in IST."""
    return datetime.now(IST)


def is_holiday(d: date) -> bool:
    """True if ``d`` is a configured NSE holiday."""
    return d.isoformat() in HOLIDAYS


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Sat=5, Sun=6


def is_trading_day(d: date | None = None) -> bool:
    """True if ``d`` (default today, IST) is a regular NSE trading day."""
    d = d or now_ist().date()
    return not is_weekend(d) and not is_holiday(d)


def is_market_open(dt: datetime | None = None) -> bool:
    """True if the market is open at ``dt`` (default now, IST)."""
    dt = dt or now_ist()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    dt = dt.astimezone(IST)
    if not is_trading_day(dt.date()):
        return False
    return MARKET_OPEN_T <= dt.time() <= MARKET_CLOSE_T


def holiday_name(d: date | None = None) -> str | None:
    d = d or now_ist().date()
    return HOLIDAYS.get(d.isoformat())


def market_status(dt: datetime | None = None) -> str:
    """Human-readable status: OPEN / CLOSED (with reason)."""
    dt = dt or now_ist()
    d = dt.astimezone(IST).date()
    if is_weekend(d):
        return "CLOSED (weekend)"
    name = holiday_name(d)
    if name:
        return f"CLOSED (holiday: {name})"
    if is_market_open(dt):
        return "OPEN"
    t = dt.astimezone(IST).time()
    if t < MARKET_OPEN_T:
        return "CLOSED (pre-open)"
    return "CLOSED (post-close)"

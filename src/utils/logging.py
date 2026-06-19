"""Structured, timezone-aware logging for Trading Buddy.

Logs to both the console and a daily rotating file under ``logs/``.
All timestamps are emitted in IST.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import config

_IST = ZoneInfo(config.TIMEZONE)
_CONFIGURED = False


class _ISTFormatter(logging.Formatter):
    """Formatter that renders timestamps in IST regardless of host TZ."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=_IST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="seconds")


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure root logging once and return the app logger."""
    global _CONFIGURED
    logger = logging.getLogger("trading_buddy")
    if _CONFIGURED:
        return logger

    os.makedirs(config.LOG_DIR, exist_ok=True)

    # Make the console UTF-8 safe (Windows defaults to cp1252, which can't
    # encode '₹' and other symbols and would crash the StreamHandler).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    fmt = _ISTFormatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    # One file per day (date in the name) — avoids the Windows rotate-rename
    # collision when several processes (scheduler + manual commands) share a log.
    log_name = f"trading_buddy_{datetime.now(_IST):%Y-%m-%d}.log"
    file_handler = logging.FileHandler(
        os.path.join(config.LOG_DIR, log_name), encoding="utf-8", delay=True
    )
    file_handler.setFormatter(fmt)

    logger.setLevel(level)
    logger.handlers.clear()
    logger.addHandler(console)
    logger.addHandler(file_handler)
    logger.propagate = False

    _CONFIGURED = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger of the configured app logger."""
    base = setup_logging()
    return base.getChild(name) if name else base

"""Tiny persistent runtime state shared between the bot and the trading loop.

Currently just the pause flag (so /pause from Telegram halts new trades).
Stored as JSON under data/ (git-ignored).
"""
from __future__ import annotations

import json
import os

import config

STATE_FILE = os.path.join(config.DATA_DIR, ".bot_state.json")


def _read() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _write(data: dict) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def is_paused() -> bool:
    return bool(_read().get("paused", False))


def set_paused(paused: bool) -> None:
    data = _read()
    data["paused"] = bool(paused)
    _write(data)

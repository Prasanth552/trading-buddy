"""Signal dataclass.

Implemented in Phase 3. Stub for now.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Signal:
    """A trade signal emitted by the signal engine (Phase 3)."""

    symbol: str
    direction: str          # "long" | "short"
    entry: float
    stop: float
    target: float
    qty: int
    max_risk: float
    rationale: str
    status: str = "new"

"""Offline tests for Upstox instrument mapping — no network, no token.

Run: python -m tests.test_upstox
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.broker import upstox_client as ux

IST = ZoneInfo("Asia/Kolkata")


def check(name: str, cond: bool) -> None:
    print(f"  [{'OK ' if cond else 'BAD'}] {name}")
    if not cond:
        check.failed += 1  # type: ignore[attr-defined]


check.failed = 0  # type: ignore[attr-defined]


def _epoch_ms(y, m, d) -> int:
    return int(datetime(y, m, d, 14, 30, tzinfo=IST).timestamp() * 1000)


def _sample_instruments():
    rows = []
    for strike in (23550, 23600, 23650):
        for itype in ("CE", "PE"):
            rows.append({
                "segment": "NSE_FO", "name": "NIFTY", "instrument_type": itype,
                "strike_price": float(strike), "expiry": _epoch_ms(2026, 6, 18),
                "lot_size": 65, "instrument_key": f"NSE_FO|NIFTY{strike}{itype}",
                "trading_symbol": f"NIFTY {strike} {itype} 18 JUN 26",
            })
    # a farther expiry + a SENSEX (BSE_FO) contract + a wrong-segment decoy
    rows.append({"segment": "NSE_FO", "name": "NIFTY", "instrument_type": "CE",
                 "strike_price": 23600.0, "expiry": _epoch_ms(2026, 6, 25),
                 "lot_size": 65, "instrument_key": "NSE_FO|FAR"})
    rows.append({"segment": "BSE_FO", "name": "SENSEX", "instrument_type": "CE",
                 "strike_price": 75500.0, "expiry": _epoch_ms(2026, 6, 18),
                 "lot_size": 20, "instrument_key": "BSE_FO|SENSEX75500CE"})
    return rows


def test_expiry_coercion() -> None:
    print("upstox _expiry_to_date:")
    check("epoch ms -> date", ux._expiry_to_date(_epoch_ms(2026, 6, 18)) == date(2026, 6, 18))
    check("iso string -> date", ux._expiry_to_date("2026-06-18T14:30:00") == date(2026, 6, 18))
    check("empty -> None", ux._expiry_to_date(0) is None)


def test_pick_nifty_ce() -> None:
    print("pick_upstox_option — NIFTY CE:")
    insts = _sample_instruments()
    got = ux.pick_upstox_option(insts, "NIFTY", date(2026, 6, 18), 23600, "CE", "NSE_FO")
    check("found", got is not None)
    check("right key", got and got["instrument_key"] == "NSE_FO|NIFTY23600CE")
    check("nearest expiry (not FAR)", got and got["instrument_key"] != "NSE_FO|FAR")
    check("lot size 65", got and got["lot_size"] == 65)


def test_pick_sensex_segment() -> None:
    print("pick_upstox_option — SENSEX on BSE_FO:")
    insts = _sample_instruments()
    got = ux.pick_upstox_option(insts, "SENSEX", date(2026, 6, 18), 75500, "CE", "BSE_FO")
    check("found on BSE_FO", got and got["instrument_key"] == "BSE_FO|SENSEX75500CE")
    # wrong segment should not match
    none = ux.pick_upstox_option(insts, "SENSEX", date(2026, 6, 18), 75500, "CE", "NSE_FO")
    check("segment filter works", none is None)


def test_no_match() -> None:
    print("pick_upstox_option — no match cases:")
    insts = _sample_instruments()
    check("wrong strike -> None",
          ux.pick_upstox_option(insts, "NIFTY", date(2026, 6, 18), 99999, "CE", "NSE_FO") is None)
    check("wrong expiry -> None",
          ux.pick_upstox_option(insts, "NIFTY", date(2026, 7, 1), 23600, "CE", "NSE_FO") is None)
    check("PE picks put",
          ux.pick_upstox_option(insts, "NIFTY", date(2026, 6, 18), 23600, "PE", "NSE_FO")["instrument_type"] == "PE")


def main() -> int:
    print("\n=== upstox mapping tests (offline) ===")
    test_expiry_coercion()
    test_pick_nifty_ce()
    test_pick_sensex_segment()
    test_no_match()
    failed = check.failed  # type: ignore[attr-defined]
    print(f"\nResult: {'PASSED' if failed == 0 else f'FAILED ({failed} checks)'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

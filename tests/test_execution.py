"""Offline tests for the execution layer + guardrails — no broker, no network.

Run: python -m tests.test_execution
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date

import config
from src.broker import instruments
from src.execution import executor, guardrails

# These monitor tests validate the fixed-target exit path with automatic stops;
# pin both so live experiment flags don't change test behaviour.
config.EXIT_MODE = "target"
config.STOP_LOSS_ENABLED = True
config.PROFIT_TARGET_RUPEES = 0.0


def check(name: str, cond: bool) -> None:
    print(f"  [{'OK ' if cond else 'BAD'}] {name}")
    if not cond:
        check.failed += 1  # type: ignore[attr-defined]


check.failed = 0  # type: ignore[attr-defined]


def test_sizing_fits() -> None:
    print("size_position — fits budget:")
    # index_risk=10, delta 0.5 -> 5 premium pts; lot_size 20 -> ₹100/lot; budget 500 -> 5 lots.
    s = executor.size_position(index_risk=10, entry_premium=80, lot_size=20,
                               budget=500, min_lots=1, delta=0.5)
    check("fits", s.fits)
    check("5 lots", s.lots == 5)
    check("qty = 100", s.qty == 100)
    check("stop premium = 75", s.stop_premium == 75.0)
    check("total risk <= budget", s.total_risk <= 500)


def test_sizing_too_big() -> None:
    print("size_position — 1 lot exceeds budget (NIFTY-like):")
    # index_risk=80, delta .5 -> 40 pts; lot 65 -> ₹2600/lot > 500 budget.
    s = executor.size_position(index_risk=80, entry_premium=120, lot_size=65,
                               budget=500, min_lots=1, delta=0.5)
    check("does not fit", not s.fits)
    check("0 lots", s.lots == 0)
    check("reason mentions budget", "budget" in s.reason)


def test_sizing_caps_at_premium() -> None:
    print("size_position — premium risk capped at premium:")
    # Huge index risk shouldn't make premium risk exceed the premium paid.
    s = executor.size_position(index_risk=10_000, entry_premium=10, lot_size=20,
                               budget=10_000, delta=0.5)
    check("stop premium floored >0", s.stop_premium >= 0.05)
    check("risk/unit capped at premium*lot", s.risk_per_lot <= 10 * 20 + 0.01)


def test_build_order_pair() -> None:
    print("build_order_pair:")
    s = executor.size_position(10, 80, 20, budget=500, delta=0.5)
    entry, stop = executor.build_order_pair("NIFTY26JUN23600CE", "NFO", s, signal_id=1)
    check("entry is BUY", entry["transaction_type"] == "BUY")
    check("stop is SELL", stop["transaction_type"] == "SELL")
    check("stop is SL-M", stop["order_type"] == config.STOP_ORDER_TYPE)
    check("stop has trigger price", stop["trigger_price"] == s.stop_premium)
    check("qty matches", entry["qty"] == stop["qty"] == s.qty)


def test_validate_order_pair() -> None:
    print("guardrails.validate_order_pair (no order without stop):")
    s = executor.size_position(10, 80, 20, budget=500, delta=0.5)
    entry, stop = executor.build_order_pair("X", "NFO", s)
    check("valid pair allowed", guardrails.validate_order_pair(entry, stop).allowed)
    check("missing stop rejected", not guardrails.validate_order_pair(entry, None).allowed)
    bad = {**stop, "trigger_price": 0}
    check("zero-trigger stop rejected", not guardrails.validate_order_pair(entry, bad).allowed)
    mismatch = {**stop, "qty": 999}
    check("qty mismatch rejected", not guardrails.validate_order_pair(entry, mismatch).allowed)


def test_pretrade_check() -> None:
    print("guardrails.pretrade_check (daily limits / kill switch):")
    ok = guardrails.pretrade_check({"trades_count": 0, "kill_switch_tripped": 0})
    check("fresh day allowed", ok.allowed)
    ks = guardrails.pretrade_check({"trades_count": 0, "kill_switch_tripped": 1})
    check("kill switch blocks", not ks.allowed)
    maxed = guardrails.pretrade_check(
        {"trades_count": config.MAX_TRADES_PER_DAY, "kill_switch_tripped": 0})
    check("max trades/day blocks", not maxed.allowed)


def test_kill_switch_threshold() -> None:
    print("guardrails.should_trip_kill_switch:")
    check("below threshold -> no trip",
          not guardrails.should_trip_kill_switch(-(config.MAX_DAILY_LOSS - 1)))
    check("at threshold -> trip",
          guardrails.should_trip_kill_switch(-config.MAX_DAILY_LOSS))
    check("realised+unrealised combine",
          guardrails.should_trip_kill_switch(-config.MAX_DAILY_LOSS * 0.7,
                                             -config.MAX_DAILY_LOSS * 0.5))
    check("profit -> no trip", not guardrails.should_trip_kill_switch(5000))


def test_option_selection() -> None:
    print("instruments.pick_atm_option:")
    insts = []
    for strike in (23500, 23550, 23600, 23650, 23700):
        for itype in ("CE", "PE"):
            insts.append({
                "name": "NIFTY", "instrument_type": itype, "strike": strike,
                "expiry": date(2026, 6, 18), "lot_size": 65,
                "tradingsymbol": f"NIFTY26JUN{strike}{itype}", "exchange": "NFO",
            })
    # add a farther expiry to ensure nearest is chosen
    insts.append({"name": "NIFTY", "instrument_type": "CE", "strike": 23600,
                  "expiry": date(2026, 6, 25), "lot_size": 65,
                  "tradingsymbol": "NIFTYFAR", "exchange": "NFO"})

    long_opt = instruments.pick_atm_option(insts, "NIFTY", 23622.9, "long",
                                           date(2026, 6, 13), 50)
    check("long -> CE", long_opt and long_opt["instrument_type"] == "CE")
    check("ATM strike 23600", long_opt and long_opt["strike"] == 23600)
    check("nearest expiry chosen", long_opt and long_opt["tradingsymbol"] == "NIFTY26JUN23600CE")

    short_opt = instruments.pick_atm_option(insts, "NIFTY", 23622.9, "short",
                                            date(2026, 6, 13), 50)
    check("short -> PE", short_opt and short_opt["instrument_type"] == "PE")

    check("atm_strike rounds correctly", instruments.atm_strike(23622.9, 50) == 23600)
    check("expired contracts skipped",
          instruments.pick_atm_option(insts, "NIFTY", 23622.9, "long",
                                      date(2026, 7, 1), 50) is None)


def test_evaluate_exit() -> None:
    print("executor.evaluate_exit:")
    check("below stop -> stop", executor.evaluate_exit(100, 90, 130, 88) == ("stop", 90))
    check("at stop -> stop", executor.evaluate_exit(100, 90, 130, 90) == ("stop", 90))
    check("above target -> target", executor.evaluate_exit(100, 90, 130, 135) == ("target", 130))
    check("at target -> target", executor.evaluate_exit(100, 90, 130, 130) == ("target", 130))
    check("in between -> hold", executor.evaluate_exit(100, 90, 130, 105) == (None, None))


def test_trailing_exit() -> None:
    print("executor.trailing_exit (chandelier ATR trail):")
    # entry 100, init stop 80 (risk 20), trail dist = ATR_TRAIL_MULT(2)*20 = 40.
    check("rises, no exit", executor.trailing_exit(100, 80, 130, None) == (False, 130, 130))
    # peak 130 -> trail stop 90; 95 holds
    check("holds above trail", executor.trailing_exit(100, 80, 95, 130) == (False, 95, 130))
    # peak 130 -> trail stop 90; 88 exits
    ex = executor.trailing_exit(100, 80, 88, 130)
    check("breaks trail -> exit", ex[0] is True and ex[1] == 88)
    # never rose -> initial stop protects
    check("initial stop holds", executor.trailing_exit(100, 80, 79, 100)[0] is True)


def _with_temp_db(fn) -> None:
    """Run fn() against an isolated temp DB, restoring config afterwards."""
    from src.storage import db
    old_db, old_dir = config.DB_PATH, config.DATA_DIR
    tmp = tempfile.mkdtemp()
    config.DATA_DIR = tmp
    config.DB_PATH = os.path.join(tmp, "test.db")
    try:
        db.init_db()
        fn(db)
    finally:
        config.DB_PATH, config.DATA_DIR = old_db, old_dir


def _open_pos(db, qty, price, stop, target, sym="SENSEX26JUN75500CE",
             mode="PAPER", broker_key=None):
    return db.insert_trade({
        "signal_id": None, "ts": "2026-06-15T10:00:00+05:30", "symbol": sym,
        "side": "BUY", "qty": qty, "price": price, "order_id": "PAPER-T-1",
        "mode": mode, "status": "OPEN", "exit_price": None, "pnl": None,
        "stop_price": stop, "target_price": target, "broker_key": broker_key,
    })


class _FakeUpstox:
    def __init__(self):
        self.orders = []

    def place_order(self, instrument_token, quantity, transaction_type,
                    order_type="MARKET", **kw):
        self.orders.append((instrument_token, quantity, transaction_type, order_type))
        return {"order_ids": ["FAKE-EXIT-1"]}


def test_monitor_target_hit() -> None:
    print("monitor_paper_positions — target hit realizes profit:")
    def body(db):
        _open_pos(db, qty=20, price=100, stop=90, target=130)
        exits = executor.monitor_paper_positions(price_fn=lambda s, e: 135)
        check("one position closed", len(exits) == 1)
        check("reason target", exits and exits[0]["reason"] == "target")
        check("pnl = (130-100)*20 = 600", exits and exits[0]["pnl"] == 600)
        ds = dict(db.get_or_create_daily_state("2026-06-15"))
        # realised pnl recorded under today's date (now_ist), not the trade ts date;
        # just assert a closed position with profit exists.
        rows = list(db.get_open_paper_positions())
        check("no open positions remain", len(rows) == 0)
    _with_temp_db(body)


def test_monitor_kill_switch() -> None:
    print("monitor_paper_positions — stop loss trips kill switch:")
    def body(db):
        from src.utils import market_calendar as mc
        # Size the loss to exceed MAX_DAILY_LOSS at any config value.
        pts = config.MAX_DAILY_LOSS / 100 + 10
        stop = 100 - pts
        _open_pos(db, qty=100, price=100, stop=stop, target=130)
        executor.monitor_paper_positions(price_fn=lambda s, e: stop - 1)
        today = mc.now_ist().date().isoformat()
        ds = dict(db.get_or_create_daily_state(today))
        check("realised loss exceeds limit", ds["realised_pnl"] <= -config.MAX_DAILY_LOSS)
        check("kill switch tripped", ds["kill_switch_tripped"] == 1)
    _with_temp_db(body)


def test_monitor_upstox() -> None:
    print("monitor_upstox_positions — closes via Upstox SELL + realizes P&L:")
    def body(db):
        _open_pos(db, qty=20, price=100, stop=90, target=130,
                  mode="UPSTOX_SANDBOX", broker_key="BSE_FO|1139521")
        fake = _FakeUpstox()
        exits = executor.monitor_upstox_positions(
            kite_client=None, upstox=fake, price_fn=lambda s, e: 135)
        check("one position closed", len(exits) == 1)
        check("reason target", exits and exits[0]["reason"] == "target")
        check("pnl = (130-100)*20 = 600", exits and exits[0]["pnl"] == 600)
        check("a SELL was placed on Upstox", fake.orders and fake.orders[0][2] == "SELL")
        check("SELL used the broker_key", fake.orders and fake.orders[0][0] == "BSE_FO|1139521")
        check("no open upstox positions remain",
              len(db.get_open_positions("UPSTOX_SANDBOX")) == 0)
    _with_temp_db(body)


def main() -> int:
    print("\n=== execution + guardrails tests (offline) ===")
    test_sizing_fits()
    test_sizing_too_big()
    test_sizing_caps_at_premium()
    test_build_order_pair()
    test_validate_order_pair()
    test_pretrade_check()
    test_kill_switch_threshold()
    test_option_selection()
    test_evaluate_exit()
    test_trailing_exit()
    test_monitor_target_hit()
    test_monitor_kill_switch()
    test_monitor_upstox()
    failed = check.failed  # type: ignore[attr-defined]
    print(f"\nResult: {'PASSED' if failed == 0 else f'FAILED ({failed} checks)'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

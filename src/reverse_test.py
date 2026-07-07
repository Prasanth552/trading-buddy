"""Validate stop-and-reverse against the backtest (both windows).

Tests the "averaging with an opposite trade" instinct in its clean form: when
an OPPOSITE signal fires while a position is open, close it and enter the new
direction (no dead leg, no double theta).

Decision rule (fixed in advance): approve only if it beats the baseline in
BOTH the recent and the out-of-sample window.

Run:  .venv/bin/python -m src.reverse_test
"""
from __future__ import annotations

import config
from src import backtest as bt


def main() -> int:
    from src.broker.session import ensure_session
    from src.broker.kite_client import KiteClientError
    try:
        client = ensure_session()
    except KiteClientError as exc:
        print(f"No Kite session: {exc}")
        return 1

    variants = [("baseline", False), ("stop-and-reverse", True)]
    results: dict[str, dict[str, float]] = {}
    print(f"\n exit={getattr(config, 'EXIT_MODE', 'target')} · "
          f"take-profit ₹{config.PROFIT_TARGET_RUPEES:.0f} · risk ₹{config.MAX_RISK_PER_TRADE}")
    print(f"\n{'variant':18} {'window':7} {'trades':>6} {'win%':>6} {'net P&L':>12}")
    print("-" * 54)
    for name, flag in variants:
        config.STOP_AND_REVERSE = flag
        results[name] = {}
        for off, label in [(0, "recent"), (90, "oos")]:
            res = bt._run_all(client, list(config.WATCHLIST), 90,
                              config.KITE_INTERVALS["15min"], off)
            tot = sum(r.get("total_pnl", 0) for r in res)
            tr = sum(r.get("trades", 0) for r in res)
            w = sum(r.get("wins", 0) for r in res)
            l = sum(r.get("losses", 0) for r in res)
            wr = round(100 * w / (w + l), 1) if (w + l) else 0.0
            results[name][label] = tot
            print(f"{name:18} {label:7} {tr:>6} {wr:>6} {tot:>12,.0f}")
    config.STOP_AND_REVERSE = False

    print("-" * 54)
    base, rev = results["baseline"], results["stop-and-reverse"]
    verdict = ("APPROVE ✅ — beats baseline in BOTH windows"
               if rev["recent"] > base["recent"] and rev["oos"] > base["oos"]
               else "REJECT ❌ — must beat baseline in BOTH windows")
    print(f" stop-and-reverse: recent {rev['recent']-base['recent']:+,.0f} · "
          f"oos {rev['oos']-base['oos']:+,.0f}  → {verdict}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

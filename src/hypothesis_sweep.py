"""Validate the weekly-review suggestions against the backtest (both windows).

Tests the Jul-3 AI-review hypotheses on ~1,000 trades of history instead of its
21-trade (era-confounded) sample:
  - require-pattern    : only enter with a confirming candlestick pattern
  - no-oversold-short  : don't short when RSI < 30 (RSI_SHORT_MIN 12 -> 30)
  - both combined

Decision rule (fixed in advance): a variant is approved only if it beats the
baseline in BOTH the recent and the out-of-sample window.

Run:  .venv/bin/python -m src.hypothesis_sweep
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

    base_rsi = config.RSI_SHORT_MIN
    combos = [
        ("baseline",          False, base_rsi),
        ("require-pattern",   True,  base_rsi),
        ("no-oversold-short", False, 30.0),
        ("both",              True,  30.0),
    ]
    results: dict[str, dict[str, float]] = {}
    print(f"\n{'variant':18} {'window':7} {'trades':>6} {'net P&L':>12}")
    print("-" * 48)
    for name, pat, rsimin in combos:
        config.REQUIRE_PATTERN = pat
        config.RSI_SHORT_MIN = rsimin
        results[name] = {}
        for off, label in [(0, "recent"), (90, "oos")]:
            res = bt._run_all(client, list(config.WATCHLIST), 90,
                              config.KITE_INTERVALS["15min"], off)
            tot = sum(r.get("total_pnl", 0) for r in res)
            tr = sum(r.get("trades", 0) for r in res)
            results[name][label] = tot
            print(f"{name:18} {label:7} {tr:>6} {tot:>12,.0f}")
    config.REQUIRE_PATTERN = False
    config.RSI_SHORT_MIN = base_rsi

    print("-" * 48)
    base = results["baseline"]
    for name in ("require-pattern", "no-oversold-short", "both"):
        r = results[name]
        verdict = ("APPROVE ✅" if r["recent"] > base["recent"] and r["oos"] > base["oos"]
                   else "REJECT ❌ (must beat baseline in BOTH windows)")
        print(f" {name:18} recent {r['recent']-base['recent']:+,.0f} · "
              f"oos {r['oos']-base['oos']:+,.0f}  → {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

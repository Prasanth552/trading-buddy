"""Run ema20_rsi60 for this week's 4 trading days."""
import os, sys
_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _root)
os.chdir(_root)
from dotenv import load_dotenv
load_dotenv()

from datetime import date
from src.strategy.stock_runner import (
    run_day, STRATEGIES, STOCKS, init_stock_strategy_db,
    _find_signal_for_date, fetch_daily_candles, _monthly_expiry_for,
    round_strike, est_put_prem, est_call_prem,
)
from src.broker.upstox_data import UpstoxData
from src.storage import db
from datetime import timedelta

init_stock_strategy_db()

STRAT = "ema20_rsi60"
params = STRATEGIES[STRAT]
dates = [date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10)]

# Filter to weekdays only
dates = [d for d in dates if d.weekday() < 5]

print(f"{'='*80}")
print(f"  ema20_rsi60 — WEEK TEST (Sep 7-10, 2026)")
print(f"{'='*80}")

week_total = 0
week_trades = 0
week_wins = 0

for ref_date in dates:
    print(f"\n{'─'*80}")
    print(f"  {ref_date.strftime('%A %Y-%m-%d')}")
    print(f"{'─'*80}")

    res = run_day(ref_date, lots=1, force=True)
    data = res.get(STRAT, {})
    trades = {k: v for k, v in data.get("stocks", {}).items() if not v.get("skipped")}
    skipped = {k: v for k, v in data.get("stocks", {}).items() if v.get("skipped")}

    if not trades:
        skip_reasons = {}
        for k, v in skipped.items():
            r = v.get("skip_reason", "unknown")
            skip_reasons[r] = skip_reasons.get(r, 0) + 1
        print(f"  No trades. Skip reasons: {skip_reasons}")
        continue

    day_pnl = 0
    day_wins = 0
    print(f"\n  {'Stock':12} {'Dir':10} {'Sell':>8} {'Buy':>8} {'Credit':>8} "
          f"{'Exit':>14} {'Exit Date':>12} {'DTE':>5} {'P&L':>10}")
    print(f"  {'─'*95}")

    for stock, t in sorted(trades.items()):
        tag = "BULL PUT" if "bull" in (t.get("direction") or "") else "BEAR CALL"
        pnl = t.get("net_pnl") or 0
        icon = "✅" if pnl > 0 else "❌"
        day_pnl += pnl
        week_trades += 1
        if pnl > 0:
            day_wins += 1
            week_wins += 1
        print(f"  {stock:12} {tag:10} {t.get('sell_strike',0):>8.0f} "
              f"{t.get('buy_strike',0):>8.0f} {t.get('net_credit',0):>8.2f} "
              f"{t.get('exit_reason','—'):>14} {t.get('exit_date','—'):>12} "
              f"{t.get('dte_at_entry','—'):>5} {pnl:>+10,.0f} {icon}")

    week_total += day_pnl
    print(f"\n  Day: {len(trades)} trades ({day_wins}W/{len(trades)-day_wins}L) "
          f"P&L: ₹{day_pnl:+,.0f}")

print(f"\n{'='*80}")
print(f"  WEEK SUMMARY — ema20_rsi60")
print(f"{'='*80}")
print(f"  Total trades: {week_trades}")
print(f"  Wins/Losses:  {week_wins}W / {week_trades - week_wins}L")
print(f"  Win Rate:     {week_wins/week_trades*100:.0f}%" if week_trades else "  Win Rate: —")
print(f"  Week P&L:     ₹{week_total:+,.0f}")
print(f"  Avg/trade:    ₹{week_total/week_trades:+,.0f}" if week_trades else "")
print(f"{'='*80}")

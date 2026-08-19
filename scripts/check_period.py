"""Check P&L for a specific period — with and without ₹8K cap.

Usage: python3 scripts/check_period.py [days]
Default: 14 days (2 weeks)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage import db
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
db.init_db()

days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
MAX_CAP = 8000

cutoff = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d")

with db.get_conn() as conn:
    rows = conn.execute(
        """SELECT id, ts, symbol, side, qty, price, exit_price, pnl, status, channel
           FROM trades
           WHERE side = 'BUY'
             AND (channel IS NULL OR channel = 'ch1')
             AND status LIKE 'CLOSED%'
             AND pnl IS NOT NULL
             AND ts >= ?
           ORDER BY ts ASC""",
        (cutoff,),
    ).fetchall()

if not rows:
    print(f"No closed CH1 trades in last {days} days.")
    sys.exit()

print(f"{'=' * 70}")
print(f"LAST {days} DAYS — CH1 TRADES (since {cutoff})")
print(f"{'=' * 70}")
print()

daily = defaultdict(lambda: {"trades": [], "pnl": 0, "capped_pnl": 0, "count": 0, "wins": 0})
total_pnl = 0
total_capped = 0
total_wins = 0
total_count = 0
capped_count = 0

for r in rows:
    pnl = r["pnl"]
    won = pnl > 0
    date = r["ts"][:10]

    capped_pnl = max(pnl, -MAX_CAP)
    saved = capped_pnl - pnl if pnl < -MAX_CAP else 0

    total_pnl += pnl
    total_capped += capped_pnl
    total_count += 1
    if won:
        total_wins += 1
    if pnl < -MAX_CAP:
        capped_count += 1

    d = daily[date]
    d["pnl"] += pnl
    d["capped_pnl"] += capped_pnl
    d["count"] += 1
    if won:
        d["wins"] += 1

    icon = "W" if won else "L"
    cap_tag = f"  ← capped (saved ₹{saved:,.0f})" if saved > 0 else ""
    print(f"  #{r['id']:<4} {r['symbol']:<28} [{icon}] ₹{pnl:>+8,.0f}{cap_tag}")

print()
print(f"{'─' * 70}")
print(f"DAILY SUMMARY")
print(f"{'─' * 70}")
print()
print(f"  {'Date':<12} {'Trades':>7} {'WR':>5} {'Actual P&L':>12} {'With ₹8K cap':>12} {'Saved':>10}")
print(f"  {'─'*12} {'─'*7} {'─'*5} {'─'*12} {'─'*12} {'─'*10}")

for date in sorted(daily.keys()):
    d = daily[date]
    wr = d["wins"] / d["count"] * 100 if d["count"] else 0
    saved = d["capped_pnl"] - d["pnl"]
    saved_str = f"₹{saved:+,.0f}" if saved != 0 else "—"
    print(f"  {date:<12} {d['count']:>7} {wr:>4.0f}% "
          f"{'₹{:+,}'.format(int(d['pnl'])):>12} "
          f"{'₹{:+,}'.format(int(d['capped_pnl'])):>12} "
          f"{saved_str:>10}")

print()
print(f"{'=' * 70}")
print(f"TOTALS — LAST {days} DAYS")
print(f"{'=' * 70}")
print()
wr = total_wins / total_count * 100 if total_count else 0
print(f"  Trades:          {total_count}")
print(f"  Win Rate:        {wr:.0f}%")
print(f"  Actual P&L:      ₹{total_pnl:+,.0f}")
print(f"  With ₹8K cap:    ₹{total_capped:+,.0f}")
saved = total_capped - total_pnl
print(f"  Cap would save:  ₹{saved:+,.0f} ({capped_count} trades capped)")
print()

avg_day = total_pnl / len(daily) if daily else 0
avg_day_capped = total_capped / len(daily) if daily else 0
print(f"  Avg P&L/day (actual):    ₹{avg_day:+,.0f}")
print(f"  Avg P&L/day (with cap):  ₹{avg_day_capped:+,.0f}")
print(f"{'=' * 70}")

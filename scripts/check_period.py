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
MAX_DAILY_LOSS = 10000

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

daily = defaultdict(lambda: {"pnl": 0, "capped_pnl": 0, "protected_pnl": 0,
                              "count": 0, "wins": 0, "stopped": False})
total_pnl = 0
total_capped = 0
total_protected = 0
total_wins = 0
total_count = 0
capped_count = 0
daily_stopped_count = 0

# Group trades by date first to simulate daily loss limit
by_date = defaultdict(list)
for r in rows:
    by_date[r["ts"][:10]].append(r)

for date in sorted(by_date.keys()):
    day_rows = by_date[date]
    running_day_pnl = 0
    stopped = False

    for r in day_rows:
        pnl = r["pnl"]
        won = pnl > 0

        capped_pnl = max(pnl, -MAX_CAP)

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

        # Simulate daily loss limit
        if not stopped:
            d["protected_pnl"] += capped_pnl
            total_protected += capped_pnl
            running_day_pnl += capped_pnl
            if running_day_pnl <= -MAX_DAILY_LOSS:
                stopped = True
                d["stopped"] = True
                daily_stopped_count += 1
        # else: trade would not have been taken

        icon = "W" if won else "L"
        tags = []
        if pnl < -MAX_CAP:
            tags.append(f"capped (saved ₹{capped_pnl - pnl:,.0f})")
        if stopped and d["protected_pnl"] != d["capped_pnl"]:
            pass  # will show STOPPED in daily summary
        cap_tag = f"  ← {', '.join(tags)}" if tags else ""
        skip_tag = "  ← WOULD SKIP (daily limit)" if stopped and capped_pnl == pnl and d["protected_pnl"] + pnl != d["protected_pnl"] else ""
        print(f"  #{r['id']:<4} {r['symbol']:<28} [{icon}] ₹{pnl:>+8,.0f}{cap_tag}")

print()
print(f"{'─' * 70}")
print(f"DAILY SUMMARY")
print(f"{'─' * 70}")
print()
print(f"  {'Date':<12} {'Trades':>7} {'WR':>5} {'Actual':>10} {'Capped':>10} {'Protected':>10} {'Note':>10}")
print(f"  {'─'*12} {'─'*7} {'─'*5} {'─'*10} {'─'*10} {'─'*10} {'─'*10}")

for date in sorted(daily.keys()):
    d = daily[date]
    wr = d["wins"] / d["count"] * 100 if d["count"] else 0
    note = "STOPPED" if d["stopped"] else ""
    print(f"  {date:<12} {d['count']:>7} {wr:>4.0f}% "
          f"{'₹{:+,}'.format(int(d['pnl'])):>10} "
          f"{'₹{:+,}'.format(int(d['capped_pnl'])):>10} "
          f"{'₹{:+,}'.format(int(d['protected_pnl'])):>10} "
          f"{note:>10}")

print()
print(f"  Protected = with ₹{MAX_CAP:,} trade cap + ₹{MAX_DAILY_LOSS:,} daily limit")
print(f"  STOPPED = daily loss limit would have kicked in")

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
print(f"  With cap + daily ₹{MAX_DAILY_LOSS//1000}K limit:  ₹{total_protected:+,.0f}")
print()

cap_saved = total_capped - total_pnl
full_saved = total_protected - total_pnl
print(f"  Trade cap saves:         ₹{cap_saved:+,.0f} ({capped_count} trades capped)")
print(f"  + Daily limit saves:     ₹{full_saved:+,.0f} ({daily_stopped_count} days stopped)")
print()

n_days = len(daily) if daily else 1
print(f"  Avg P&L/day (actual):      ₹{total_pnl / n_days:+,.0f}")
print(f"  Avg P&L/day (protected):   ₹{total_protected / n_days:+,.0f}")
print(f"{'=' * 70}")

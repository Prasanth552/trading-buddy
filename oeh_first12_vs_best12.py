import sqlite3
from collections import defaultdict

conn = sqlite3.connect("data/trading_buddy.db")
conn.row_factory = sqlite3.Row

trades = conn.execute("""
    SELECT *, COALESCE(pnl,0) - COALESCE(charges,0) as net_pnl, date(ts) as day
    FROM trades WHERE status='CLOSED' AND channel='oeh' ORDER BY ts
""").fetchall()

by_day = defaultdict(list)
for t in trades:
    by_day[t["day"]].append(t)

orig_total = sum(t["net_pnl"] for t in trades)
first12_total = 0
best12_total = 0

header = f"{'Day':<12} {'All':>10} {'First12':>10} {'Best12':>10} {'Count':>6}"
print(header)
print("-" * len(header))

for day in sorted(by_day):
    dt = by_day[day]
    all_pnl = sum(t["net_pnl"] for t in dt)
    first12_pnl = sum(t["net_pnl"] for t in dt[:12])
    best12 = sorted(dt, key=lambda x: x["net_pnl"], reverse=True)[:12]
    best12_pnl = sum(t["net_pnl"] for t in best12)
    first12_total += first12_pnl
    best12_total += best12_pnl
    marker = " ***" if len(dt) > 12 else ""
    print(f"  {day:<12} {all_pnl:>10,.0f} {first12_pnl:>10,.0f} {best12_pnl:>10,.0f} {len(dt):>5}{marker}")

print()
print(f"  TOTAL:      {orig_total:>10,.0f} {first12_total:>10,.0f} {best12_total:>10,.0f}")
print(f"  vs Original:          {first12_total-orig_total:>+10,.0f} {best12_total-orig_total:>+10,.0f}")
print()

# Win days comparison
orig_wins = sum(1 for d in by_day if sum(t["net_pnl"] for t in by_day[d]) > 0)
first12_wins = sum(1 for d in by_day if sum(t["net_pnl"] for t in by_day[d][:12]) > 0)
print(f"  Win days — All: {orig_wins}/{len(by_day)}, First12: {first12_wins}/{len(by_day)}")

"""Query OEH trades from DB for last 10 days."""
import sqlite3, sys
sys.path.insert(0, ".")
import config

conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT date(ts) as day,
           count(*) as trades,
           sum(case when pnl > 0 then 1 else 0 end) as wins,
           sum(case when pnl <= 0 then 1 else 0 end) as losses,
           round(sum(pnl), 0) as total_pnl,
           round(avg(case when pnl > 0 then pnl end), 0) as avg_win,
           round(avg(case when pnl <= 0 then pnl end), 0) as avg_loss
    FROM trades
    WHERE channel='oeh' AND status='closed' AND ts >= '2026-09-21'
    GROUP BY day ORDER BY day
""").fetchall()

print(f"{'Day':12s} {'Trades':>6s} {'W':>3s} {'L':>3s} {'PnL':>10s} {'AvgW':>8s} {'AvgL':>8s}")
print("-" * 60)
total = 0
for r in rows:
    print(f"{r['day']:12s} {r['trades']:>6d} {r['wins']:>3d} {r['losses']:>3d} "
          f"₹{r['total_pnl']:>+9,.0f} ₹{r['avg_win'] or 0:>+7,.0f} ₹{r['avg_loss'] or 0:>+7,.0f}")
    total += r['total_pnl']
print("-" * 60)
print(f"{'TOTAL':12s} {'':>6s} {'':>3s} {'':>3s} ₹{total:>+9,.0f}")

conn.close()

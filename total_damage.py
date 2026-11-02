import sys
sys.path.insert(0, ".")
from src.storage import db
db.init_db()
with db.get_conn() as conn:
    # All-time PnL by channel
    rows = conn.execute(
        "SELECT channel, COUNT(*) as trades, "
        "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
        "ROUND(SUM(pnl),0) as pnl, "
        "MIN(ts) as first_trade, MAX(ts) as last_trade "
        "FROM trades WHERE status LIKE 'CLOSED%' AND pnl IS NOT NULL "
        "GROUP BY channel ORDER BY pnl"
    ).fetchall()

    print("=" * 85)
    print(f"{'Channel':>10} {'Trades':>7} {'WR':>5} {'PnL':>12} {'First Trade':<12} {'Last Trade':<12}")
    print("=" * 85)
    grand_total = 0
    for r in rows:
        wr = round(r["wins"] / r["trades"] * 100) if r["trades"] else 0
        first = str(r["first_trade"] or "")[:10]
        last = str(r["last_trade"] or "")[:10]
        print(f"{r['channel']:>10} {r['trades']:>7} {wr:>4}% {r['pnl']:>+12,.0f} {first:<12} {last:<12}")
        grand_total += r["pnl"]
    print("=" * 85)
    print(f"{'TOTAL':>10} {'':>7} {'':>5} {grand_total:>+12,.0f}")

    # Open trades
    opens = conn.execute(
        "SELECT COUNT(*) as cnt, ROUND(SUM(price * qty), 0) as capital_locked "
        "FROM trades WHERE status='OPEN'"
    ).fetchone()
    if opens["cnt"]:
        print(f"\nOpen trades: {opens['cnt']} (capital locked: ~₹{opens['capital_locked']:,.0f})")

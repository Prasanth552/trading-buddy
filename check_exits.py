import sys
sys.path.insert(0, ".")
from src.storage import db
db.init_db()
with db.get_conn() as conn:
    rows = conn.execute(
        "SELECT channel, symbol, price, qty, pnl, stop_price, status, ts "
        "FROM trades WHERE status LIKE 'CLOSED%' AND ts LIKE '2026-10-28%' "
        "AND channel IN ('orb_ce','orb_pe','mom_ce','mom_pe','bsq_ce','bsq_pe') "
        "ORDER BY channel, pnl"
    ).fetchall()
    print(f"{'Ch':>8} {'Symbol':<25} {'Qty':>5} {'Entry':>8} {'SL':>8} {'PnL':>8} {'Status':<25}")
    print("-" * 105)
    for r in rows:
        sl = r["stop_price"] or 0
        pnl = r["pnl"] or 0
        # Flag small losses (not near max SL)
        max_loss = sl * r["qty"] if sl > 0 else 0
        flag = ""
        if pnl < 0 and abs(pnl) < 2000:
            flag = " <<<SMALL LOSS"
        print(f"{r['channel']:>8} {r['symbol']:<25} {r['qty']:>5} {r['price']:>8.2f} {sl:>8.2f} {pnl:>+8.0f} {r['status']:<25}{flag}")

    # Also check exit reasons from status field
    print("\n\nExit status breakdown:")
    status_counts = conn.execute(
        "SELECT status, COUNT(*) as cnt, ROUND(AVG(pnl),0) as avg_pnl "
        "FROM trades WHERE status LIKE 'CLOSED%' AND ts LIKE '2026-10-28%' "
        "AND channel IN ('orb_ce','orb_pe','mom_ce','mom_pe','bsq_ce','bsq_pe') "
        "GROUP BY status ORDER BY cnt DESC"
    ).fetchall()
    for s in status_counts:
        print(f"  {s['status']:<30} count={s['cnt']:>3}  avg_pnl={s['avg_pnl']:>+8.0f}")

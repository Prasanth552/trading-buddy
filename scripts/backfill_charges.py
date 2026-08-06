#!/usr/bin/env python3
"""Backfill charges for closed trades that have NULL charges."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from src.storage import db
from src.notify.channel_listener import calc_charges

db.init_db()

with db.get_conn() as conn:
    rows = conn.execute(
        "SELECT id, price, exit_price, qty FROM trades "
        "WHERE charges IS NULL AND exit_price IS NOT NULL AND price IS NOT NULL AND qty IS NOT NULL"
    ).fetchall()

    print(f"Found {len(rows)} trades with missing charges")

    for r in rows:
        ch = calc_charges(r["price"], r["exit_price"], r["qty"])
        old_pnl_row = conn.execute("SELECT pnl FROM trades WHERE id = ?", (r["id"],)).fetchone()
        old_pnl = old_pnl_row["pnl"] if old_pnl_row else None

        # Recalculate net P&L with charges
        gross = (r["exit_price"] - r["price"]) * r["qty"]
        net = gross - ch["total"]

        conn.execute(
            "UPDATE trades SET charges = ?, pnl = ? WHERE id = ?",
            (ch["total"], round(net, 2), r["id"]),
        )
        print(f"  id={r['id']}: entry={r['price']} exit={r['exit_price']} qty={r['qty']} "
              f"charges={ch['total']:.2f} old_pnl={old_pnl} new_pnl={net:.2f}")

print("Done.")

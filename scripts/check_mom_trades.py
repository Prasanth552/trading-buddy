#!/usr/bin/env python3
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
import config
from src.storage.db import get_conn

with get_conn() as c:
    rows = c.execute(
        "SELECT id, symbol, side, qty, price, exit_price, pnl, status, channel, ts "
        "FROM trades WHERE channel='mom' ORDER BY ts DESC LIMIT 40"
    ).fetchall()
    print(f"MOM trades total: {len(rows)}")
    wins = losses = 0
    total_pnl = 0
    for r in rows:
        pnl = r["pnl"] or 0
        total_pnl += pnl
        if pnl > 0: wins += 1
        elif pnl < 0: losses += 1
        exit_p = r["exit_price"] or 0
        print(f"  id={r['id']:>5} {r['symbol']:25s} qty={r['qty']:>5} "
              f"entry={r['price']:>8.2f} exit={exit_p:>8.2f} "
              f"pnl={pnl:>+8.0f} {r['status']:6s} {r['ts']}")
    print(f"\nTotal MOM PnL: {total_pnl:+,.0f}")
    print(f"Wins: {wins} | Losses: {losses} | Open: {len(rows)-wins-losses}")

    # Also check today's MOM trades
    print("\n--- Today's MOM trades ---")
    today_rows = c.execute(
        "SELECT id, symbol, side, qty, price, exit_price, pnl, status, ts "
        "FROM trades WHERE channel='mom' AND ts >= '2026-10-15' ORDER BY ts"
    ).fetchall()
    today_pnl = 0
    for r in today_rows:
        pnl = r["pnl"] or 0
        today_pnl += pnl
        exit_p = r["exit_price"] or 0
        print(f"  id={r['id']:>5} {r['symbol']:25s} qty={r['qty']:>5} "
              f"entry={r['price']:>8.2f} exit={exit_p:>8.2f} "
              f"pnl={pnl:>+8.0f} {r['status']:6s} {r['ts']}")
    print(f"Today MOM PnL: {today_pnl:+,.0f} ({len(today_rows)} trades)")

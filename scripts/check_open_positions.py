#!/usr/bin/env python3
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
import config
from src.storage.db import get_conn

with get_conn() as conn:
    rows = conn.execute(
        "SELECT id, strategy, symbol, strike, option_type, entry_price, lot_size, quantity, status, created_at "
        "FROM signals WHERE status='OPEN' ORDER BY strategy, created_at"
    ).fetchall()
    print(f"Total OPEN positions: {len(rows)}")
    total_cap = 0
    by_strategy = {}
    for r in rows:
        cap = r[5] * r[7]
        total_cap += cap
        strat = r[1] or "unknown"
        by_strategy.setdefault(strat, []).append(cap)
        print(f"  id={r[0]} {strat:10s} {r[2]:15s} {r[3]:>7} {r[4]} qty={r[7]:>5} entry={r[5]:>8.2f} cap={cap:>10,.0f}  {r[9]}")
    print(f"\nTotal capital locked: {total_cap:,.0f}")
    print("\nBy strategy:")
    for s, caps in sorted(by_strategy.items()):
        print(f"  {s}: {len(caps)} positions, capital={sum(caps):,.0f}")

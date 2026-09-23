"""Rerun strategy straddles for a given date and print results."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from datetime import date
from src.strategy.live_runner import run_day, STRATEGIES, INDEXES

ref = date(2026, 9, 23)
print(f"Running all strategies for {ref} (force=True)...\n")

results = run_day(ref, lots=1, force=True)

grand_total = 0
for sname in STRATEGIES:
    data = results.get(sname, {})
    day_pnl = data.get("day_pnl", 0)
    grand_total += day_pnl
    print(f"=== {sname} | Day P&L: ₹{day_pnl:+,.0f} ===")
    for idx_name in INDEXES:
        r = data.get("indexes", {}).get(idx_name, {})
        if not r:
            print(f"  {idx_name}: no data")
            continue
        if r.get("skipped"):
            print(f"  {idx_name}: SKIPPED ({r.get('skip_reason')})")
            continue
        legs = r.get("legs", 1)
        src = r.get("premium_source", "bs")
        print(f"  {idx_name}: ₹{r['net_pnl']:+,.0f}  "
              f"exit={r.get('exit_reason','?')}  "
              f"entry={r.get('entry_time','?')}→{r.get('exit_time','?')}  "
              f"CE={r.get('ce_entry',0):.1f}→{r.get('ce_exit',0):.1f}  "
              f"PE={r.get('pe_entry',0):.1f}→{r.get('pe_exit',0):.1f}  "
              f"legs={legs} src={src}")
    print()

print(f"GRAND TOTAL: ₹{grand_total:+,.0f}")

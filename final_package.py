"""Final package backtest: Remove OEL + PDHL, keep OEH(cap12) + ORB + BSQ + MOM + CH1.
Also test with reduced capital pools to match 2-3L capital.
"""
import sqlite3
from collections import defaultdict

conn = sqlite3.connect("data/trading_buddy.db")
conn.row_factory = sqlite3.Row

trades = conn.execute("""
    SELECT *, COALESCE(pnl,0) - COALESCE(charges,0) as net_pnl, date(ts) as day
    FROM trades WHERE status='CLOSED' ORDER BY ts
""").fetchall()

KILLED = {"oel", "pdhl", "orf", "aft", "ch1b"}

# OEH first-12 by time
oeh_by_day = defaultdict(list)
for t in trades:
    if t["channel"] == "oeh":
        oeh_by_day[t["day"]].append(t)
oeh_keep_ids = set()
for day, dt in oeh_by_day.items():
    for t in dt[:12]:
        oeh_keep_ids.add(t["id"])

all_days = sorted(set(t["day"] for t in trades))

# Scenario A: Original
# Scenario B: Remove OEL + PDHL only
# Scenario C: Remove OEL + PDHL + OEH cap 12
# Scenario D: Remove OEL + PDHL + OEH cap 12 + skip CE on orb main

daily = {"A": defaultdict(float), "B": defaultdict(float), "C": defaultdict(float), "D": defaultdict(float)}
counts = {"A": 0, "B": 0, "C": 0, "D": 0}

def _is_ce(sym):
    if not sym: return False
    parts = sym.split()
    return any(p.endswith("CE") for p in parts) or sym.endswith("CE")

for t in trades:
    ch = t["channel"]
    net = t["net_pnl"]
    day = t["day"]

    # A: original
    daily["A"][day] += net
    counts["A"] += 1

    # B: remove OEL + PDHL
    if ch in KILLED:
        continue
    daily["B"][day] += net
    counts["B"] += 1

    # C: B + OEH cap 12
    if ch == "oeh" and t["id"] not in oeh_keep_ids:
        continue
    daily["C"][day] += net
    counts["C"] += 1

    # D: C + skip CE on orb main
    if ch == "orb" and _is_ce(t["symbol"]):
        continue
    daily["D"][day] += net
    counts["D"] += 1

def stats(daily_dict):
    pnls = [daily_dict.get(d, 0) for d in all_days]
    total = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    worst = min(pnls)
    peak = running = max_dd = 0
    for p in pnls:
        running += p
        if running > peak: peak = running
        dd = peak - running
        if dd > max_dd: max_dd = dd
    return total, wins, losses, worst, max_dd

print("=" * 85)
print("  FINAL PACKAGE COMPARISON")
print("=" * 85)
print(f"\n  {'Scenario':<40} {'P&L':>10} {'W/L':>6} {'Worst':>10} {'MaxDD':>10} {'Trades':>7}")
print(f"  {'-'*40} {'-'*10} {'-'*6} {'-'*10} {'-'*10} {'-'*7}")

labels = {
    "A": "Original (all strategies)",
    "B": "Remove OEL + PDHL",
    "C": "B + OEH cap 12/day",
    "D": "C + skip ORB CE",
}
for key in ["A", "B", "C", "D"]:
    total, wins, losses, worst, max_dd = stats(daily[key])
    wl = f"{wins}/{losses}"
    print(f"  {labels[key]:<40} {total:>+10,.0f} {wl:>6} {worst:>+10,.0f} {max_dd:>10,.0f} {counts[key]:>7,}")

# Detailed daily for scenario C (the recommended one)
print(f"\n{'='*85}")
print("  RECOMMENDED: Remove OEL+PDHL + OEH cap 12 — daily breakdown")
print(f"{'='*85}")
print(f"  {'Date':<12} {'Original':>10} {'Filtered':>10} {'Delta':>10} {'Running':>10}")
print(f"  {'-'*12} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
running = 0
for d in all_days:
    o = daily["A"].get(d, 0)
    f = daily["C"].get(d, 0)
    running += f
    delta = f - o
    print(f"  {d:<12} {o:>+10,.0f} {f:>+10,.0f} {delta:>+10,.0f} {running:>10,.0f}")

total_c = stats(daily["C"])[0]
print(f"\n  Average daily P&L: ₹{total_c/len(all_days):>,.0f}")
print(f"  Monthly (22 days): ₹{total_c/len(all_days)*22:>,.0f}")

# Channel contribution in filtered scenario
print(f"\n{'='*85}")
print("  CHANNEL CONTRIBUTION (after removing OEL+PDHL, OEH capped)")
print(f"{'='*85}")
ch_pnl = defaultdict(float)
ch_cnt = defaultdict(int)
for t in trades:
    ch = t["channel"]
    if ch in KILLED: continue
    if ch == "oeh" and t["id"] not in oeh_keep_ids: continue
    ch_pnl[ch] += t["net_pnl"]
    ch_cnt[ch] += 1

print(f"  {'Channel':<12} {'Trades':>8} {'Net P&L':>12} {'Avg/trade':>10} {'% of total':>10}")
print(f"  {'-'*12} {'-'*8} {'-'*12} {'-'*10} {'-'*10}")
total_filtered = sum(ch_pnl.values())
for ch in sorted(ch_pnl, key=lambda c: ch_pnl[c], reverse=True):
    pct = ch_pnl[ch] * 100 / total_filtered if total_filtered else 0
    avg = ch_pnl[ch] / ch_cnt[ch] if ch_cnt[ch] else 0
    print(f"  {ch:<12} {ch_cnt[ch]:>8} {ch_pnl[ch]:>+12,.0f} {avg:>+10,.0f} {pct:>9.1f}%")
print(f"  {'TOTAL':<12} {sum(ch_cnt.values()):>8} {total_filtered:>+12,.0f}")

# Capital needed: what's the configured pool for kept strategies
print(f"\n{'='*85}")
print("  CAPITAL REQUIRED")
print(f"{'='*85}")
print(f"  OEH:    ₹3,00,000 (cap 12 trades — avg ₹27K × 12 = ₹3.2L peak)")
print(f"  ORB:    ₹3,00,000 (avg ₹25K × 15 max = ₹3.75L peak)")
print(f"  MOM:    ₹3,00,000 (avg ₹11K × 20 max = ₹2.2L peak)")
print(f"  BSQ:    ₹3,00,000 (avg ₹9K × 30 max = ₹2.7L peak)")
print(f"  CH1:    no pool   (avg ₹53K × ~5-8 = ₹2.6-4.2L)")
print(f"  ---")
print(f"  Total config: ₹12L + CH1 margin")
print(f"  But pools SHARE the same Upstox account")
print(f"  Not all strategies peak simultaneously")
print(f"\n  Realistic minimum: ₹4-5L (positions overlap < 50% of the time)")
print(f"  Comfortable:       ₹6-8L (handles worst-case concurrent positions)")

"""Test: skip ALL CE trades across ALL scanner channels.
CH1 (telegram) is excluded from this — we don't control those signals.
"""
import sqlite3
from collections import defaultdict

conn = sqlite3.connect("data/trading_buddy.db")
conn.row_factory = sqlite3.Row

trades = conn.execute("""
    SELECT *, COALESCE(pnl,0) - COALESCE(charges,0) as net_pnl, date(ts) as day
    FROM trades WHERE status='CLOSED' ORDER BY ts
""").fetchall()

# Channels where WE pick trades (scanners) — CE can be filtered
SCANNER_CHANNELS = {"oeh", "oel", "orb", "orb_ce", "orb_pe", "bsq", "bsq_ce", "bsq_pe",
                    "mom", "mom_ce", "mom_pe", "pdhl", "orf", "aft"}

# Scenarios:
# A) Original (no filter)
# B) Skip CE on ORB/BSQ/PDHL only (previous proposal)
# C) Skip CE on ALL scanner channels
# D) Skip CE everywhere including CH1
# E) OEH cap 12 + skip CE on scanners + kill OEL (the full package)

scenarios = {
    "A_original": lambda t: True,
    "B_ce_orb_bsq_pdhl": lambda t: not (
        t["channel"] in ("orb", "bsq", "pdhl") and _is_ce(t["symbol"])
    ),
    "C_ce_all_scanners": lambda t: not (
        t["channel"] in SCANNER_CHANNELS and _is_ce(t["symbol"])
    ),
    "D_ce_everywhere": lambda t: not _is_ce(t["symbol"]),
}

def _is_ce(sym):
    if not sym:
        return False
    parts = sym.split()
    return any(p.endswith("CE") for p in parts) or sym.endswith("CE")

def _is_pe(sym):
    if not sym:
        return False
    parts = sym.split()
    return any(p.endswith("PE") for p in parts) or sym.endswith("PE")

all_days = sorted(set(t["day"] for t in trades))

print("=" * 80)
print("  SKIP CE ANALYSIS — what if we only trade PE?")
print("=" * 80)

# First show CE vs PE breakdown for each scanner channel
print(f"\n  CE vs PE per scanner channel:")
print(f"  {'Channel':<12} {'CE trades':>10} {'CE P&L':>12} {'PE trades':>10} {'PE P&L':>12}")
print(f"  {'-'*12} {'-'*10} {'-'*12} {'-'*10} {'-'*12}")

ch_ce = defaultdict(lambda: {"count": 0, "pnl": 0})
ch_pe = defaultdict(lambda: {"count": 0, "pnl": 0})
for t in trades:
    if _is_ce(t["symbol"]):
        ch_ce[t["channel"]]["count"] += 1
        ch_ce[t["channel"]]["pnl"] += t["net_pnl"]
    elif _is_pe(t["symbol"]):
        ch_pe[t["channel"]]["count"] += 1
        ch_pe[t["channel"]]["pnl"] += t["net_pnl"]

for ch in sorted(set(list(ch_ce.keys()) + list(ch_pe.keys()))):
    ce = ch_ce[ch]
    pe = ch_pe[ch]
    print(f"  {ch:<12} {ce['count']:>10} {ce['pnl']:>+12,.0f} {pe['count']:>10} {pe['pnl']:>+12,.0f}")

# Run scenarios
print(f"\n{'='*80}")
print(f"  {'Scenario':<25} {'Net P&L':>12} {'Trades':>8} {'Skipped':>8} {'Win/Loss':>8} {'Worst':>10} {'MaxDD':>10}")
print(f"  {'-'*25} {'-'*12} {'-'*8} {'-'*8} {'-'*8} {'-'*10} {'-'*10}")

for name, filter_fn in scenarios.items():
    daily = defaultdict(float)
    taken = skipped = 0
    for t in trades:
        if filter_fn(t):
            daily[t["day"]] += t["net_pnl"]
            taken += 1
        else:
            skipped += 1

    pnls = [daily.get(d, 0) for d in all_days]
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

    print(f"  {name:<25} {total:>+12,.0f} {taken:>8,} {skipped:>8,} {wins}/{losses:>6} {worst:>+10,.0f} {max_dd:>10,.0f}")

# Now the FULL PACKAGE: OEH cap 12 + skip CE on scanners + kill OEL
print(f"\n{'='*80}")
print("  FULL PACKAGE: OEH cap 12 + skip CE on scanners + kill OEL")
print(f"{'='*80}")

# Group OEH by day, keep first 12 by time
oeh_by_day = defaultdict(list)
for t in trades:
    if t["channel"] == "oeh":
        oeh_by_day[t["day"]].append(t)

oeh_keep_ids = set()
for day, dt in oeh_by_day.items():
    for i, t in enumerate(dt[:12]):
        oeh_keep_ids.add(t["id"])

daily_orig = defaultdict(float)
daily_full = defaultdict(float)
full_taken = full_skipped = 0

for t in trades:
    daily_orig[t["day"]] += t["net_pnl"]

    # Kill OEL
    if t["channel"] == "oel":
        full_skipped += 1
        continue

    # Skip CE on scanner channels
    if t["channel"] in SCANNER_CHANNELS and _is_ce(t["symbol"]):
        full_skipped += 1
        continue

    # OEH cap at 12
    if t["channel"] == "oeh" and t["id"] not in oeh_keep_ids:
        full_skipped += 1
        continue

    daily_full[t["day"]] += t["net_pnl"]
    full_taken += 1

orig_pnls = [daily_orig.get(d, 0) for d in all_days]
full_pnls = [daily_full.get(d, 0) for d in all_days]

orig_total = sum(orig_pnls)
full_total = sum(full_pnls)

def calc_stats(pnls):
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    worst = min(pnls)
    peak = running = max_dd = 0
    for p in pnls:
        running += p
        if running > peak: peak = running
        dd = peak - running
        if dd > max_dd: max_dd = dd
    return wins, losses, worst, max_dd

ow, ol, owst, odd = calc_stats(orig_pnls)
fw, fl, fwst, fdd = calc_stats(full_pnls)

print(f"\n  {'Metric':<20} {'Original':>14} {'Full Package':>14} {'Change':>14}")
print(f"  {'-'*20} {'-'*14} {'-'*14} {'-'*14}")
print(f"  {'Net P&L':<20} {orig_total:>+14,.0f} {full_total:>+14,.0f} {full_total-orig_total:>+14,.0f}")
print(f"  {'Trades':<20} {len(trades):>14,} {full_taken:>14,} {full_taken-len(trades):>14,}")
print(f"  {'Win/Loss days':<20} {ow}/{ol:>12} {fw}/{fl:>12}")
print(f"  {'Worst day':<20} {owst:>+14,.0f} {fwst:>+14,.0f}")
print(f"  {'Max drawdown':<20} {odd:>14,.0f} {fdd:>14,.0f}")
print(f"  {'Avg daily':<20} {orig_total/len(all_days):>+14,.0f} {full_total/len(all_days):>+14,.0f}")

# Day by day for full package
print(f"\n  Daily comparison (full package):")
print(f"  {'Date':<12} {'Original':>10} {'Filtered':>10} {'Delta':>10}")
print(f"  {'-'*12} {'-'*10} {'-'*10} {'-'*10}")
for d in all_days:
    o = daily_orig.get(d, 0)
    f = daily_full.get(d, 0)
    delta = f - o
    marker = " ▲" if delta > 5000 else " ▼" if delta < -5000 else ""
    print(f"  {d:<12} {o:>+10,.0f} {f:>+10,.0f} {delta:>+10,.0f}{marker}")

# Per-day average
print(f"\n  Average daily P&L: {full_total/len(all_days):>+,.0f}")
print(f"  Trading days needed for ₹5K/day target: {'ALREADY EXCEEDS' if full_total/len(all_days) > 5000 else 'Not yet'}")
print(f"  Trading days needed for ₹10K/day target: {'ALREADY EXCEEDS' if full_total/len(all_days) > 10000 else 'Not yet'}")

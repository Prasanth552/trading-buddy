"""Replay existing trades with proposed filter changes.

Rules applied:
1. Disable CE trades on ORB, BSQ, PDHL (keep PE only)
2. Cap OEH to max 12 trades/day
3. Kill OEL completely
4. Hard cap per-trade loss at ₹10,000
5. Reduce position exposure on Tuesdays (0.5x — simulated as halving PnL)
"""
import sqlite3
from collections import defaultdict
from datetime import datetime

DB = "data/trading_buddy.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

trades = conn.execute("""
    SELECT *, COALESCE(pnl,0) - COALESCE(charges,0) as net_pnl,
           date(ts) as day,
           CAST(strftime('%w', ts) AS INT) as dow
    FROM trades WHERE status='CLOSED'
    ORDER BY ts
""").fetchall()

# ── Original totals ──────────────────────────────────────────────
orig_total = sum(t['net_pnl'] for t in trades)
orig_by_day = defaultdict(float)
for t in trades:
    orig_by_day[t['day']] += t['net_pnl']

# ── Apply filters ────────────────────────────────────────────────
new_total = 0
new_by_day = defaultdict(float)
skipped_trades = defaultdict(list)
oeh_daily_count = defaultdict(int)
capped_saves = 0
tuesday_saves = 0

# First pass: count OEH trades per day to know which ones to keep
oeh_trades_per_day = defaultdict(list)
for t in trades:
    if t['channel'] == 'oeh':
        oeh_trades_per_day[t['day']].append(t)

# Sort OEH trades per day by net_pnl descending (keep the best ones)
for day in oeh_trades_per_day:
    oeh_trades_per_day[day].sort(key=lambda x: x['net_pnl'], reverse=True)

oeh_keep_ids = set()
for day, day_trades in oeh_trades_per_day.items():
    for i, t in enumerate(day_trades):
        if i < 12:  # keep top 12
            oeh_keep_ids.add(t['id'])

for t in trades:
    ch = t['channel']
    sym = t['symbol']
    net = t['net_pnl']
    day = t['day']
    dow = t['dow']
    is_ce = sym.endswith('CE') or 'CE' in sym.split()[-1] if sym else False
    # More robust CE detection
    parts = sym.split() if sym else []
    is_ce = any(p.endswith('CE') for p in parts) or sym.endswith('CE')
    is_pe = any(p.endswith('PE') for p in parts) or sym.endswith('PE')

    # Rule 3: Kill OEL
    if ch == 'oel':
        skipped_trades['oel_killed'].append(net)
        continue

    # Rule 1: Disable CE on ORB, BSQ, PDHL
    if ch in ('orb', 'bsq', 'pdhl') and is_ce:
        skipped_trades['ce_disabled'].append(net)
        continue

    # Rule 2: Cap OEH to 12 trades/day (keep best 12)
    if ch == 'oeh' and t['id'] not in oeh_keep_ids:
        skipped_trades['oeh_capped'].append(net)
        continue

    # Rule 4: Hard cap per-trade loss at ₹10K
    adjusted_net = net
    if net < -10000:
        saved = net - (-10000)  # negative number (how much we saved)
        capped_saves += abs(saved)
        adjusted_net = -10000
        skipped_trades['loss_capped'].append((net, adjusted_net))

    # Rule 5: Tuesday = 0.5x
    if dow == 2:  # Tuesday
        orig_contrib = adjusted_net
        adjusted_net = adjusted_net * 0.5
        tuesday_saves += orig_contrib - adjusted_net

    new_total += adjusted_net
    new_by_day[day] += adjusted_net

# ── Results ──────────────────────────────────────────────────────
print("=" * 60)
print("  REPLAY: Before vs After Filter Changes")
print("=" * 60)
print(f"\n  Original net P&L:  ₹{orig_total:>12,.0f}")
print(f"  Filtered net P&L:  ₹{new_total:>12,.0f}")
print(f"  Improvement:       ₹{new_total - orig_total:>12,.0f} ({(new_total-orig_total)*100/abs(orig_total):.1f}%)")

print(f"\n  ── Savings breakdown ──")
oel_saved = sum(skipped_trades['oel_killed'])
ce_saved = sum(skipped_trades['ce_disabled'])
oeh_saved = sum(skipped_trades['oeh_capped'])
print(f"  OEL killed:        ₹{-oel_saved:>10,.0f} saved (removed {len(skipped_trades['oel_killed'])} trades)")
print(f"  CE disabled:       ₹{-ce_saved:>10,.0f} saved (removed {len(skipped_trades['ce_disabled'])} trades)")
print(f"  OEH capped:        ₹{-oeh_saved:>10,.0f} saved (removed {len(skipped_trades['oeh_capped'])} trades)")
print(f"  Loss cap ₹10K:     ₹{capped_saves:>10,.0f} saved ({len(skipped_trades['loss_capped'])} trades capped)")
print(f"  Tuesday 0.5x:      ₹{tuesday_saves:>10,.0f} {'saved' if tuesday_saves > 0 else 'lost'}")

# Day-by-day comparison
print(f"\n  ── Daily comparison ──")
print(f"  {'Date':<12} {'Original':>10} {'Filtered':>10} {'Change':>10}")
print(f"  {'-'*12} {'-'*10} {'-'*10} {'-'*10}")
all_days = sorted(set(list(orig_by_day.keys()) + list(new_by_day.keys())))
orig_wins = new_wins = 0
orig_losses = new_losses = 0
for day in all_days:
    o = orig_by_day.get(day, 0)
    n = new_by_day.get(day, 0)
    delta = n - o
    marker = "▲" if delta > 0 else "▼" if delta < 0 else "="
    print(f"  {day:<12} {o:>10,.0f} {n:>10,.0f} {delta:>+10,.0f} {marker}")
    if o > 0: orig_wins += 1
    else: orig_losses += 1
    if n > 0: new_wins += 1
    else: new_losses += 1

print(f"\n  Original: {orig_wins} win days / {orig_losses} loss days")
print(f"  Filtered: {new_wins} win days / {new_losses} loss days")

# Worst day comparison
orig_worst = min(orig_by_day.values())
new_worst = min(new_by_day.values()) if new_by_day else 0
print(f"\n  Worst day original: ₹{orig_worst:,.0f}")
print(f"  Worst day filtered: ₹{new_worst:,.0f}")

# Max drawdown calculation
def calc_dd(daily_pnls):
    peak = 0
    running = 0
    max_dd = 0
    for p in daily_pnls:
        running += p
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    return max_dd

orig_dd = calc_dd([orig_by_day[d] for d in all_days])
new_dd = calc_dd([new_by_day.get(d, 0) for d in all_days])
print(f"\n  Max drawdown original: ₹{orig_dd:,.0f}")
print(f"  Max drawdown filtered: ₹{new_dd:,.0f}")

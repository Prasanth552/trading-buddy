import sqlite3
from collections import defaultdict

conn = sqlite3.connect("data/trading_buddy.db")
conn.row_factory = sqlite3.Row

# Determine market direction from CH1 trades (index options)
ch1 = conn.execute("""
    SELECT date(ts) as day, symbol,
           COALESCE(pnl,0) - COALESCE(charges,0) as net_pnl
    FROM trades WHERE status='CLOSED' AND channel='ch1'
""").fetchall()

day_direction = {}
for t in ch1:
    day = t["day"]
    sym = t["symbol"]
    if day not in day_direction:
        day_direction[day] = {"pe": 0, "ce": 0}
    if "PE" in sym:
        day_direction[day]["pe"] += 1
    elif "CE" in sym:
        day_direction[day]["ce"] += 1

for day in day_direction:
    d = day_direction[day]
    if d["pe"] > d["ce"]:
        day_direction[day] = "BEARISH"
    elif d["ce"] > d["pe"]:
        day_direction[day] = "BULLISH"
    else:
        day_direction[day] = "MIXED"

# Get all trades grouped by day and channel
all_trades = conn.execute("""
    SELECT date(ts) as day, channel, symbol,
           COALESCE(pnl,0) - COALESCE(charges,0) as net_pnl
    FROM trades WHERE status='CLOSED' ORDER BY ts
""").fetchall()

# Aggregate per day per channel
day_channel = defaultdict(lambda: defaultdict(float))
day_total = defaultdict(float)
for t in all_trades:
    day_channel[t["day"]][t["channel"]] += t["net_pnl"]
    day_total[t["day"]] += t["net_pnl"]

# Print bullish vs bearish day performance
print("=" * 80)
print("  BULLISH vs BEARISH DAY ANALYSIS")
print("=" * 80)

channels = ["ch1", "oeh", "oel", "orb", "bsq", "mom", "pdhl"]

# Separate days
bullish_days = [d for d in sorted(day_direction) if day_direction[d] == "BULLISH"]
bearish_days = [d for d in sorted(day_direction) if day_direction[d] == "BEARISH"]

print(f"\n  Bullish days: {len(bullish_days)}, Bearish days: {len(bearish_days)}")

# Per-channel performance on bullish vs bearish days
print(f"\n  {'Channel':<10} {'Bullish avg':>12} {'Bearish avg':>12} {'Verdict':>12}")
print(f"  {'-'*10} {'-'*12} {'-'*12} {'-'*12}")

for ch in channels:
    bull_vals = [day_channel[d][ch] for d in bullish_days if ch in day_channel[d]]
    bear_vals = [day_channel[d][ch] for d in bearish_days if ch in day_channel[d]]
    bull_avg = sum(bull_vals) / len(bull_vals) if bull_vals else 0
    bear_avg = sum(bear_vals) / len(bear_vals) if bear_vals else 0

    if bull_avg > 0 and bear_avg > 0:
        verdict = "BOTH GOOD"
    elif bull_avg > 0:
        verdict = "BULL ONLY"
    elif bear_avg > 0:
        verdict = "BEAR ONLY"
    else:
        verdict = "BOTH BAD"

    print(f"  {ch:<10} {bull_avg:>+12,.0f} {bear_avg:>+12,.0f} {verdict:>12}")

# Total P&L on bullish vs bearish days
bull_total = [day_total[d] for d in bullish_days]
bear_total = [day_total[d] for d in bearish_days]
print(f"\n  {'TOTAL':<10} {sum(bull_total)/len(bull_total) if bull_total else 0:>+12,.0f} {sum(bear_total)/len(bear_total) if bear_total else 0:>+12,.0f}")

# Detailed day-by-day
print(f"\n{'='*80}")
print("  BULLISH DAYS — detailed")
print(f"{'='*80}")
print(f"  {'Date':<12} {'Direction':>9} {'Total':>10} {'CH1':>8} {'OEH':>8} {'ORB':>8} {'BSQ':>8} {'MOM':>8}")
print(f"  {'-'*12} {'-'*9} {'-'*10} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
for d in bullish_days:
    total = day_total.get(d, 0)
    parts = [f"{day_channel[d].get(ch, 0):>+8,.0f}" for ch in ["ch1", "oeh", "orb", "bsq", "mom"]]
    print(f"  {d:<12} {'BULLISH':>9} {total:>+10,.0f} {' '.join(parts)}")

bull_wins = sum(1 for d in bullish_days if day_total.get(d, 0) > 0)
print(f"\n  Bullish win rate: {bull_wins}/{len(bullish_days)} ({bull_wins*100/len(bullish_days):.0f}%)")
print(f"  Bullish total: {sum(day_total.get(d,0) for d in bullish_days):>+,.0f}")
print(f"  Bullish avg/day: {sum(day_total.get(d,0) for d in bullish_days)/len(bullish_days):>+,.0f}")

print(f"\n{'='*80}")
print("  BEARISH DAYS — detailed")
print(f"{'='*80}")
print(f"  {'Date':<12} {'Direction':>9} {'Total':>10} {'CH1':>8} {'OEH':>8} {'ORB':>8} {'BSQ':>8} {'MOM':>8}")
print(f"  {'-'*12} {'-'*9} {'-'*10} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
for d in bearish_days:
    total = day_total.get(d, 0)
    parts = [f"{day_channel[d].get(ch, 0):>+8,.0f}" for ch in ["ch1", "oeh", "orb", "bsq", "mom"]]
    print(f"  {d:<12} {'BEARISH':>9} {total:>+10,.0f} {' '.join(parts)}")

bear_wins = sum(1 for d in bearish_days if day_total.get(d, 0) > 0)
print(f"\n  Bearish win rate: {bear_wins}/{len(bearish_days)} ({bear_wins*100/len(bearish_days):.0f}%)")
print(f"  Bearish total: {sum(day_total.get(d,0) for d in bearish_days):>+,.0f}")
print(f"  Bearish avg/day: {sum(day_total.get(d,0) for d in bearish_days)/len(bearish_days):>+,.0f}")

# Also check: on bullish days, do PE-based strategies (OEH) bleed?
print(f"\n{'='*80}")
print("  KEY QUESTION: Does OEH (bearish strategy) bleed on bullish days?")
print(f"{'='*80}")
oeh_bull = [day_channel[d]["oeh"] for d in bullish_days if "oeh" in day_channel[d]]
oeh_bear = [day_channel[d]["oeh"] for d in bearish_days if "oeh" in day_channel[d]]
print(f"  OEH on bullish days:  {len(oeh_bull)} days, avg={sum(oeh_bull)/len(oeh_bull):>+,.0f}, total={sum(oeh_bull):>+,.0f}")
print(f"  OEH on bearish days:  {len(oeh_bear)} days, avg={sum(oeh_bear)/len(oeh_bear):>+,.0f}, total={sum(oeh_bear):>+,.0f}")
print(f"  OEH bullish win rate: {sum(1 for v in oeh_bull if v > 0)}/{len(oeh_bull)}")
print(f"  OEH bearish win rate: {sum(1 for v in oeh_bear if v > 0)}/{len(oeh_bear)}")

# Check ORB PE on bullish days
print(f"\n  KEY QUESTION: Does ORB PE bleed on bullish days?")
orb_trades = conn.execute("""
    SELECT date(ts) as day, symbol,
           COALESCE(pnl,0) - COALESCE(charges,0) as net_pnl
    FROM trades WHERE status='CLOSED' AND channel='orb'
""").fetchall()

orb_pe_by_day = defaultdict(float)
orb_ce_by_day = defaultdict(float)
for t in orb_trades:
    if "PE" in t["symbol"]:
        orb_pe_by_day[t["day"]] += t["net_pnl"]
    else:
        orb_ce_by_day[t["day"]] += t["net_pnl"]

orb_pe_bull = [orb_pe_by_day[d] for d in bullish_days if d in orb_pe_by_day]
orb_pe_bear = [orb_pe_by_day[d] for d in bearish_days if d in orb_pe_by_day]
if orb_pe_bull:
    print(f"  ORB PE on bullish days: {len(orb_pe_bull)} days, avg={sum(orb_pe_bull)/len(orb_pe_bull):>+,.0f}, total={sum(orb_pe_bull):>+,.0f}")
if orb_pe_bear:
    print(f"  ORB PE on bearish days: {len(orb_pe_bear)} days, avg={sum(orb_pe_bear)/len(orb_pe_bear):>+,.0f}, total={sum(orb_pe_bear):>+,.0f}")

"""Market Bias Detector — backtest multiple detection methods.

We test several ways to predict today's market direction, then apply
strategy filters based on the prediction and compare P&L.

Detection methods:
  1. prev_day       — yesterday's CH1 direction (trend continuation)
  2. prev_3day      — majority of last 3 days
  3. always_bear    — always predict bearish (baseline)
  4. always_bull    — always predict bullish (baseline)
  5. no_filter      — original, no filtering (baseline)

Strategy filter rules:
  BEARISH predicted → disable OEL, keep ORB/BSQ/PDHL/OEH/MOM/CH1
  BULLISH predicted → disable ORB/BSQ/PDHL, keep OEH/MOM/CH1, enable OEL
  (CH1 and OEH always on — they profit in both directions)
"""
import sqlite3
from collections import defaultdict

conn = sqlite3.connect("data/trading_buddy.db")
conn.row_factory = sqlite3.Row

# ── Step 1: Get actual market direction per day from CH1 ─────────
ch1_trades = conn.execute("""
    SELECT date(ts) as day, symbol
    FROM trades WHERE status='CLOSED' AND channel='ch1'
""").fetchall()

day_actual = {}
for t in ch1_trades:
    day = t["day"]
    if day not in day_actual:
        day_actual[day] = {"pe": 0, "ce": 0}
    if "PE" in t["symbol"]:
        day_actual[day]["pe"] += 1
    elif "CE" in t["symbol"]:
        day_actual[day]["ce"] += 1

for day in day_actual:
    d = day_actual[day]
    day_actual[day] = "BEARISH" if d["pe"] > d["ce"] else "BULLISH" if d["ce"] > d["pe"] else "MIXED"

# ── Step 2: Get all trades ───────────────────────────────────────
all_trades = conn.execute("""
    SELECT date(ts) as day, channel, symbol,
           COALESCE(pnl,0) - COALESCE(charges,0) as net_pnl
    FROM trades WHERE status='CLOSED' ORDER BY ts
""").fetchall()

# Group by day
day_trades = defaultdict(list)
for t in all_trades:
    day_trades[t["day"]].append(t)

all_days = sorted(day_trades.keys())

# ── Step 3: Define detectors ────────────────────────────────────
def detect_prev_day(day_idx):
    """Use previous day's actual direction."""
    if day_idx == 0:
        return "BEARISH"  # default for first day
    prev = all_days[day_idx - 1]
    return day_actual.get(prev, "BEARISH")

def detect_prev_3day(day_idx):
    """Majority of last 3 days."""
    lookback = []
    for i in range(max(0, day_idx - 3), day_idx):
        d = day_actual.get(all_days[i], "BEARISH")
        lookback.append(d)
    if not lookback:
        return "BEARISH"
    bear_count = sum(1 for x in lookback if x == "BEARISH")
    return "BEARISH" if bear_count > len(lookback) / 2 else "BULLISH"

def detect_always_bear(day_idx):
    return "BEARISH"

def detect_always_bull(day_idx):
    return "BULLISH"

def detect_none(day_idx):
    return "NONE"  # no filter

# ── Step 4: Define strategy filter ──────────────────────────────
# Channels that bleed on bullish days
BEARISH_ONLY_CHANNELS = {"orb", "bsq", "bsq_ce", "bsq_pe", "pdhl", "orb_pe", "orb_ce"}
# Channels that bleed on bearish days
BULLISH_ONLY_CHANNELS = {"oel"}
# Always-on channels
ALWAYS_ON = {"ch1", "ch1b", "ch2", "ch5", "oeh", "mom", "mom_ce", "mom_pe", "orf", "aft"}

def should_take_trade(channel, prediction):
    """Return True if this channel should trade given the market prediction."""
    if prediction == "NONE":
        return True  # no filter
    if channel in ALWAYS_ON:
        return True
    if prediction == "BULLISH" and channel in BEARISH_ONLY_CHANNELS:
        return False  # skip bearish strategies on bullish days
    if prediction == "BEARISH" and channel in BULLISH_ONLY_CHANNELS:
        return False  # skip bullish strategies on bearish days
    return True

# ── Step 5: Run backtest for each detector ──────────────────────
detectors = {
    "no_filter": detect_none,
    "always_bear": detect_always_bear,
    "always_bull": detect_always_bull,
    "prev_day": detect_prev_day,
    "prev_3day": detect_prev_3day,
}

results = {}
for name, detector in detectors.items():
    total_pnl = 0
    daily_pnls = []
    trades_taken = 0
    trades_skipped = 0
    skipped_pnl = 0
    correct_predictions = 0

    for idx, day in enumerate(all_days):
        prediction = detector(idx)
        actual = day_actual.get(day, "UNKNOWN")
        if prediction == actual:
            correct_predictions += 1

        day_pnl = 0
        for t in day_trades[day]:
            if should_take_trade(t["channel"], prediction):
                day_pnl += t["net_pnl"]
                trades_taken += 1
            else:
                trades_skipped += 1
                skipped_pnl += t["net_pnl"]

        total_pnl += day_pnl
        daily_pnls.append(day_pnl)

    # Calculate stats
    win_days = sum(1 for p in daily_pnls if p > 0)
    loss_days = sum(1 for p in daily_pnls if p <= 0)
    worst_day = min(daily_pnls) if daily_pnls else 0

    # Max drawdown
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

    accuracy = correct_predictions * 100 / len(all_days) if name not in ("no_filter",) else 0

    results[name] = {
        "total_pnl": total_pnl,
        "trades_taken": trades_taken,
        "trades_skipped": trades_skipped,
        "skipped_pnl": skipped_pnl,
        "win_days": win_days,
        "loss_days": loss_days,
        "worst_day": worst_day,
        "max_dd": max_dd,
        "accuracy": accuracy,
        "daily_pnls": daily_pnls,
    }

# ── Step 6: Print results ───────────────────────────────────────
print("=" * 90)
print("  MARKET BIAS DETECTOR — BACKTEST RESULTS")
print("=" * 90)

print(f"\n  Actual market split: {sum(1 for v in day_actual.values() if v=='BEARISH')} bearish / "
      f"{sum(1 for v in day_actual.values() if v=='BULLISH')} bullish out of {len(all_days)} days")

print(f"\n  {'Detector':<14} {'Net P&L':>12} {'Trades':>8} {'Skipped':>8} {'Skip PnL':>10} "
      f"{'Win/Loss':>8} {'Worst':>10} {'MaxDD':>10} {'Accuracy':>8}")
print(f"  {'-'*14} {'-'*12} {'-'*8} {'-'*8} {'-'*10} {'-'*8} {'-'*10} {'-'*10} {'-'*8}")

for name in ["no_filter", "always_bear", "always_bull", "prev_day", "prev_3day"]:
    r = results[name]
    wl = f"{r['win_days']}/{r['loss_days']}"
    acc = f"{r['accuracy']:.0f}%" if r['accuracy'] > 0 else "—"
    skip_sign = "+" if r['skipped_pnl'] < 0 else "-"  # negative skipped pnl = we avoided losses
    print(f"  {name:<14} {r['total_pnl']:>+12,.0f} {r['trades_taken']:>8,} {r['trades_skipped']:>8,} "
          f"{r['skipped_pnl']:>+10,.0f} {wl:>8} {r['worst_day']:>+10,.0f} {r['max_dd']:>10,.0f} {acc:>8}")

# ── Step 7: Day-by-day comparison for best detector ─────────────
print(f"\n{'='*90}")
print("  DAY-BY-DAY: prev_day detector vs no_filter")
print(f"{'='*90}")
print(f"  {'Date':<12} {'Actual':>8} {'Predict':>8} {'NoFilter':>10} {'Filtered':>10} {'Delta':>10} {'Correct':>7}")
print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*10} {'-'*10} {'-'*10} {'-'*7}")

no_filter_daily = results["no_filter"]["daily_pnls"]
prev_day_daily = results["prev_day"]["daily_pnls"]

for idx, day in enumerate(all_days):
    actual = day_actual.get(day, "?")
    prediction = detect_prev_day(idx)
    nf = no_filter_daily[idx]
    pd = prev_day_daily[idx]
    delta = pd - nf
    correct = "✓" if prediction == actual else "✗"
    marker = " ▲" if delta > 5000 else " ▼" if delta < -5000 else ""
    print(f"  {day:<12} {actual:>8} {prediction:>8} {nf:>+10,.0f} {pd:>+10,.0f} {delta:>+10,.0f} {correct:>7}{marker}")

# ── Step 8: What trades were skipped and their impact ───────────
print(f"\n{'='*90}")
print("  PREV_DAY DETECTOR — skipped trades breakdown")
print(f"{'='*90}")

skip_by_channel = defaultdict(lambda: {"count": 0, "pnl": 0})
for idx, day in enumerate(all_days):
    prediction = detect_prev_day(idx)
    for t in day_trades[day]:
        if not should_take_trade(t["channel"], prediction):
            skip_by_channel[t["channel"]]["count"] += 1
            skip_by_channel[t["channel"]]["pnl"] += t["net_pnl"]

print(f"  {'Channel':<12} {'Skipped':>8} {'Skipped PnL':>12} {'Verdict':>12}")
print(f"  {'-'*12} {'-'*8} {'-'*12} {'-'*12}")
for ch in sorted(skip_by_channel, key=lambda c: skip_by_channel[c]["pnl"]):
    s = skip_by_channel[ch]
    verdict = "GOOD SKIP" if s["pnl"] < 0 else "BAD SKIP"
    print(f"  {ch:<12} {s['count']:>8} {s['pnl']:>+12,.0f} {verdict:>12}")

total_skip_pnl = sum(s["pnl"] for s in skip_by_channel.values())
good_skips = sum(s["pnl"] for s in skip_by_channel.values() if s["pnl"] < 0)
bad_skips = sum(s["pnl"] for s in skip_by_channel.values() if s["pnl"] > 0)
print(f"\n  Total skipped P&L: {total_skip_pnl:>+,.0f}")
print(f"  Good skips (avoided losses): {good_skips:>+,.0f}")
print(f"  Bad skips (missed profits):  {bad_skips:>+,.0f}")

# ── Step 9: Combined approach — OEH cap + market bias ───────────
print(f"\n{'='*90}")
print("  COMBINED: OEH cap 12 + prev_day market bias filter")
print(f"{'='*90}")

# Replay with BOTH filters
oeh_by_day = defaultdict(list)
for t in all_trades:
    if t["channel"] == "oeh":
        oeh_by_day[t["day"]].append(t)

combined_total = 0
combined_daily = []
for idx, day in enumerate(all_days):
    prediction = detect_prev_day(idx)
    day_pnl = 0
    oeh_count = 0
    # Sort OEH by time (already in order)
    oeh_today = oeh_by_day.get(day, [])
    oeh_ids_kept = set()
    for t in oeh_today[:12]:
        oeh_ids_kept.add(id(t))

    for t in day_trades[day]:
        # Market bias filter
        if not should_take_trade(t["channel"], prediction):
            continue
        # OEH cap
        if t["channel"] == "oeh" and id(t) not in oeh_ids_kept:
            continue
        day_pnl += t["net_pnl"]

    combined_total += day_pnl
    combined_daily.append(day_pnl)

comb_wins = sum(1 for p in combined_daily if p > 0)
comb_losses = sum(1 for p in combined_daily if p <= 0)
comb_worst = min(combined_daily)
# Max DD
peak = running = max_dd_c = 0
for p in combined_daily:
    running += p
    if running > peak: peak = running
    dd = peak - running
    if dd > max_dd_c: max_dd_c = dd

orig = results["no_filter"]
print(f"\n  {'Metric':<20} {'Original':>12} {'Combined':>12} {'Change':>12}")
print(f"  {'-'*20} {'-'*12} {'-'*12} {'-'*12}")
print(f"  {'Net P&L':<20} {orig['total_pnl']:>+12,.0f} {combined_total:>+12,.0f} {combined_total-orig['total_pnl']:>+12,.0f}")
print(f"  {'Win/Loss days':<20} {orig['win_days']}/{orig['loss_days']:>10} {comb_wins}/{comb_losses:>10}")
print(f"  {'Worst day':<20} {orig['worst_day']:>+12,.0f} {comb_worst:>+12,.0f}")
print(f"  {'Max drawdown':<20} {orig['max_dd']:>12,.0f} {max_dd_c:>12,.0f}")
print(f"  {'Avg daily':<20} {orig['total_pnl']/len(all_days):>+12,.0f} {combined_total/len(all_days):>+12,.0f}")

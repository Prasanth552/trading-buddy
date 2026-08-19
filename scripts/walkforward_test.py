"""Walk-forward test for the adaptive filter.

The honest way to test a learning system: at each trade, the filter
only sees trades that CLOSED before this one. No future peeking.

For each CH1 trade chronologically:
  1. Build a "journal" from only earlier closed trades
  2. Score this trade using that journal
  3. Record whether filter would TAKE or SKIP
  4. Compare: all trades P&L vs filtered-only P&L

Market-data factors (FII, crude, sector index) are skipped since we
don't have historical snapshots. This tests the core question:
"Does learning from your own past trades improve future decisions?"

Run on VM:  python3 scripts/walkforward_test.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

import config
from src.storage import db

IST = ZoneInfo("Asia/Kolkata")
db.init_db()


def extract_stock(symbol):
    parts = symbol.strip().split()
    return parts[0].upper() if parts else symbol.upper()


def extract_opt_type(symbol):
    parts = symbol.strip().split()
    if parts and parts[-1] in ("CE", "PE"):
        return parts[-1]
    return "CE"


def win_rate(trades):
    if len(trades) < 3:
        return None
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return wins / len(trades)


def avg_pnl(trades):
    if not trades:
        return 0
    return sum(t["pnl"] for t in trades) / len(trades)


def score_with_journal(stock, opt_type, hour, weekday, journal):
    """Score a trade using only the journal (past trades). No live data."""
    score = 50
    reasons = []

    # 1. STOCK TRACK RECORD
    stock_trades = [t for t in journal if t["stock"] == stock]
    stock_same = [t for t in stock_trades if t["opt_type"] == opt_type]

    if len(stock_same) >= 3:
        wr = win_rate(stock_same)
        avg = avg_pnl(stock_same)
        if wr is not None:
            if wr >= 0.65:
                bonus = min(20, int((wr - 0.5) * 60))
                score += bonus
                reasons.append(f"{stock} {opt_type}: {wr:.0%} WR ({len(stock_same)}t) +{bonus}")
            elif wr <= 0.35:
                penalty = min(20, int((0.5 - wr) * 60))
                score -= penalty
                reasons.append(f"{stock} {opt_type}: {wr:.0%} WR ({len(stock_same)}t) -{penalty}")

            if avg > 500:
                score += 5
            elif avg < -500:
                score -= 5
    elif len(stock_trades) >= 3:
        wr = win_rate(stock_trades)
        if wr is not None:
            if wr >= 0.60:
                score += 8
            elif wr <= 0.35:
                score -= 8

    # 2. OPTION TYPE OVERALL
    type_trades = [t for t in journal if t["opt_type"] == opt_type]
    if len(type_trades) >= 10:
        wr = win_rate(type_trades)
        if wr is not None:
            if wr >= 0.55:
                score += 5
            elif wr <= 0.40:
                score -= 5

    # 3. TIME OF DAY
    hour_trades = [t for t in journal if t["hour"] == hour]
    if len(hour_trades) >= 5:
        wr = win_rate(hour_trades)
        if wr is not None:
            if wr >= 0.60:
                score += 10
            elif wr <= 0.35:
                score -= 10
    else:
        if hour == 9:
            score += 5
        elif hour >= 14:
            score -= 3

    # 4. DAY OF WEEK
    day_trades = [t for t in journal if t["weekday"] == weekday]
    if len(day_trades) >= 5:
        wr = win_rate(day_trades)
        if wr is not None:
            if wr >= 0.60:
                score += 5
            elif wr <= 0.35:
                score -= 5

    return score, reasons


# ---------------------------------------------------------------------------
# Pull all closed CH1 trades, oldest first
# ---------------------------------------------------------------------------
with db.get_conn() as conn:
    rows = conn.execute(
        """SELECT id, ts, symbol, side, qty, price, exit_price, pnl, status, channel
           FROM trades
           WHERE side = 'BUY'
             AND (channel IS NULL OR channel = 'ch1')
             AND status LIKE 'CLOSED%'
             AND pnl IS NOT NULL
           ORDER BY ts ASC""",
    ).fetchall()

if not rows:
    print("No closed CH1 trades found.")
    sys.exit()

print("=" * 80)
print("WALK-FORWARD TEST — Adaptive Filter")
print("=" * 80)
print(f"Total closed CH1 trades: {len(rows)}")
print()
print("Rules: at each trade, filter only sees trades that closed BEFORE it.")
print("No future peeking. Exactly how it would run live.")
print("=" * 80)
print()

# Build the walk-forward
journal = []  # grows as we process each trade
all_pnl = 0
filtered_pnl = 0
all_count = 0
filt_count = 0
all_wins = 0
filt_wins = 0
skipped_losers = 0
skipped_winners = 0
skipped_losers_pnl = 0
skipped_winners_pnl = 0

daily = defaultdict(lambda: {"all_pnl": 0, "filt_pnl": 0, "all_count": 0, "filt_count": 0})

# First N trades go into "training" — filter needs some history
MIN_JOURNAL = 10

for i, row in enumerate(rows):
    stock = extract_stock(row["symbol"])
    opt_type = extract_opt_type(row["symbol"])
    pnl = row["pnl"]
    won = pnl > 0
    ts = row["ts"]
    trade_date = ts[:10]
    hour = int(ts[11:13]) if len(ts) > 13 else 9
    weekday = datetime.fromisoformat(ts[:19]).weekday()

    all_pnl += pnl
    all_count += 1
    if won:
        all_wins += 1
    daily[trade_date]["all_pnl"] += pnl
    daily[trade_date]["all_count"] += 1

    if i < MIN_JOURNAL:
        # Training phase — take all trades, add to journal
        filtered_pnl += pnl
        filt_count += 1
        if won:
            filt_wins += 1
        daily[trade_date]["filt_pnl"] += pnl
        daily[trade_date]["filt_count"] += 1
        tag = "TRAIN"
        score = 50
    else:
        # Test phase — score using only past journal
        score, reasons = score_with_journal(stock, opt_type, hour, weekday, journal)
        would_take = score >= 50

        if would_take:
            filtered_pnl += pnl
            filt_count += 1
            if won:
                filt_wins += 1
            daily[trade_date]["filt_pnl"] += pnl
            daily[trade_date]["filt_count"] += 1
            tag = "TAKE"
        else:
            tag = "SKIP"
            if won:
                skipped_winners += 1
                skipped_winners_pnl += pnl
            else:
                skipped_losers += 1
                skipped_losers_pnl += abs(pnl)

    # Add this trade to journal for future trades to learn from
    journal.append({
        "stock": stock,
        "opt_type": opt_type,
        "pnl": pnl,
        "won": won,
        "ts": ts,
        "weekday": weekday,
        "hour": hour,
    })

    icon = "W" if won else "L"
    pnl_str = f"₹{pnl:+,.0f}"
    print(f"  #{row['id']:<4} {row['symbol']:<25} [{icon}] {pnl_str:>10}  "
          f"score={score:>3}  {tag}")

# ---------------------------------------------------------------------------
# Daily breakdown
# ---------------------------------------------------------------------------
print()
print("=" * 80)
print("DAILY BREAKDOWN")
print("=" * 80)
print()
print(f"  {'Date':<12} {'All Trades':>12} {'All P&L':>12} {'Filt Trades':>12} {'Filt P&L':>12} {'Edge':>10}")
print(f"  {'─' * 12} {'─' * 12} {'─' * 12} {'─' * 12} {'─' * 12} {'─' * 10}")

for date in sorted(daily.keys()):
    d = daily[date]
    edge = d["filt_pnl"] - d["all_pnl"]
    edge_str = f"₹{edge:+,.0f}" if edge != 0 else "—"
    print(f"  {date:<12} {d['all_count']:>12} {'₹{:+,}'.format(int(d['all_pnl'])):>12} "
          f"{d['filt_count']:>12} {'₹{:+,}'.format(int(d['filt_pnl'])):>12} {edge_str:>10}")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
print("=" * 80)
print("WALK-FORWARD RESULTS")
print("=" * 80)
print()

all_wr = all_wins / all_count * 100 if all_count else 0
filt_wr = filt_wins / filt_count * 100 if filt_count else 0

print(f"  {'Metric':<30} {'All Trades':>15} {'Filtered':>15}")
print(f"  {'─' * 30} {'─' * 15} {'─' * 15}")
print(f"  {'Total Trades':<30} {all_count:>15} {filt_count:>15}")
print(f"  {'Wins':<30} {all_wins:>15} {filt_wins:>15}")
print(f"  {'Win Rate':<30} {all_wr:>14.1f}% {filt_wr:>14.1f}%")
print(f"  {'Total P&L':<30} {'₹{:+,}'.format(int(all_pnl)):>15} {'₹{:+,}'.format(int(filtered_pnl)):>15}")
avg_all = int(all_pnl / all_count) if all_count else 0
avg_filt = int(filtered_pnl / filt_count) if filt_count else 0
print(f"  {'Avg P&L / trade':<30} {'₹{:+,}'.format(avg_all):>15} {'₹{:+,}'.format(avg_filt):>15}")

print()
edge = filtered_pnl - all_pnl
print(f"  Filter edge: ₹{edge:+,.0f}")
print()
print(f"  Correctly skipped losers:  {skipped_losers} trades (saved ₹{skipped_losers_pnl:,.0f})")
print(f"  Wrongly skipped winners:   {skipped_winners} trades (missed ₹{skipped_winners_pnl:,.0f})")

if skipped_losers + skipped_winners > 0:
    skip_accuracy = skipped_losers / (skipped_losers + skipped_winners) * 100
    print(f"  Skip accuracy:             {skip_accuracy:.0f}% (of skipped trades, how many were actual losers)")

print()
if edge > 0:
    print(f"  VERDICT: Filter HELPS — saves ₹{edge:,.0f} by learning from past trades")
elif edge < 0:
    print(f"  VERDICT: Filter HURTS — costs ₹{abs(edge):,.0f} (skipping too many winners)")
else:
    print(f"  VERDICT: No difference")

print()
print("  Note: This test uses ONLY journal-based learning (stock track record,")
print("  time/day patterns). Live market factors (FII, crude, sector trend)")
print("  would add more signal in real-time but can't be backtested here.")
print("=" * 80)

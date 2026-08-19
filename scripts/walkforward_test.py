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


def avg_win_pnl(trades):
    winners = [t["pnl"] for t in trades if t["pnl"] > 0]
    return sum(winners) / len(winners) if winners else 0


def worst_loss(trades):
    losers = [t["pnl"] for t in trades if t["pnl"] <= 0]
    return min(losers) if losers else 0


def recent_streak(trades, n=3):
    if len(trades) < n:
        return "mixed"
    recent = sorted(trades, key=lambda t: t["ts"], reverse=True)[:n]
    if all(t["won"] for t in recent):
        return "winning"
    if all(not t["won"] for t in recent):
        return "losing"
    return "mixed"


def expectancy(trades):
    if len(trades) < 5:
        return None
    wr = win_rate(trades)
    if wr is None:
        return None
    aw = avg_win_pnl(trades)
    losers = [t["pnl"] for t in trades if t["pnl"] <= 0]
    al = sum(losers) / len(losers) if losers else 0
    return (wr * aw) + ((1 - wr) * al)


def score_with_journal(stock, opt_type, hour, weekday, journal):
    """Risk-focused scoring. Heavier penalties than boosts — asymmetric."""
    score = 50
    reasons = []

    stock_trades = [t for t in journal if t["stock"] == stock]
    stock_same = [t for t in stock_trades if t["opt_type"] == opt_type]

    # 1. RECENT STREAK (biggest risk signal)
    if len(stock_same) >= 2:
        streak = recent_streak(stock_same, n=min(3, len(stock_same)))
        if streak == "losing":
            n = min(3, len(stock_same))
            score -= 20
            reasons.append(f"{stock} {opt_type}: last {n} ALL lost (-20)")
        elif streak == "winning":
            score += 5
    elif len(stock_trades) >= 2:
        streak = recent_streak(stock_trades, n=min(3, len(stock_trades)))
        if streak == "losing":
            score -= 15

    # 2. LOSS MAGNITUDE — blowup history
    if len(stock_same) >= 3:
        worst = worst_loss(stock_same)
        aw = avg_win_pnl(stock_same)
        if worst < -15000:
            score -= 15
            reasons.append(f"{stock} {opt_type}: ₹{abs(worst):,.0f} blowup in history (-15)")
        elif worst < -8000 and aw > 0 and abs(worst) > aw * 3:
            score -= 10
            reasons.append(f"{stock}: worst loss {abs(worst)/aw:.0f}x avg win (-10)")
    elif len(stock_trades) >= 3:
        worst = worst_loss(stock_trades)
        if worst < -15000:
            score -= 12

    # 3. EXPECTANCY
    if len(stock_same) >= 5:
        exp = expectancy(stock_same)
        if exp is not None:
            if exp < -500:
                score -= 15
                reasons.append(f"{stock}: negative expectancy ₹{exp:+,.0f}/trade (-15)")
            elif exp < 0:
                score -= 5
            elif exp > 2000:
                score += 10
            elif exp > 500:
                score += 5

    # 4. STOCK WIN RATE (mild)
    if len(stock_same) >= 5:
        wr = win_rate(stock_same)
        if wr is not None:
            if wr >= 0.70:
                score += 8
            elif wr <= 0.35:
                score -= 10

    # 5. OVERALL RECENT LOSING STREAK
    if len(journal) >= 5:
        recent_5 = sorted(journal, key=lambda t: t["ts"], reverse=True)[:5]
        if all(not t["won"] for t in recent_5):
            score -= 15
            reasons.append("Last 5 trades ALL lost (-15)")
        elif sum(1 for t in recent_5 if not t["won"]) >= 4:
            score -= 8

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
print("WALK-FORWARD TEST v2 — Risk-Focused Filter")
print("=" * 80)
print(f"Total closed CH1 trades: {len(rows)}")
print()
print("Rules: at each trade, filter only sees trades that closed BEFORE it.")
print("Focus: detect RISK (streaks, blowups, bad expectancy) not just win rate.")
print("=" * 80)
print()

journal = []
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
reduced_count = 0
reduced_saved = 0

daily = defaultdict(lambda: {"all_pnl": 0, "filt_pnl": 0, "all_count": 0, "filt_count": 0})

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
        filtered_pnl += pnl
        filt_count += 1
        if won:
            filt_wins += 1
        daily[trade_date]["filt_pnl"] += pnl
        daily[trade_date]["filt_count"] += 1
        tag = "TRAIN"
        score = 50
    else:
        score, reasons = score_with_journal(stock, opt_type, hour, weekday, journal)

        if score >= 50:
            # TAKE — full size
            filtered_pnl += pnl
            filt_count += 1
            if won:
                filt_wins += 1
            daily[trade_date]["filt_pnl"] += pnl
            daily[trade_date]["filt_count"] += 1
            tag = "TAKE"
        elif score >= 30:
            # REDUCE — half size (simulate by counting half P&L)
            half_pnl = pnl / 2
            filtered_pnl += half_pnl
            filt_count += 1
            if won:
                filt_wins += 1
            daily[trade_date]["filt_pnl"] += half_pnl
            daily[trade_date]["filt_count"] += 1
            reduced_count += 1
            if pnl < 0:
                reduced_saved += abs(pnl) / 2
            tag = "REDUCE"
        else:
            # SKIP
            tag = "SKIP"
            if won:
                skipped_winners += 1
                skipped_winners_pnl += pnl
            else:
                skipped_losers += 1
                skipped_losers_pnl += abs(pnl)

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
    extra = ""
    if tag == "SKIP" and not won:
        extra = f"  ← saved ₹{abs(pnl):,.0f}"
    elif tag == "REDUCE" and not won:
        extra = f"  ← halved loss"
    print(f"  #{row['id']:<4} {row['symbol']:<25} [{icon}] {pnl_str:>10}  "
          f"score={score:>3}  {tag}{extra}")

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
print(f"  SKIP decisions:")
print(f"    Correctly skipped losers:  {skipped_losers} trades (saved ₹{skipped_losers_pnl:,.0f})")
print(f"    Wrongly skipped winners:   {skipped_winners} trades (missed ₹{skipped_winners_pnl:,.0f})")
if skipped_losers + skipped_winners > 0:
    skip_accuracy = skipped_losers / (skipped_losers + skipped_winners) * 100
    print(f"    Skip accuracy:             {skip_accuracy:.0f}%")
print()
print(f"  REDUCE decisions:")
print(f"    Trades at half size:       {reduced_count}")
print(f"    Saved by halving losses:   ₹{reduced_saved:,.0f}")

print()
if edge > 0:
    print(f"  VERDICT: Filter SAVES ₹{edge:,.0f}")
    print(f"  How: skipping {skipped_losers} bad trades + halving {reduced_count} risky ones")
elif edge < 0:
    print(f"  VERDICT: Filter COSTS ₹{abs(edge):,.0f}")
else:
    print(f"  VERDICT: No difference")

print()
print("  Note: REDUCE simulates half position size (P&L ÷ 2).")
print("  Live market factors (FII, crude, sector) add more signal in real-time.")
print("=" * 80)

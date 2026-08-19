"""Retroactively run the smart filter on last 8 days of CH1 trades.

Compares: all CH1 trades vs filtered-only (score >= 50).
Run on VM: python3 scripts/check_filter_history.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage import db
from src.signals.smart_filter import evaluate_signal
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

IST = ZoneInfo("Asia/Kolkata")
db.init_db()

# Get last 8 trading days of CH1 trades
cutoff = (datetime.now(IST) - timedelta(days=10)).strftime("%Y-%m-%d")

with db.get_conn() as conn:
    rows = conn.execute(
        """SELECT id, ts, symbol, side, qty, price, exit_price, pnl, status,
                  stop_price, target_price, channel, filter_score
           FROM trades
           WHERE side = 'BUY'
             AND (channel IS NULL OR channel = 'ch1')
             AND ts >= ?
           ORDER BY ts""",
        (cutoff,),
    ).fetchall()

if not rows:
    print("No CH1 trades found in last 8 days")
    sys.exit()

# Parse symbol into stock name and option type
def parse_symbol(sym):
    parts = sym.strip().split()
    if len(parts) >= 3:
        stock = parts[0]
        opt_type = parts[-1]  # CE or PE
        return stock, opt_type
    return sym, "CE"

print("=" * 80)
print("CH1 FILTER BACKCHECK — Last 8 Days")
print("=" * 80)
print()

daily = defaultdict(lambda: {"trades": [], "all_pnl": 0, "filtered_pnl": 0,
                              "all_count": 0, "filt_count": 0,
                              "all_wins": 0, "filt_wins": 0})
total_all = 0
total_filtered = 0
total_trades = 0
total_filt_trades = 0
total_all_wins = 0
total_filt_wins = 0

for row in rows:
    trade_date = row["ts"][:10]
    symbol = row["symbol"]
    pnl = row["pnl"] or 0
    status = row["status"] or ""
    entry = row["price"] or 0
    saved_score = row["filter_score"]

    stock, opt_type = parse_symbol(symbol)

    # Run filter retroactively
    try:
        filt = evaluate_signal(stock, opt_type, "ch1", entry)
        score = filt.score
        action = filt.action
    except Exception as e:
        score = saved_score or -1
        action = "ERR"

    is_closed = "CLOSED" in status
    won = pnl > 0 if is_closed else None
    would_take = score >= 50

    d = daily[trade_date]
    if is_closed:
        d["all_pnl"] += pnl
        d["all_count"] += 1
        total_all += pnl
        total_trades += 1
        if won:
            d["all_wins"] += 1
            total_all_wins += 1

        if would_take:
            d["filtered_pnl"] += pnl
            d["filt_count"] += 1
            total_filtered += pnl
            total_filt_trades += 1
            if won:
                d["filt_wins"] += 1
                total_filt_wins += 1

    icon = "W" if won else ("L" if won is False else "?")
    filt_icon = "TAKE" if would_take else "SKIP"
    skip_saved = f" (saved ₹{abs(pnl):,})" if not would_take and pnl < 0 and is_closed else ""

    d["trades"].append({
        "id": row["id"], "symbol": symbol, "pnl": pnl, "score": score,
        "action": action, "won": icon, "filt": filt_icon, "status": status,
        "skip_saved": skip_saved,
    })

for trade_date in sorted(daily.keys()):
    d = daily[trade_date]
    print(f"  {trade_date}")
    print(f"  {'─' * 75}")

    for t in d["trades"]:
        pnl_str = f"₹{t['pnl']:+,}" if t['pnl'] else "OPEN"
        print(f"    #{t['id']:<4} {t['symbol']:<25} score={t['score']:>3}  "
              f"{t['filt']:<4}  {pnl_str:>10}  [{t['won']}]{t['skip_saved']}")

    if d["all_count"] > 0:
        print(f"    All trades:      {d['all_count']} trades, {d['all_wins']}W, "
              f"P&L = ₹{d['all_pnl']:+,}")
        print(f"    Filtered (≥50):  {d['filt_count']} trades, {d['filt_wins']}W, "
              f"P&L = ₹{d['filtered_pnl']:+,}")
        diff = d["filtered_pnl"] - d["all_pnl"]
        if diff != 0:
            print(f"    Filter edge:     ₹{diff:+,}")
    print()

print("=" * 80)
print("SUMMARY — 8 DAY COMPARISON")
print("=" * 80)
print()
all_wr = total_all_wins / total_trades * 100 if total_trades > 0 else 0
filt_wr = total_filt_wins / total_filt_trades * 100 if total_filt_trades > 0 else 0

print(f"  {'Metric':<25} {'All CH1':>15} {'Filtered (≥50)':>15}")
print(f"  {'─' * 25} {'─' * 15} {'─' * 15}")
print(f"  {'Trades':<25} {total_trades:>15} {total_filt_trades:>15}")
print(f"  {'Wins':<25} {total_all_wins:>15} {total_filt_wins:>15}")
print(f"  {'Win Rate':<25} {all_wr:>14.0f}% {filt_wr:>14.0f}%")
print(f"  {'Total P&L':<25} {'₹{:+,}'.format(total_all):>15} {'₹{:+,}'.format(total_filtered):>15}")
print(f"  {'Avg P&L/trade':<25} {'₹{:+,}'.format(int(total_all/total_trades) if total_trades else 0):>15} {'₹{:+,}'.format(int(total_filtered/total_filt_trades) if total_filt_trades else 0):>15}")
days = len(daily)
print(f"  {'Avg P&L/day':<25} {'₹{:+,}'.format(int(total_all/days)):>15} {'₹{:+,}'.format(int(total_filtered/days)):>15}")
print()

edge = total_filtered - total_all
print(f"  Filter edge (8 days): ₹{edge:+,}")
if edge > 0:
    print(f"  Filter saves ₹{edge:,} by skipping bad trades")
elif edge < 0:
    print(f"  Filter would have cost ₹{abs(edge):,} by skipping some winners")
else:
    print(f"  No difference")
print("=" * 80)

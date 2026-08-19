"""Deep honest analysis — where is the money actually going?

Not another filter test. This looks at the RAW truth:
  - Where are the big losses coming from?
  - What's the avg win vs avg loss?
  - Are index options (NIFTY/SENSEX/BANKNIFTY) different from stock options?
  - What would happen with a hard stoploss cap?

Run on VM: python3 scripts/deep_analysis.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage import db
from collections import defaultdict

db.init_db()

with db.get_conn() as conn:
    rows = conn.execute(
        """SELECT id, ts, symbol, side, qty, price, exit_price, pnl, status,
                  stop_price, target_price, channel
           FROM trades
           WHERE side = 'BUY'
             AND (channel IS NULL OR channel = 'ch1')
             AND status LIKE 'CLOSED%'
             AND pnl IS NOT NULL
           ORDER BY ts ASC""",
    ).fetchall()

if not rows:
    print("No trades found.")
    sys.exit()


def extract_stock(sym):
    parts = sym.strip().split()
    return parts[0].upper() if parts else sym.upper()


def is_index(sym):
    stock = extract_stock(sym)
    return stock in ("NIFTY", "SENSEX", "BANKNIFTY")


# ---------------------------------------------------------------------------
# 1. BASIC TRUTH
# ---------------------------------------------------------------------------
all_pnl = [r["pnl"] for r in rows]
winners = [p for p in all_pnl if p > 0]
losers = [p for p in all_pnl if p <= 0]

print("=" * 70)
print("DEEP ANALYSIS — WHERE IS THE MONEY GOING?")
print("=" * 70)
print()
print(f"  Total trades:    {len(rows)}")
print(f"  Winners:         {len(winners)} ({len(winners)/len(rows)*100:.0f}%)")
print(f"  Losers:          {len(losers)} ({len(losers)/len(rows)*100:.0f}%)")
print(f"  Total P&L:       ₹{sum(all_pnl):+,.0f}")
print()
print(f"  Avg WIN:         ₹{sum(winners)/len(winners):+,.0f}")
print(f"  Avg LOSS:        ₹{sum(losers)/len(losers):+,.0f}")
print(f"  Biggest WIN:     ₹{max(winners):+,.0f}")
print(f"  Biggest LOSS:    ₹{min(losers):+,.0f}")
print()

ratio = abs(sum(losers)/len(losers)) / (sum(winners)/len(winners)) if winners else 0
print(f"  Loss:Win ratio:  {ratio:.1f}x  (your avg loss is {ratio:.1f}x your avg win)")
if ratio > 2:
    print(f"  ⚠ This is the problem. You win more often but losses are too big.")
print()

# ---------------------------------------------------------------------------
# 2. INDEX vs STOCK OPTIONS
# ---------------------------------------------------------------------------
idx_trades = [r for r in rows if is_index(r["symbol"])]
stk_trades = [r for r in rows if not is_index(r["symbol"])]

idx_pnl = sum(r["pnl"] for r in idx_trades)
stk_pnl = sum(r["pnl"] for r in stk_trades)
idx_wins = sum(1 for r in idx_trades if r["pnl"] > 0)
stk_wins = sum(1 for r in stk_trades if r["pnl"] > 0)
idx_losers = [r["pnl"] for r in idx_trades if r["pnl"] <= 0]
stk_losers = [r["pnl"] for r in stk_trades if r["pnl"] <= 0]
idx_winners = [r["pnl"] for r in idx_trades if r["pnl"] > 0]
stk_winners = [r["pnl"] for r in stk_trades if r["pnl"] > 0]

print("─" * 70)
print("INDEX OPTIONS (NIFTY/SENSEX/BANKNIFTY) vs STOCK OPTIONS")
print("─" * 70)
print()
print(f"  {'':30} {'Index':>15} {'Stocks':>15}")
print(f"  {'─'*30} {'─'*15} {'─'*15}")
print(f"  {'Trades':<30} {len(idx_trades):>15} {len(stk_trades):>15}")
print(f"  {'Win Rate':<30} {idx_wins/len(idx_trades)*100 if idx_trades else 0:>14.0f}% {stk_wins/len(stk_trades)*100 if stk_trades else 0:>14.0f}%")
print(f"  {'Total P&L':<30} {'₹{:+,}'.format(int(idx_pnl)):>15} {'₹{:+,}'.format(int(stk_pnl)):>15}")
print(f"  {'Avg Win':<30} {'₹{:+,}'.format(int(sum(idx_winners)/len(idx_winners))) if idx_winners else 'N/A':>15} {'₹{:+,}'.format(int(sum(stk_winners)/len(stk_winners))) if stk_winners else 'N/A':>15}")
print(f"  {'Avg Loss':<30} {'₹{:+,}'.format(int(sum(idx_losers)/len(idx_losers))) if idx_losers else 'N/A':>15} {'₹{:+,}'.format(int(sum(stk_losers)/len(stk_losers))) if stk_losers else 'N/A':>15}")
print(f"  {'Worst Loss':<30} {'₹{:+,}'.format(int(min(idx_losers))) if idx_losers else 'N/A':>15} {'₹{:+,}'.format(int(min(stk_losers))) if stk_losers else 'N/A':>15}")
print()

# ---------------------------------------------------------------------------
# 3. THE BLOWUP LIST — trades that lost more than ₹10K
# ---------------------------------------------------------------------------
big_losers = sorted([r for r in rows if r["pnl"] < -10000], key=lambda r: r["pnl"])

print("─" * 70)
print(f"BLOWUP TRADES (loss > ₹10K) — {len(big_losers)} trades")
print("─" * 70)
print()
blowup_total = sum(r["pnl"] for r in big_losers)
rest_total = sum(r["pnl"] for r in rows if r["pnl"] >= -10000)
print(f"  These {len(big_losers)} trades lost: ₹{abs(blowup_total):,.0f}")
print(f"  Everything else ({len(rows)-len(big_losers)} trades): ₹{rest_total:+,.0f}")
print()
for r in big_losers:
    entry = r["price"] or 0
    exit_p = r["exit_price"] or 0
    drop_pct = ((exit_p - entry) / entry * 100) if entry > 0 else 0
    print(f"  #{r['id']:<4} {r['symbol']:<28} ₹{r['pnl']:+,.0f}  "
          f"(entry ₹{entry:.0f} → exit ₹{exit_p:.0f}, {drop_pct:+.0f}%)")
print()

# ---------------------------------------------------------------------------
# 4. WHAT IF — hard stoploss cap at different levels
# ---------------------------------------------------------------------------
print("─" * 70)
print("WHAT IF — HARD STOPLOSS CAP")
print("─" * 70)
print()
print("  If every trade had a hard cap on max loss, what would total P&L be?")
print()

for cap in [5000, 8000, 10000, 15000]:
    capped_pnl = 0
    capped_count = 0
    for r in rows:
        pnl = r["pnl"]
        if pnl < -cap:
            capped_pnl += -cap
            capped_count += 1
        else:
            capped_pnl += pnl

    saved = capped_pnl - sum(all_pnl)
    print(f"  Max loss ₹{cap:,}:   Total P&L = ₹{capped_pnl:+,.0f}  "
          f"(would save ₹{saved:+,.0f}, capped {capped_count} trades)")

print()

# ---------------------------------------------------------------------------
# 5. WEEKLY P&L TREND — are things getting better or worse?
# ---------------------------------------------------------------------------
print("─" * 70)
print("WEEKLY TREND — Is it getting better or worse?")
print("─" * 70)
print()

from datetime import datetime
weekly = defaultdict(lambda: {"pnl": 0, "count": 0, "wins": 0})
for r in rows:
    dt = datetime.fromisoformat(r["ts"][:19])
    week = dt.strftime("%Y-W%U")
    weekly[week]["pnl"] += r["pnl"]
    weekly[week]["count"] += 1
    if r["pnl"] > 0:
        weekly[week]["wins"] += 1

for week in sorted(weekly.keys()):
    w = weekly[week]
    wr = w["wins"] / w["count"] * 100 if w["count"] else 0
    bar_len = min(30, abs(int(w["pnl"] / 2000)))
    bar = "█" * bar_len
    if w["pnl"] >= 0:
        bar_display = f"  +{'█' * bar_len}"
    else:
        bar_display = f"  -{'█' * bar_len}"
    print(f"  {week}  {w['count']:>3} trades  {wr:>3.0f}% WR  {'₹{:+,}'.format(int(w['pnl'])):>12} {bar_display}")

print()
print("=" * 70)
print("THE HONEST TRUTH")
print("=" * 70)
print()
print("  The filter can't fix this. The problem is STOPLOSS MANAGEMENT.")
print(f"  Your avg win is ₹{sum(winners)/len(winners):,.0f} but avg loss is ₹{abs(sum(losers)/len(losers)):,.0f}.")
print(f"  You need your avg loss ≤ 1.5x avg win to be profitable at 62% WR.")
print(f"  Right now it's {ratio:.1f}x.")
print()
target_max = sum(winners)/len(winners) * 1.5
print(f"  Target max avg loss: ₹{target_max:,.0f}")
print(f"  That means a hard SL cap around ₹{target_max:,.0f} per trade.")
print()

# What would P&L be with that cap?
ideal_pnl = 0
for r in rows:
    pnl = r["pnl"]
    if pnl < -target_max:
        ideal_pnl += -target_max
    else:
        ideal_pnl += pnl

print(f"  With ₹{target_max:,.0f} max loss cap:")
print(f"    Current P&L:  ₹{sum(all_pnl):+,.0f}")
print(f"    Would-be P&L: ₹{ideal_pnl:+,.0f}")
print(f"    Difference:   ₹{ideal_pnl - sum(all_pnl):+,.0f}")
print()
print("=" * 70)

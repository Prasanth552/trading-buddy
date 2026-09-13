"""kitchen_sink short-straddle backtest — last N trading days.

Runs the kitchen_sink strategy (short ATM straddle at 9:30, combined 35% SL,
trailing stop, vol filter) on NIFTY / BANKNIFTY / SENSEX with 1 lot each.

Uses real index 5-min candles from Upstox, Black-Scholes premium estimates.
Results are saved to strategy_results DB for caching (use --force to re-run).

Usage:
  PYTHONPATH=. .venv/bin/python3 scripts/kitchen_sink_backtest.py [days] [--force]
"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import date, timedelta
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()

from src.strategy.live_runner import (
    run_day, INDEXES, STRATEGIES, init_strategy_db,
)

STRATEGY = "kitchen_sink"
LOTS = 1

days_back = 22
force = False
from_date = None
to_date = None
for arg in sys.argv[1:]:
    if arg == "--force":
        force = True
    elif arg.startswith("--lots="):
        LOTS = int(arg.split("=")[1])
    elif arg.startswith("--from="):
        from_date = date.fromisoformat(arg.split("=")[1])
    elif arg.startswith("--to="):
        to_date = date.fromisoformat(arg.split("=")[1])
    else:
        try:
            days_back = int(arg)
        except ValueError:
            pass

# Generate trading days
end_date = to_date or date.today()
start_date = from_date or (end_date - timedelta(days=days_back + 10))
trading_days = []
d = start_date
while d <= end_date:
    if d.weekday() < 5:
        trading_days.append(d)
    d += timedelta(days=1)
if not from_date:
    trading_days = trading_days[-days_back:] if len(trading_days) >= days_back else trading_days

init_strategy_db()

print(f"\n{'='*130}")
print(f"  KITCHEN SINK BACKTEST — short straddle, {LOTS} lot(s), last {len(trading_days)} trading days")
print(f"  Dates: {trading_days[0]} to {trading_days[-1]}")
print(f"  Config: entry=9:30, SL=35% combined, trailing=yes, vol_filter=yes")
print(f"  Indexes: {', '.join(INDEXES.keys())} (lot sizes: {', '.join(str(v['lot_size']) for v in INDEXES.values())})")
print(f"{'='*130}\n")

all_results = []  # list of (date, idx, result_dict)
day_summaries = {}

for day in trading_days:
    print(f"  {day}:", end=" ", flush=True)
    try:
        res = run_day(day, lots=LOTS, force=force)
    except Exception as e:
        print(f"ERROR: {e}")
        day_summaries[day.isoformat()] = {"net": 0, "trades": 0, "skipped": 3}
        continue

    ks = res.get(STRATEGY, {})
    idx_results = ks.get("indexes", {})
    day_net = 0
    day_trades = 0
    day_skipped = 0
    parts = []

    for idx_name in INDEXES:
        r = idx_results.get(idx_name, {})
        if isinstance(r, dict) and r.get("skipped"):
            parts.append(f"{idx_name}=SKIP({r.get('skip_reason','')})")
            day_skipped += 1
        else:
            if isinstance(r, dict):
                pnl = r.get("net_pnl", 0) or 0
            else:
                pnl = 0
            day_net += pnl
            day_trades += 1
            all_results.append((day.isoformat(), idx_name, r if isinstance(r, dict) else {}))
            icon = "+" if pnl > 0 else ""
            parts.append(f"{idx_name}={icon}₹{pnl:,.0f}")

    print(f"  {'  |  '.join(parts)}  →  Net: ₹{day_net:+,.0f}")
    day_summaries[day.isoformat()] = {"net": day_net, "trades": day_trades, "skipped": day_skipped}

# ── Detailed Trade Table ──
print(f"\n{'='*130}")
print(f"  {'Date':>12} {'Index':>10} {'Spot':>8} {'ATM':>8} {'CE Ent':>8} {'PE Ent':>8} "
      f"{'CE Exit':>8} {'PE Exit':>8} {'CE P&L':>10} {'PE P&L':>10} "
      f"{'Charges':>8} {'Net P&L':>10} {'DTE':>4} {'Exit':>12}")
print(f"  {'─'*125}")

total_net = 0
total_charges = 0
total_ce_pnl = 0
total_pe_pnl = 0
wins = losses = skipped_count = 0

# Per-index stats
idx_stats = defaultdict(lambda: {"net": 0, "wins": 0, "losses": 0, "count": 0, "charges": 0})

for dt, idx_name, r in all_results:
    ce_pnl = r.get("ce_pnl", 0) or 0
    pe_pnl = r.get("pe_pnl", 0) or 0
    charges = r.get("charges", 0) or 0
    net = r.get("net_pnl", 0) or 0
    spot = r.get("spot_entry", 0) or 0
    atm = r.get("atm_strike", 0) or 0
    ce_entry = r.get("ce_entry", 0) or 0
    pe_entry = r.get("pe_entry", 0) or 0
    ce_exit = r.get("ce_exit", 0) or 0
    pe_exit = r.get("pe_exit", 0) or 0
    dte = r.get("dte", "")
    exit_r = r.get("exit_reason", "")

    icon = "✅" if net > 0 else "❌" if net < 0 else "⏸️"
    print(f"  {dt:>12} {idx_name:>10} {spot:>8.0f} {atm:>8.0f} {ce_entry:>8.1f} {pe_entry:>8.1f} "
          f"{ce_exit:>8.1f} {pe_exit:>8.1f} {ce_pnl:>+10,.0f} {pe_pnl:>+10,.0f} "
          f"{charges:>8,.0f} {net:>+10,.0f} {dte:>4} {exit_r:>12} {icon}")

    total_net += net
    total_charges += charges
    total_ce_pnl += ce_pnl
    total_pe_pnl += pe_pnl
    if net > 0:
        wins += 1
    elif net < 0:
        losses += 1

    idx_stats[idx_name]["net"] += net
    idx_stats[idx_name]["charges"] += charges
    idx_stats[idx_name]["count"] += 1
    if net > 0:
        idx_stats[idx_name]["wins"] += 1
    elif net < 0:
        idx_stats[idx_name]["losses"] += 1

# ── Summary ──
trade_count = wins + losses
print(f"\n{'='*130}")
print(f"  SUMMARY — kitchen_sink, {LOTS} lot(s), {len(trading_days)} trading days")
print(f"  {'─'*60}")
print(f"  {'Trades executed:':30} {len(all_results)}")
if trade_count > 0:
    print(f"  {'Win / Loss:':30} {wins}W / {losses}L ({wins/trade_count*100:.0f}% win)")
print(f"  {'':30}")
print(f"  {'Total CE P&L:':30} ₹{total_ce_pnl:>+12,.0f}")
print(f"  {'Total PE P&L:':30} ₹{total_pe_pnl:>+12,.0f}")
print(f"  {'Total Charges:':30} ₹{total_charges:>12,.0f}")
print(f"  {'Total Net P&L:':30} ₹{total_net:>+12,.0f}")
print(f"  {'':30}")
if len(all_results):
    print(f"  {'Avg net P&L per trade:':30} ₹{total_net/len(all_results):>12,.0f}")
    print(f"  {'Avg net P&L per day:':30} ₹{total_net/len(trading_days):>12,.0f}")
    print(f"  {'Avg charges per trade:':30} ₹{total_charges/len(all_results):>12,.0f}")

# ── Per-Index Breakdown ──
print(f"\n  PER-INDEX:")
print(f"  {'Index':>10} {'Trades':>8} {'W/L':>8} {'Net P&L':>12} {'Charges':>10} {'Avg/Trade':>10}")
print(f"  {'─'*62}")
for idx_name in INDEXES:
    s = idx_stats[idx_name]
    if s["count"] == 0:
        continue
    print(f"  {idx_name:>10} {s['count']:>8} {s['wins']}W/{s['losses']}L "
          f"{s['net']:>+12,.0f} {s['charges']:>10,.0f} {s['net']/s['count']:>+10,.0f}")

# ── Per-Day Breakdown ──
print(f"\n  PER-DAY:")
print(f"  {'Date':>12} {'Trades':>8} {'Skip':>6} {'Net P&L':>12}")
print(f"  {'─'*42}")
for d in sorted(day_summaries):
    ds = day_summaries[d]
    icon = "🟢" if ds["net"] > 0 else "🔴" if ds["net"] < 0 else "⚪"
    skip_str = f"({ds['skipped']})" if ds["skipped"] else ""
    print(f"  {d:>12} {ds['trades']:>8} {skip_str:>6} {ds['net']:>+12,.0f} {icon}")

# ── Weekly Breakdown ──
weeks = defaultdict(lambda: {"net": 0, "count": 0, "wins": 0, "losses": 0})
for dt, idx_name, r in all_results:
    d = date.fromisoformat(dt)
    iso = d.isocalendar()
    wk = f"{iso.year}-W{iso.week:02d}"
    net = r.get("net_pnl", 0) or 0
    weeks[wk]["net"] += net
    weeks[wk]["count"] += 1
    if net > 0:
        weeks[wk]["wins"] += 1
    elif net < 0:
        weeks[wk]["losses"] += 1

print(f"\n  WEEKLY:")
print(f"  {'Week':>10} {'Trades':>8} {'W/L':>8} {'Net P&L':>12}")
print(f"  {'─'*42}")
for wk in sorted(weeks):
    w = weeks[wk]
    icon = "🟢" if w["net"] > 0 else "🔴"
    print(f"  {wk:>10} {w['count']:>8} {w['wins']}W/{w['losses']}L {w['net']:>+12,.0f} {icon}")

# ── Cumulative P&L ──
print(f"\n  CUMULATIVE P&L:")
cum = 0
max_dd = 0
peak = 0
for d in sorted(day_summaries):
    ds = day_summaries[d]
    cum += ds["net"]
    peak = max(peak, cum)
    dd = peak - cum
    max_dd = max(max_dd, dd)
    bar = "█" * max(1, int(abs(cum) / 500)) if cum != 0 else ""
    icon = "🟢" if cum > 0 else "🔴"
    print(f"  {d:>12}  ₹{cum:>+10,.0f}  {bar} {icon}")

print(f"\n  {'Peak P&L:':30} ₹{peak:>+12,.0f}")
print(f"  {'Max drawdown:':30} ₹{max_dd:>12,.0f}")
print(f"  {'Final P&L:':30} ₹{cum:>+12,.0f}")

print(f"\n{'='*130}")
print(f"  BACKTEST COMPLETE")
print(f"{'='*130}")

"""Daily trade analysis — OEH, Index Straddles, Stock Spreads."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()

import config
from src.storage.db import get_conn

IST = ZoneInfo("Asia/Kolkata")
today = sys.argv[1] if len(sys.argv) > 1 else datetime.now(IST).strftime("%Y-%m-%d")

print(f"{'#'*80}")
print(f"  DAILY TRADE ANALYSIS — {today}")
print(f"{'#'*80}")

# ── OEH Trades ──
print(f"\n{'='*80}")
print(f"  OEH SCANNER")
print(f"{'='*80}")
with get_conn() as conn:
    rows = conn.execute(
        "SELECT id, ts, symbol, qty, price, exit_price, pnl, stop_price, "
        "target_price, status, peak_price, charges "
        "FROM trades WHERE channel='oeh' AND date(ts)=? ORDER BY ts",
        (today,)).fetchall()

if not rows:
    print("  No OEH trades today.")
else:
    oeh_pnl = 0
    oeh_wins = 0
    oeh_losses = 0
    print(f"\n  {'ID':>5} {'Symbol':24} {'Qty':>6} {'Entry':>8} {'Exit':>8} {'SL':>8} {'TGT':>8} {'P&L':>10} {'Status'}")
    print(f"  {'─'*95}")
    for r in rows:
        pnl = r['pnl'] or 0
        status = r['status'] or 'OPEN'
        exit_p = f"{r['exit_price']:.2f}" if r['exit_price'] else "—"
        icon = "✅" if pnl > 0 else "❌" if pnl < 0 else "⏳"
        lots = "2L" if r['qty'] and r['qty'] > 1000 else "1L"
        print(f"  {r['id']:>5} {r['symbol']:24} {r['qty']:>5}({lots}) {r['price']:>8.2f} {exit_p:>8} "
              f"{r['stop_price'] or 0:>8.2f} {r['target_price'] or 0:>8.2f} "
              f"{pnl:>+10,.0f} {icon} {status}")
        if status != 'OPEN':
            oeh_pnl += pnl
            if pnl > 0: oeh_wins += 1
            elif pnl <= 0: oeh_losses += 1

    closed = [r for r in rows if (r['status'] or '') != 'OPEN']
    open_t = [r for r in rows if (r['status'] or '') == 'OPEN']
    print(f"\n  Summary: {len(closed)} closed ({oeh_wins}W/{oeh_losses}L), {len(open_t)} open")
    print(f"  OEH P&L: ₹{oeh_pnl:+,.0f}")

# ── Channel Trades (CH1, CH2, CH3) ──
print(f"\n{'='*80}")
print(f"  CHANNEL TRADES")
print(f"{'='*80}")
with get_conn() as conn:
    ch_rows = conn.execute(
        "SELECT id, ts, symbol, qty, price, exit_price, pnl, stop_price, "
        "target_price, status, channel, charges "
        "FROM trades WHERE channel NOT IN ('oeh','oel') AND date(ts)=? ORDER BY channel, ts",
        (today,)).fetchall()

if not ch_rows:
    print("  No channel trades today.")
else:
    ch_pnl = 0
    print(f"\n  {'ID':>5} {'CH':>4} {'Symbol':24} {'Qty':>6} {'Entry':>8} {'Exit':>8} {'P&L':>10} {'Status'}")
    print(f"  {'─'*80}")
    for r in ch_rows:
        pnl = r['pnl'] or 0
        status = r['status'] or 'OPEN'
        exit_p = f"{r['exit_price']:.2f}" if r['exit_price'] else "—"
        icon = "✅" if pnl > 0 else "❌" if pnl < 0 else "⏳"
        print(f"  {r['id']:>5} {r['channel']:>4} {r['symbol']:24} {r['qty']:>6} {r['price']:>8.2f} "
              f"{exit_p:>8} {pnl:>+10,.0f} {icon} {status}")
        if status != 'OPEN':
            ch_pnl += pnl
    print(f"\n  Channel P&L: ₹{ch_pnl:+,.0f}")

# ── Index Straddle Strategies ──
print(f"\n{'='*80}")
print(f"  INDEX STRADDLE STRATEGIES")
print(f"{'='*80}")
try:
    with get_conn() as conn:
        strat_rows = conn.execute(
            "SELECT * FROM strategy_live WHERE date=? ORDER BY strategy, idx",
            (today,)).fetchall()
    if not strat_rows:
        print("  No strategy trades today.")
    else:
        by_strat = {}
        for r in strat_rows:
            r = dict(r)
            s = r['strategy']
            if s not in by_strat:
                by_strat[s] = []
            by_strat[s].append(r)

        strat_total = 0
        for sname, trades in by_strat.items():
            s_pnl = sum(t.get('net_pnl', 0) or 0 for t in trades)
            closed = [t for t in trades if t.get('status') == 'CLOSED']
            open_t = [t for t in trades if t.get('status') == 'OPEN']
            strat_total += s_pnl
            print(f"\n  {sname} — ₹{s_pnl:+,.0f} ({len(closed)} closed, {len(open_t)} open)")
            print(f"  {'Idx':12} {'CE P&L':>10} {'PE P&L':>10} {'Net':>10} {'Status':>8}")
            print(f"  {'─'*55}")
            for t in trades:
                net = (t.get('net_pnl') or 0)
                ce = (t.get('ce_pnl') or 0)
                pe = (t.get('pe_pnl') or 0)
                icon = "✅" if net > 0 else "❌" if net < 0 else "⏳"
                print(f"  {t['idx']:12} {ce:>+10,.0f} {pe:>+10,.0f} {net:>+10,.0f} {icon} {t.get('status','—')}")

        print(f"\n  Strategy Total: ₹{strat_total:+,.0f}")
except Exception as e:
    print(f"  Error loading strategies: {e}")

# ── Stock Credit Spreads ──
print(f"\n{'='*80}")
print(f"  STOCK CREDIT SPREADS")
print(f"{'='*80}")
try:
    from src.strategy.stock_runner import init_stock_strategy_db
    init_stock_strategy_db()
    with get_conn() as conn:
        stock_rows = conn.execute(
            "SELECT * FROM stock_strategy_results WHERE date=? AND skipped=0 "
            "ORDER BY strategy, stock", (today,)).fetchall()
    if not stock_rows:
        # Check for active positions (entered earlier, exit today or later)
        with get_conn() as conn:
            active_rows = conn.execute(
                "SELECT * FROM stock_strategy_results WHERE skipped=0 "
                "AND entry_date <= ? AND exit_date >= ? ORDER BY strategy, stock",
                (today, today)).fetchall()
        if active_rows:
            print(f"\n  No new entries today, but {len(active_rows)} ACTIVE positions:")
            print(f"\n  {'Stock':12} {'Direction':12} {'Sell':>8} {'Buy':>8} {'Credit':>8} "
                  f"{'Exit Reason':>14} {'Exit Date':>12} {'P&L':>10}")
            print(f"  {'─'*95}")
            for r in active_rows:
                r = dict(r)
                tag = "BULL PUT" if 'bull' in (r.get('direction') or '') else "BEAR CALL"
                pnl = r.get('net_pnl') or 0
                icon = "✅" if pnl > 0 else "❌" if pnl < 0 else "⏳"
                print(f"  {r['stock']:12} {tag:12} {r.get('sell_strike',0):>8.0f} "
                      f"{r.get('buy_strike',0):>8.0f} {r.get('net_credit',0):>8.2f} "
                      f"{r.get('exit_reason','—'):>14} {r.get('exit_date','—'):>12} "
                      f"{pnl:>+10,.0f} {icon}")
            active_pnl = sum((dict(r).get('net_pnl') or 0) for r in active_rows)
            print(f"\n  Active Positions P&L: ₹{active_pnl:+,.0f}")
        else:
            print("  No stock trades today and no active positions.")
    else:
        by_strat = {}
        for r in stock_rows:
            r = dict(r)
            s = r['strategy']
            if s not in by_strat:
                by_strat[s] = []
            by_strat[s].append(r)

        stock_total = 0
        for sname, trades in by_strat.items():
            s_pnl = sum(t.get('net_pnl', 0) or 0 for t in trades)
            wins = sum(1 for t in trades if (t.get('net_pnl') or 0) > 0)
            stock_total += s_pnl
            print(f"\n  {sname.replace('_',' ')} — ₹{s_pnl:+,.0f} ({len(trades)} trades, {wins}W)")
            print(f"  {'Stock':12} {'Direction':12} {'Sell':>8} {'Buy':>8} {'Credit':>8} "
                  f"{'Exit':>14} {'P&L':>10}")
            print(f"  {'─'*80}")
            for t in trades:
                tag = "BULL PUT" if 'bull' in (t.get('direction') or '') else "BEAR CALL"
                pnl = t.get('net_pnl') or 0
                icon = "✅" if pnl > 0 else "❌"
                print(f"  {t['stock']:12} {tag:12} {t.get('sell_strike',0):>8.0f} "
                      f"{t.get('buy_strike',0):>8.0f} {t.get('net_credit',0):>8.2f} "
                      f"{t.get('exit_reason','—'):>14} {pnl:>+10,.0f} {icon}")

        print(f"\n  Stock Spreads Total: ₹{stock_total:+,.0f}")
except Exception as e:
    print(f"  Error loading stock strategies: {e}")

# ── Grand Total ──
print(f"\n{'#'*80}")
print(f"  GRAND TOTAL")
print(f"{'#'*80}")
totals = []
if rows:
    totals.append(("OEH", oeh_pnl))
if ch_rows:
    totals.append(("Channels", ch_pnl))
try:
    if strat_rows:
        totals.append(("Straddles", strat_total))
except:
    pass
try:
    if stock_rows:
        totals.append(("Stocks", stock_total))
    elif active_rows:
        totals.append(("Stocks (active)", active_pnl))
except:
    pass

grand = sum(v for _, v in totals)
for label, val in totals:
    icon = "🟢" if val >= 0 else "🔴"
    print(f"  {icon} {label:20} ₹{val:>+10,.0f}")
print(f"  {'─'*35}")
grand_icon = "🟢" if grand >= 0 else "🔴"
print(f"  {grand_icon} {'TOTAL':20} ₹{grand:>+10,.0f}")
print(f"{'#'*80}")

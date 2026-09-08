"""Analyze OEH SL-hit trades: did the premium spike enough that 2 lots
would have reached the target floor before SL was triggered?"""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()

import config
from src.storage.db import get_conn
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo("Asia/Kolkata")

token = load_cached_token()
ud = UpstoxData(access_token=token)
master = ud._load_master()

def find_option_key(symbol_str):
    """Resolve 'INFY 1120 PE' to instrument_key from master."""
    parts = symbol_str.strip().split()
    if len(parts) < 3:
        return None
    name = parts[0]
    strike = int(parts[1])
    opt_type = parts[2]  # CE or PE

    for inst in master:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO"):
            continue
        tsym = (inst.get("trading_symbol") or "").upper()
        iname = (inst.get("name") or "").upper()
        if iname != name:
            continue
        ist_strike = inst.get("strike_price") or inst.get("strike")
        if ist_strike is None:
            continue
        if abs(float(ist_strike) - strike) > 0.5:
            continue
        itype = (inst.get("option_type") or inst.get("instrument_type") or "").upper()
        if itype not in (opt_type, opt_type[0]):
            continue
        return inst.get("instrument_key")
    return None


with get_conn() as conn:
    rows = conn.execute(
        "SELECT id, ts, symbol, qty, price, exit_price, pnl, stop_price, target_price "
        "FROM trades WHERE channel='oeh' AND status='CLOSED_SL' "
        "AND date(ts) >= '2026-09-01' ORDER BY ts"
    ).fetchall()

print(f"{'='*100}")
print(f"OEH SL-Hit Trade Spike Analysis — Did 2 lots reach the floor?")
print(f"{'='*100}")
print(f"Found {len(rows)} SL-hit trades to analyze\n")

for r in rows:
    tid, ts, sym, qty, entry, sl_exit, pnl, sl_price, target = r
    lot_size = qty  # 1 lot = qty

    print(f"\n{'─'*80}")
    print(f"Trade #{tid}: {sym}")
    print(f"  Entry: {entry:.1f} | SL: {sl_price:.1f} | Target: {target:.1f} | Qty: {qty}")
    print(f"  SL Loss (1 lot): {pnl:+,.0f}")

    # Target P&L with 1 lot and 2 lots
    target_pnl_1 = (target - entry) * qty
    target_pnl_2 = (target - entry) * qty * 2
    print(f"  Target P&L: 1 lot = +{target_pnl_1:,.0f} | 2 lots = +{target_pnl_2:,.0f}")

    # Parse entry time
    entry_dt = datetime.fromisoformat(ts)
    trade_date = entry_dt.date()
    from_dt = datetime.combine(trade_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(trade_date, datetime.min.time()).replace(hour=15, minute=30)

    # Resolve option instrument
    opt_key = find_option_key(sym)
    if not opt_key:
        print(f"  ⚠ Could not resolve option instrument — skipping candle analysis")
        continue

    try:
        candles = ud.historical_data(opt_key, from_dt, to_dt, "1minute")
        time.sleep(0.4)
    except Exception as e:
        print(f"  ⚠ Candle fetch failed: {e}")
        continue

    if not candles:
        print(f"  ⚠ No candles returned")
        continue

    # Filter candles from entry onwards
    entry_mins = entry_dt.hour * 60 + entry_dt.minute
    post_entry = []
    for c in candles:
        ct = c.get("date") or c.get("timestamp") or c.get("ts") or ""
        if isinstance(ct, str):
            try:
                cdt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            except:
                continue
        else:
            cdt = ct
        try:
            cdt_ist = cdt.astimezone(IST)
        except:
            cdt_ist = cdt
        cmins = cdt_ist.hour * 60 + cdt_ist.minute
        if cmins >= entry_mins:
            post_entry.append({
                "time": f"{cdt_ist.hour}:{cdt_ist.minute:02d}",
                "open": c["open"], "high": c["high"],
                "low": c["low"], "close": c["close"],
            })

    if not post_entry:
        print(f"  ⚠ No candles after entry time")
        continue

    # Find peak premium (highest high after entry)
    peak = 0
    peak_time = ""
    peak_close = 0

    # Track every 5-min interval for display
    print(f"\n  {'Time':>7} {'High':>8} {'Close':>8} {'P&L 1L':>10} {'P&L 2L':>10} {'vs Target':>12}")
    print(f"  {'─'*62}")

    interval_count = 0
    for c in post_entry:
        if c["high"] > peak:
            peak = c["high"]
            peak_time = c["time"]
            peak_close = c["close"]

        # Show every 5 mins
        interval_count += 1
        if interval_count % 5 == 0 or c["high"] == peak:
            pnl_1 = (c["close"] - entry) * qty
            pnl_2 = (c["close"] - entry) * qty * 2
            pnl_peak_1 = (c["high"] - entry) * qty
            pnl_peak_2 = (c["high"] - entry) * qty * 2
            pct_of_target = (c["high"] - entry) / (target - entry) * 100 if target != entry else 0
            marker = " ◀ PEAK" if c["high"] == peak and interval_count > 1 else ""
            print(f"  {c['time']:>7} {c['high']:>8.1f} {c['close']:>8.1f} {pnl_peak_1:>+10,.0f} {pnl_peak_2:>+10,.0f} {pct_of_target:>10.0f}%{marker}")

    # Summary
    peak_pnl_1 = (peak - entry) * qty
    peak_pnl_2 = (peak - entry) * qty * 2
    pct_target = (peak - entry) / (target - entry) * 100 if target != entry else 0

    # "Floor" = would 2-lot peak P&L have matched or exceeded 1-lot target P&L?
    floor_hit = peak_pnl_2 >= target_pnl_1

    print(f"\n  PEAK: {peak:.1f} at {peak_time}")
    print(f"  Peak P&L — 1 lot: {peak_pnl_1:+,.0f} | 2 lots: {peak_pnl_2:+,.0f}")
    print(f"  Peak reached {pct_target:.0f}% of target")
    print(f"  2-lot peak vs 1-lot target: {'✅ YES — 2 lots would have hit the floor!' if floor_hit else '❌ NO — spike not enough even with 2 lots'}")
    print(f"  Actual SL loss (1 lot): {pnl:+,.0f} | Would be (2 lots): {pnl*2:+,.0f}")

print(f"\n{'='*100}")
print("SUMMARY")
print(f"{'='*100}")

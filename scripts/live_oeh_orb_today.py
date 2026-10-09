#!/usr/bin/env python3
"""Live OEH + ORB dashboard for today — refreshes every 2s."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

import config
from src.storage import db
from src.broker.upstox_data import UpstoxData, load_cached_token


def calc_charges(entry_price: float, exit_price: float, qty: int) -> dict[str, float]:
    turnover = (entry_price + exit_price) * qty
    brokerage = min(40, turnover * 0.0003)
    stt = exit_price * qty * 0.000625
    exchange = turnover * 0.0005
    sebi = turnover * 0.000001
    gst = (brokerage + exchange + sebi) * 0.18
    stamp = entry_price * qty * 0.00003
    total = brokerage + stt + exchange + sebi + gst + stamp
    return {"total": total}

IST = ZoneInfo(config.TIMEZONE)


def run():
    db.init_db()
    token = load_cached_token()
    if not token:
        print("No Upstox token for today!")
        return

    ud = UpstoxData(access_token=token)

    while True:
        now = datetime.now(IST)
        os.system("clear")
        print(f"{'=' * 72}")
        print(f"  LIVE OEH + ORB Dashboard — {now.strftime('%Y-%m-%d %H:%M:%S')} IST")
        print(f"{'=' * 72}")

        with db.get_conn() as c:
            all_trades = c.execute(
                "SELECT id, symbol, price, qty, stop_price, exit_price, pnl, "
                "peak_price, channel, status, broker_key, ts "
                "FROM trades WHERE date(ts) = date('now') AND channel IN ('oeh','orb') "
                "ORDER BY channel, id"
            ).fetchall()

        open_trades = [t for t in all_trades if t["status"] == "OPEN"]
        closed_trades = [t for t in all_trades if t["status"] == "CLOSED"]

        # Fetch live LTP for open trades
        live_pnl = {}
        if open_trades:
            keys = {t["broker_key"]: t for t in open_trades if t["broker_key"]}
            if keys:
                try:
                    ltp_data = ud._get("/v2/market-quote/ltp",
                                       params={"instrument_key": ",".join(keys)}).get("data", {})
                    for item in ltp_data.values():
                        ikey = item.get("instrument_token", "")
                        ltp = item.get("last_price")
                        if ltp and ikey in keys:
                            trade = keys[ikey]
                            tid = trade["id"]
                            gross = (ltp - trade["price"]) * trade["qty"]
                            charges = calc_charges(trade["price"], ltp, trade["qty"])["total"]
                            live_pnl[tid] = {
                                "ltp": ltp,
                                "gross": gross,
                                "charges": charges,
                                "net": gross - charges,
                                "peak": trade["peak_price"] or 0,
                            }
                except Exception as e:
                    print(f"  LTP fetch error: {e}")

        # --- OPEN TRADES ---
        for ch_name in ["oeh", "orb"]:
            ch_open = [t for t in open_trades if t["channel"] == ch_name]
            ch_closed = [t for t in closed_trades if t["channel"] == ch_name]

            print(f"\n  {ch_name.upper()} — {len(ch_open)} open, {len(ch_closed)} closed")
            print(f"  {'-' * 68}")

            if ch_open:
                print(f"  {'Symbol':<22} {'Entry':>8} {'LTP':>8} {'Qty':>6} {'Gross':>9} {'Net':>9} {'Peak':>9}")
                print(f"  {'─' * 68}")
                ch_open_total = 0
                for t in ch_open:
                    tid = t["id"]
                    if tid in live_pnl:
                        p = live_pnl[tid]
                        marker = ""
                        if p["net"] > 0:
                            marker = " ▲"
                        elif p["net"] < 0:
                            marker = " ▼"
                        print(f"  {t['symbol']:<22} {t['price']:>8.2f} {p['ltp']:>8.2f} {t['qty']:>6} "
                              f"{p['gross']:>+9.0f} {p['net']:>+9.0f} {p['peak']:>+9.0f}{marker}")
                        ch_open_total += p["net"]
                    else:
                        print(f"  {t['symbol']:<22} {t['price']:>8.2f} {'?':>8} {t['qty']:>6} "
                              f"{'?':>9} {'?':>9} {(t['peak_price'] or 0):>+9.0f}")
                print(f"  {'─' * 68}")
                print(f"  {'OPEN TOTAL':<22} {'':>8} {'':>8} {'':>6} {'':>9} {ch_open_total:>+9.0f}")

            # --- CLOSED TRADES ---
            if ch_closed:
                closed_total = sum(t["pnl"] or 0 for t in ch_closed)
                print(f"\n  Closed trades:")
                print(f"  {'Symbol':<22} {'Entry':>8} {'Exit':>8} {'Qty':>6} {'PnL':>9} {'Peak':>9}")
                print(f"  {'─' * 68}")
                for t in ch_closed:
                    pnl = t["pnl"] or 0
                    peak = t["peak_price"] or 0
                    leak = peak - pnl if peak > 0 else 0
                    print(f"  {t['symbol']:<22} {t['price']:>8.2f} {(t['exit_price'] or 0):>8.2f} {t['qty']:>6} "
                          f"{pnl:>+9.0f} {peak:>+9.0f}  (leak: ₹{leak:,.0f})")
                print(f"  {'─' * 68}")
                print(f"  {'CLOSED TOTAL':<22} {'':>8} {'':>8} {'':>6} {closed_total:>+9.0f}")

        # --- GRAND TOTAL ---
        closed_pnl = sum(t["pnl"] or 0 for t in closed_trades)
        open_pnl = sum(v["net"] for v in live_pnl.values())
        grand = closed_pnl + open_pnl

        print(f"\n  {'=' * 68}")
        print(f"  GRAND TOTAL:  Closed ₹{closed_pnl:+,.0f}  |  Open ₹{open_pnl:+,.0f}  |  Total ₹{grand:+,.0f}")
        print(f"  Trades: {len(all_trades)} ({len(open_trades)} open, {len(closed_trades)} closed)")
        print(f"  {'=' * 68}")
        print(f"\n  Refreshing every 2s ... (Ctrl+C to stop)")

        time.sleep(2)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nStopped.")

"""Strategy scheduler — runs as a persistent service during market hours.

Triggers index straddle and stock credit spread strategies automatically.
Designed to run as a systemd service alongside the channel listener.

Schedule (IST):
  09:16  — Pre-market: fetch candles, warm caches
  15:35  — Post-close: run index straddles (full day data)
  15:40  — Post-close: run stock credit spreads
  15:45  — Log daily summary

Usage:
    .venv/bin/python3 -m src.strategy.scheduler
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import config
from src.utils import market_calendar as mc

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("strategy.scheduler")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_shutdown = False

def _handle_signal(signum, frame):
    global _shutdown
    log.info("Received signal %d, shutting down...", signum)
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def now_ist() -> datetime:
    return datetime.now(IST)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5


def sleep_until(target: datetime):
    """Sleep until target time, checking for shutdown every 30s."""
    while not _shutdown:
        remaining = (target - now_ist()).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 30))


def run_index_strategies(ref_date: date):
    """Run all 3 index straddle strategies."""
    log.info("Running index straddle strategies for %s...", ref_date)
    try:
        from src.strategy.live_runner import run_day
        res = run_day(ref_date, lots=1, force=True)
        for sname, data in res.items():
            log.info("  %s: %+,.0f", sname, data["day_pnl"])
        total = sum(data["day_pnl"] for data in res.values())
        log.info("  INDEX TOTAL: %+,.0f", total)
        return res
    except Exception:
        log.exception("Index strategy error")
        return {}


def run_stock_strategies(ref_date: date):
    """Run all 4 stock credit spread strategies."""
    log.info("Running stock credit spread strategies for %s...", ref_date)
    try:
        from src.strategy.stock_runner import run_day
        res = run_day(ref_date, lots=1, force=True)
        for sname, data in res.items():
            active = sum(1 for v in data.get("stocks", {}).values() if not v.get("skipped"))
            if active:
                log.info("  %s: %+,.0f (%d trades)", sname, data["day_pnl"], active)
        total = sum(data["day_pnl"] for data in res.values() if data["day_pnl"])
        if total:
            log.info("  STOCK TOTAL: %+,.0f", total)
        return res
    except Exception:
        log.exception("Stock strategy error")
        return {}


def warm_caches(ref_date: date):
    """Pre-fetch candle data to speed up strategy runs."""
    log.info("Warming candle caches for %s...", ref_date)
    try:
        from src.broker.upstox_data import UpstoxData
        from src.strategy.live_runner import INDEXES, fetch_candles
        uclient = UpstoxData()
        for idx_name in INDEXES:
            fetch_candles(uclient, idx_name, ref_date)
        log.info("  Index candle caches warmed")
    except Exception:
        log.exception("Cache warming error (non-fatal)")


def run_trading_day(ref_date: date):
    """Execute the full schedule for one trading day."""
    today = ref_date

    # 09:16 IST — warm caches (market just opened, first candle available)
    pre_market = datetime(today.year, today.month, today.day, 9, 16, tzinfo=IST)
    if now_ist() < pre_market:
        log.info("Waiting until 09:16 IST for pre-market cache warming...")
        sleep_until(pre_market)
    if _shutdown:
        return

    if now_ist().date() == today and now_ist().hour < 15:
        warm_caches(today)

    # 15:35 IST — run index straddles (market closed, full day data)
    post_close = datetime(today.year, today.month, today.day, 15, 35, tzinfo=IST)
    if now_ist() < post_close:
        log.info("Waiting until 15:35 IST for post-market strategy run...")
        sleep_until(post_close)
    if _shutdown:
        return

    idx_res = run_index_strategies(today)

    # 15:40 IST — run stock credit spreads
    stock_time = datetime(today.year, today.month, today.day, 15, 40, tzinfo=IST)
    if now_ist() < stock_time:
        sleep_until(stock_time)
    if _shutdown:
        return

    stk_res = run_stock_strategies(today)

    # Summary
    idx_total = sum(d["day_pnl"] for d in idx_res.values()) if idx_res else 0
    stk_total = sum(d["day_pnl"] for d in stk_res.values() if d.get("day_pnl")) if stk_res else 0
    log.info("=== DAY COMPLETE %s === Index: %+,.0f | Stocks: %+,.0f | Combined: %+,.0f",
             today, idx_total, stk_total, idx_total + stk_total)


def main():
    log.info("Strategy scheduler started")

    while not _shutdown:
        today = now_ist().date()

        if not is_trading_day(today):
            # Sleep until next weekday
            days_ahead = 1
            while (today + timedelta(days=days_ahead)).weekday() >= 5:
                days_ahead += 1
            next_day = today + timedelta(days=days_ahead)
            wake = datetime(next_day.year, next_day.month, next_day.day, 9, 0, tzinfo=IST)
            log.info("Weekend/holiday. Sleeping until %s...", wake.strftime("%Y-%m-%d %H:%M IST"))
            sleep_until(wake)
            continue

        current = now_ist()
        market_end = datetime(today.year, today.month, today.day, 16, 0, tzinfo=IST)

        if current > market_end:
            # Already past market hours — run if not already done today, then sleep to next day
            from src.storage import db
            from src.strategy.live_runner import init_strategy_db
            init_strategy_db()
            with db.get_conn() as conn:
                done = conn.execute(
                    "SELECT COUNT(*) FROM strategy_results WHERE date=?",
                    (today.isoformat(),)).fetchone()[0]
            if done == 0:
                log.info("Late start — running strategies for today...")
                run_index_strategies(today)
                run_stock_strategies(today)
            else:
                log.info("Strategies already ran for today (%d results)", done)

            # Sleep until next trading day 9:00 AM
            next_day = today + timedelta(days=1)
            while next_day.weekday() >= 5:
                next_day += timedelta(days=1)
            wake = datetime(next_day.year, next_day.month, next_day.day, 9, 0, tzinfo=IST)
            log.info("Sleeping until %s...", wake.strftime("%Y-%m-%d %H:%M IST"))
            sleep_until(wake)
            continue

        # Normal trading day flow
        run_trading_day(today)

        # After today's run, sleep until next trading day
        next_day = today + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        wake = datetime(next_day.year, next_day.month, next_day.day, 9, 0, tzinfo=IST)
        log.info("Sleeping until next trading day %s...", wake.strftime("%Y-%m-%d %H:%M IST"))
        sleep_until(wake)

    log.info("Strategy scheduler stopped")


if __name__ == "__main__":
    main()

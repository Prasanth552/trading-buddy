"""Real-time intraday strategy tracker.

Enters positions at scheduled times, monitors P&L every minute,
exits on SL/trailing/time. Shows live OPEN positions in dashboard.

Schedule (IST):
  09:16  — Pre-market cache warm
  09:20  — Enter vf_920_sl30 positions
  09:30  — Enter kitchen_sink positions
  09:45  — Enter entry_945_sl30 positions
  09:20–15:10 — Monitor every 60s: check SL, trailing, update live P&L
  15:10  — Exit all remaining OPEN positions
  15:35  — Run stock credit spreads (daily check, multi-day positions)
  15:45  — Log daily summary

Replaces scheduler.py for live tracking.
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
from src.storage import db

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("strategy.tracker")
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

# ---------------------------------------------------------------------------
# DB schema — adds status column to track OPEN/CLOSED positions
# ---------------------------------------------------------------------------
LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_live (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    idx         TEXT NOT NULL,
    status      TEXT DEFAULT 'OPEN',
    lots        INTEGER DEFAULT 1,
    entry_time  TEXT,
    exit_time   TEXT,
    exit_reason TEXT,
    spot_entry  REAL,
    spot_current REAL,
    atm_strike  REAL,
    ce_entry    REAL,
    pe_entry    REAL,
    ce_current  REAL,
    pe_current  REAL,
    ce_exit     REAL,
    pe_exit     REAL,
    ce_pnl      REAL,
    pe_pnl      REAL,
    unrealized_pnl REAL DEFAULT 0,
    charges     REAL,
    net_pnl     REAL DEFAULT 0,
    skipped     INTEGER DEFAULT 0,
    skip_reason TEXT,
    dte         INTEGER,
    sl_level    REAL,
    ce_sl       REAL,
    pe_sl       REAL,
    trail_best  REAL DEFAULT 0,
    trail_active INTEGER DEFAULT 0,
    updated_at  TEXT,
    UNIQUE(date, strategy, idx)
);
CREATE INDEX IF NOT EXISTS idx_sl_date ON strategy_live(date);
CREATE INDEX IF NOT EXISTS idx_sl_status ON strategy_live(status);
"""

def init_live_db():
    with db.get_conn() as conn:
        conn.executescript(LIVE_SCHEMA)


# ---------------------------------------------------------------------------
# Imports from live_runner (reuse B-S, candle fetch, charges)
# ---------------------------------------------------------------------------
from src.strategy.live_runner import (
    INDEXES, STRATEGIES, EXPIRY_WEEKDAY,
    est_prem, round_strike, calc_charges, fetch_candles,
    _days_to_expiry, _dte_fraction, _first_candle_range,
    _candle_hm, _candle_time_str,
)
from src.broker.upstox_data import UpstoxData


def now_ist() -> datetime:
    return datetime.now(IST)


def sleep_until(target: datetime):
    while not _shutdown:
        remaining = (target - now_ist()).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 10))


# ---------------------------------------------------------------------------
# Entry: open positions for a strategy at the right time
# ---------------------------------------------------------------------------
def enter_positions(strategy_name: str, ref_date: date, lots: int = 1):
    """Fetch current candles, calculate entry premiums, save OPEN positions."""
    params = STRATEGIES[strategy_name]
    init_live_db()
    uclient = UpstoxData()

    for idx_name, idx in INDEXES.items():
        iv = idx["iv_annual"]
        lot_size = idx["lot_size"] * lots
        step = idx["strike_step"]
        dte = _days_to_expiry(ref_date, idx_name)

        candles = fetch_candles(uclient, idx_name, ref_date, "5minute")
        if not candles or len(candles) < 3:
            _save_skipped(ref_date, strategy_name, idx_name, lots, dte, "no_data")
            continue

        # Vol filter check
        if params.get("vol_filter"):
            fr = _first_candle_range(candles)
            if fr > idx["vol_skip_range"]:
                _save_skipped(ref_date, strategy_name, idx_name, lots, dte,
                              f"vol_filter ({fr:.0f}>{idx['vol_skip_range']})")
                continue

        entry_candle = candles[-1]
        spot = entry_candle["close"]
        atm = round_strike(spot, step)

        mins = (now_ist().hour - 9) * 60 + (now_ist().minute - 15)
        T_entry = _dte_fraction(ref_date, idx_name, mins)
        ce_entry = est_prem(spot, atm, "CE", T_entry, iv)
        pe_entry = est_prem(spot, atm, "PE", T_entry, iv)
        total_prem = ce_entry + pe_entry

        # SL levels
        sl_pct = params["sl_pct"]
        if params.get("combined_sl"):
            sl_level = total_prem * (1 + sl_pct)
            ce_sl = pe_sl = 0
        else:
            sl_level = 0
            ce_sl = ce_entry * (1 + sl_pct)
            pe_sl = pe_entry * (1 + sl_pct)

        entry_time = now_ist().strftime("%H:%M")
        with db.get_conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO strategy_live
                (date, strategy, idx, status, lots, entry_time, spot_entry,
                 spot_current, atm_strike, ce_entry, pe_entry, ce_current, pe_current,
                 unrealized_pnl, dte, sl_level, ce_sl, pe_sl, skipped, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ref_date.isoformat(), strategy_name, idx_name, "OPEN", lots,
                 entry_time, round(spot, 2), round(spot, 2), atm,
                 round(ce_entry, 2), round(pe_entry, 2),
                 round(ce_entry, 2), round(pe_entry, 2),
                 0.0, dte, round(sl_level, 2), round(ce_sl, 2), round(pe_sl, 2),
                 0, now_ist().isoformat()))

        log.info("  OPEN %s/%s: spot=%,.0f atm=%,.0f CE=%.1f PE=%.1f total=%.1f DTE=%d",
                 strategy_name, idx_name, spot, atm, ce_entry, pe_entry, total_prem, dte)


def _save_skipped(ref_date, strategy_name, idx_name, lots, dte, reason):
    with db.get_conn() as conn:
        conn.execute("""INSERT OR REPLACE INTO strategy_live
            (date, strategy, idx, status, lots, skipped, skip_reason, dte, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (ref_date.isoformat(), strategy_name, idx_name, "SKIPPED", lots,
             1, reason, dte, now_ist().isoformat()))
    log.info("  SKIP %s/%s: %s", strategy_name, idx_name, reason)


# ---------------------------------------------------------------------------
# Monitor: update live P&L, check SL/trailing every tick
# ---------------------------------------------------------------------------
def monitor_tick(ref_date: date):
    """One monitoring cycle — fetch current spots, update all OPEN positions."""
    init_live_db()
    with db.get_conn() as conn:
        open_positions = conn.execute(
            "SELECT * FROM strategy_live WHERE date=? AND status='OPEN'",
            (ref_date.isoformat(),)
        ).fetchall()

    if not open_positions:
        return 0

    uclient = UpstoxData()
    candle_cache = {}
    updated = 0

    for pos in open_positions:
        pos = dict(pos)
        idx_name = pos["idx"]
        strategy_name = pos["strategy"]
        params = STRATEGIES[strategy_name]
        idx = INDEXES[idx_name]
        iv = idx["iv_annual"]
        lot_size = idx["lot_size"] * (pos["lots"] or 1)

        # Fetch latest candles (intraday endpoint gives today's bars)
        if idx_name not in candle_cache:
            try:
                candle_cache[idx_name] = fetch_candles(uclient, idx_name, ref_date, "5minute")
            except Exception as e:
                log.warning("Failed to fetch candles for %s: %s", idx_name, e)
                continue

        candles = candle_cache[idx_name]
        if not candles:
            continue

        last_candle = candles[-1]
        spot = last_candle["close"]
        atm = pos["atm_strike"]
        mins = (now_ist().hour - 9) * 60 + (now_ist().minute - 15)
        T = _dte_fraction(ref_date, idx_name, mins)

        ce_now = est_prem(spot, atm, "CE", T, iv)
        pe_now = est_prem(spot, atm, "PE", T, iv)

        ce_entry = pos["ce_entry"]
        pe_entry = pos["pe_entry"]
        total_prem = ce_entry + pe_entry

        # Check exit conditions
        exit_reason = None
        ce_exit = pe_exit = None

        # Worst case from candle high/low
        ce_worst = est_prem(last_candle["high"], atm, "CE", T, iv)
        pe_worst = est_prem(last_candle["low"], atm, "PE", T, iv)

        if params.get("combined_sl"):
            if ce_worst + pe_worst >= pos["sl_level"]:
                exit_reason = "combined_sl"
                ce_exit, pe_exit = ce_now, pe_now
        else:
            ce_sl = pos["ce_sl"]
            pe_sl = pos["pe_sl"]
            if ce_worst >= ce_sl and pe_worst >= pe_sl:
                exit_reason = "both_sl"
                ce_exit = min(ce_now, ce_sl)
                pe_exit = min(pe_now, pe_sl)
            elif ce_worst >= ce_sl:
                exit_reason = "ce_sl"
                ce_exit = min(ce_now, ce_sl)
                pe_exit = pe_now
            elif pe_worst >= pe_sl:
                exit_reason = "pe_sl"
                ce_exit = ce_now
                pe_exit = min(pe_now, pe_sl)

        # Trailing stop (kitchen_sink)
        if params.get("trailing") and exit_reason is None:
            current_profit = total_prem - (ce_now + pe_now)
            trail_best = max(pos["trail_best"] or 0, current_profit)
            trail_active = pos["trail_active"] or 0

            if current_profit / total_prem >= 0.40:
                trail_active = 1
            if trail_active and trail_best > 0:
                give_back = trail_best * 0.20
                if current_profit < trail_best - give_back:
                    exit_reason = "trailing"
                    ce_exit, pe_exit = ce_now, pe_now

            with db.get_conn() as conn:
                conn.execute("UPDATE strategy_live SET trail_best=?, trail_active=? WHERE id=?",
                             (round(trail_best, 2), trail_active, pos["id"]))

        # Time exit at 15:10
        cur = now_ist()
        if cur.hour >= 15 and cur.minute >= 10 and exit_reason is None:
            exit_reason = "time_3:10"
            ce_exit, pe_exit = ce_now, pe_now

        if exit_reason:
            # Close the position
            ce_pnl = (ce_entry - ce_exit) * lot_size
            pe_pnl = (pe_entry - pe_exit) * lot_size
            charges = calc_charges(ce_entry, ce_exit, lot_size) + \
                      calc_charges(pe_entry, pe_exit, lot_size)
            net_pnl = ce_pnl + pe_pnl - charges

            with db.get_conn() as conn:
                conn.execute("""UPDATE strategy_live SET
                    status='CLOSED', exit_time=?, exit_reason=?,
                    spot_current=?, ce_current=?, pe_current=?,
                    ce_exit=?, pe_exit=?, ce_pnl=?, pe_pnl=?,
                    charges=?, net_pnl=?, unrealized_pnl=0, updated_at=?
                    WHERE id=?""",
                    (cur.strftime("%H:%M"), exit_reason,
                     round(spot, 2), round(ce_now, 2), round(pe_now, 2),
                     round(ce_exit, 2), round(pe_exit, 2),
                     round(ce_pnl, 2), round(pe_pnl, 2),
                     round(charges, 2), round(net_pnl, 2),
                     now_ist().isoformat(), pos["id"]))

            log.info("  CLOSED %s/%s: %s | PnL: %+,.0f (charges: %.0f)",
                     strategy_name, idx_name, exit_reason, net_pnl, charges)

            # Also write to strategy_results for historical continuity
            _sync_to_results(ref_date, strategy_name, idx_name, pos, ce_exit, pe_exit,
                             ce_pnl, pe_pnl, charges, net_pnl, exit_reason, cur.strftime("%H:%M"))
        else:
            # Update live P&L
            ce_pnl_unreal = (ce_entry - ce_now) * lot_size
            pe_pnl_unreal = (pe_entry - pe_now) * lot_size
            est_charges = calc_charges(ce_entry, ce_now, lot_size) + \
                          calc_charges(pe_entry, pe_now, lot_size)
            unrealized = ce_pnl_unreal + pe_pnl_unreal - est_charges

            with db.get_conn() as conn:
                conn.execute("""UPDATE strategy_live SET
                    spot_current=?, ce_current=?, pe_current=?,
                    unrealized_pnl=?, updated_at=?
                    WHERE id=?""",
                    (round(spot, 2), round(ce_now, 2), round(pe_now, 2),
                     round(unrealized, 2), now_ist().isoformat(), pos["id"]))

        updated += 1

    return updated


def _sync_to_results(ref_date, strategy_name, idx_name, pos,
                     ce_exit, pe_exit, ce_pnl, pe_pnl, charges, net_pnl,
                     exit_reason, exit_time):
    """Write final result to strategy_results table for historical tracking."""
    from src.strategy.live_runner import init_strategy_db
    init_strategy_db()
    with db.get_conn() as conn:
        conn.execute("""INSERT OR REPLACE INTO strategy_results
            (date, strategy, idx, lots, entry_time, exit_time, exit_reason,
             spot_entry, atm_strike, ce_entry, pe_entry, ce_exit, pe_exit,
             ce_pnl, pe_pnl, charges, net_pnl, skipped, skip_reason, dte)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ref_date.isoformat(), strategy_name, idx_name, pos["lots"],
             pos["entry_time"], exit_time, exit_reason,
             pos["spot_entry"], pos["atm_strike"],
             pos["ce_entry"], pos["pe_entry"],
             round(ce_exit, 2), round(pe_exit, 2),
             round(ce_pnl, 2), round(pe_pnl, 2),
             round(charges, 2), round(net_pnl, 2),
             0, None, pos["dte"]))


# ---------------------------------------------------------------------------
# Stock strategies (unchanged — daily check at 15:35)
# ---------------------------------------------------------------------------
def _notify(msg: str) -> None:
    try:
        from src.notify.telegram_bot import TelegramNotifier
        TelegramNotifier().send_message(msg)
    except Exception:
        log.warning("Could not send Telegram notification")


def run_stock_strategies(ref_date: date):
    log.info("Running stock credit spread strategies for %s...", ref_date)
    try:
        from src.strategy.stock_runner import run_day
        res = run_day(ref_date, lots=1, force=True)
        for sname, data in res.items():
            trades = {k: v for k, v in data.get("stocks", {}).items() if not v.get("skipped")}
            if not trades:
                continue
            log.info("  %s: %+,.0f (%d trades)", sname, data["day_pnl"], len(trades))
            for stock, t in trades.items():
                direction = t.get("direction", "—")
                tag = "BULL PUT" if "bull" in direction else "BEAR CALL"
                _notify(
                    f"📈 *[STOCK] {tag} — {stock}*\n"
                    f"Strategy: {sname.replace('_', ' ')}\n"
                    f"Spot: {t.get('spot_entry', 0):.1f} | "
                    f"Sell: {t.get('sell_strike', '—')} | Buy: {t.get('buy_strike', '—')}\n"
                    f"Credit: {t.get('net_credit', 0):.2f} | DTE: {t.get('dte_at_entry', '—')}\n"
                    f"Expiry: {t.get('expiry_date', '—')} | "
                    f"Exit: {t.get('exit_reason', '—')} on {t.get('exit_date', '—')}\n"
                    f"P&L: ₹{t.get('net_pnl', 0):+,.0f}"
                )
        return res
    except Exception:
        log.exception("Stock strategy error")
        return {}


# ---------------------------------------------------------------------------
# Dashboard query helpers
# ---------------------------------------------------------------------------
def get_live_positions(ref_date: date = None) -> list[dict]:
    """Get all positions for today (OPEN + CLOSED) for dashboard."""
    if ref_date is None:
        ref_date = now_ist().date()
    init_live_db()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_live WHERE date=? ORDER BY strategy, idx",
            (ref_date.isoformat(),)
        ).fetchall()
    return [dict(r) for r in rows]


def get_live_summary(ref_date: date = None) -> dict:
    """Aggregate live position data for dashboard display."""
    positions = get_live_positions(ref_date)
    if not positions:
        return {}

    by_strategy = {}
    for p in positions:
        s = p["strategy"]
        if s not in by_strategy:
            by_strategy[s] = {
                "status": "WAITING",
                "total_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "positions": [],
                "open_count": 0,
                "closed_count": 0,
            }
        by_strategy[s]["positions"].append(p)
        if p["status"] == "OPEN":
            by_strategy[s]["unrealized_pnl"] += p.get("unrealized_pnl") or 0
            by_strategy[s]["open_count"] += 1
            by_strategy[s]["status"] = "LIVE"
        elif p["status"] == "CLOSED":
            by_strategy[s]["total_pnl"] += p.get("net_pnl") or 0
            by_strategy[s]["closed_count"] += 1
        elif p["status"] == "SKIPPED":
            pass

    for s in by_strategy:
        d = by_strategy[s]
        if d["open_count"] == 0 and d["closed_count"] > 0:
            d["status"] = "CLOSED"
        d["combined_pnl"] = round(d["total_pnl"] + d["unrealized_pnl"], 2)
        d["total_pnl"] = round(d["total_pnl"], 2)
        d["unrealized_pnl"] = round(d["unrealized_pnl"], 2)

    return by_strategy


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run_trading_day(ref_date: date, lots: int = 1):
    """Full intraday schedule for one trading day."""
    init_live_db()
    from src.strategy.live_runner import init_strategy_db
    init_strategy_db()

    entry_schedule = [
        ("09:20", "vf_920_sl30"),
        ("09:30", "kitchen_sink"),
        ("09:45", "entry_945_sl30"),
    ]

    log.info("=== Trading day %s ===", ref_date)

    # Phase 1: Entries at scheduled times
    for time_str, strategy_name in entry_schedule:
        if _shutdown:
            return
        hh, mm = map(int, time_str.split(":"))
        target = datetime(ref_date.year, ref_date.month, ref_date.day, hh, mm, tzinfo=IST)

        if now_ist() > target + timedelta(minutes=2):
            # Missed this entry window — enter now if before 15:00
            if now_ist().hour < 15:
                log.info("Late entry for %s (was scheduled %s, entering now)...", strategy_name, time_str)
                enter_positions(strategy_name, ref_date, lots)
            else:
                log.info("Skipping %s — past market hours", strategy_name)
            continue

        if now_ist() < target:
            log.info("Waiting until %s for %s entry...", time_str, strategy_name)
            sleep_until(target)

        if _shutdown:
            return
        log.info("Entering %s positions...", strategy_name)
        enter_positions(strategy_name, ref_date, lots)

    # Phase 2: Monitor every 60 seconds until 15:15
    market_end = datetime(ref_date.year, ref_date.month, ref_date.day, 15, 15, tzinfo=IST)
    log.info("Monitoring positions (every 60s until 15:15)...")

    while not _shutdown and now_ist() < market_end:
        try:
            n = monitor_tick(ref_date)
            if n == 0:
                log.info("All positions closed. Monitoring complete.")
                break
        except Exception:
            log.exception("Monitor tick error")
        time.sleep(60)

    # Force-close any remaining OPEN positions
    if not _shutdown:
        with db.get_conn() as conn:
            still_open = conn.execute(
                "SELECT COUNT(*) FROM strategy_live WHERE date=? AND status='OPEN'",
                (ref_date.isoformat(),)
            ).fetchone()[0]
        if still_open:
            log.info("Force-closing %d remaining positions at 15:15...", still_open)
            monitor_tick(ref_date)

    # Phase 3: Stock strategies at 15:35
    stock_time = datetime(ref_date.year, ref_date.month, ref_date.day, 15, 35, tzinfo=IST)
    if now_ist() < stock_time:
        log.info("Waiting until 15:35 for stock strategies...")
        sleep_until(stock_time)
    if not _shutdown:
        run_stock_strategies(ref_date)

    # Summary
    summary = get_live_summary(ref_date)
    grand_total = sum(d["combined_pnl"] for d in summary.values())
    log.info("=== DAY COMPLETE %s === Combined P&L: %+,.0f", ref_date, grand_total)
    for s, d in summary.items():
        log.info("  %s: %+,.0f (%d closed, %d open)", s, d["combined_pnl"],
                 d["closed_count"], d["open_count"])


def main():
    log.info("Intraday strategy tracker started")
    init_live_db()

    while not _shutdown:
        today = now_ist().date()

        if today.weekday() >= 5:
            days_ahead = 1
            while (today + timedelta(days=days_ahead)).weekday() >= 5:
                days_ahead += 1
            next_day = today + timedelta(days=days_ahead)
            wake = datetime(next_day.year, next_day.month, next_day.day, 9, 0, tzinfo=IST)
            log.info("Weekend. Sleeping until %s...", wake.strftime("%Y-%m-%d %H:%M IST"))
            sleep_until(wake)
            continue

        current = now_ist()
        market_end = datetime(today.year, today.month, today.day, 16, 0, tzinfo=IST)

        if current > market_end:
            # Past market hours — check if we already ran today
            with db.get_conn() as conn:
                done = conn.execute(
                    "SELECT COUNT(*) FROM strategy_live WHERE date=?",
                    (today.isoformat(),)).fetchone()[0]
            if done == 0:
                log.info("Late start — running batch mode for today...")
                from src.strategy.live_runner import run_day
                run_day(today, lots=1, force=True)
                run_stock_strategies(today)
            else:
                log.info("Today already tracked (%d positions)", done)

            next_day = today + timedelta(days=1)
            while next_day.weekday() >= 5:
                next_day += timedelta(days=1)
            wake = datetime(next_day.year, next_day.month, next_day.day, 9, 0, tzinfo=IST)
            log.info("Sleeping until %s...", wake.strftime("%Y-%m-%d %H:%M IST"))
            sleep_until(wake)
            continue

        # Normal trading day — run intraday tracking
        run_trading_day(today, lots=1)

        # Sleep until next trading day
        next_day = today + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        wake = datetime(next_day.year, next_day.month, next_day.day, 9, 0, tzinfo=IST)
        log.info("Sleeping until next trading day %s...", wake.strftime("%Y-%m-%d %H:%M IST"))
        sleep_until(wake)

    log.info("Intraday strategy tracker stopped")


if __name__ == "__main__":
    main()

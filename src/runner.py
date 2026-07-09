"""Orchestration loop + scheduler (Ongoing/Reliability).

Ties the components together into an unattended service:
  - run_cycle(): one full pass — news → snapshot → signals → analysis+alert →
    paper/live execution, all behind the guardrails and market-hours gate.
  - eod_summary(): end-of-day Telegram report.
  - start_scheduler(): APScheduler loop running cycles every ANALYSIS_INTERVAL_MIN
    during market hours, EOD summary after close, and a pre-open token refresh.

Alerts are intentionally low-noise: full 8-section analysis + a trade alert are
pushed only when a signal fires; plus the daily EOD summary and error alerts.
"""
from __future__ import annotations

from typing import Any

import config
from src.utils import market_calendar as mc
from src.utils.logging import get_logger

log = get_logger("runner")


def _safe_notify(notifier: Any | None, text: str) -> None:
    if notifier is None:
        return
    try:
        notifier.send_message(text)
    except Exception as exc:  # noqa: BLE001 - alerts must never crash the loop
        log.error("Telegram send failed: %s", exc)


def _entry_block_reason(index_symbol: str, date_iso: str) -> str | None:
    """Return why a new entry should be skipped, or None if it's allowed.

    Enforces the same trade-management rules the backtest was validated under:
      - only trade inside the ENTRY_START..ENTRY_END session window
      - one open position per symbol at a time
      - at most MAX_TRADES_PER_SYMBOL_PER_DAY entries per symbol per day
    """
    from datetime import time as _t
    from src.storage import db

    def _hhmm(s: str) -> _t:
        h, m = s.split(":")
        return _t(int(h), int(m))

    now_t = mc.now_ist().time()
    start = _hhmm(getattr(config, "ENTRY_START", "09:15"))
    end = _hhmm(getattr(config, "ENTRY_END", "15:30"))
    if not (start <= now_t <= end):
        return f"outside entry window {config.ENTRY_START}-{config.ENTRY_END}"
    if db.has_open_position_for(index_symbol):
        return "a position is already open for this symbol"
    cap = getattr(config, "MAX_TRADES_PER_SYMBOL_PER_DAY", config.MAX_TRADES_PER_DAY)
    if db.symbol_trades_today(index_symbol, date_iso) >= cap:
        return f"per-symbol daily cap reached ({cap})"
    return None


def run_cycle(client: Any, notifier: Any | None, executor: Any) -> dict[str, Any]:
    """Run one full analysis/execution pass. Returns a small result summary."""
    from src.data import market_data
    from src.news import feeds, analyzer
    from src.signals import engine
    from src.notify import analysis as analysis_mod
    from src.execution import guardrails
    from src.storage import db

    date_iso = mc.now_ist().date().isoformat()
    db.init_db()

    # Kill-switch gate (realised loss so far today).
    ds = dict(db.get_or_create_daily_state(date_iso))
    if ds.get("kill_switch_tripped"):
        log.info("Kill switch tripped — cycle skipped.")
        return {"skipped": "kill_switch"}
    if guardrails.should_trip_kill_switch(ds["realised_pnl"]):
        guardrails.trip_kill_switch(date_iso, client=client, notifier=notifier)
        return {"skipped": "kill_switch_tripped_now"}

    # 0. Monitor open positions first (close on stop/target, realize P&L, and
    #    trip the kill switch if the daily loss limit is breached).
    try:
        if config.EXECUTION_BROKER == "upstox":
            from src.execution.executor import monitor_upstox_positions
            exits = monitor_upstox_positions(kite_client=client, notifier=notifier)
        else:
            from src.execution.executor import monitor_paper_positions
            exits = monitor_paper_positions(client=client, notifier=notifier)
        if exits:
            log.info("Monitor closed %d position(s).", len(exits))
    except Exception as exc:  # noqa: BLE001
        log.error("Monitor step failed: %s", exc)

    # Re-read kill-switch state in case the monitor just tripped it.
    ds = dict(db.get_or_create_daily_state(date_iso))
    if ds.get("kill_switch_tripped"):
        log.info("Kill switch tripped during monitor — no new trades.")
        return {"skipped": "kill_switch", "monitored": True}

    # 1. News (cheap Haiku tagging) — failures shouldn't stop the cycle.
    try:
        items = feeds.poll(relevant_only=True)
        stored = analyzer.analyze_and_store(items)
        log.info("Cycle: %d news item(s) tagged.", len(stored))
    except Exception as exc:  # noqa: BLE001
        log.error("News step failed: %s", exc)
        _safe_notify(notifier, f"⚠️ News step error: {exc}")

    # 2. Technicals → signals → (on signal) analysis + alert + execution.
    signals_fired = 0
    snaps = market_data.snapshot_watchlist(client, symbols=config.WATCHLIST)
    for snap in snaps:
        news = engine.news_view_for_symbol(snap["symbol"])
        sig = engine.evaluate(snap, news=news)
        if sig is None:
            continue
        signals_fired += 1

        # Trade-management gates (match the backtest the strategy was validated
        # with): skip the noisy open/close auctions, hold one position per symbol
        # at a time, and cap re-entries per symbol per day.
        sym = snap["symbol"]
        block = _entry_block_reason(sym, date_iso)
        if block:
            log.info("Signal for %s not traded: %s", sym, block)
            continue
        import json
        context = json.dumps({
            "ltp": snap.get("ltp"), "trend": snap.get("trend"),
            "rsi_fast": snap.get("rsi_fast"), "rsi_slow": snap.get("rsi_slow"),
            "rsi_fast_state": snap.get("rsi_fast_state"),
            "rsi_slow_state": snap.get("rsi_slow_state"),
            "patterns": snap.get("patterns"), "nearest_pivot": snap.get("nearest_pivot"),
            "news_net": news.get("net"),
        })
        sig_row = {**sig.__dict__, "ts": mc.now_ist().isoformat(timespec="seconds"),
                   "context": context}
        sid = db.insert_signal(sig_row)
        signal_dict = {**sig.__dict__, "id": sid}

        # 8-section analysis + trade alert.
        try:
            text = analysis_mod.generate_analysis(snap, news_view=news, signal=signal_dict)
            _safe_notify(notifier, f"📊 *{snap['symbol']}*\n\n{text}")
        except Exception as exc:  # noqa: BLE001
            log.error("Analysis failed for %s: %s", snap["symbol"], exc)

        # Execution (PAPER unless LIVE+confirm).
        try:
            from src.broker import instruments
            opt = instruments.resolve_weekly_atm_option(
                client, snap["symbol"], sig.direction, snap["ltp"])
            if opt:
                full = f"{opt['exchange']}:{opt['tradingsymbol']}"
                opt["last_price"] = client.ltp([full])[full]["last_price"]
                result = executor.execute_signal(signal_dict, opt)
                log.info("Execution result %s: %s", snap["symbol"], result)
        except Exception as exc:  # noqa: BLE001 - fail safe: log + alert, don't crash loop
            log.error("Execution error for %s: %s", snap["symbol"], exc)
            _safe_notify(notifier, f"⚠️ Execution error {snap['symbol']}: {exc}")

    log.info("Cycle complete — %d signal(s) fired.", signals_fired)
    return {"signals": signals_fired, "symbols": len(snaps)}


def eod_summary(client: Any, notifier: Any | None) -> str:
    """Build and send the end-of-day summary."""
    from src.storage import db
    date_iso = mc.now_ist().date().isoformat()
    ds = dict(db.get_or_create_daily_state(date_iso))
    with db.get_conn() as conn:
        trades = conn.execute(
            "SELECT ts, symbol, qty, price, exit_price, pnl, status, is_hedge, "
            "index_entry FROM trades WHERE ts LIKE ? AND side='BUY' ORDER BY id",
            (f"{date_iso}%",),
        ).fetchall()
        sig_count = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE ts LIKE ?", (f"{date_iso}%",)
        ).fetchone()[0]

    from src.notify import messages
    # One clear line per trade: direction, index entry point, premium in -> out, P&L.
    orders = []
    for t in trades:
        hhmm = (t["ts"] or "")[11:16]
        d = dict(t)
        is_call = d["symbol"].upper().endswith("CE")
        direction = "📈CALL வாங்கல் (ஏறும்)" if is_call else "📉PUT வாங்கல் (இறங்கும்)"
        hedge = " 🛡️hedge" if d.get("is_hedge") else ""
        idx = (f" | index @{d['index_entry']:,.0f}" if d.get("index_entry") else "")
        if d["exit_price"] is not None:
            pnl = d["pnl"] or 0.0
            mark = "🟢" if pnl > 0 else "🔴"
            reason = (d["status"] or "").replace("CLOSED_", "")
            orders.append(
                f"{mark} {hhmm} {direction}{hedge} {d['symbol']} x{d['qty']}{idx} | "
                f"prem {d['price']} → {d['exit_price']} | ₹{pnl:,.0f} ({reason})")
        else:
            orders.append(
                f"⏳ {hhmm} {direction}{hedge} {d['symbol']} x{d['qty']}{idx} | "
                f"prem {d['price']} | still OPEN")
    text = messages.eod_summary(
        date_iso, config.MODE, sig_count, ds["trades_count"],
        config.MAX_TRADES_PER_DAY, ds["realised_pnl"],
        bool(ds["kill_switch_tripped"]), orders=orders or None)
    _safe_notify(notifier, text)

    # Whole-day AI insight (one consolidated view instead of scattered
    # per-signal analyses). Best-effort: never break the summary.
    if notifier is not None and trades:
        try:
            insight = _daily_insight(date_iso, [dict(t) for t in trades], ds)
            _safe_notify(notifier, "📔 *இன்றைய முழு-நாள் பகுப்பாய்வு*\n\n" + insight)
        except Exception as exc:  # noqa: BLE001
            log.error("Daily insight failed: %s", exc)

    log.info("EOD summary sent.")
    return text


def _daily_insight(date_iso: str, trades: list[dict], ds: dict) -> str:
    """One LLM-generated whole-day review: what the market did, what the bot
    did (originals vs hedges), what worked/hurt, and one thing to watch."""
    from src.llm.client import LLMClient

    lines = []
    for t in trades:
        kind = "HEDGE" if t.get("is_hedge") else "ORIG"
        opt = "CALL(bullish)" if t["symbol"].upper().endswith("CE") else "PUT(bearish)"
        lines.append(
            f"{(t['ts'] or '')[11:16]} {kind} {opt} {t['symbol']} x{t['qty']} "
            f"index@{t.get('index_entry')} prem {t['price']}->{t['exit_price']} "
            f"pnl {t['pnl']} status {t['status']}")
    tamil = config.ALERT_LANGUAGE.lower() in ("tamil", "ta")
    system = (
        "You are a trading-day reviewer for an NSE index-options PAPER-trading bot "
        "that buys options and uses a hedge-recovery flow (opposite-side hedges when "
        "a position bleeds). Given today's trade log, write a WHOLE-DAY review: "
        "(1) one-line day summary with net P&L, (2) what the market likely did based "
        "on the trade directions/outcomes, (3) what worked, (4) what hurt, "
        "(5) ONE concrete thing to watch tomorrow. Be factual, use only the given "
        "data, keep under ~160 words."
        + (" Respond in simple Tamil; keep numbers and NIFTY/SENSEX/CALL/PUT in "
           "English." if tamil else " Respond in concise English.")
    )
    user = (f"Date: {date_iso}\nRealised P&L: Rs {ds['realised_pnl']:,.0f}\n"
            f"Trades ({len(trades)}):\n" + "\n".join(lines))
    return LLMClient().complete_text(
        system=system, user=user, model=config.LLM_SMART_MODEL, max_tokens=500)


def weekly_review(notifier: Any | None) -> str:
    """Generate the AI 'learn from mistakes' review and send it to Telegram."""
    from src.storage import db
    from src.review.performance import generate_review
    rows = db.closed_trades_with_context(limit=200)
    text = generate_review(rows)
    header = ("🧠 *வாராந்திர செயல்திறன் மதிப்பாய்வு*\n\n"
              if config.ALERT_LANGUAGE.lower() in ("tamil", "ta")
              else "🧠 *Weekly performance review*\n\n")
    _safe_notify(notifier, header + text)
    log.info("Weekly review sent.")
    return text


def _settings_banner() -> str:
    """One-line daily statement of the ACTIVE rules the running process loaded.

    Sent at startup and at the 9:05 pre-open check so silence is never
    ambiguous and a deploy that didn't take effect is visible immediately."""
    stops = "ON" if getattr(config, "STOP_LOSS_ENABLED", True) else "OFF (manual cuts)"
    hedge = "ON" if getattr(config, "HEDGE_ON_LOSS", False) else "OFF"
    style = "intraday" if getattr(config, "INTRADAY_SQUAREOFF", True) else "positional"
    return (f"⚙️ Bot alive · stops={stops} · hedge chain {hedge} · {style} · "
            f"TP ₹{getattr(config, 'PROFIT_TARGET_RUPEES', 0):,.0f} · "
            f"{getattr(config, 'MAX_LOTS_PER_TRADE', 0)} lots · "
            f"monitor {getattr(config, 'MONITOR_INTERVAL_MIN', 15)}min")


def _ensure_or_refresh_session(notifier: Any | None) -> Any | None:
    """Pre-open: reuse cached token, try TOTP auto-login, else alert for manual login."""
    from src.broker.session import ensure_session, automated_login
    from src.broker.kite_client import KiteClientError
    try:
        return ensure_session()
    except KiteClientError:
        pass
    try:
        client = automated_login()
        _safe_notify(notifier, "✅ Kite session auto-refreshed for today.")
        return client
    except Exception as exc:  # noqa: BLE001
        log.error("Auto token refresh unavailable: %s", exc)
        _safe_notify(notifier, "⚠️ Kite login needed — run `python main.py --login`.")
        return None


def start_scheduler(confirm_live: bool = False) -> None:
    """Start the blocking APScheduler loop. Ctrl-C to stop."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from src.broker.session import ensure_session
    from src.broker.kite_client import KiteClientError
    from src.execution.executor import Executor

    notifier = None
    try:
        from src.notify.telegram_bot import TelegramNotifier
        notifier = TelegramNotifier()
    except Exception:  # noqa: BLE001 - alerts optional
        notifier = None

    sched = BlockingScheduler(timezone=config.TIMEZONE)

    def cycle_job() -> None:
        if not mc.is_market_open():
            return
        try:
            client = ensure_session()
        except KiteClientError as exc:
            log.warning("No session: %s", exc)
            _safe_notify(notifier, "⚠️ No Kite session — run `python main.py --login`.")
            return
        executor = Executor(client=client, notifier=notifier,
                            mode=config.MODE, confirm_live=confirm_live)
        try:
            run_cycle(client, notifier, executor)
        except Exception as exc:  # noqa: BLE001 - fail safe
            log.error("Cycle crashed: %s", exc)
            _safe_notify(notifier, f"⚠️ Cycle error: {exc}")

    def monitor_job() -> None:
        """Fast exit-only check (every MONITOR_INTERVAL_MIN min) so a stop is cut
        near its level instead of up to a full analysis-cycle late."""
        if not mc.is_market_open():
            return
        try:
            client = ensure_session()
        except KiteClientError:
            return
        try:
            from src.storage import db
            date_iso = mc.now_ist().date().isoformat()
            ds = dict(db.get_or_create_daily_state(date_iso))
            if ds.get("kill_switch_tripped"):
                return
            if config.EXECUTION_BROKER == "upstox":
                from src.execution.executor import monitor_upstox_positions
                exits = monitor_upstox_positions(kite_client=client, notifier=notifier)
            else:
                from src.execution.executor import monitor_paper_positions
                exits = monitor_paper_positions(client=client, notifier=notifier)
            if exits:
                log.info("Fast monitor closed %d position(s).", len(exits))
        except Exception as exc:  # noqa: BLE001 - never let the monitor crash the loop
            log.error("Fast monitor failed: %s", exc)

    def eod_job() -> None:
        if not mc.is_trading_day():
            return
        try:
            client = ensure_session()
        except KiteClientError:
            client = None
        eod_summary(client, notifier)

    def preopen_job() -> None:
        if mc.is_trading_day():
            _ensure_or_refresh_session(notifier)
            _safe_notify(notifier, _settings_banner())

    # Cycle: every ANALYSIS_INTERVAL_MIN minutes, weekdays, market hours (gated inside).
    sched.add_job(cycle_job, CronTrigger(
        day_of_week="mon-fri", hour="9-15",
        minute=f"*/{config.ANALYSIS_INTERVAL_MIN}", timezone=config.TIMEZONE))
    # Fast exit monitor: every MONITOR_INTERVAL_MIN min so stops are cut on time.
    sched.add_job(monitor_job, CronTrigger(
        day_of_week="mon-fri", hour="9-15",
        minute=f"*/{config.MONITOR_INTERVAL_MIN}", timezone=config.TIMEZONE))
    # EOD summary just after close.
    sched.add_job(eod_job, CronTrigger(
        day_of_week="mon-fri", hour=15, minute=31, timezone=config.TIMEZONE))
    # Pre-open token check/refresh.
    sched.add_job(preopen_job, CronTrigger(
        day_of_week="mon-fri", hour=9, minute=5, timezone=config.TIMEZONE))

    # Weekly AI performance review — Friday after close.
    def review_job() -> None:
        if mc.is_trading_day():
            weekly_review(notifier)
    sched.add_job(review_job, CronTrigger(
        day_of_week="fri", hour=15, minute=45, timezone=config.TIMEZONE))

    # News refresh — every 30 min, ALL hours/days, so the dashboard always shows
    # fresh news. Skipped during market hours (the trading cycle handles it then).
    def news_job() -> None:
        if mc.is_market_open():
            return
        try:
            from src.news import feeds, analyzer
            stored = analyzer.analyze_and_store(feeds.poll(relevant_only=True))
            if stored:
                log.info("News refresh tagged %d new item(s).", len(stored))
        except Exception as exc:  # noqa: BLE001
            log.error("News refresh failed: %s", exc)
    sched.add_job(news_job, CronTrigger(minute="*/30", timezone=config.TIMEZONE))

    # Initial news fill at startup so the dashboard isn't stale right away.
    try:
        from src.news import feeds, analyzer
        analyzer.analyze_and_store(feeds.poll(relevant_only=True))
    except Exception as exc:  # noqa: BLE001
        log.error("Initial news fill failed: %s", exc)

    # Establish a session at startup so clicking the launcher works at any time
    # (uses cached token, or auto-login if KITE_USER_ID/PASSWORD/TOTP_SECRET are set).
    if mc.is_trading_day():
        _ensure_or_refresh_session(notifier)

    from src.notify import messages
    log.info("Scheduler started (MODE=%s). Market hours %s-%s IST. Ctrl-C to stop.",
             config.MODE, config.MARKET_OPEN, config.MARKET_CLOSE)
    _safe_notify(notifier, messages.started(config.MODE))
    _safe_notify(notifier, _settings_banner())
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")

"""Trading Buddy — entry point & scheduler loop.

Phase 0 implements ``--selftest`` only: it creates the database, prints the
non-secret config, and confirms the IST clock + market-hours logic, with NO
live connections (no broker, LLM, or Telegram calls).

Later phases wire the scheduler loop (news -> technicals -> signals ->
alerts -> execution within guardrails).
"""
from __future__ import annotations

import argparse
import json
import sys

# Render UTF-8 on the Windows console so em-dashes / symbols don't show as '?'.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import config
from src.storage import db
from src.utils.logging import get_logger
from src.utils import market_calendar as mc


def selftest() -> int:
    """Run offline self-checks. Returns process exit code."""
    log = get_logger("selftest")
    log.info("Trading Buddy self-test starting (no live connections).")

    # 1. Database
    db.init_db()
    tables = db.table_names()
    expected = {"news_items", "signals", "trades", "daily_state", "app_log"}
    missing = expected - set(tables)
    if missing:
        log.error("DB init failed — missing tables: %s", sorted(missing))
        return 1
    log.info("DB OK at %s", config.DB_PATH)
    log.info("Tables: %s", ", ".join(sorted(tables)))

    # 2. Config dump
    print("\n=== CONFIG (non-secret) ===")
    print(json.dumps(config.as_dict(), indent=2, default=str))

    # 3. IST clock & market-hours logic
    now = mc.now_ist()
    print("\n=== CLOCK / MARKET ===")
    print(f"Now (IST)      : {now.isoformat(timespec='seconds')}")
    print(f"Timezone       : {now.tzinfo}")
    print(f"Trading day    : {mc.is_trading_day(now.date())}")
    print(f"Market open now: {mc.is_market_open(now)}")
    print(f"Market status  : {mc.market_status(now)}")
    print(f"Market hours   : {config.MARKET_OPEN}-{config.MARKET_CLOSE} IST, Mon-Fri")

    # 4. Sanity-check market-hours logic against fixed sample datetimes.
    from datetime import datetime
    samples = {
        "Weekday 10:00 (open)":  datetime(2026, 6, 12, 10, 0, tzinfo=mc.IST),
        "Weekday 08:00 (pre)":   datetime(2026, 6, 12, 8, 0, tzinfo=mc.IST),
        "Weekday 16:00 (post)":  datetime(2026, 6, 12, 16, 0, tzinfo=mc.IST),
        "Saturday 10:00":        datetime(2026, 6, 13, 10, 0, tzinfo=mc.IST),
        "Republic Day 10:00":    datetime(2026, 1, 26, 10, 0, tzinfo=mc.IST),
    }
    print("\n=== MARKET-HOURS LOGIC CHECK ===")
    ok = True
    expected_open = {
        "Weekday 10:00 (open)": True,
        "Weekday 08:00 (pre)": False,
        "Weekday 16:00 (post)": False,
        "Saturday 10:00": False,
        "Republic Day 10:00": False,
    }
    for label, dt in samples.items():
        got = mc.is_market_open(dt)
        flag = "OK " if got == expected_open[label] else "BAD"
        if got != expected_open[label]:
            ok = False
        print(f"  [{flag}] {label:24s} -> open={got}  ({mc.market_status(dt)})")

    if not ok:
        log.error("Market-hours logic check FAILED.")
        return 1

    log.info("Self-test PASSED.")
    print("\nSelf-test PASSED.")
    return 0


def cmd_init() -> int:
    """First-run setup + health check: DB, .env keys, and every live connection."""
    import os
    from dotenv import load_dotenv
    from src.storage import db
    from src.utils import market_calendar as mc
    load_dotenv()

    OK, WARN, FAIL = "[ OK ]", "[WARN]", "[FAIL]"
    results: list[str] = []   # collect FAIL/WARN reasons for the verdict

    def line(status: str, label: str, detail: str = "") -> None:
        print(f"  {status} {label}" + (f" — {detail}" if detail else ""))
        if status in (WARN, FAIL):
            results.append(status)

    print("\n=== TRADING BUDDY — INITIALIZATION & HEALTH CHECK ===\n")

    # 1. Database
    print("Storage")
    try:
        db.init_db()
        tables = set(db.table_names())
        need = {"news_items", "signals", "trades", "daily_state", "app_log"}
        if need <= tables:
            line(OK, "Database", f"ready at {config.DB_PATH}")
        else:
            line(FAIL, "Database", f"missing tables: {sorted(need - tables)}")
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "Database", str(exc))

    # 2. Clock / market
    print("\nClock & market")
    now = mc.now_ist()
    line(OK, "IST clock", now.isoformat(timespec="seconds"))
    line(OK, "Market", mc.market_status())

    # 3. Config posture
    print("\nConfig")
    line(OK, "Mode", f"{config.MODE}  (execution broker: {config.EXECUTION_BROKER})")
    line(OK, "Risk", f"₹{config.MAX_RISK_PER_TRADE}/trade, max {config.MAX_TRADES_PER_DAY}/day, "
                     f"daily-loss kill ₹{config.MAX_DAILY_LOSS}")

    # 4. .env keys present
    print("\nSecrets (.env)")
    required = ["KITE_API_KEY", "KITE_API_SECRET", "ANTHROPIC_API_KEY",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    for k in required:
        line(OK if os.getenv(k) else FAIL, k, "set" if os.getenv(k) else "MISSING")
    optional = ["KITE_TOTP_SECRET", "KITE_USER_ID", "KITE_PASSWORD",
                "UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_SANDBOX_TOKEN"]
    for k in optional:
        line(OK if os.getenv(k) else WARN, k, "set" if os.getenv(k) else "not set (optional)")

    # 5. Kite — session + data
    print("\nKite (data)")
    try:
        from src.broker.session import ensure_session
        from src.broker.kite_client import KiteClientError
        try:
            client = ensure_session()
            prof = client.profile()
            line(OK, "Kite session", f"logged in as {prof.get('user_id', '?')}")
            try:
                q = client.ltp(["NSE:NIFTY 50"]).get("NSE:NIFTY 50", {})
                line(OK, "Kite live data", f"NIFTY 50 LTP {q.get('last_price')}")
            except Exception as exc:  # noqa: BLE001
                line(FAIL, "Kite live data", f"{exc} (data subscription active?)")
        except KiteClientError:
            if all(os.getenv(k) for k in ("KITE_USER_ID", "KITE_PASSWORD", "KITE_TOTP_SECRET")):
                line(OK, "Kite session", "auto-login configured (logs in automatically at startup)")
            else:
                line(WARN, "Kite session", "no valid token today — run `python main.py --login`")
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "Kite", str(exc))

    # 6. Anthropic — tiny live call
    print("\nAnthropic (analysis)")
    if not os.getenv("ANTHROPIC_API_KEY"):
        line(WARN, "Anthropic", "skipped (no key)")
    else:
        try:
            from src.llm.client import LLMClient
            c = LLMClient()
            c.client.messages.create(model=config.LLM_FAST_MODEL, max_tokens=4,
                                     messages=[{"role": "user", "content": "ping"}])
            line(OK, "Anthropic", f"key valid ({config.LLM_FAST_MODEL})")
        except Exception as exc:  # noqa: BLE001
            line(FAIL, "Anthropic", str(exc)[:120])

    # 7. Telegram — validate bot + chat id
    print("\nTelegram (alerts)")
    tok = os.getenv("TELEGRAM_BOT_TOKEN")
    if not tok:
        line(WARN, "Telegram", "skipped (no token)")
    else:
        try:
            import requests
            r = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=15).json()
            if r.get("ok"):
                line(OK, "Telegram bot", f"@{r['result'].get('username')}")
            else:
                line(FAIL, "Telegram bot", f"invalid token: {r}")
        except Exception as exc:  # noqa: BLE001
            line(FAIL, "Telegram bot", str(exc)[:120])
        line(OK if os.getenv("TELEGRAM_CHAT_ID") else FAIL, "Telegram chat id",
             os.getenv("TELEGRAM_CHAT_ID") or "MISSING")

    # 8. Upstox — execution (optional until configured)
    print("\nUpstox (execution)")
    if config.EXECUTION_BROKER == "upstox":
        line(OK if os.getenv("UPSTOX_SANDBOX_TOKEN") else FAIL, "Upstox token",
             "set" if os.getenv("UPSTOX_SANDBOX_TOKEN") else "MISSING (EXECUTION_BROKER=upstox)")
    elif os.getenv("UPSTOX_SANDBOX_TOKEN"):
        line(OK, "Upstox token", "set (switch EXECUTION_BROKER='upstox' to use)")
    else:
        line(WARN, "Upstox", "not configured — using internal paper simulation for now")

    # Verdict
    fails = results.count(FAIL)
    print("\n" + "=" * 55)
    if fails == 0:
        print("  RESULT: READY ✅")
        if all(os.getenv(k) for k in ("KITE_USER_ID", "KITE_PASSWORD", "KITE_TOTP_SECRET")):
            print("  Next: just `python main.py` (auto-login handles the daily token).")
        else:
            print("  Next: `python main.py --login` (daily), then `python main.py`.")
    else:
        print(f"  RESULT: NOT READY ❌  ({fails} item(s) need attention above)")
    print("=" * 55 + "\n")
    return 0 if fails == 0 else 1


def cmd_login() -> int:
    """Interactive Kite login -> cache today's access token."""
    from src.broker.session import interactive_login
    from src.broker.kite_client import KiteClientError
    log = get_logger("login")
    try:
        interactive_login()
        return 0
    except KiteClientError as exc:
        log.error("Login failed: %s", exc)
        print(f"Login failed: {exc}")
        return 1


def cmd_snapshot() -> int:
    """Phase 1 acceptance: print a clean technical snapshot per watchlist symbol."""
    from src.broker.session import ensure_session
    from src.broker.kite_client import KiteClientError
    from src.data import market_data
    from src.data.indicators import format_snapshot
    log = get_logger("snapshot")
    try:
        client = ensure_session()
    except KiteClientError as exc:
        print(f"\nCannot fetch data: {exc}")
        return 1

    snaps = market_data.snapshot_watchlist(client)
    if not snaps:
        print("No snapshots produced (check watchlist / market data access).")
        return 1
    print(f"\n=== TECHNICAL SNAPSHOT ({mc.now_ist():%Y-%m-%d %H:%M IST}) ===")
    print(f"Market: {mc.market_status()}\n")
    for s in snaps:
        print(format_snapshot(s))
        print()
    log.info("Printed %d snapshot(s).", len(snaps))
    return 0


def cmd_news() -> int:
    """Phase 2: poll feeds, tag with the LLM, store in the DB."""
    from src.news import feeds, analyzer
    from src.llm.client import LLMError
    log = get_logger("news")
    db.init_db()
    items = feeds.poll(relevant_only=True)
    print(f"\nFetched {len(items)} relevant item(s) from feeds.")
    if not items:
        print("Nothing to analyze.")
        return 0
    try:
        stored = analyzer.analyze_and_store(items)
    except LLMError as exc:
        print(f"\nCannot analyze: {exc}")
        return 1
    print(f"Tagged and stored {len(stored)} item(s) in the DB.\n")
    for s in stored[:20]:
        print(f"  [{s.get('sentiment'):8s} {s.get('confidence'):6s} {str(s.get('symbol')):12s}] "
              f"{s.get('headline', '')[:80]}")
    log.info("News cycle stored %d items.", len(stored))
    return 0


def cmd_signals() -> int:
    """Phase 3: build snapshots, combine with news, emit + store signals."""
    from src.broker.session import ensure_session
    from src.broker.kite_client import KiteClientError
    from src.data import market_data
    from src.signals import engine
    from src.storage import db
    log = get_logger("signals")
    db.init_db()
    try:
        client = ensure_session()
    except KiteClientError as exc:
        print(f"\nCannot fetch data: {exc}")
        return 1

    snaps = market_data.snapshot_watchlist(client, symbols=config.WATCHLIST)
    print(f"\n=== SIGNAL SCAN ({mc.now_ist():%Y-%m-%d %H:%M IST}) - {mc.market_status()} ===\n")
    emitted = 0
    for snap in snaps:
        news = engine.news_view_for_symbol(snap["symbol"])
        sig = engine.evaluate(snap, news=news)
        if sig is None:
            print(f"  {snap['symbol']:16s} -> no low-risk setup "
                  f"(LTP {snap['ltp']:,.2f}, RSI {snap['rsi_fast']:.1f}, news {news['net']})")
            continue
        sig_dict = {**sig.__dict__, "ts": mc.now_ist().isoformat(timespec="seconds")}
        sid = db.insert_signal(sig_dict)
        emitted += 1
        print(f"  {snap['symbol']:16s} -> SIGNAL #{sid} {sig.direction.upper()} "
              f"entry {sig.entry:,.2f} stop {sig.stop:,.2f} target {sig.target:,.2f}")
        print(f"      {sig.rationale}")
    print(f"\n{emitted} signal(s) emitted and stored.")
    log.info("Signal scan emitted %d signal(s).", emitted)
    return 0


def cmd_analyze() -> int:
    """Phase 4: generate the 8-section LLM analysis for each watchlist symbol."""
    from src.broker.session import ensure_session
    from src.broker.kite_client import KiteClientError
    from src.data import market_data
    from src.signals import engine
    from src.notify import analysis
    from src.llm.client import LLMError
    log = get_logger("analyze")
    try:
        client = ensure_session()
    except KiteClientError as exc:
        print(f"\nCannot fetch data: {exc}")
        return 1
    snaps = market_data.snapshot_watchlist(client, symbols=config.WATCHLIST)
    print(f"\n=== 8-SECTION ANALYSIS ({mc.now_ist():%Y-%m-%d %H:%M IST}) - {mc.market_status()} ===")
    for snap in snaps:
        news = engine.news_view_for_symbol(snap["symbol"])
        sig = engine.evaluate(snap, news=news)
        sig_dict = sig.__dict__ if sig else None
        try:
            text = analysis.generate_analysis(snap, news_view=news, signal=sig_dict)
        except LLMError as exc:
            print(f"\nCannot analyze: {exc}")
            return 1
        print(f"\n----- {snap['symbol']} -----\n{text}")
    log.info("Generated analysis for %d symbol(s).", len(snaps))
    return 0


def cmd_chat_id() -> int:
    """Print the Telegram chat ID (you must have messaged the bot first)."""
    from src.notify.telegram_bot import resolve_chat_id, TelegramError
    try:
        cid = resolve_chat_id()
    except TelegramError as exc:
        print(f"{exc}")
        return 1
    if cid:
        print(f"\nYour TELEGRAM_CHAT_ID is: {cid}\nAdd it to .env.")
        return 0
    print("No chat found. Send your bot a message first, then re-run.")
    return 1


def cmd_telegram_test() -> int:
    """Send a test message to confirm Telegram delivery works."""
    from src.notify.telegram_bot import TelegramNotifier, TelegramError
    try:
        TelegramNotifier().send_message(
            f"✅ *Trading Buddy* connected.\nMode: `{config.MODE}` | "
            f"{mc.market_status()} | {mc.now_ist():%Y-%m-%d %H:%M IST}")
    except TelegramError as exc:
        print(f"{exc}")
        return 1
    print("Test message sent — check your phone.")
    return 0


def cmd_bot() -> int:
    """Run the long-running Telegram command bot (blocks)."""
    from src.notify.telegram_bot import run_bot, TelegramError
    try:
        run_bot()
    except TelegramError as exc:
        print(f"{exc}")
        return 1
    return 0


def cmd_alert() -> int:
    """Generate the 8-section analysis per symbol + push it (and signals) to Telegram."""
    from src.broker.session import ensure_session
    from src.broker.kite_client import KiteClientError
    from src.data import market_data
    from src.signals import engine
    from src.notify import analysis
    from src.notify.telegram_bot import TelegramNotifier, TelegramError
    from src.llm.client import LLMError
    from src.storage import db
    log = get_logger("alert")
    db.init_db()
    try:
        notifier = TelegramNotifier()
    except TelegramError as exc:
        print(f"{exc}")
        return 1
    try:
        client = ensure_session()
    except KiteClientError as exc:
        print(f"Cannot fetch data: {exc}")
        return 1

    snaps = market_data.snapshot_watchlist(client, symbols=config.WATCHLIST)
    for snap in snaps:
        news = engine.news_view_for_symbol(snap["symbol"])
        sig = engine.evaluate(snap, news=news)
        sig_dict = sig.__dict__ if sig else None
        try:
            text = analysis.generate_analysis(snap, news_view=news, signal=sig_dict)
        except LLMError as exc:
            print(f"Cannot analyze: {exc}")
            return 1
        notifier.send_message(f"📊 *{snap['symbol']}*\n\n{text}")
        if sig is not None:
            sig_row = {**sig.__dict__, "ts": mc.now_ist().isoformat(timespec="seconds")}
            db.insert_signal(sig_row)
            notifier.send_signal_alert(sig.__dict__)
    print(f"Pushed analysis for {len(snaps)} symbol(s) to Telegram.")
    log.info("Alert cycle pushed %d analyses.", len(snaps))
    return 0


def cmd_paper_trade(confirm_live: bool = False) -> int:
    """Phase 5: scan signals, size + place orders (PAPER simulated), with guardrails."""
    from src.broker.session import ensure_session
    from src.broker.kite_client import KiteClientError
    from src.broker import instruments
    from src.data import market_data
    from src.signals import engine
    from src.execution import guardrails
    from src.execution.executor import Executor
    from src.storage import db
    log = get_logger("paper_trade")
    db.init_db()

    notifier = None
    try:
        from src.notify.telegram_bot import TelegramNotifier, TelegramError
        notifier = TelegramNotifier()
    except Exception:  # noqa: BLE001 - alerts are optional for the scan
        notifier = None

    try:
        client = ensure_session()
    except KiteClientError as exc:
        print(f"\nCannot fetch data: {exc}")
        return 1

    date_iso = mc.now_ist().date().isoformat()
    ds = dict(db.get_or_create_daily_state(date_iso))

    if config.MODE == "LIVE" and not confirm_live:
        print("MODE=LIVE but --confirm-live not passed — orders will be SIMULATED (safe).")

    # Kill-switch pre-check (realised loss so far today).
    if guardrails.should_trip_kill_switch(ds["realised_pnl"]):
        guardrails.trip_kill_switch(date_iso, client=client, notifier=notifier)
        print("Kill switch is tripped — no trading today.")
        return 0

    executor = Executor(client=client, notifier=notifier,
                        mode=config.MODE, confirm_live=confirm_live)

    print(f"\n=== PAPER/LIVE SCAN ({mc.now_ist():%Y-%m-%d %H:%M IST}) - "
          f"{mc.market_status()} | MODE={config.MODE} ===\n")
    snaps = market_data.snapshot_watchlist(client, symbols=config.WATCHLIST)
    for snap in snaps:
        news = engine.news_view_for_symbol(snap["symbol"])
        sig = engine.evaluate(snap, news=news)
        if sig is None:
            print(f"  {snap['symbol']:16s} -> no low-risk setup")
            continue
        sig_row = {**sig.__dict__, "ts": mc.now_ist().isoformat(timespec="seconds")}
        sid = db.insert_signal(sig_row)
        signal_dict = {**sig.__dict__, "id": sid}

        opt = instruments.resolve_weekly_atm_option(
            client, snap["symbol"], sig.direction, snap["ltp"])
        if opt is None:
            print(f"  {snap['symbol']:16s} -> SIGNAL but no option contract resolved")
            continue
        full = f"{opt['exchange']}:{opt['tradingsymbol']}"
        try:
            opt["last_price"] = client.ltp([full])[full]["last_price"]
        except Exception as exc:  # noqa: BLE001
            print(f"  {snap['symbol']:16s} -> SIGNAL but option LTP unavailable: {exc}")
            continue

        result = executor.execute_signal(signal_dict, opt)
        if result["placed"]:
            print(f"  {snap['symbol']:16s} -> {config.MODE} ORDER {opt['tradingsymbol']} "
                  f"x{result['qty']} @ ~{result['entry_premium']} "
                  f"stop {result['stop_premium']} (risk ₹{result['total_risk']:,.0f})")
        else:
            print(f"  {snap['symbol']:16s} -> SIGNAL not executed: {result['reason']}")

    print()
    log.info("Paper-trade scan complete.")
    return 0


def cmd_run_once(confirm_live: bool = False) -> int:
    """Run a single orchestration cycle now (news → signals → analysis → execution)."""
    from src.broker.session import ensure_session
    from src.broker.kite_client import KiteClientError
    from src.execution.executor import Executor
    from src import runner
    try:
        client = ensure_session()
    except KiteClientError as exc:
        print(f"\nCannot fetch data: {exc}")
        return 1
    notifier = None
    try:
        from src.notify.telegram_bot import TelegramNotifier
        notifier = TelegramNotifier()
    except Exception:  # noqa: BLE001
        notifier = None
    executor = Executor(client=client, notifier=notifier,
                        mode=config.MODE, confirm_live=confirm_live)
    result = runner.run_cycle(client, notifier, executor)
    print(f"\nCycle result: {result}")
    return 0


def cmd_monitor() -> int:
    """Check open PAPER positions against live premiums; close on stop/target."""
    from src.broker.session import ensure_session
    from src.broker.kite_client import KiteClientError
    from src.execution.executor import monitor_paper_positions, monitor_upstox_positions
    from src.storage import db
    db.init_db()
    try:
        client = ensure_session()
    except KiteClientError as exc:
        print(f"\nCannot fetch data: {exc}")
        return 1
    notifier = None
    try:
        from src.notify.telegram_bot import TelegramNotifier
        notifier = TelegramNotifier()
    except Exception:  # noqa: BLE001
        notifier = None
    if config.EXECUTION_BROKER == "upstox":
        exits = monitor_upstox_positions(kite_client=client, notifier=notifier)
    else:
        exits = monitor_paper_positions(client=client, notifier=notifier)
    if not exits:
        print("No positions closed (none hit stop/target).")
    for e in exits:
        print(f"  EXIT {e['reason']} {e['symbol']} @ {e['exit_price']} | P&L ₹{e['pnl']:,.2f}")
    return 0


def cmd_eod() -> int:
    """Send the end-of-day summary now."""
    from src.broker.session import ensure_session
    from src.broker.kite_client import KiteClientError
    from src import runner
    try:
        client = ensure_session()
    except KiteClientError:
        client = None
    notifier = None
    try:
        from src.notify.telegram_bot import TelegramNotifier
        notifier = TelegramNotifier()
    except Exception:  # noqa: BLE001
        notifier = None
    text = runner.eod_summary(client, notifier)
    print("\n" + text)
    return 0


def cmd_dashboard() -> int:
    """Serve the interactive web dashboard (status, trades, P&L, pause/resume)."""
    import os
    import uvicorn
    from src.storage import db
    db.init_db()
    port = int(os.getenv("DASHBOARD_PORT", "8000"))
    print(f"\nDashboard running:")
    print(f"  • This PC:        http://localhost:{port}")
    print(f"  • Phone/other PC: http://<this-PC-IP>:{port}   (same Wi-Fi)")
    if not os.getenv("DASHBOARD_PASSWORD"):
        print("  ⚠️  No DASHBOARD_PASSWORD set — open access (fine on home Wi-Fi; set one before deploying).")
    print("  Ctrl-C to stop.\n")
    uvicorn.run("src.dashboard.app:app", host="0.0.0.0", port=port, log_level="warning")
    return 0


def run_live_loop(confirm_live: bool = False) -> int:
    """Start the unattended scheduler (default when no command flag is given)."""
    from src import runner
    runner.start_scheduler(confirm_live=confirm_live)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trading Buddy")
    parser.add_argument(
        "--selftest", action="store_true",
        help="Run offline self-checks (DB, config, clock, market hours).",
    )
    parser.add_argument(
        "--init", action="store_true",
        help="First-run setup + health check: DB, .env keys, and all live connections.",
    )
    parser.add_argument(
        "--login", action="store_true",
        help="Interactive Kite login; caches today's access token.",
    )
    parser.add_argument(
        "--snapshot", action="store_true",
        help="Print a technical snapshot for each watchlist symbol (Phase 1).",
    )
    parser.add_argument(
        "--news", action="store_true",
        help="Poll feeds, tag with the LLM, store in the DB (Phase 2).",
    )
    parser.add_argument(
        "--signals", action="store_true",
        help="Scan the watchlist, combine technicals + news, emit signals (Phase 3).",
    )
    parser.add_argument(
        "--analyze", action="store_true",
        help="Generate the 8-section LLM analysis per watchlist symbol (Phase 4).",
    )
    parser.add_argument(
        "--alert", action="store_true",
        help="Generate the 8-section analysis and push it + signals to Telegram (Phase 4).",
    )
    parser.add_argument(
        "--bot", action="store_true",
        help="Run the Telegram command bot (/status /pause /resume ...). Blocks.",
    )
    parser.add_argument(
        "--telegram-test", action="store_true",
        help="Send a test message to confirm Telegram delivery.",
    )
    parser.add_argument(
        "--chat-id", action="store_true",
        help="Print your Telegram chat ID (message the bot first).",
    )
    parser.add_argument(
        "--paper-trade", action="store_true",
        help="Scan signals and place orders (PAPER simulated unless LIVE+confirm) (Phase 5).",
    )
    parser.add_argument(
        "--run-once", action="store_true",
        help="Run a single orchestration cycle now (news → signals → analysis → exec).",
    )
    parser.add_argument(
        "--eod", action="store_true",
        help="Send the end-of-day summary now.",
    )
    parser.add_argument(
        "--monitor", action="store_true",
        help="Check open PAPER positions; close on stop/target and realize P&L.",
    )
    parser.add_argument(
        "--dashboard", action="store_true",
        help="Serve the interactive web dashboard (status, trades, P&L, controls).",
    )
    parser.add_argument(
        "--confirm-live", action="store_true",
        help="Required (with MODE=LIVE) to enable real order placement.",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.init:
        return cmd_init()
    if args.login:
        return cmd_login()
    if args.snapshot:
        return cmd_snapshot()
    if args.news:
        return cmd_news()
    if args.signals:
        return cmd_signals()
    if args.analyze:
        return cmd_analyze()
    if args.chat_id:
        return cmd_chat_id()
    if args.telegram_test:
        return cmd_telegram_test()
    if args.alert:
        return cmd_alert()
    if args.bot:
        return cmd_bot()
    if args.paper_trade:
        return cmd_paper_trade(confirm_live=args.confirm_live)
    if args.run_once:
        return cmd_run_once(confirm_live=args.confirm_live)
    if args.eod:
        return cmd_eod()
    if args.monitor:
        return cmd_monitor()
    if args.dashboard:
        return cmd_dashboard()
    return run_live_loop(confirm_live=args.confirm_live)


if __name__ == "__main__":
    sys.exit(main())

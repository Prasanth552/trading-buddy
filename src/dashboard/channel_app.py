"""Standalone channel trades dashboard — runs on port 8001.

Lightweight FastAPI app showing only channel signal trades.
Separate from the main dashboard (port 8000) for independent operation.

Run:  python -m src.dashboard.channel_app
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

import asyncio
import json
import time as _time

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

import config
from src.storage import db
from src.utils import market_calendar as mc

load_dotenv()
app = FastAPI(title="Channel Trades", docs_url=None, redoc_url=None)

_db_ready = False

_api_cache: dict[str, tuple[float, dict]] = {}
_API_CACHE_TTL = 10  # seconds

# ---------------------------------------------------------------------------
# WebSocket hub — push updates to all connected clients
# ---------------------------------------------------------------------------
_ws_clients: set[WebSocket] = set()


async def _ws_broadcast(data: dict) -> None:
    msg = json.dumps(data)
    dead: list[WebSocket] = []
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _ws_clients.discard(websocket)


async def _ws_push_loop():
    """Background task: push fresh data to WebSocket clients every 3s."""
    while True:
        await asyncio.sleep(3)
        if not _ws_clients:
            continue
        try:
            _ensure_db()
            today_iso = mc.now_ist().date().isoformat()
            payload: dict[str, dict] = {}
            for ch in ("ch1", "ch2", "ch2f", "ch3", "oeh", "oel"):
                cf = _ch_filter(ch)
                with db.get_conn() as conn:
                    row = conn.execute(f"""SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_count,
                        SUM(CASE WHEN status LIKE 'CLOSED%' THEN 1 ELSE 0 END) AS closed,
                        SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                        SUM(CASE WHEN pnl IS NOT NULL AND pnl <= 0 THEN 1 ELSE 0 END) AS losses,
                        COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl END),0) AS total_pnl,
                        COALESCE(SUM(CASE WHEN pnl IS NOT NULL AND ts >= '{today_iso}' THEN pnl END),0) AS today_pnl,
                        SUM(CASE WHEN ts >= '{today_iso}' THEN 1 ELSE 0 END) AS today_count
                    FROM trades WHERE {cf}""").fetchone()
                    s = dict(row)
                    closed = s["closed"] or 0
                    wins = s["wins"] or 0
                    payload[ch] = {
                        "today_pnl": round(s["today_pnl"], 2),
                        "today_count": s["today_count"] or 0,
                        "total_pnl": round(s["total_pnl"], 2),
                        "wins": wins, "losses": s["losses"] or 0,
                        "open": s["open_count"] or 0,
                        "closed": closed,
                        "win_rate": round(wins / closed * 100, 1) if closed > 0 else 0,
                    }
            await _ws_broadcast({"type": "tick", "channels": payload,
                                 "now": mc.now_ist().strftime("%Y-%m-%d %H:%M:%S IST")})
        except Exception:
            pass


@app.on_event("startup")
async def _start_ws_push():
    asyncio.create_task(_ws_push_loop())

_CHANNEL_FILTER_BASE = "(symbol LIKE '% % CE' OR symbol LIKE '% % PE')"

# Channel 1 = old trades (channel IS NULL or 'ch1'), Channel 2 = 'ch2'
_CH_FILTERS = {
    "ch1": f"({_CHANNEL_FILTER_BASE} AND (channel IS NULL OR channel = 'ch1'))",
    "ch1b": f"({_CHANNEL_FILTER_BASE} AND channel = 'ch1b')",
    "ch2": f"({_CHANNEL_FILTER_BASE} AND channel = 'ch2' AND ts >= '2026-08-20')",
    "ch3": f"({_CHANNEL_FILTER_BASE} AND channel = 'ch3')",
    "ch5": f"({_CHANNEL_FILTER_BASE} AND channel = 'ch5')",
    "oeh": f"({_CHANNEL_FILTER_BASE} AND channel = 'oeh')",
    "oel": f"({_CHANNEL_FILTER_BASE} AND channel = 'oel')",
    "ch2f": f"({_CHANNEL_FILTER_BASE} AND channel = 'ch2f')",
    "ch1f": f"({_CHANNEL_FILTER_BASE} AND (channel IS NULL OR channel = 'ch1') AND filter_score >= 50)",
}


def _ch_filter(channel: str) -> str:
    return _CH_FILTERS.get(channel, _CH_FILTERS["ch1"])


def _ensure_db():
    global _db_ready
    if not _db_ready:
        db.init_db()
        with db.get_conn() as conn:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_channel ON trades(channel)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts)")
        _db_ready = True


def _rows(query: str, params: tuple = ()) -> list[dict[str, Any]]:
    _ensure_db()
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


@app.get("/api/trades")
def api_trades(channel: str = "ch1") -> JSONResponse:
    _ensure_db()
    cf = _ch_filter(channel)
    rows = _rows(
        f"SELECT id, ts, symbol, side, qty, price, exit_price, pnl, mode, status, "
        f"stop_price, target_price, index_entry, broker_key, charges "
        f"FROM trades WHERE {cf} ORDER BY id DESC LIMIT 100")
    return JSONResponse(rows)


@app.get("/api/stats")
def api_stats(channel: str = "ch1") -> JSONResponse:
    _ensure_db()
    cf = _ch_filter(channel)
    today_iso = mc.now_ist().date().isoformat()
    with db.get_conn() as conn:
        row = conn.execute(f"""SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_count,
            SUM(CASE WHEN status LIKE 'CLOSED%' THEN 1 ELSE 0 END) AS closed,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN pnl IS NOT NULL AND pnl <= 0 THEN 1 ELSE 0 END) AS losses,
            COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl END),0) AS total_pnl,
            COALESCE(MAX(pnl),0) AS best,
            COALESCE(MIN(CASE WHEN pnl IS NOT NULL THEN pnl END),0) AS worst,
            COALESCE(AVG(CASE WHEN pnl IS NOT NULL THEN pnl END),0) AS avg_pnl,
            COALESCE(SUM(CASE WHEN pnl IS NOT NULL AND ts >= '{today_iso}' THEN pnl END),0) AS today_pnl,
            SUM(CASE WHEN ts >= '{today_iso}' THEN 1 ELSE 0 END) AS today_count,
            COALESCE(SUM(CASE WHEN charges IS NOT NULL THEN charges END),0) AS total_charges,
            COALESCE(SUM(CASE WHEN charges IS NOT NULL AND ts >= '{today_iso}' THEN charges END),0) AS today_charges,
            COALESCE(SUM(CASE WHEN status='OPEN' THEN price * qty END),0) AS utilized
        FROM trades WHERE {cf}""").fetchone()
        s = dict(row)
        pnl_series = [dict(r) for r in conn.execute(
            f"SELECT ts, pnl FROM trades WHERE {cf} AND pnl IS NOT NULL ORDER BY id"
        ).fetchall()]

    closed = s["closed"] or 0
    wins = s["wins"] or 0
    win_rate = (wins / closed * 100) if closed > 0 else 0
    cumulative = []
    running = 0
    for r in pnl_series:
        running += r["pnl"]
        cumulative.append({"ts": r["ts"], "pnl": r["pnl"], "cumulative": round(running, 2)})

    return JSONResponse({
        "total": s["total"], "open": s["open_count"] or 0, "closed": closed,
        "wins": wins, "losses": s["losses"] or 0, "win_rate": round(win_rate, 1),
        "total_pnl": round(s["total_pnl"], 2), "best_trade": round(s["best"], 2),
        "worst_trade": round(s["worst"], 2), "avg_pnl": round(s["avg_pnl"], 2),
        "today_pnl": round(s["today_pnl"], 2), "today_count": s["today_count"] or 0,
        "total_charges": round(s["total_charges"], 2), "today_charges": round(s["today_charges"], 2),
        "capital": 100000, "utilized": round(s["utilized"], 2),
        "pnl_curve": cumulative,
        "now": mc.now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
    })


@app.get("/api/all")
def api_all(channel: str = "ch1") -> JSONResponse:
    """Combined stats + trades in one call to reduce round trips."""
    cache_key = f"all:{channel}"
    now = _time.monotonic()
    cached = _api_cache.get(cache_key)
    if cached and now - cached[0] < _API_CACHE_TTL:
        return JSONResponse(cached[1])
    _ensure_db()
    cf = _ch_filter(channel)
    today_iso = mc.now_ist().date().isoformat()
    with db.get_conn() as conn:
        stats_row = conn.execute(f"""SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_count,
            SUM(CASE WHEN status LIKE 'CLOSED%' THEN 1 ELSE 0 END) AS closed,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN pnl IS NOT NULL AND pnl <= 0 THEN 1 ELSE 0 END) AS losses,
            COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl END),0) AS total_pnl,
            COALESCE(MAX(pnl),0) AS best,
            COALESCE(MIN(CASE WHEN pnl IS NOT NULL THEN pnl END),0) AS worst,
            COALESCE(AVG(CASE WHEN pnl IS NOT NULL THEN pnl END),0) AS avg_pnl,
            COALESCE(SUM(CASE WHEN pnl IS NOT NULL AND ts >= '{today_iso}' THEN pnl END),0) AS today_pnl,
            SUM(CASE WHEN ts >= '{today_iso}' THEN 1 ELSE 0 END) AS today_count,
            COALESCE(SUM(CASE WHEN charges IS NOT NULL THEN charges END),0) AS total_charges,
            COALESCE(SUM(CASE WHEN charges IS NOT NULL AND ts >= '{today_iso}' THEN charges END),0) AS today_charges,
            COALESCE(SUM(CASE WHEN status='OPEN' THEN price * qty END),0) AS utilized
        FROM trades WHERE {cf}""").fetchone()
        s = dict(stats_row)
        pnl_series = [dict(r) for r in conn.execute(
            f"SELECT ts, pnl FROM trades WHERE {cf} AND pnl IS NOT NULL ORDER BY id"
        ).fetchall()]
        trades = [dict(r) for r in conn.execute(
            f"SELECT id, ts, symbol, side, qty, price, exit_price, pnl, mode, status, "
            f"stop_price, target_price, index_entry, broker_key, charges "
            f"FROM trades WHERE {cf} ORDER BY id DESC LIMIT 100"
        ).fetchall()]

    closed = s["closed"] or 0
    wins = s["wins"] or 0
    win_rate = (wins / closed * 100) if closed > 0 else 0
    cumulative = []
    running = 0
    for r in pnl_series:
        running += r["pnl"]
        cumulative.append({"ts": r["ts"], "pnl": r["pnl"], "cumulative": round(running, 2)})

    payload = {
        "stats": {
            "total": s["total"], "open": s["open_count"] or 0, "closed": closed,
            "wins": wins, "losses": s["losses"] or 0, "win_rate": round(win_rate, 1),
            "total_pnl": round(s["total_pnl"], 2), "best_trade": round(s["best"], 2),
            "worst_trade": round(s["worst"], 2), "avg_pnl": round(s["avg_pnl"], 2),
            "today_pnl": round(s["today_pnl"], 2), "today_count": s["today_count"] or 0,
            "total_charges": round(s["total_charges"], 2), "today_charges": round(s["today_charges"], 2),
            "capital": 100000, "utilized": round(s["utilized"], 2),
            "pnl_curve": cumulative,
            "now": mc.now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        },
        "trades": trades,
    }
    _api_cache[cache_key] = (now, payload)
    return JSONResponse(payload)


@app.get("/api/ltp")
def api_ltp(channel: str = "ch1") -> JSONResponse:
    """Fetch live LTP for all open channel trades that have a broker_key."""
    _ensure_db()
    cf = _ch_filter(channel)
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, broker_key FROM trades WHERE {cf} "
            "AND status = 'OPEN' AND broker_key IS NOT NULL"
        ).fetchall()
    if not rows:
        return JSONResponse({})
    try:
        import requests as _req
        keys = {r["broker_key"]: r["id"] for r in rows}
        url = "https://api.upstox.com/v2/market-quote/ltp"
        from src.broker.upstox_data import load_cached_token
        token = load_cached_token()
        if not token:
            return JSONResponse({"error": "no token"}, status_code=503)
        resp = _req.get(url, params={"instrument_key": ",".join(keys.keys())},
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                        timeout=10)
        data = resp.json().get("data", {})
        result: dict[str, float] = {}
        for item in data.values():
            ikey = item.get("instrument_token", "")
            tid = keys.get(ikey)
            if tid is not None:
                result[str(tid)] = item.get("last_price", 0)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/ml")
def api_ml() -> JSONResponse:
    try:
        _ensure_db()
        with db.get_conn() as conn:
            tbl = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ml_signals'"
            ).fetchone()
            if not tbl:
                return JSONResponse({"enabled": False})
            today = mc.now_ist().date().isoformat()
            sigs = [dict(r) for r in conn.execute(
                "SELECT * FROM ml_signals WHERE date=? ORDER BY id DESC", (today,)
            ).fetchall()]
            closed = [s for s in sigs if s["status"] == "CLOSED"]
            wins = sum(1 for s in closed if (s["pnl"] or 0) > 0)
            total_pnl = sum(s["pnl"] or 0 for s in closed)
            return JSONResponse({
                "enabled": True, "date": today, "signals": sigs,
                "open_count": sum(1 for s in sigs if s["status"] == "OPEN"),
                "closed_count": len(closed),
                "summary": {
                    "total_trades": len(closed),
                    "wins": wins, "losses": len(closed) - wins,
                    "win_rate": f"{wins/len(closed)*100:.0f}%" if closed else "—",
                    "net_pnl": round(total_pnl, 2),
                    "open_trades": sum(1 for s in sigs if s["status"] == "OPEN"),
                },
            })
    except Exception:  # noqa: BLE001
        return JSONResponse({"enabled": False})


@app.get("/api/strategy/summary")
def api_strategy_summary(days: int = 30) -> JSONResponse:
    cache_key = f"strat_sum:{days}"
    now = _time.monotonic()
    cached = _api_cache.get(cache_key)
    if cached and now - cached[0] < 30:
        return JSONResponse(cached[1])
    try:
        from src.strategy.live_runner import get_daily_summary
        payload = get_daily_summary(days)
        _api_cache[cache_key] = (now, payload)
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/strategy/today")
def api_strategy_today() -> JSONResponse:
    try:
        from src.strategy.live_runner import get_today_detail
        return JSONResponse(get_today_detail())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/strategy/history")
def api_strategy_history(days: int = 30) -> JSONResponse:
    try:
        from src.strategy.live_runner import get_history
        return JSONResponse(get_history(days))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/strategy/run")
def api_strategy_run(run_date: str = None) -> JSONResponse:
    """Trigger strategy run for a specific date (or today)."""
    try:
        from datetime import date as _date
        from src.strategy.live_runner import run_day
        d = _date.fromisoformat(run_date) if run_date else mc.now_ist().date()
        res = run_day(d, lots=1, force=True)
        summary = {s: {"day_pnl": data["day_pnl"]} for s, data in res.items()}
        return JSONResponse({"date": d.isoformat(), "results": summary})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/strategy/backfill")
def api_strategy_backfill(from_date: str = None, to_date: str = None) -> JSONResponse:
    """Backfill strategy results for a date range."""
    try:
        from datetime import date as _date
        from src.strategy.live_runner import backfill
        fd = _date.fromisoformat(from_date) if from_date else _date.today() - __import__('datetime').timedelta(days=30)
        td = _date.fromisoformat(to_date) if to_date else _date.today()
        backfill(fd, td)
        return JSONResponse({"status": "ok", "from": fd.isoformat(), "to": td.isoformat()})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Stock Strategy API endpoints
# ---------------------------------------------------------------------------
@app.get("/api/stock-strategy/summary")
def api_stock_strategy_summary(days: int = 90) -> JSONResponse:
    cache_key = f"stock_sum:{days}"
    now = _time.monotonic()
    cached = _api_cache.get(cache_key)
    if cached and now - cached[0] < 30:
        return JSONResponse(cached[1])
    try:
        from src.strategy.stock_runner import get_daily_summary
        payload = get_daily_summary(days)
        _api_cache[cache_key] = (now, payload)
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/stock-strategy/today")
def api_stock_strategy_today() -> JSONResponse:
    try:
        from src.strategy.stock_runner import get_today_detail
        return JSONResponse(get_today_detail())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.get("/api/stock-strategy/stocks")
def api_stock_strategy_stocks(days: int = 90) -> JSONResponse:
    try:
        from src.strategy.stock_runner import get_stock_summary
        return JSONResponse(get_stock_summary(days))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@app.post("/api/stock-strategy/backfill")
def api_stock_strategy_backfill(from_date: str = None, to_date: str = None) -> JSONResponse:
    try:
        from datetime import date as _date
        from src.strategy.stock_runner import backfill
        fd = _date.fromisoformat(from_date) if from_date else _date.today() - __import__('datetime').timedelta(days=30)
        td = _date.fromisoformat(to_date) if to_date else _date.today()
        backfill(fd, td)
        return JSONResponse({"status": "ok", "from": fd.isoformat(), "to": td.isoformat()})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


_ML_EMPTY = lambda: JSONResponse({"stats": {
    "total": 0, "open": 0, "closed": 0, "wins": 0, "losses": 0,
    "win_rate": 0, "total_pnl": 0, "best_trade": 0, "worst_trade": 0,
    "avg_pnl": 0, "today_pnl": 0, "today_count": 0,
    "total_charges": 0, "today_charges": 0, "capital": 0, "utilized": 0,
    "pnl_curve": [], "now": mc.now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
}, "trades": []})


@app.get("/api/ml/all")
def api_ml_all() -> JSONResponse:
    """ML signals shaped like /api/all so the ML tab renders identically to channel tabs."""
    try:
        _ensure_db()
        with db.get_conn() as conn:
            # Check if ml_signals table exists
            tbl = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ml_signals'"
            ).fetchone()
            if not tbl:
                return _ML_EMPTY()

            today_iso = mc.now_ist().date().isoformat()
            row = conn.execute(f"""SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_count,
                SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) AS closed,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN pnl IS NOT NULL AND pnl <= 0 THEN 1 ELSE 0 END) AS losses,
                COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl END),0) AS total_pnl,
                COALESCE(MAX(pnl),0) AS best,
                COALESCE(MIN(CASE WHEN pnl IS NOT NULL THEN pnl END),0) AS worst,
                COALESCE(AVG(CASE WHEN pnl IS NOT NULL THEN pnl END),0) AS avg_pnl,
                COALESCE(SUM(CASE WHEN pnl IS NOT NULL AND date='{today_iso}' THEN pnl END),0) AS today_pnl,
                SUM(CASE WHEN date='{today_iso}' THEN 1 ELSE 0 END) AS today_count,
                COALESCE(SUM(CASE WHEN status='OPEN' THEN entry*qty END),0) AS utilized
            FROM ml_signals""").fetchone()
            s = dict(row)
            pnl_series = [dict(r) for r in conn.execute(
                "SELECT ts, pnl FROM ml_signals WHERE pnl IS NOT NULL AND status='CLOSED' ORDER BY id"
            ).fetchall()]
            signals = [dict(r) for r in conn.execute(
                "SELECT * FROM ml_signals ORDER BY id DESC LIMIT 100"
            ).fetchall()]

        closed = s["closed"] or 0
        wins = s["wins"] or 0
        win_rate = (wins / closed * 100) if closed > 0 else 0
        cumulative, running = [], 0
        for r in pnl_series:
            running += r["pnl"]
            cumulative.append({"ts": r["ts"], "pnl": r["pnl"], "cumulative": round(running, 2)})

        trades = []
        for sig in signals:
            trades.append({
                "id": sig["id"], "ts": sig["ts"],
                "symbol": f"{sig['index_sym']} PE {sig['strike']}",
                "side": "BUY", "qty": sig["qty"],
                "price": sig["entry"], "exit_price": sig.get("exit_price"),
                "pnl": sig.get("pnl"), "mode": "ML",
                "status": sig["status"],
                "stop_price": sig["sl"], "target_price": sig["tgt"],
                "index_entry": sig["spot"], "broker_key": None, "charges": None,
                "confidence": sig["confidence"],
                "exit_reason": sig.get("exit_reason"),
            })

        return JSONResponse({
            "stats": {
                "total": s["total"], "open": s["open_count"] or 0, "closed": closed,
                "wins": wins, "losses": s["losses"] or 0, "win_rate": round(win_rate, 1),
                "total_pnl": round(s["total_pnl"], 2),
                "best_trade": round(s["best"], 2), "worst_trade": round(s["worst"], 2),
                "avg_pnl": round(s["avg_pnl"], 2),
                "today_pnl": round(s["today_pnl"], 2), "today_count": s["today_count"] or 0,
                "total_charges": 0, "today_charges": 0,
                "capital": 0, "utilized": round(s["utilized"], 2),
                "pnl_curve": cumulative,
                "now": mc.now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
            },
            "trades": trades,
        })
    except Exception as exc:  # noqa: BLE001
        return _ML_EMPTY()


@app.get("/api/scan")
def api_scan() -> JSONResponse:
    """Run the market scanner and return today's top signals."""
    try:
        from src.signals.market_scanner import MarketScanner
        scanner = MarketScanner()
        signals = scanner.scan()
        return JSONResponse([{
            "symbol": s.symbol,
            "option_type": s.option_type,
            "confidence": s.confidence,
            "strategy": s.strategy,
            "reasons": s.reasons,
            "entry_window": s.entry_window,
        } for s in signals])
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/close/{trade_id}")
def api_close_trade(trade_id: int) -> JSONResponse:
    _ensure_db()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, price, qty, broker_key FROM trades WHERE id = ? AND status = 'OPEN'",
            (trade_id,)).fetchone()
        if not row:
            return JSONResponse({"closed": False, "reason": "Trade not found or already closed"}, status_code=404)

    ltp = None
    if row["broker_key"]:
        try:
            from src.broker.upstox_data import UpstoxData
            ud = UpstoxData()
            ltp_data = ud._get("/v2/market-quote/ltp",
                               params={"instrument_key": row["broker_key"]}).get("data", {})
            for item in ltp_data.values():
                ltp = item.get("last_price")
                break
        except Exception:
            pass

    if not ltp:
        return JSONResponse({"closed": False, "reason": "Could not fetch LTP"}, status_code=503)

    exit_price = float(ltp)
    gross_pnl = (exit_price - row["price"]) * row["qty"]
    from src.notify.channel_listener import calc_charges
    charges = calc_charges(row["price"], exit_price, row["qty"])
    net_pnl = gross_pnl - charges["total"]

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE trades SET status='CLOSED', exit_price=?, pnl=?, charges=? WHERE id=?",
            (exit_price, net_pnl, charges["total"], trade_id))
    return JSONResponse({"closed": True, "trade_id": trade_id,
                         "exit_price": exit_price, "pnl": round(net_pnl, 2)})


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _PAGE


_PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,user-scalable=no">
<title>Trading Buddy</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0b0f14">
<style>
:root{
  --bg:#0b0f14;--sf:#121820;--el:#1a2230;--bd:#232d3d;
  --tx:#dfe6ee;--mt:#6b7a8d;--ft:#3a4858;
  --gn:#22c55e;--gd:rgba(34,197,94,.12);
  --rd:#ef4444;--rdd:rgba(239,68,68,.12);
  --bl:#3b82f6;--bld:rgba(59,130,246,.1);
  --am:#f59e0b;--amd:rgba(245,158,11,.12);
  --cy:#06b6d4;--cyd:rgba(6,182,212,.12);
  --pp:#a855f7;--ppd:rgba(168,85,247,.12);
  --mn:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sn:-apple-system,system-ui,Segoe UI,Roboto,sans-serif;
}
@media(prefers-color-scheme:light){:root{
  --bg:#f0f2f5;--sf:#fff;--el:#fff;--bd:#e2e8f0;
  --tx:#1e293b;--mt:#64748b;--ft:#cbd5e1;
  --gn:#16a34a;--gd:rgba(22,163,74,.08);
  --rd:#dc2626;--rdd:rgba(220,38,38,.08);
  --bl:#2563eb;--bld:rgba(37,99,235,.06);
  --am:#d97706;--amd:rgba(217,119,6,.08);
  --cy:#0891b2;--cyd:rgba(8,145,178,.08);
  --pp:#9333ea;--ppd:rgba(147,51,234,.08);
}}
:root[data-theme=light]{
  --bg:#f0f2f5;--sf:#fff;--el:#fff;--bd:#e2e8f0;
  --tx:#1e293b;--mt:#64748b;--ft:#cbd5e1;
  --gn:#16a34a;--gd:rgba(22,163,74,.08);
  --rd:#dc2626;--rdd:rgba(220,38,38,.08);
  --bl:#2563eb;--bld:rgba(37,99,235,.06);
  --am:#d97706;--amd:rgba(217,119,6,.08);
  --cy:#0891b2;--cyd:rgba(8,145,178,.08);
  --pp:#9333ea;--ppd:rgba(147,51,234,.08);
}
:root[data-theme=dark]{
  --bg:#0b0f14;--sf:#121820;--el:#1a2230;--bd:#232d3d;
  --tx:#dfe6ee;--mt:#6b7a8d;--ft:#3a4858;
  --gn:#22c55e;--gd:rgba(34,197,94,.12);
  --rd:#ef4444;--rdd:rgba(239,68,68,.12);
  --bl:#3b82f6;--bld:rgba(59,130,246,.1);
  --am:#f59e0b;--amd:rgba(245,158,11,.12);
  --cy:#06b6d4;--cyd:rgba(6,182,212,.12);
  --pp:#a855f7;--ppd:rgba(168,85,247,.12);
}

*{box-sizing:border-box;margin:0;-webkit-tap-highlight-color:transparent}
body{font-family:var(--sn);background:var(--bg);color:var(--tx);padding:0;
  line-height:1.5;-webkit-font-smoothing:antialiased;overflow-x:hidden}
.wrap{max-width:480px;margin:0 auto;padding:0 0 80px}

/* Header */
.hdr{background:var(--sf);border-bottom:1px solid var(--bd);padding:12px 16px;
  position:sticky;top:0;z-index:100;backdrop-filter:blur(12px)}
.hdr-top{display:flex;align-items:center;gap:8px}
.hdr h1{font-size:18px;font-weight:800;letter-spacing:-.02em;flex:1}
.hdr h1 span{color:var(--bl)}
.live-dot{width:8px;height:8px;border-radius:50%;background:var(--gn);
  box-shadow:0 0 6px var(--gn);animation:pulse 2s infinite}
.live-dot.off{background:var(--rd);box-shadow:none;animation:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.hdr-time{font-size:10px;color:var(--mt);font-family:var(--mn)}

/* Channel tabs — horizontal scroll on mobile */
.tabs{display:flex;gap:0;overflow-x:auto;-webkit-overflow-scrolling:touch;
  scrollbar-width:none;padding:8px 16px 0;background:var(--sf);border-bottom:1px solid var(--bd)}
.tabs::-webkit-scrollbar{display:none}
.tab{flex-shrink:0;padding:8px 14px;font-size:12px;font-weight:700;cursor:pointer;
  border:none;background:none;color:var(--mt);border-bottom:2px solid transparent;
  margin-bottom:-1px;font-family:var(--sn);transition:all .15s;white-space:nowrap}
.tab.active{color:var(--bl);border-bottom-color:var(--bl)}
.tab .ico{margin-right:3px}
.ws-badge{margin-left:4px;font-size:9px;font-family:var(--mn);font-weight:700;opacity:.8}
.ws-badge.pos{color:var(--gn)}.ws-badge.neg{color:var(--rd)}

/* Hero P&L card */
.hero{padding:14px 16px;display:flex;gap:10px;align-items:center;overflow:hidden}
.hero-pnl{flex:1;min-width:0}
.hero-pnl .label{font-size:10px;color:var(--mt);text-transform:uppercase;letter-spacing:.08em;font-weight:600}
.hero-pnl .val{font-size:28px;font-weight:800;font-family:var(--mn);font-variant-numeric:tabular-nums;
  letter-spacing:-.02em;line-height:1.1;margin:2px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hero-pnl .sub{font-size:10px;color:var(--mt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hero-ring{width:64px;height:64px;position:relative;flex-shrink:0}
.hero-ring canvas{width:64px;height:64px;display:block}
.hero-ring .ring-txt{position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;font-family:var(--mn)}
.hero-ring .ring-pct{font-size:16px;font-weight:800}
.hero-ring .ring-sub{font-size:7px;color:var(--mt);text-transform:uppercase;letter-spacing:.05em}

/* Stat chips row — swipeable */
.chips{display:flex;gap:8px;padding:0 16px 12px;overflow-x:auto;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex-shrink:0;background:var(--sf);border:1px solid var(--bd);border-radius:10px;
  padding:8px 12px;min-width:100px}
.chip .cl{font-size:9px;color:var(--mt);text-transform:uppercase;letter-spacing:.06em;font-weight:600}
.chip .cv{font-size:16px;font-weight:800;font-family:var(--mn);font-variant-numeric:tabular-nums;margin-top:1px}
.chip .cd{font-size:9px;color:var(--mt);margin-top:1px}

/* Sections */
.sec{padding:0 16px;margin-bottom:14px}
.sec-h{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
  color:var(--mt);margin-bottom:6px;display:flex;align-items:center;gap:6px}
.badge{background:var(--bld);color:var(--bl);font-size:10px;padding:2px 7px;
  border-radius:8px;font-weight:700}

/* Chart */
.cw{background:var(--sf);border:1px solid var(--bd);border-radius:12px;padding:10px;position:relative}
.cw canvas{width:100%;height:120px;display:block}
.cl-label{position:absolute;top:8px;right:10px;font-size:10px;color:var(--mt);font-family:var(--mn)}

/* Cards for open positions (mobile-friendly) */
.pos-card{background:var(--sf);border:1px solid var(--bd);border-radius:12px;
  padding:12px;margin-bottom:8px}
.pos-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.pos-sym{font-size:13px;font-weight:700;font-family:var(--mn)}
.pos-pnl{font-size:15px;font-weight:800;font-family:var(--mn)}
.pos-row{display:flex;gap:8px;flex-wrap:wrap}
.pos-tag{font-size:10px;color:var(--mt);background:var(--el);border-radius:6px;padding:2px 8px}
.pos-tag b{color:var(--tx);font-weight:600}
.pos-close{width:100%;margin-top:8px;padding:8px;border:1px solid var(--rd);border-radius:8px;
  background:var(--rdd);color:var(--rd);font-size:12px;font-weight:700;cursor:pointer}
.pos-close:active{opacity:.7}

/* Trade history cards */
.trade-card{background:var(--sf);border:1px solid var(--bd);border-radius:10px;
  padding:10px 12px;margin-bottom:6px;cursor:pointer;transition:border-color .15s}
.trade-card:hover{border-color:var(--ac)}
.tc-top{display:flex;align-items:center;gap:10px}
.tc-icon{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;
  justify-content:center;font-size:14px;font-weight:800;flex-shrink:0}
.tc-icon.w{background:var(--gd);color:var(--gn)}
.tc-icon.l{background:var(--rdd);color:var(--rd)}
.tc-body{flex:1;min-width:0}
.tc-sym{font-size:12px;font-weight:700;font-family:var(--mn);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.tc-meta{font-size:10px;color:var(--mt)}
.tc-pnl{font-size:14px;font-weight:800;font-family:var(--mn);text-align:right;flex-shrink:0}
.tc-detail{display:none;margin-top:8px;padding-top:8px;border-top:1px solid var(--bd)}
.trade-card.expanded .tc-detail{display:block}
.tc-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px 12px}
.tc-item{font-size:11px;color:var(--mt)}
.tc-item b{color:var(--fg);font-weight:600;font-family:var(--mn)}
.tc-reason{display:inline-block;margin-top:6px;font-size:10px;font-weight:700;
  padding:2px 8px;border-radius:4px;text-transform:uppercase;letter-spacing:.5px}
.tc-reason.tgt{background:var(--gd);color:var(--gn)}
.tc-reason.sl{background:var(--rdd);color:var(--rd)}
.tc-reason.floor{background:#f0ad4e22;color:#f0ad4e}
.tc-reason.eod{background:var(--bd);color:var(--mt)}

/* Filter pills */
.fpills{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.fpill{padding:5px 12px;border:1px solid var(--bd);border-radius:20px;
  background:var(--sf);color:var(--mt);font-size:11px;cursor:pointer;font-weight:600}
.fpill.a{background:var(--bld);color:var(--bl);border-color:var(--bl)}

/* Scanner panel */
.scan-panel{background:var(--sf);border:1px solid var(--bd);border-radius:12px;padding:12px;margin-bottom:8px}
.scan-hdr{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.scan-hdr .scan-title{font-size:12px;font-weight:700;flex:1}
.scan-btn{padding:6px 14px;border:1px solid var(--cy);border-radius:8px;background:var(--cyd);
  color:var(--cy);font-size:11px;font-weight:700;cursor:pointer}
.scan-btn:active{opacity:.7}
.scan-card{background:var(--el);border-radius:8px;padding:10px;margin-bottom:6px}
.scan-card:last-child{margin-bottom:0}
.scan-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.scan-sym{font-size:13px;font-weight:800;font-family:var(--mn)}
.scan-conf{font-size:11px;font-weight:700;font-family:var(--mn);padding:2px 8px;
  border-radius:6px}
.scan-conf.hi{background:var(--gd);color:var(--gn)}
.scan-conf.md{background:var(--amd);color:var(--am)}
.scan-conf.lo{background:var(--rdd);color:var(--rd)}
.scan-strat{font-size:10px;color:var(--cy);font-weight:600;margin-bottom:4px}
.scan-reasons{font-size:10px;color:var(--mt);line-height:1.4}

.pill{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:700}
.pill.op{background:var(--amd);color:var(--am)}
.pill.cl{background:var(--gd);color:var(--gn)}
.pill.sl{background:var(--rdd);color:var(--rd)}

.empty{padding:28px;text-align:center;color:var(--mt);font-size:12px;
  background:var(--sf);border-radius:12px;border:1px solid var(--bd)}

/* Strategy cards */
.str-card{background:var(--sf);border:1px solid var(--bd);border-radius:12px;padding:12px}
.str-card.best{border-color:var(--gn);border-width:2px}
.str-name{font-size:12px;font-weight:800;font-family:var(--mn);margin-bottom:4px;display:flex;align-items:center;gap:6px}
.str-pnl{font-size:22px;font-weight:800;font-family:var(--mn);font-variant-numeric:tabular-nums}
.str-stats{display:flex;gap:8px;margin-top:6px;flex-wrap:wrap}
.str-stat{font-size:10px;color:var(--mt);background:var(--el);border-radius:6px;padding:2px 8px}
.str-stat b{color:var(--tx);font-weight:600}
.str-badge{font-size:9px;padding:2px 6px;border-radius:4px;font-weight:700}
.str-badge.a{background:var(--gd);color:var(--gn)}
.str-badge.b{background:var(--amd);color:var(--am)}
.str-badge.c{background:var(--rdd);color:var(--rd)}
.str-today-card{background:var(--sf);border:1px solid var(--bd);border-radius:10px;padding:10px 12px;margin-bottom:6px}
.str-idx-row{display:flex;justify-content:space-between;align-items:center}
.str-idx-name{font-size:12px;font-weight:700;font-family:var(--mn)}
.str-idx-pnl{font-size:14px;font-weight:800;font-family:var(--mn)}
.str-idx-meta{font-size:10px;color:var(--mt);margin-top:2px}
.str-day-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--bd)}
.str-day-row:last-child{border-bottom:none}
.str-day-date{font-size:11px;font-family:var(--mn);width:70px;flex-shrink:0;color:var(--mt)}
.str-day-bar{flex:1;height:18px;border-radius:4px;position:relative;overflow:hidden}
.str-day-fill{height:100%;border-radius:4px;min-width:2px}
.str-day-val{font-size:11px;font-family:var(--mn);font-weight:700;width:65px;text-align:right;flex-shrink:0}

.pos{color:var(--gn)}.neg{color:var(--rd)}

/* Skeleton loading */
@keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}
.skel{border-radius:6px;background:linear-gradient(90deg,var(--el) 25%,var(--sf) 50%,var(--el) 75%);background-size:200% 100%;animation:shimmer 1.5s infinite}

/* Bottom nav */
.bnav{position:fixed;bottom:0;left:0;right:0;background:var(--sf);border-top:1px solid var(--bd);
  display:flex;z-index:100;padding-bottom:env(safe-area-inset-bottom)}
.bnav button{flex:1;padding:8px 0 6px;border:none;background:none;color:var(--mt);
  font-size:9px;font-weight:600;cursor:pointer;display:flex;flex-direction:column;
  align-items:center;gap:2px;font-family:var(--sn)}
.bnav button.active{color:var(--bl)}
.bnav .nav-ico{font-size:18px}

@media(min-width:600px){
  .wrap{max-width:520px}
  .hero-pnl .val{font-size:36px}
}
</style></head><body>

<div class=hdr>
  <div class=hdr-top>
    <h1>Trading <span>Buddy</span></h1>
    <div class="live-dot" id=sd></div>
    <span class=hdr-time id=ck></span>
  </div>
</div>

<div class=tabs id=tabbar>
  <button class="tab active" onclick="switchCh('ch1')" id="tab-ch1"><span class=ico>1</span> Paid</button>
  <button class="tab" onclick="switchCh('ch2')" id="tab-ch2"><span class=ico>2</span> G Prime</button>
  <button class="tab" onclick="switchCh('ch2f')" id="tab-ch2f"><span class=ico>F</span> CH2 Filtered</button>
  <button class="tab" onclick="switchCh('oeh')" id="tab-oeh"><span class=ico>O</span> OEH</button>
  <button class="tab" onclick="switchCh('oel')" id="tab-oel"><span class=ico>L</span> OEL</button>
  <button class="tab" onclick="switchCh('strat')" id="tab-strat"><span class=ico>S</span> Strategy</button>
  <button class="tab" onclick="switchCh('stocks')" id="tab-stocks"><span class=ico>$</span> Stocks</button>
</div>

<div class=wrap>

<!-- Hero P&L -->
<div class=hero>
  <div class=hero-pnl>
    <div class=label>Today's P&L</div>
    <div class="val" id=hv>-</div>
    <div class=sub id=hs></div>
  </div>
  <div class=hero-ring>
    <canvas id=ring width=128 height=128></canvas>
    <div class=ring-txt>
      <div class=ring-pct id=rp>-</div>
      <div class=ring-sub>Win Rate</div>
    </div>
  </div>
</div>

<!-- Stat chips -->
<div class=chips id=chips></div>

<!-- Scanner signals (only visible on ch5 tab) -->
<div class=sec id=scanSec style="display:none">
  <div class=sec-h>Scanner Signals</div>
  <div id=scanList></div>
</div>

<!-- Equity curve -->
<div class=sec>
  <div class=sec-h>Equity Curve</div>
  <div class=cw><canvas id=cv></canvas><div class="cl-label" id=cvl></div></div>
</div>

<!-- Open positions -->
<div class=sec>
  <div class=sec-h>Open Positions <span class=badge id=oc>0</span></div>
  <div id=ow></div>
</div>

<!-- Trade history -->
<div class=sec>
  <div class=sec-h>Trades <span class=badge id=hc>0</span></div>
  <div class=fpills id=fb></div>
  <div id=hw></div>
</div>

</div>

<!-- Strategy view (hidden by default) -->
<div class=wrap id=stratView style="display:none">

<div class=sec>
  <div class=sec-h>Strategy comparison <span class=badge id=stratDays>-</span></div>
  <div class=fpills id=stratPills></div>
</div>

<!-- Strategy summary cards -->
<div style="padding:0 16px 12px">
  <div id=stratCards style="display:grid;grid-template-columns:1fr;gap:8px"></div>
</div>

<!-- Strategy equity curves -->
<div class=sec>
  <div class=sec-h>Equity curves</div>
  <div class=cw style="height:160px"><canvas id=stratChart></canvas></div>
</div>

<!-- Today's trades detail -->
<div class=sec>
  <div class=sec-h>Today's breakdown</div>
  <div id=stratToday></div>
</div>

<!-- Daily history table -->
<div class=sec>
  <div class=sec-h>Daily log</div>
  <div id=stratLog style="max-height:400px;overflow-y:auto"></div>
</div>

</div>

<!-- Stocks Strategy View -->
<div class=wrap id=stocksView style="display:none">

<!-- Strategy comparison -->
<div class=sec>
  <div class=sec-h>Stock Credit Spreads <span class=badge id=stockDays>-</span></div>
  <div class=fpills id=stockPills></div>
</div>

<!-- Strategy cards -->
<div class=sec>
  <div id=stockCards style="display:grid;grid-template-columns:1fr;gap:8px"></div>
</div>

<!-- Equity curves -->
<div class=sec>
  <div class=sec-h>Equity Curves</div>
  <div class=cw style="height:160px"><canvas id=stockChart></canvas></div>
</div>

<!-- Per-stock breakdown -->
<div class=sec>
  <div class=sec-h>Stock Breakdown</div>
  <div id=stockBreakdown style="max-height:350px;overflow-y:auto"></div>
</div>

<!-- Today's trades -->
<div class=sec>
  <div class=sec-h>Today's trades</div>
  <div id=stockToday></div>
</div>

<!-- Daily log -->
<div class=sec>
  <div class=sec-h>Daily log</div>
  <div id=stockLog style="max-height:400px;overflow-y:auto"></div>
</div>

</div>

<!-- Bottom nav -->
<div class=bnav>
  <button class=active id=nav-trades onclick="switchView('trades')"><span class=nav-ico>&#9776;</span>Trades</button>
  <button id=nav-scan onclick="switchView('scan')"><span class=nav-ico>&#9881;</span>Scanner</button>
  <button id=nav-refresh onclick="_stratCacheTs=0;_scanCacheTs=0;_stocksCacheTs=0;load()"><span class=nav-ico>&#8635;</span>Refresh</button>
</div>

<script>
const $=id=>document.getElementById(id);
let AT=[],CF='all',LTP={},CH='ch1',VIEW='trades';
let _loading=false,_abortCtrl=null,_scanCache=null,_scanCacheTs=0,_refreshTimer=null;
let _stratCache=null,_stratCacheTs=0,_stratFocus='kitchen_sink';
let _stocksCache=null,_stocksCacheTs=0,_stocksFocus='ema20_rsi50';
const REFRESH_MS=30000,SCAN_CACHE_MS=300000,STRAT_CACHE_MS=120000,STOCKS_CACHE_MS=120000;

// WebSocket — live push updates
let _ws=null,_wsRetry=1000;
function wsConnect(){
  const proto=location.protocol==='https:'?'wss:':'ws:';
  _ws=new WebSocket(proto+'//'+location.host+'/ws');
  _ws.onopen=()=>{_wsRetry=1000;$('sd').className='live-dot';
    clearInterval(_refreshTimer);_refreshTimer=null};
  _ws.onmessage=e=>{
    try{
      const d=JSON.parse(e.data);
      if(d.type==='tick'&&d.channels&&d.channels[CH]&&CH!=='strat'&&CH!=='stocks'){
        const s=d.channels[CH];
        $('ck').textContent=d.now;
        // Update hero P&L instantly
        $('hv').textContent=inr(s.today_pnl);
        $('hv').className='val '+(s.today_pnl>=0?'pos':'neg');
        $('hs').textContent=s.today_count+' trades today | Total: '+inr(s.total_pnl);
        // Update win rate ring
        const wr=s.closed>0?s.win_rate:0;
        $('rp').textContent=s.closed>0?wr+'%':'--';
        $('rp').className='ring-pct '+(wr>=50?'pos':'neg');
        // Update tab badges
        for(const[ch,cs]of Object.entries(d.channels)){
          const tab=$('tab-'+ch);
          if(tab){
            const badge=tab.querySelector('.ws-badge');
            const pnl=cs.today_pnl;
            const txt=(pnl>=0?'+':'')+'₹'+Math.abs(Math.round(pnl)).toLocaleString('en-IN');
            if(badge){badge.textContent=txt;badge.className='ws-badge '+(pnl>=0?'pos':'neg')}
            else{const sp=document.createElement('span');sp.className='ws-badge '+(pnl>=0?'pos':'neg');sp.textContent=txt;tab.appendChild(sp)}
          }
        }
      }
    }catch(err){}
  };
  _ws.onclose=()=>{
    $('sd').className='live-dot off';
    setTimeout(wsConnect,Math.min(_wsRetry,10000));_wsRetry*=2;
    if(!_refreshTimer)startRefresh();
  };
  _ws.onerror=()=>{_ws.close()};
}
wsConnect();

function inr(v){if(v==null||isNaN(v))return'-';return(v<0?'-':'+')+'₹'+Math.abs(Math.round(v)).toLocaleString('en-IN')}
function inr0(v){if(v==null||isNaN(v))return'-';return'₹'+Math.abs(Math.round(v)).toLocaleString('en-IN')}
function pc(v){return v>0?'pos':v<0?'neg':''}
function tf(t){if(!t)return'-';const d=new Date(t),mm=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];return d.getDate()+' '+mm[d.getMonth()]+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')}

async function cp(id,sym){if(!confirm('Close '+sym+' at market?'))return;await fetch('/api/close/'+id,{method:'POST'});load()}

function switchView(v){
  VIEW=v;
  document.querySelectorAll('.bnav button').forEach(b=>b.classList.remove('active'));
  $('nav-'+v).classList.add('active');
  if(v==='scan'){switchCh('ch5')}
}

function rHero(s){
  const v=s.today_pnl;
  $('hv').textContent=inr(v);
  $('hv').className='val '+(v>=0?'pos':'neg');
  $('hs').textContent=s.today_count+' trades today | Total: '+inr(s.total_pnl);
}

function rRing(s){
  const c=$('ring'),x=c.getContext('2d'),dp=devicePixelRatio||1;
  c.width=128*dp;c.height=128*dp;x.scale(dp,dp);
  const cs=getComputedStyle(document.documentElement);
  const gn=cs.getPropertyValue('--gn').trim(),rd=cs.getPropertyValue('--rd').trim();
  const bd=cs.getPropertyValue('--bd').trim();
  const wr=s.closed>0?s.wins/s.closed:0;
  const cx=64,cy=64,r=26,lw=7;
  x.beginPath();x.arc(cx,cy,r,0,Math.PI*2);x.strokeStyle=bd;x.lineWidth=lw;x.stroke();
  if(s.closed>0){
    const start=-Math.PI/2,wEnd=start+wr*Math.PI*2;
    x.beginPath();x.arc(cx,cy,r,start,wEnd);x.strokeStyle=gn;x.lineWidth=lw;x.lineCap='round';x.stroke();
    if(wr<1){x.beginPath();x.arc(cx,cy,r,wEnd,start+Math.PI*2);x.strokeStyle=rd;x.lineWidth=lw;x.lineCap='round';x.stroke()}
  }
  $('rp').textContent=s.closed>0?s.win_rate+'%':'--';
  $('rp').className='ring-pct '+(s.win_rate>=50?'pos':'neg');
}

function rChips(s){
  const items=[
    {l:'Total P&L',v:inr(s.total_pnl),c:pc(s.total_pnl),d:s.closed+' closed'},
    {l:'Open',v:String(s.open),c:'',d:inr0(s.utilized)+' used'},
    {l:'Best',v:inr(s.best_trade),c:'pos',d:'single trade'},
    {l:'Worst',v:inr(s.worst_trade),c:'neg',d:'single trade'},
    {l:'Avg P&L',v:inr(s.avg_pnl),c:pc(s.avg_pnl),d:'per trade'},
    {l:'Charges',v:inr0(s.total_charges),c:'neg',d:'today '+inr0(s.today_charges)},
  ];
  $('chips').innerHTML=items.map(i=>
    '<div class=chip><div class=cl>'+i.l+'</div><div class="cv '+i.c+'">'+i.v+'</div><div class=cd>'+i.d+'</div></div>'
  ).join('');
}

function rC(curve){
  const c=$('cv'),x=c.getContext('2d'),dp=devicePixelRatio||1,r=c.getBoundingClientRect();
  c.width=r.width*dp;c.height=r.height*dp;x.scale(dp,dp);
  const W=r.width,H=r.height;
  if(!curve||curve.length<2){x.fillStyle=getComputedStyle(document.documentElement).getPropertyValue('--mt');x.font='11px system-ui';x.textAlign='center';x.fillText('Waiting for closed trades',W/2,H/2);return}
  const V=curve.map(c=>c.cumulative),mn=Math.min(0,...V),mx=Math.max(0,...V),rg=mx-mn||1;
  const p={t:12,b:20,l:40,r:8},cw=W-p.l-p.r,ch=H-p.t-p.b;
  const X=i=>p.l+(i/(V.length-1))*cw,Y=v=>p.t+ch-(((v-mn)/rg)*ch);
  const cs=getComputedStyle(document.documentElement);
  const gn=cs.getPropertyValue('--gn').trim(),rd=cs.getPropertyValue('--rd').trim();
  const mt=cs.getPropertyValue('--mt').trim(),bd=cs.getPropertyValue('--bd').trim();
  const lv=V[V.length-1],lc=lv>=0?gn:rd;
  x.strokeStyle=bd;x.lineWidth=.5;
  for(let i=0;i<=3;i++){const yy=p.t+(ch/3)*i;x.beginPath();x.moveTo(p.l,yy);x.lineTo(W-p.r,yy);x.stroke();
    x.fillStyle=mt;x.font='9px var(--mn)';x.textAlign='right';x.fillText(Math.round(mx-((mx-mn)/3)*i).toLocaleString('en-IN'),p.l-4,yy+3)}
  if(mn<0&&mx>0){x.strokeStyle=mt;x.lineWidth=.8;x.setLineDash([3,3]);x.beginPath();x.moveTo(p.l,Y(0));x.lineTo(W-p.r,Y(0));x.stroke();x.setLineDash([])}
  x.beginPath();x.moveTo(X(0),Y(0));for(let i=0;i<V.length;i++)x.lineTo(X(i),Y(V[i]));x.lineTo(X(V.length-1),Y(0));x.closePath();
  const g=x.createLinearGradient(0,p.t,0,H-p.b);g.addColorStop(0,lc+'40');g.addColorStop(1,lc+'05');x.fillStyle=g;x.fill();
  x.beginPath();x.moveTo(X(0),Y(V[0]));for(let i=1;i<V.length;i++)x.lineTo(X(i),Y(V[i]));x.strokeStyle=lc;x.lineWidth=2;x.lineJoin='round';x.stroke();
  const ex=X(V.length-1),ey=Y(lv);x.beginPath();x.arc(ex,ey,4,0,Math.PI*2);x.fillStyle=lc;x.fill();
  $('cvl').textContent='Cumulative: '+inr(lv);$('cvl').style.color=lc;
}

function rO(T){
  const O=T.filter(t=>t.status==='OPEN');$('oc').textContent=O.length;
  if(!O.length){$('ow').innerHTML='<div class=empty>No open positions</div>';return}
  $('ow').innerHTML=O.map(t=>{
    const cmp=LTP[t.id];
    const cmpStr=cmp!=null?cmp.toFixed(2):'--';
    const upnl=cmp!=null?(cmp-t.price)*t.qty:null;
    const upnlStr=upnl!=null?inr(upnl):'--';
    const cls=upnl!=null?pc(upnl):'';
    return '<div class=pos-card>'+
      '<div class=pos-top><span class=pos-sym>'+t.symbol+'</span><span class="pos-pnl '+cls+'">'+upnlStr+'</span></div>'+
      '<div class=pos-row>'+
        '<span class=pos-tag>Entry <b>'+t.price+'</b></span>'+
        '<span class=pos-tag>CMP <b>'+cmpStr+'</b></span>'+
        '<span class=pos-tag>SL <b>'+t.stop_price+'</b></span>'+
        '<span class=pos-tag>TGT <b>'+(t.target_price||'-')+'</b></span>'+
        '<span class=pos-tag>Qty <b>'+t.qty+'</b></span>'+
      '</div>'+
      '<button class=pos-close onclick="cp('+t.id+",\'"+t.symbol.replace(/'/g,'')+"\')"+'"">Close Position</button>'+
    '</div>'
  }).join('');
}

function exitLabel(s){
  if(!s)return{t:'closed',c:'eod'};
  const sl=s.toLowerCase();
  if(sl.includes('target'))return{t:'Target Hit',c:'tgt'};
  if(sl.includes('profit_floor')||sl.includes('floor'))return{t:'Profit Floor',c:'floor'};
  if(sl.includes('sl_hit')||sl.includes('stop'))return{t:'SL Hit',c:'sl'};
  if(sl.includes('max_loss'))return{t:'Max Loss',c:'sl'};
  if(sl.includes('eod')||sl.includes('square'))return{t:'EOD',c:'eod'};
  if(sl.includes('manual'))return{t:'Manual',c:'eod'};
  return{t:s.replace(/CLOSED_?/i,'').replace(/_/g,' ')||'Closed',c:'eod'};
}
function dur(entry,exit){
  if(!entry||!exit)return'-';
  const ms=new Date(exit)-new Date(entry);
  if(ms<0)return'-';
  const m=Math.floor(ms/60000),h=Math.floor(m/60),rm=m%60;
  return h>0?h+'h '+rm+'m':rm+'m';
}
function togCard(el){el.classList.toggle('expanded')}

function rH(T){
  const C=T.filter(t=>t.status!=='OPEN');
  let F=C;if(CF==='w')F=C.filter(t=>t.pnl>0);else if(CF==='l')F=C.filter(t=>t.pnl!==null&&t.pnl<=0);
  const w=C.filter(t=>t.pnl>0).length,l=C.filter(t=>t.pnl!==null&&t.pnl<=0).length;
  $('hc').textContent=C.length;
  $('fb').innerHTML=[
    {k:'all',t:'All '+C.length},{k:'w',t:'Wins '+w},{k:'l',t:'Losses '+l}
  ].map(f=>'<div class="fpill '+(CF===f.k?'a':'')+'" onclick="sF(\''+f.k+'\')">'+f.t+'</div>').join('');
  if(!F.length){$('hw').innerHTML='<div class=empty>No trades yet</div>';return}
  $('hw').innerHTML=F.map(t=>{
    const isW=t.pnl>0;
    const ex=exitLabel(t.status);
    const entryP=t.price!=null?t.price.toFixed(2):'-';
    const exitP=t.exit_price!=null?t.exit_price.toFixed(2):'-';
    const slP=t.stop_price!=null?t.stop_price.toFixed(2):'-';
    const tgtP=t.target_price!=null?t.target_price.toFixed(2):'-';
    const ch=t.charges!=null?'₹'+Math.abs(Math.round(t.charges)).toLocaleString('en-IN'):'-';
    const netPnl=t.pnl!=null&&t.charges!=null?t.pnl-t.charges:t.pnl;
    return '<div class=trade-card onclick="togCard(this)">'+
      '<div class=tc-top>'+
        '<div class="tc-icon '+(isW?'w':'l')+'">'+(isW?'W':'L')+'</div>'+
        '<div class=tc-body><div class=tc-sym>'+t.symbol+'</div>'+
          '<div class=tc-meta>'+tf(t.ts)+' | Qty '+t.qty+(t.confidence?' | <span class="scan-conf '+(t.confidence>=0.7?'hi':'md')+'" style="font-size:9px;padding:1px 5px">'+(t.confidence*100).toFixed(0)+'%</span>':'')+'</div></div>'+
        '<div class="tc-pnl '+pc(t.pnl)+'">'+inr(t.pnl)+'</div>'+
      '</div>'+
      '<div class=tc-detail>'+
        '<div class=tc-grid>'+
          '<div class=tc-item>Entry <b>'+entryP+'</b></div>'+
          '<div class=tc-item>Exit <b>'+exitP+'</b></div>'+
          '<div class=tc-item>Duration <b>'+dur(t.ts,t.exit_ts||t.ts)+'</b></div>'+
          '<div class=tc-item>SL <b>'+slP+'</b></div>'+
          '<div class=tc-item>TGT <b>'+tgtP+'</b></div>'+
          '<div class=tc-item>Qty <b>'+t.qty+'</b></div>'+
          '<div class=tc-item>Charges <b>'+ch+'</b></div>'+
          '<div class=tc-item>Net P&L <b class="'+pc(netPnl)+'">'+inr(netPnl)+'</b></div>'+
          '<div class=tc-item>Broker <b style="font-size:9px;word-break:break-all">'+(t.broker_key||'-')+'</b></div>'+
        '</div>'+
        '<div class="tc-reason '+ex.c+'">'+ex.t+'</div>'+
      '</div>'+
    '</div>'
  }).join('');
}

function sF(f){CF=f;rH(AT)}

function switchCh(ch){
  if(ch===CH)return;
  CH=ch;CF='all';LTP={};AT=[];
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  $('tab-'+ch).classList.add('active');

  const isStrat=ch==='strat',isStocks=ch==='stocks';
  const isSpecial=isStrat||isStocks;
  document.querySelector('.wrap:not(#stratView):not(#stocksView)').style.display=isSpecial?'none':'';
  document.querySelector('.hero').style.display=isSpecial?'none':'';
  document.querySelector('.chips').style.display=isSpecial?'none':'';
  $('stratView').style.display=isStrat?'':'none';
  $('stocksView').style.display=isStocks?'':'none';

  if(isStrat){loadStrat();return}
  if(isStocks){loadStocks();return}
  $('hv').textContent='...';$('hv').className='val';
  $('hs').textContent='loading...';
  $('rp').textContent='--';$('rp').className='ring-pct';
  $('chips').innerHTML='';$('ow').innerHTML='';$('hw').innerHTML='';
  $('oc').textContent='0';$('hc').textContent='0';
  rC([]);
  load();
}

async function loadScan(){
  if(_scanCache&&Date.now()-_scanCacheTs<SCAN_CACHE_MS){
    renderScan(_scanCache);return;
  }
  $('scanList').innerHTML='<div class=empty>Loading scanner...</div>';
  try{
    const data=await fetch('/api/scan').then(r=>r.json());
    if(Array.isArray(data)){_scanCache=data;_scanCacheTs=Date.now();renderScan(data)}
    else{$('scanList').innerHTML='<div class=empty>No scanner signals right now</div>'}
  }catch(e){$('scanList').innerHTML='<div class=empty>Scanner unavailable</div>'}
}

function renderScan(data){
  if(!data||!data.length){$('scanList').innerHTML='<div class=empty>No scanner signals right now</div>';return}
  $('scanList').innerHTML=data.map(s=>{
    const cc=s.confidence>=70?'hi':s.confidence>=50?'md':'lo';
    return '<div class=scan-card>'+
      '<div class=scan-top><span class=scan-sym>'+s.symbol+' '+s.option_type+'</span>'+
        '<span class="scan-conf '+cc+'">'+s.confidence+'/100</span></div>'+
      '<div class=scan-strat>'+s.strategy+' | '+s.entry_window+'</div>'+
      '<div class=scan-reasons>'+s.reasons.slice(0,3).join(' | ')+'</div>'+
    '</div>'
  }).join('');
}

// ── Strategy tab rendering ──
async function loadStrat(){
  if(_stratCache&&Date.now()-_stratCacheTs<STRAT_CACHE_MS){renderStrat(_stratCache);return}
  $('stratCards').innerHTML='<div class="skel" style="height:90px"></div><div class="skel" style="height:90px;margin-top:8px"></div><div class="skel" style="height:90px;margin-top:8px"></div>';
  $('stratToday').innerHTML='<div class="skel" style="height:60px"></div>';
  $('stratLog').innerHTML='<div class="skel" style="height:200px"></div>';
  try{
    const [sumResp,todayResp]=await Promise.all([
      fetch('/api/strategy/summary?days=45'),
      fetch('/api/strategy/today')
    ]);
    const sum=await sumResp.json(),today=await todayResp.json();
    _stratCache={sum,today};_stratCacheTs=Date.now();
    renderStrat(_stratCache);
  }catch(e){$('stratCards').innerHTML='<div class=empty>'+e+'</div>'}
}

function renderStrat(data){
  const {sum,today}=data;
  if(!sum||sum.error){$('stratCards').innerHTML='<div class=empty>No strategy data yet. Run backfill first.</div>';return}
  const order=['kitchen_sink','vf_920_sl30','entry_945_sl30'];
  const strats=order.filter(s=>sum[s]);
  if(!strats.length){$('stratCards').innerHTML='<div class=empty>No strategy data</div>';return}

  // Strategy pills
  $('stratPills').innerHTML=strats.map(s=>'<div class="fpill '+(_stratFocus===s?'a':'')+'" onclick="focusStrat(\''+s+'\')">'+s.replace(/_/g,' ')+'</div>').join('');

  // Summary cards
  let maxTotal=-Infinity;strats.forEach(s=>{if(sum[s].total>maxTotal)maxTotal=sum[s].total});
  $('stratDays').textContent=sum[strats[0]].traded+' days';
  $('stratCards').innerHTML=strats.map(s=>{
    const d=sum[s];
    const wr=d.win_rate;
    const grade=wr>=90?'A+':wr>=80?'A':wr>=70?'B':wr>=60?'C':'F';
    const gc=grade.startsWith('A')?'a':grade==='B'?'b':'c';
    const isBest=d.total===maxTotal;
    return '<div class="str-card '+(isBest?'best':'')+'">'+
      '<div class=str-name>'+s.replace(/_/g,' ')+' <span class="str-badge '+gc+'">'+grade+'</span>'+(isBest?' <span class="str-badge a">BEST</span>':'')+'</div>'+
      '<div class="str-pnl '+(d.total>=0?'pos':'neg')+'">'+inr(d.total)+'</div>'+
      '<div class=str-stats>'+
        '<span class=str-stat>Win <b>'+d.green+'/'+d.traded+'</b> ('+wr+'%)</span>'+
        '<span class=str-stat>Avg <b>'+inr(d.avg_day)+'</b>/day</span>'+
        '<span class=str-stat>Best <b>'+inr(d.max_day)+'</b></span>'+
        '<span class=str-stat>Worst <b class=neg>'+inr(d.min_day)+'</b></span>'+
        '<span class=str-stat>3 lots <b>'+inr(d.avg_day*3)+'</b>/day</span>'+
      '</div></div>'
  }).join('');

  // Equity curves (all 3 on one chart)
  renderStratChart(sum,strats);

  // Today's detail
  renderStratToday(today,strats);

  // Daily log for focused strategy
  renderStratLog(sum[_stratFocus],_stratFocus);
}

function renderStratChart(sum,strats){
  const c=$('stratChart'),x=c.getContext('2d'),dp=devicePixelRatio||1,r=c.getBoundingClientRect();
  c.width=r.width*dp;c.height=r.height*dp;x.scale(dp,dp);
  const W=r.width,H=r.height,p={t:14,b:24,l:44,r:8};
  const cs=getComputedStyle(document.documentElement);
  const mt=cs.getPropertyValue('--mt').trim(),bd=cs.getPropertyValue('--bd').trim();
  const colors=['#22c55e','#3b82f6','#f59e0b'];

  let allV=[];strats.forEach(s=>{const cum=sum[s].cumulative;cum.forEach(c=>allV.push(c.cumulative))});
  if(!allV.length)return;
  const mn=Math.min(0,...allV),mx=Math.max(0,...allV),rg=mx-mn||1;
  const cw=W-p.l-p.r,ch=H-p.t-p.b;
  const Y=v=>p.t+ch-(((v-mn)/rg)*ch);

  // Grid
  x.strokeStyle=bd;x.lineWidth=.5;
  for(let i=0;i<=3;i++){const yy=p.t+(ch/3)*i;x.beginPath();x.moveTo(p.l,yy);x.lineTo(W-p.r,yy);x.stroke();
    x.fillStyle=mt;x.font='9px system-ui';x.textAlign='right';x.fillText(Math.round(mx-((mx-mn)/3)*i).toLocaleString('en-IN'),p.l-4,yy+3)}

  // Zero line
  if(mn<0&&mx>0){x.strokeStyle=mt;x.lineWidth=.8;x.setLineDash([3,3]);x.beginPath();x.moveTo(p.l,Y(0));x.lineTo(W-p.r,Y(0));x.stroke();x.setLineDash([])}

  // Lines
  strats.forEach((s,si)=>{
    const cum=sum[s].cumulative;if(!cum.length)return;
    const X=i=>p.l+(i/(cum.length-1))*cw;
    x.beginPath();x.moveTo(X(0),Y(cum[0].cumulative));
    for(let i=1;i<cum.length;i++)x.lineTo(X(i),Y(cum[i].cumulative));
    x.strokeStyle=colors[si];x.lineWidth=s===_stratFocus?2.5:1.2;x.lineJoin='round';x.stroke();
    // End dot
    const lv=cum[cum.length-1].cumulative;
    x.beginPath();x.arc(X(cum.length-1),Y(lv),3,0,Math.PI*2);x.fillStyle=colors[si];x.fill();
  });

  // Legend at bottom
  const lx=p.l;
  strats.forEach((s,si)=>{
    const xp=lx+si*120;
    x.fillStyle=colors[si];x.fillRect(xp,H-10,8,8);
    x.fillStyle=mt;x.font='9px system-ui';x.textAlign='left';
    x.fillText(s.replace(/_/g,' ').slice(0,14),xp+12,H-3);
  });
}

function renderStratToday(today,strats){
  if(!today||!Object.keys(today).length){
    $('stratToday').innerHTML='<div class=empty>No trades today yet</div>';return}
  const idxOrder=['NIFTY','BANKNIFTY','SENSEX'];
  let html='';
  strats.forEach(s=>{
    const sd=today[s];if(!sd)return;
    const tag=sd.day_pnl>0?'pos':sd.day_pnl<0?'neg':'';
    html+='<div class=str-today-card><div class=str-idx-row><span class=str-idx-name>'+s.replace(/_/g,' ')+'</span><span class="str-idx-pnl '+tag+'">'+inr(sd.day_pnl)+'</span></div>';
    idxOrder.forEach(idx=>{
      const r=sd.indexes[idx];if(!r)return;
      if(r.skipped){html+='<div class=str-idx-meta>'+idx+': skipped ('+r.skip_reason+')</div>';return}
      html+='<div class=str-idx-meta>'+idx+': '+inr(r.net_pnl)+' | DTE='+r.dte+' | '+(r.exit_reason||'time')+'</div>';
    });
    html+='</div>';
  });
  $('stratToday').innerHTML=html||'<div class=empty>No trades today</div>';
}

function renderStratLog(data,sname){
  if(!data||!data.dates||!data.dates.length){$('stratLog').innerHTML='<div class=empty>No history</div>';return}
  const mx=Math.max(...data.pnls.map(Math.abs))||1;
  $('stratLog').innerHTML=data.dates.map((d,i)=>{
    const p=data.pnls[i];const isGreen=p>0;
    const pct=Math.abs(p)/mx*100;
    const col=isGreen?'var(--gn)':'var(--rd)';
    const bg=isGreen?'var(--gd)':'var(--rdd)';
    const wd=d.split('-');const short=wd[1]+'-'+wd[2];
    return '<div class=str-day-row>'+
      '<span class=str-day-date>'+short+'</span>'+
      '<div class=str-day-bar style="background:'+bg+'"><div class=str-day-fill style="width:'+pct+'%;background:'+col+'"></div></div>'+
      '<span class="str-day-val '+(isGreen?'pos':'neg')+'">'+inr(p)+'</span></div>'
  }).join('');
}

function focusStrat(s){_stratFocus=s;if(_stratCache)renderStrat(_stratCache)}

// ── Stocks tab rendering ──
async function loadStocks(){
  if(_stocksCache&&Date.now()-_stocksCacheTs<STOCKS_CACHE_MS){renderStocks(_stocksCache);return}
  $('stockCards').innerHTML='<div class="skel" style="height:90px"></div><div class="skel" style="height:90px;margin-top:8px"></div><div class="skel" style="height:90px;margin-top:8px"></div><div class="skel" style="height:90px;margin-top:8px"></div>';
  $('stockToday').innerHTML='<div class="skel" style="height:60px"></div>';
  $('stockLog').innerHTML='<div class="skel" style="height:200px"></div>';
  $('stockBreakdown').innerHTML='<div class="skel" style="height:150px"></div>';
  try{
    const [sumResp,todayResp,stocksResp]=await Promise.all([
      fetch('/api/stock-strategy/summary?days=120'),
      fetch('/api/stock-strategy/today'),
      fetch('/api/stock-strategy/stocks?days=120')
    ]);
    const sum=await sumResp.json(),today=await todayResp.json(),stocks=await stocksResp.json();
    _stocksCache={sum,today,stocks};_stocksCacheTs=Date.now();
    renderStocks(_stocksCache);
  }catch(e){$('stockCards').innerHTML='<div class=empty>'+e+'</div>'}
}

function renderStocks(data){
  const {sum,today,stocks}=data;
  if(!sum||sum.error||!Object.keys(sum).length){$('stockCards').innerHTML='<div class=empty>No stock strategy data yet. Run backfill first.</div>';return}
  const order=['ema20_rsi50','ema20_rsi60','ema20_rsi50_tight','ema20_rsi50_wide'];
  const strats=order.filter(s=>sum[s]);
  if(!strats.length){$('stockCards').innerHTML='<div class=empty>No data</div>';return}

  $('stockPills').innerHTML=strats.map(s=>'<div class="fpill '+(_stocksFocus===s?'a':'')+'" onclick="focusStock(\''+s+'\')">'+s.replace(/_/g,' ')+'</div>').join('');

  let maxTotal=-Infinity;strats.forEach(s=>{if(sum[s].total>maxTotal)maxTotal=sum[s].total});
  $('stockDays').textContent=(sum[strats[0]]||{}).traded+' entry days';
  $('stockCards').innerHTML=strats.map(s=>{
    const d=sum[s];
    const wr=d.win_rate;
    const grade=wr>=80?'A+':wr>=70?'A':wr>=60?'B':wr>=50?'C':'F';
    const gc=grade.startsWith('A')?'a':grade==='B'?'b':'c';
    const isBest=d.total===maxTotal;
    return '<div class="str-card '+(isBest?'best':'')+'">'+
      '<div class=str-name>'+s.replace(/_/g,' ')+' <span class="str-badge '+gc+'">'+grade+'</span>'+(isBest?' <span class="str-badge a">BEST</span>':'')+'</div>'+
      '<div class="str-pnl '+(d.total>=0?'pos':'neg')+'">'+inr(d.total)+'</div>'+
      '<div class=str-stats>'+
        '<span class=str-stat>Days <b>'+d.traded+'</b></span>'+
        '<span class=str-stat>WR <b>'+wr+'%</b> ('+d.green+'/'+d.traded+')</span>'+
        '<span class=str-stat>Avg <b>'+inr(d.avg_day)+'</b>/day</span>'+
        '<span class=str-stat>Best <b>'+inr(d.max_day)+'</b></span>'+
        '<span class=str-stat>Worst <b class=neg>'+inr(d.min_day)+'</b></span>'+
      '</div></div>'
  }).join('');

  renderStockChart(sum,strats);
  renderStockBreakdown(stocks);
  renderStockToday(today,strats);
  renderStockLog(sum[_stocksFocus],_stocksFocus);
}

function renderStockChart(sum,strats){
  const c=$('stockChart'),x=c.getContext('2d'),dp=devicePixelRatio||1,r=c.getBoundingClientRect();
  c.width=r.width*dp;c.height=r.height*dp;x.scale(dp,dp);
  const W=r.width,H=r.height,p={t:14,b:24,l:50,r:8};
  const cs=getComputedStyle(document.documentElement);
  const mt=cs.getPropertyValue('--mt').trim(),bd=cs.getPropertyValue('--bd').trim();
  const colors=['#22c55e','#3b82f6','#f59e0b','#ec4899'];
  let allV=[];strats.forEach(s=>{const cum=sum[s].cumulative;cum.forEach(c=>allV.push(c.cumulative))});
  if(!allV.length)return;
  const mn=Math.min(0,...allV),mx=Math.max(0,...allV),rg=mx-mn||1;
  const cw=W-p.l-p.r,ch=H-p.t-p.b;
  const Y=v=>p.t+ch-(((v-mn)/rg)*ch);

  x.strokeStyle=bd;x.lineWidth=.5;
  for(let i=0;i<=3;i++){const yy=p.t+(ch/3)*i;x.beginPath();x.moveTo(p.l,yy);x.lineTo(W-p.r,yy);x.stroke();
    x.fillStyle=mt;x.font='9px system-ui';x.textAlign='right';x.fillText(Math.round(mx-((mx-mn)/3)*i).toLocaleString('en-IN'),p.l-4,yy+3)}
  if(mn<0&&mx>0){x.strokeStyle=mt;x.lineWidth=.8;x.setLineDash([3,3]);x.beginPath();x.moveTo(p.l,Y(0));x.lineTo(W-p.r,Y(0));x.stroke();x.setLineDash([])}

  strats.forEach((s,si)=>{
    const cum=sum[s].cumulative;if(!cum.length)return;
    const X=i=>p.l+(i/(cum.length-1))*cw;
    x.beginPath();x.moveTo(X(0),Y(cum[0].cumulative));
    for(let i=1;i<cum.length;i++)x.lineTo(X(i),Y(cum[i].cumulative));
    x.strokeStyle=colors[si];x.lineWidth=s===_stocksFocus?2.5:1.2;x.lineJoin='round';x.stroke();
    const lv=cum[cum.length-1].cumulative;
    x.beginPath();x.arc(X(cum.length-1),Y(lv),3,0,Math.PI*2);x.fillStyle=colors[si];x.fill();
  });
  const lx=p.l;
  strats.forEach((s,si)=>{
    const xp=lx+si*110;
    x.fillStyle=colors[si];x.fillRect(xp,H-10,8,8);
    x.fillStyle=mt;x.font='8px system-ui';x.textAlign='left';
    x.fillText(s.replace(/_/g,' ').slice(0,12),xp+12,H-3);
  });
}

function renderStockBreakdown(stocks){
  if(!stocks||!stocks.length){$('stockBreakdown').innerHTML='<div class=empty>No data</div>';return}
  const focused=stocks.filter(s=>s.strategy===_stocksFocus);
  focused.sort((a,b)=>b.total_pnl-a.total_pnl);
  $('stockBreakdown').innerHTML=focused.map(s=>{
    const tag=s.total_pnl>0?'pos':s.total_pnl<0?'neg':'';
    return '<div class=str-today-card>'+
      '<div class=str-idx-row><span class=str-idx-name>'+s.stock+'</span>'+
      '<span class="str-idx-pnl '+tag+'">'+inr(s.total_pnl)+'</span></div>'+
      '<div class=str-idx-meta>'+s.trades+' trades | WR: '+s.win_rate+'% ('+s.wins+'/'+s.trades+')</div>'+
    '</div>'
  }).join('');
}

function renderStockToday(today,strats){
  if(!today||!Object.keys(today).length){
    $('stockToday').innerHTML='<div class=empty>No stock trades today</div>';return}
  let html='';
  strats.forEach(s=>{
    const sd=today[s];if(!sd)return;
    const tag=sd.day_pnl>0?'pos':sd.day_pnl<0?'neg':'';
    html+='<div class=str-today-card><div class=str-idx-row><span class=str-idx-name>'+s.replace(/_/g,' ')+'</span><span class="str-idx-pnl '+tag+'">'+inr(sd.day_pnl)+'</span></div>';
    Object.entries(sd.stocks||{}).forEach(([stock,r])=>{
      html+='<div class=str-idx-meta>'+stock+': '+inr(r.net_pnl)+' | '+r.direction+' | '+(r.exit_reason||'active')+'</div>';
    });
    html+='</div>';
  });
  $('stockToday').innerHTML=html||'<div class=empty>No stock trades today</div>';
}

function renderStockLog(data,sname){
  if(!data||!data.dates||!data.dates.length){$('stockLog').innerHTML='<div class=empty>No history</div>';return}
  const mx=Math.max(...data.pnls.map(Math.abs))||1;
  $('stockLog').innerHTML=data.dates.map((d,i)=>{
    const p=data.pnls[i];const isGreen=p>0;
    const pct=Math.abs(p)/mx*100;
    const col=isGreen?'var(--gn)':'var(--rd)';
    const bg=isGreen?'var(--gd)':'var(--rdd)';
    const wd=d.split('-');const short=wd[1]+'-'+wd[2];
    const cum=data.cumulative[i];
    const trades=cum?cum.trades:'?';
    return '<div class=str-day-row>'+
      '<span class=str-day-date>'+short+'</span>'+
      '<div class=str-day-bar style="background:'+bg+'"><div class=str-day-fill style="width:'+pct+'%;background:'+col+'"></div></div>'+
      '<span class="str-day-val '+(isGreen?'pos':'neg')+'">'+inr(p)+'</span>'+
      '<span style="font-size:9px;color:var(--mt);width:30px;text-align:center">'+trades+'t</span></div>'
  }).join('');
}

function focusStock(s){_stocksFocus=s;if(_stocksCache)renderStocks(_stocksCache)}

async function load(){
  if(CH==='strat'){loadStrat();return}
  if(CH==='stocks'){loadStocks();return}
  if(_abortCtrl)_abortCtrl.abort();
  _abortCtrl=new AbortController();
  const sig=_abortCtrl.signal;
  const ch=CH;
  try{
    const url='/api/all?channel='+ch;
    const resp=await fetch(url,{signal:sig});
    if(ch!==CH)return;
    const d=await resp.json();
    if(!d||!d.stats){
      $('hv').textContent='No data';$('hv').className='val';
      $('hs').textContent='No data for this channel';
      $('chips').innerHTML='';$('ow').innerHTML='';$('hw').innerHTML='';
      rC([]);return;
    }
    const s=d.stats,t=d.trades||[];
    AT=t;$('ck').textContent=s.now;$('sd').className='live-dot';
    rHero(s);rRing(s);rChips(s);rC(s.pnl_curve);rH(t);
    rO(t);
    fetch('/api/ltp?channel='+ch,{signal:sig}).then(r=>r.json()).then(ltp=>{if(!ltp.error&&ch===CH){LTP=ltp;rO(t)}}).catch(()=>{});
  }catch(e){if(e.name!=='AbortError'){$('sd').className='live-dot off';
    $('hv').textContent='Error';$('hs').textContent=String(e)}}
}

function startRefresh(){
  clearInterval(_refreshTimer);
  _refreshTimer=setInterval(load,REFRESH_MS);
}

// Pause refresh when tab is hidden
document.addEventListener('visibilitychange',()=>{
  if(document.hidden){clearInterval(_refreshTimer)}
  else{load();startRefresh()}
});

load();startRefresh();
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)

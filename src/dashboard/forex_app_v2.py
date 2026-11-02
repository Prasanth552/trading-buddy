"""Forex paper trading bot v2 — Realistic CFD model.

Realistic paper trading using S/R + LinReg Reversion strategies.
Key differences from v1:
  - Entry at NEXT candle's OPEN (not signal candle's close)
  - Spread deducted from every trade
  - P&L based on actual pip movement × lot size (not binary payout)
  - Slippage simulation (random 0-0.5 pip adverse)

Mounted on main dashboard at /forex, or run standalone on port 8002:
    python -m src.dashboard.forex_app
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

UTC = ZoneInfo("UTC")
IST = ZoneInfo("Asia/Kolkata")

app = FastAPI(title="Forex Paper Trader v2", docs_url=None, redoc_url=None)

# ── Config ──────────────────────────────────────────────────────────────────
STARTING_CAPITAL = 150_000
MAX_DAILY_LOSS = 20_000
MAX_PAIR_DAILY_LOSS = 10_000
MIN_CAPITAL = 20_000
BEST_HOURS_UTC = {0, 1, 2, 3, 4, 19, 21, 22, 23}
POLL_INTERVAL = 60

# CFD-realistic config per pair
PAIR_CONFIG = {
    "EUR/GBP": {
        "symbol": "EURGBP=X",
        "spread_pips": 1.5,
        "pip_size": 0.0001,
        "pip_value_inr": 1100,  # per standard lot (100K units)
        "lot_size": 0.5,        # trade 50K units
    },
    "CAD/CHF": {
        "symbol": "CADCHF=X",
        "spread_pips": 2.5,
        "pip_size": 0.0001,
        "pip_value_inr": 950,
        "lot_size": 0.5,
    },
}
PAIRS = {name: cfg["symbol"] for name, cfg in PAIR_CONFIG.items()}
SLIPPAGE_MAX_PIPS = 0.5

DATA_FILE = Path(__file__).parent.parent.parent / "data" / "forex_paper_trades_v2.json"

# ── State ───────────────────────────────────────────────────────────────────
_state: dict[str, Any] = {
    "capital": STARTING_CAPITAL,
    "peak_capital": STARTING_CAPITAL,
    "trades": [],
    "daily_pnl": {},
    "pair_daily_pnl": {},
    "candles": {},
    "running": False,
    "last_poll": None,
    "last_price": {},
    "today_trades": 0,
    "today_pnl": 0.0,
    "total_wins": 0,
    "total_losses": 0,
    "total_draws": 0,
    "max_drawdown": 0,
    "win_streak": 0,
    "loss_streak": 0,
    "max_win_streak": 0,
    "max_loss_streak": 0,
    "streak": 0,
    "started_at": None,
    "errors": [],
    "total_pips": 0.0,
    "total_spread_cost": 0.0,
}

_ws_clients: set[WebSocket] = set()
_poll_thread: threading.Thread | None = None
_stop_event = threading.Event()
_loop: asyncio.AbstractEventLoop | None = None

# Track pending signals: signal fires on candle N, we enter on candle N+1 open
_pending_signals: dict[str, list[dict]] = {}  # pair -> [signals waiting for next candle]
_last_signal_candle_time: dict[str, int] = {}  # pair -> last candle time we checked


# ── Persistence ─────────────────────────────────────────────────────────────

def _save_state() -> None:
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        saveable = {
            "capital": _state["capital"],
            "peak_capital": _state["peak_capital"],
            "trades": _state["trades"][-500:],
            "daily_pnl": _state["daily_pnl"],
            "total_wins": _state["total_wins"],
            "total_losses": _state["total_losses"],
            "total_draws": _state["total_draws"],
            "max_drawdown": _state["max_drawdown"],
            "max_win_streak": _state["max_win_streak"],
            "max_loss_streak": _state["max_loss_streak"],
            "started_at": _state["started_at"],
            "total_pips": _state["total_pips"],
            "total_spread_cost": _state["total_spread_cost"],
        }
        DATA_FILE.write_text(json.dumps(saveable, indent=2))
    except Exception:
        pass


def _load_state() -> None:
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text())
            for k in ("capital", "peak_capital", "trades", "daily_pnl",
                       "total_wins", "total_losses", "total_draws",
                       "max_drawdown", "max_win_streak", "max_loss_streak",
                       "started_at", "total_pips", "total_spread_cost"):
                if k in data:
                    _state[k] = data[k]
        except Exception:
            pass


# ── Strategies ──────────────────────────────────────────────────────────────

def strategy_sr(candles: list[dict]) -> list[tuple[int, str]]:
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    signals = []
    lookback = 25
    threshold_pct = 0.0005
    for i in range(lookback, len(candles)):
        resistance = max(highs[i - lookback:i])
        support = min(lows[i - lookback:i])
        price = closes[i]
        rng = resistance - support
        if rng == 0:
            continue
        if (price - support) / rng < threshold_pct * 10:
            signals.append((i, "CALL"))
        elif (resistance - price) / rng < threshold_pct * 10:
            signals.append((i, "PUT"))
    return signals


def strategy_linreg(candles: list[dict], period: int = 20, std_mult: float = 2.0) -> list[tuple[int, str]]:
    closes = [c["close"] for c in candles]
    signals = []
    for i in range(period, len(candles)):
        window = closes[i - period:i]
        n = len(window)
        sx = n * (n - 1) / 2
        sy = sum(window)
        sxx = sum(j * j for j in range(n))
        sxy = sum(j * v for j, v in enumerate(window))
        denom = n * sxx - sx * sx
        if denom == 0:
            continue
        slope = (n * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / n
        predicted = intercept + slope * (n - 1)
        residuals = [window[j] - (intercept + slope * j) for j in range(n)]
        std = (sum(r ** 2 for r in residuals) / n) ** 0.5
        if std == 0:
            continue
        price = closes[i]
        if price < predicted - std_mult * std:
            signals.append((i, "CALL"))
        elif price > predicted + std_mult * std:
            signals.append((i, "PUT"))
    return signals


# ── Trading engine (realistic) ─────────────────────────────────────────────

def _check_for_signals(candles: list[dict]) -> list[dict]:
    """Check the LATEST COMPLETED candle for signals.
    These signals will be QUEUED and executed at the NEXT candle's open."""
    if len(candles) < 30:
        return []
    # The last candle in the list is the most recently completed one
    # (yfinance returns completed candles, current partial candle may be included)
    last_idx = len(candles) - 1

    dt_utc = datetime.fromtimestamp(candles[last_idx]["time"], UTC)
    if dt_utc.hour not in BEST_HOURS_UTC:
        return []

    results = []
    for name, fn in [("S/R", strategy_sr), ("LinReg", strategy_linreg)]:
        signals = fn(candles)
        for idx, direction in signals:
            if idx == last_idx:
                results.append({
                    "strategy": name,
                    "direction": direction,
                    "signal_price": candles[idx]["close"],
                    "signal_time": candles[idx]["time"],
                })
    return results


def _execute_realistic_trade(signal: dict, entry_candle: dict, exit_candle: dict,
                              pair_name: str) -> dict | None:
    """Execute a trade with realistic CFD pricing.

    Entry: next candle's OPEN + spread + slippage
    Exit: that same candle's CLOSE
    P&L: (exit - entry) * direction * lot_value - spread_cost
    """
    day_key = datetime.now(IST).strftime("%Y-%m-%d")
    day_pnl = _state["daily_pnl"].get(day_key, 0)
    if day_pnl <= -MAX_DAILY_LOSS:
        return None
    pair_key = f"{day_key}_{pair_name}"
    pair_pnl = _state["pair_daily_pnl"].get(pair_key, 0)
    if pair_pnl <= -MAX_PAIR_DAILY_LOSS:
        return None
    if _state["capital"] < MIN_CAPITAL:
        return None

    cfg = PAIR_CONFIG[pair_name]
    pip_size = cfg["pip_size"]
    spread_pips = cfg["spread_pips"]
    pip_value = cfg["pip_value_inr"]
    lot_size = cfg["lot_size"]
    slippage_pips = random.uniform(0, SLIPPAGE_MAX_PIPS)

    direction = signal["direction"]
    raw_entry = entry_candle["open"]
    raw_exit = exit_candle["close"]

    # Apply spread + slippage to entry (adverse direction)
    spread_cost_price = spread_pips * pip_size
    slippage_cost_price = slippage_pips * pip_size

    if direction == "CALL":
        # Buy: enter at ask (higher), exit at bid (lower)
        entry_price = raw_entry + (spread_cost_price / 2) + slippage_cost_price
        exit_price = raw_exit - (spread_cost_price / 2)
        pip_move = (exit_price - entry_price) / pip_size
    else:
        # Sell: enter at bid (lower), exit at ask (higher)
        entry_price = raw_entry - (spread_cost_price / 2) - slippage_cost_price
        exit_price = raw_exit + (spread_cost_price / 2)
        pip_move = (entry_price - exit_price) / pip_size

    # P&L in INR
    pnl = pip_move * pip_value * lot_size
    spread_cost_inr = spread_pips * pip_value * lot_size
    slippage_cost_inr = slippage_pips * pip_value * lot_size

    if pnl > 0:
        result = "WIN"
        _state["total_wins"] += 1
        _state["streak"] = _state["streak"] + 1 if _state["streak"] > 0 else 1
    elif pnl < 0:
        result = "LOSS"
        _state["total_losses"] += 1
        _state["streak"] = _state["streak"] - 1 if _state["streak"] < 0 else -1
    else:
        result = "DRAW"
        _state["total_draws"] += 1
        _state["streak"] = 0

    _state["max_win_streak"] = max(_state["max_win_streak"],
                                    _state["streak"] if _state["streak"] > 0 else 0)
    _state["max_loss_streak"] = max(_state["max_loss_streak"],
                                     abs(_state["streak"]) if _state["streak"] < 0 else 0)

    _state["capital"] += pnl
    _state["peak_capital"] = max(_state["peak_capital"], _state["capital"])
    dd = _state["peak_capital"] - _state["capital"]
    _state["max_drawdown"] = max(_state["max_drawdown"], dd)
    _state["daily_pnl"][day_key] = day_pnl + pnl
    _state["pair_daily_pnl"][pair_key] = pair_pnl + pnl
    _state["total_pips"] = _state.get("total_pips", 0) + pip_move
    _state["total_spread_cost"] = _state.get("total_spread_cost", 0) + spread_cost_inr

    trade = {
        "id": len(_state["trades"]) + 1,
        "ts": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        "day": day_key,
        "pair": pair_name,
        "strategy": signal["strategy"],
        "direction": direction,
        "signal_price": round(signal["signal_price"], 5),
        "entry": round(entry_price, 5),
        "exit": round(exit_price, 5),
        "raw_entry": round(raw_entry, 5),
        "raw_exit": round(raw_exit, 5),
        "pips": round(pip_move, 1),
        "spread_cost": round(spread_cost_inr, 0),
        "slippage_pips": round(slippage_pips, 2),
        "result": result,
        "pnl": round(pnl, 0),
        "capital": round(_state["capital"], 0),
    }
    _state["trades"].append(trade)
    return trade


# ── Price polling ───────────────────────────────────────────────────────────

def _fetch_candles(symbol: str) -> list[dict]:
    import yfinance as yf
    ticker = yf.Ticker(symbol)
    df = ticker.history(period="1d", interval="1m")
    if df.empty:
        df = ticker.history(period="2d", interval="1m")
    candles = []
    for ts, row in df.iterrows():
        candles.append({
            "time": int(ts.timestamp()),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        })
    return candles


def _poll_loop() -> None:
    """Background thread: poll prices, check signals, execute with realistic timing.

    Flow:
    1. Fetch candles (completed candles from yfinance)
    2. If we have pending signals from last poll, execute them using
       current candle as entry/exit (simulates: signal fired → wait for next candle → enter at open, exit at close)
    3. Check latest candle for new signals → queue as pending
    """
    global _loop
    while not _stop_event.is_set():
        for pair_name, symbol in PAIRS.items():
            try:
                candles = _fetch_candles(symbol)
                if not candles:
                    _state["errors"].append(f"{datetime.now(IST):%H:%M} {pair_name} no candles")
                    continue

                _state["candles"][pair_name] = candles
                _state["last_price"][pair_name] = candles[-1]["close"]
                _state["last_poll"] = datetime.now(IST).strftime("%H:%M:%S")

                current_candle_time = candles[-1]["time"]

                # Step 1: Execute pending signals from previous poll
                if pair_name in _pending_signals and _pending_signals[pair_name]:
                    # We need at least 2 candles: the entry candle and exit candle
                    # Since we poll every 60s, the latest candle serves as both
                    # entry (open) and exit (close) for the pending signal
                    if len(candles) >= 2:
                        entry_candle = candles[-1]  # enter at this candle's open
                        exit_candle = candles[-1]    # exit at this candle's close

                        for sig in _pending_signals[pair_name]:
                            # Verify this isn't a stale signal (should be from previous candle)
                            if sig["signal_time"] < current_candle_time:
                                trade = _execute_realistic_trade(
                                    sig, entry_candle, exit_candle, pair_name
                                )
                                if trade:
                                    trade["_candle_key"] = f"{pair_name}_{current_candle_time}"
                                    _save_state()
                                    if _loop:
                                        asyncio.run_coroutine_threadsafe(_ws_broadcast({
                                            "type": "trade", "trade": {
                                                k: v for k, v in trade.items()
                                                if not k.startswith("_")
                                            },
                                        }), _loop)

                    _pending_signals[pair_name] = []

                # Step 2: Check current candle for new signals → queue for next poll
                last_checked = _last_signal_candle_time.get(pair_name, 0)
                if current_candle_time != last_checked and len(candles) >= 30:
                    signals = _check_for_signals(candles)
                    if signals:
                        for sig in signals:
                            sig["pair"] = pair_name
                        _pending_signals[pair_name] = signals
                    _last_signal_candle_time[pair_name] = current_candle_time

            except Exception as exc:
                err = f"{datetime.now(IST):%H:%M} {pair_name}: {exc}"
                _state["errors"] = (_state["errors"] + [err])[-20:]

        if _loop:
            asyncio.run_coroutine_threadsafe(_ws_broadcast({
                "type": "tick",
                "prices": _state["last_price"],
                "time": _state["last_poll"],
                "capital": _state["capital"],
            }), _loop)

        _stop_event.wait(POLL_INTERVAL)


# ── WebSocket ───────────────────────────────────────────────────────────────

async def _ws_broadcast(data: dict) -> None:
    msg = json.dumps(data)
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _ws_clients.discard(websocket)


# ── API ─────────────────────────────────────────────────────────────────────

@app.get("/api/forex/status")
def api_status() -> JSONResponse:
    total = _state["total_wins"] + _state["total_losses"]
    wr = (_state["total_wins"] / total * 100) if total else 0
    net = _state["capital"] - STARTING_CAPITAL
    day_key = datetime.now(IST).strftime("%Y-%m-%d")
    today_pnl = _state["daily_pnl"].get(day_key, 0)
    today_trades = sum(1 for t in _state["trades"] if t.get("day") == day_key)

    green_days = sum(1 for v in _state["daily_pnl"].values() if v > 0)
    red_days = sum(1 for v in _state["daily_pnl"].values() if v < 0)

    dd_pct = (_state["max_drawdown"] / _state["peak_capital"] * 100) if _state["peak_capital"] else 0

    avg_pips = (_state.get("total_pips", 0) / (total + _state["total_draws"])) if (total + _state["total_draws"]) else 0

    return JSONResponse({
        "running": _state["running"],
        "capital": _state["capital"],
        "starting_capital": STARTING_CAPITAL,
        "net_pnl": net,
        "net_pct": round(net / STARTING_CAPITAL * 100, 1),
        "peak_capital": _state["peak_capital"],
        "max_drawdown": _state["max_drawdown"],
        "max_drawdown_pct": round(dd_pct, 1),
        "total_trades": total + _state["total_draws"],
        "wins": _state["total_wins"],
        "losses": _state["total_losses"],
        "draws": _state["total_draws"],
        "win_rate": round(wr, 1),
        "max_win_streak": _state["max_win_streak"],
        "max_loss_streak": _state["max_loss_streak"],
        "today_pnl": today_pnl,
        "today_trades": today_trades,
        "last_price": _state["last_price"],
        "last_poll": _state["last_poll"],
        "green_days": green_days,
        "red_days": red_days,
        "total_days": green_days + red_days,
        "total_pips": round(_state.get("total_pips", 0), 1),
        "avg_pips_per_trade": round(avg_pips, 1),
        "total_spread_cost": round(_state.get("total_spread_cost", 0), 0),
        "pairs": list(PAIRS.keys()),
        "pair_config": {k: {"spread": v["spread_pips"], "lot": v["lot_size"]}
                        for k, v in PAIR_CONFIG.items()},
        "started_at": _state["started_at"],
        "errors": _state["errors"][-5:],
        "now": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "model": "CFD (realistic)",
    })


@app.get("/api/forex/trades")
def api_trades(limit: int = 50) -> JSONResponse:
    clean = []
    for t in _state["trades"]:
        c = {k: v for k, v in t.items() if not k.startswith("_")}
        clean.append(c)
    return JSONResponse(clean[-limit:][::-1])


@app.get("/api/forex/daily")
def api_daily() -> JSONResponse:
    daily = []
    for day in sorted(_state["daily_pnl"].keys()):
        pnl = _state["daily_pnl"][day]
        day_trades = [t for t in _state["trades"] if t.get("day") == day]
        wins = sum(1 for t in day_trades if t["result"] == "WIN")
        losses = sum(1 for t in day_trades if t["result"] == "LOSS")
        wr = (wins / (wins + losses) * 100) if (wins + losses) else 0
        total_pips = sum(t.get("pips", 0) for t in day_trades)
        daily.append({
            "day": day,
            "pnl": pnl,
            "trades": len(day_trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wr, 1),
            "pips": round(total_pips, 1),
        })
    return JSONResponse(daily[::-1])


@app.get("/api/forex/equity")
def api_equity() -> JSONResponse:
    curve = []
    running = STARTING_CAPITAL
    for day in sorted(_state["daily_pnl"].keys()):
        running += _state["daily_pnl"][day]
        curve.append({"day": day, "capital": running})
    return JSONResponse(curve)


@app.post("/api/forex/start")
def api_start() -> JSONResponse:
    global _poll_thread
    if _state["running"]:
        return JSONResponse({"ok": False, "reason": "already running"})
    _state["running"] = True
    _state["started_at"] = _state["started_at"] or datetime.now(IST).strftime("%Y-%m-%d %H:%M")
    _stop_event.clear()
    _poll_thread = threading.Thread(target=_poll_loop, daemon=True)
    _poll_thread.start()
    _save_state()
    return JSONResponse({"ok": True})


@app.post("/api/forex/stop")
def api_stop() -> JSONResponse:
    _state["running"] = False
    _stop_event.set()
    _save_state()
    return JSONResponse({"ok": True})


@app.post("/api/forex/reset")
def api_reset() -> JSONResponse:
    if _state["running"]:
        return JSONResponse({"ok": False, "reason": "stop bot first"})
    _state["capital"] = STARTING_CAPITAL
    _state["peak_capital"] = STARTING_CAPITAL
    _state["trades"] = []
    _state["daily_pnl"] = {}
    _state["pair_daily_pnl"] = {}
    _state["total_wins"] = 0
    _state["total_losses"] = 0
    _state["total_draws"] = 0
    _state["max_drawdown"] = 0
    _state["max_win_streak"] = 0
    _state["max_loss_streak"] = 0
    _state["streak"] = 0
    _state["started_at"] = None
    _state["errors"] = []
    _state["total_pips"] = 0.0
    _state["total_spread_cost"] = 0.0
    _pending_signals.clear()
    _last_signal_candle_time.clear()
    _save_state()
    return JSONResponse({"ok": True})


# ── Startup ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    global _loop, _poll_thread
    _loop = asyncio.get_event_loop()
    _load_state()
    if not _state["running"]:
        _state["running"] = True
        _state["started_at"] = _state["started_at"] or datetime.now(IST).strftime("%Y-%m-%d %H:%M")
        _stop_event.clear()
        _poll_thread = threading.Thread(target=_poll_loop, daemon=True)
        _poll_thread.start()
        _save_state()


# ── HTML Dashboard ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _PAGE


_PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,user-scalable=no">
<title>Forex Paper Trader v2</title>
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
}}
:root[data-theme=light]{
  --bg:#f0f2f5;--sf:#fff;--el:#fff;--bd:#e2e8f0;
  --tx:#1e293b;--mt:#64748b;--ft:#cbd5e1;
  --gn:#16a34a;--gd:rgba(22,163,74,.08);
  --rd:#dc2626;--rdd:rgba(220,38,38,.08);
  --bl:#2563eb;--bld:rgba(37,99,235,.06);
  --am:#d97706;--amd:rgba(217,119,6,.08);
}
:root[data-theme=dark]{
  --bg:#0b0f14;--sf:#121820;--el:#1a2230;--bd:#232d3d;
  --tx:#dfe6ee;--mt:#6b7a8d;--ft:#3a4858;
  --gn:#22c55e;--gd:rgba(34,197,94,.12);
  --rd:#ef4444;--rdd:rgba(239,68,68,.12);
  --bl:#3b82f6;--bld:rgba(59,130,246,.1);
  --am:#f59e0b;--amd:rgba(245,158,11,.12);
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:var(--sn);background:var(--bg);color:var(--tx);
  -webkit-font-smoothing:antialiased;min-height:100vh}
.wrap{max-width:480px;margin:0 auto;padding:12px 16px 80px}
.hdr{background:var(--sf);border-bottom:1px solid var(--bd);padding:14px 16px;
  position:sticky;top:0;z-index:50;display:flex;align-items:center;justify-content:space-between}
.hdr h1{font-size:17px;font-weight:700}
.hdr h1 span{color:var(--cy)}
.hdr-right{display:flex;align-items:center;gap:8px}
.live-dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.live-dot.on{background:var(--gn);box-shadow:0 0 6px var(--gn);animation:pulse 2s infinite}
.live-dot.off{background:var(--rd)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.hdr-time{font-size:11px;color:var(--mt);font-family:var(--mn)}
.back{font-size:13px;color:var(--cy);text-decoration:none;margin-right:10px}
.model-tag{font-size:10px;background:var(--amd);color:var(--am);padding:2px 8px;border-radius:6px;font-weight:600}
.hero{display:flex;justify-content:space-between;align-items:center;padding:20px 0 16px}
.hero-pnl .label{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--mt)}
.hero-pnl .val{font-size:28px;font-weight:800;font-family:var(--mn);font-variant-numeric:tabular-nums}
.hero-pnl .sub{font-size:12px;color:var(--mt);margin-top:2px;font-family:var(--mn)}
.pos{color:var(--gn)}.neg{color:var(--rd)}
.hero-ring{position:relative;width:100px;height:100px}
.hero-ring canvas{width:100px;height:100px}
.ring-txt{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center}
.ring-pct{font-size:20px;font-weight:800;font-family:var(--mn)}
.ring-sub{font-size:9px;text-transform:uppercase;letter-spacing:.8px;color:var(--mt)}
.chips{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:16px}
.chip{background:var(--sf);border:1px solid var(--bd);border-radius:10px;padding:10px;text-align:center}
.chip .cv{font-size:16px;font-weight:700;font-family:var(--mn);font-variant-numeric:tabular-nums}
.chip .cl{font-size:9px;text-transform:uppercase;letter-spacing:.6px;color:var(--mt);margin-top:2px}
.ctrls{display:flex;gap:8px;margin-bottom:16px}
.btn{flex:1;padding:10px;border:none;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer}
.btn-start{background:var(--gn);color:#fff}
.btn-start:disabled{opacity:.4;cursor:default}
.btn-stop{background:var(--rd);color:#fff}
.btn-stop:disabled{opacity:.4;cursor:default}
.btn-reset{background:var(--el);color:var(--mt);border:1px solid var(--bd)}
.sec{background:var(--sf);border:1px solid var(--bd);border-radius:12px;margin-bottom:12px;overflow:hidden}
.sec-h{padding:12px 14px 8px;font-size:13px;font-weight:700;display:flex;align-items:center;gap:6px}
.badge{background:var(--bld);color:var(--bl);font-size:10px;padding:2px 7px;border-radius:8px;font-weight:600}
.sec-body{padding:0 14px 12px}
.ticker{display:flex;align-items:center;gap:10px;background:var(--sf);border:1px solid var(--bd);
  border-radius:10px;padding:12px 14px;margin-bottom:8px}
.ticker .pair{font-size:13px;font-weight:700;color:var(--cy)}
.ticker .price{font-size:22px;font-weight:800;font-family:var(--mn);font-variant-numeric:tabular-nums}
.ticker .time{font-size:10px;color:var(--mt);margin-left:auto;font-family:var(--mn)}
.ticker .status{font-size:10px;padding:3px 8px;border-radius:6px;font-weight:600}
.ticker .status.on{background:var(--gd);color:var(--gn)}
.ticker .status.off{background:var(--rdd);color:var(--rd)}
.tcard{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--bd)}
.tcard:last-child{border-bottom:none}
.tcard .dir{font-size:10px;font-weight:700;padding:3px 8px;border-radius:6px;letter-spacing:.3px}
.tcard .dir.CALL{background:var(--gd);color:var(--gn)}
.tcard .dir.PUT{background:var(--rdd);color:var(--rd)}
.tcard .info{flex:1;min-width:0}
.tcard .strat{font-size:12px;font-weight:600}
.tcard .meta{font-size:10px;color:var(--mt);font-family:var(--mn)}
.tcard .pips{font-size:10px;color:var(--mt);font-family:var(--mn)}
.tcard .res{font-size:13px;font-weight:700;font-family:var(--mn);text-align:right}
.tcard .res.WIN{color:var(--gn)}.tcard .res.LOSS{color:var(--rd)}.tcard .res.DRAW{color:var(--mt)}
.drow{display:flex;align-items:center;padding:8px 14px;border-bottom:1px solid var(--bd);font-size:12px}
.drow:last-child{border-bottom:none}
.drow .day{width:90px;font-weight:600;font-family:var(--mn)}
.drow .bar{flex:1;height:6px;border-radius:3px;background:var(--el);overflow:hidden;margin:0 10px}
.drow .bar-fill{height:100%;border-radius:3px}
.drow .dpnl{width:80px;text-align:right;font-weight:700;font-family:var(--mn);font-variant-numeric:tabular-nums}
.drow .dwr{width:40px;text-align:right;color:var(--mt);font-family:var(--mn)}
.chart-wrap{padding:8px 14px 14px}
.chart-wrap canvas{width:100%;height:160px}
.errs{padding:8px 14px 12px;font-size:11px;color:var(--rd);font-family:var(--mn)}
.empty{color:var(--mt);font-size:13px;padding:16px 14px;text-align:center}
.bnav{position:fixed;bottom:0;left:0;right:0;background:var(--sf);border-top:1px solid var(--bd);
  display:flex;justify-content:center;padding:8px 0 max(8px,env(safe-area-inset-bottom));z-index:50}
.bnav a{color:var(--mt);text-decoration:none;font-size:10px;text-align:center;padding:4px 16px}
.bnav a.active{color:var(--cy)}
.bnav .nav-ico{font-size:18px;display:block}
@media(min-width:600px){.wrap{max-width:520px}.hero-pnl .val{font-size:36px}}
</style></head><body>

<div class=hdr>
  <div style="display:flex;align-items:center;gap:8px">
    <a href="/" class=back>&larr; Dashboard</a>
    <h1>Forex <span>Paper v2</span></h1>
    <span class=model-tag>CFD Mode</span>
  </div>
  <div class=hdr-right>
    <div class=live-dot id=sd></div>
    <span class=hdr-time id=ck></span>
  </div>
</div>

<div class=wrap>

<div class=ticker>
  <span class=pair>EUR/GBP</span>
  <span class=price id=lp>&mdash;</span>
  <span class=time id=lt></span>
  <span class=status id=st>OFF</span>
</div>

<div class=ctrls>
  <button class="btn btn-start" id=bstart onclick="ctl('start')">&#9654; Start</button>
  <button class="btn btn-stop" id=bstop onclick="ctl('stop')">&#9208; Stop</button>
  <button class="btn btn-reset" onclick="if(confirm('Reset all paper trades?'))ctl('reset')">&#8634; Reset</button>
</div>

<div class=hero>
  <div class=hero-pnl>
    <div class=label>Net P&L</div>
    <div class=val id=hv>&mdash;</div>
    <div class=sub id=hs></div>
  </div>
  <div class=hero-ring>
    <canvas id=ring width=100 height=100></canvas>
    <div class=ring-txt>
      <div class=ring-pct id=rp>&mdash;</div>
      <div class=ring-sub>Win Rate</div>
    </div>
  </div>
</div>

<div class=chips id=chips></div>

<div class=sec>
  <div class=sec-h>Config</div>
  <div class=sec-body id=cfginfo style="font-size:12px;color:var(--mt);font-family:var(--mn)"></div>
</div>

<div class=sec>
  <div class=sec-h>Equity Curve</div>
  <div class=chart-wrap><canvas id=cv></canvas></div>
</div>

<div class=sec>
  <div class=sec-h>Today's Trades <span class=badge id=tc>0</span></div>
  <div id=todayTrades></div>
</div>

<div class=sec>
  <div class=sec-h>Daily P&L <span class=badge id=dc>0</span></div>
  <div id=dailyRows></div>
</div>

<div class=sec>
  <div class=sec-h>All Trades <span class=badge id=ac>0</span></div>
  <div id=allTrades></div>
</div>

<div class=sec id=errSec style="display:none">
  <div class=sec-h style="color:var(--rd)">Errors</div>
  <div class=errs id=errList></div>
</div>

</div>

<div class=bnav>
  <a href="/"><span class=nav-ico>&#128200;</span>Dashboard</a>
  <a href="/channel"><span class=nav-ico>&#128225;</span>Channel</a>
  <a href="/forex" class=active><span class=nav-ico>&#128177;</span>Forex</a>
</div>

<script>
const $=id=>document.getElementById(id);
const fmt=v=>{if(v==null)return'—';const a=Math.abs(v);
  if(a>=100000)return(v>=0?'+':'')+'₹'+(v/1000).toFixed(0)+'K';
  return(v>=0?'+':'')+'₹'+Math.round(v).toLocaleString('en-IN')};
const fmtC=v=>'₹'+Math.round(v).toLocaleString('en-IN');

function drawRing(pct){
  const c=$('ring'),ctx=c.getContext('2d'),w=c.width,h=c.height,cx=w/2,cy=h/2,r=40,lw=8;
  ctx.clearRect(0,0,w,h);
  ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);
  ctx.strokeStyle=getComputedStyle(document.documentElement).getPropertyValue('--bd');
  ctx.lineWidth=lw;ctx.stroke();
  if(pct>0){
    ctx.beginPath();ctx.arc(cx,cy,r,-Math.PI/2,-Math.PI/2+Math.PI*2*pct/100);
    ctx.strokeStyle=pct>=55?getComputedStyle(document.documentElement).getPropertyValue('--gn')
      :getComputedStyle(document.documentElement).getPropertyValue('--rd');
    ctx.lineWidth=lw;ctx.lineCap='round';ctx.stroke();
  }
}

function drawEquity(data){
  const c=$('cv'),ctx=c.getContext('2d');
  const dpr=window.devicePixelRatio||1;
  c.width=c.offsetWidth*dpr;c.height=160*dpr;ctx.scale(dpr,dpr);
  const W=c.offsetWidth,H=160;
  if(!data.length){ctx.fillStyle=getComputedStyle(document.documentElement).getPropertyValue('--mt');
    ctx.font='12px sans-serif';ctx.fillText('No data yet',W/2-30,H/2);return}
  const vals=data.map(d=>d.capital);
  const mn=Math.min(...vals)*0.998,mx=Math.max(...vals)*1.002;
  const x=i=>i/(data.length-1)*W;
  const y=v=>(1-(v-mn)/(mx-mn||1))*(H-30)+15;
  ctx.strokeStyle=getComputedStyle(document.documentElement).getPropertyValue('--bd');
  ctx.lineWidth=0.5;
  for(let g=0;g<4;g++){const gy=15+(H-30)/3*g;ctx.beginPath();ctx.moveTo(0,gy);ctx.lineTo(W,gy);ctx.stroke()}
  const last=vals[vals.length-1],start=150000;
  ctx.beginPath();
  data.forEach((d,i)=>{i?ctx.lineTo(x(i),y(d.capital)):ctx.moveTo(x(i),y(d.capital))});
  ctx.strokeStyle=last>=start?getComputedStyle(document.documentElement).getPropertyValue('--gn')
    :getComputedStyle(document.documentElement).getPropertyValue('--rd');
  ctx.lineWidth=2;ctx.stroke();
  ctx.lineTo(x(data.length-1),H);ctx.lineTo(0,H);ctx.closePath();
  ctx.fillStyle=last>=start?'rgba(34,197,94,.08)':'rgba(239,68,68,.08)';ctx.fill();
  ctx.beginPath();ctx.arc(x(data.length-1),y(last),4,0,Math.PI*2);
  ctx.fillStyle=last>=start?getComputedStyle(document.documentElement).getPropertyValue('--gn')
    :getComputedStyle(document.documentElement).getPropertyValue('--rd');ctx.fill();
  ctx.fillStyle=getComputedStyle(document.documentElement).getPropertyValue('--mt');
  ctx.font='10px '+getComputedStyle(document.documentElement).getPropertyValue('--mn');
  if(data.length>1){ctx.fillText(data[0].day,2,H-2);ctx.textAlign='right';ctx.fillText(data[data.length-1].day,W-2,H-2);ctx.textAlign='left'}
  ctx.fillText(fmtC(mx),2,12);
}

function renderTrades(trades, el){
  if(!trades.length){el.innerHTML='<div class=empty>No trades yet</div>';return}
  el.innerHTML=trades.map(t=>{
    const pipTxt=t.pips!=null?t.pips.toFixed(1)+' pips':'';
    const spreadTxt=t.spread_cost?'spread ₹'+t.spread_cost:'';
    return`<div class=tcard>
    <span class="dir ${t.direction}">${t.direction}</span>
    <div class=info><div class=strat>${t.strategy} · ${t.pair||'EUR/GBP'}</div>
      <div class=meta>${t.ts} · ${t.entry} → ${t.exit||t.expiry}</div>
      <div class=pips>${pipTxt} ${spreadTxt?'· '+spreadTxt:''}</div></div>
    <div class="res ${t.result}">${t.result==='WIN'?'+₹'+Math.round(t.pnl).toLocaleString('en-IN')
      :t.result==='LOSS'?'-₹'+Math.abs(Math.round(t.pnl)).toLocaleString('en-IN'):'DRAW'}</div>
  </div>`}).join('');
}

function renderDaily(days){
  if(!days.length){$('dailyRows').innerHTML='<div class=empty>No data yet</div>';return}
  const mx=Math.max(...days.map(d=>Math.abs(d.pnl)),1);
  $('dailyRows').innerHTML=days.map(d=>{
    const pct=Math.min(Math.abs(d.pnl)/mx*100,100);
    const clr=d.pnl>=0?'var(--gn)':'var(--rd)';
    return`<div class=drow><span class=day>${d.day.slice(5)}</span>
      <span class=bar><span class=bar-fill style="width:${pct}%;background:${clr}"></span></span>
      <span class="dpnl ${d.pnl>=0?'pos':'neg'}">${fmt(d.pnl)}</span>
      <span class=dwr>${d.win_rate}%</span></div>`
  }).join('');
}

let ws;
function connectWS(){
  const proto=location.protocol==='https:'?'wss:':'ws:';
  ws=new WebSocket(proto+'//'+location.host+'/ws');
  ws.onmessage=e=>{
    const d=JSON.parse(e.data);
    if(d.type==='tick'){
      if(d.prices){
        const p=Object.values(d.prices)[0];
        $('lp').textContent=typeof p==='number'?p.toFixed(5):'—';
      }
      $('lt').textContent=d.time||'';
    }
    if(d.type==='trade'){load()}
  };
  ws.onclose=()=>setTimeout(connectWS,3000);
}

async function ctl(action){
  await fetch('/api/forex/'+action,{method:'POST'});
  load();
}

async function load(){
  try{
    const [status,trades,daily,equity]=await Promise.all([
      fetch('/api/forex/status').then(r=>r.json()),
      fetch('/api/forex/trades?limit=50').then(r=>r.json()),
      fetch('/api/forex/daily').then(r=>r.json()),
      fetch('/api/forex/equity').then(r=>r.json()),
    ]);
    const s=status;
    $('sd').className='live-dot '+(s.running?'on':'off');
    $('st').textContent=s.running?'LIVE':'OFF';
    $('st').className='status '+(s.running?'on':'off');
    $('ck').textContent=s.now?.split(' ').slice(1).join(' ')||'';
    $('bstart').disabled=s.running;
    $('bstop').disabled=!s.running;
    if(s.last_price){
      const p=Object.values(s.last_price)[0];
      $('lp').textContent=typeof p==='number'?p.toFixed(5):'—';
    }
    if(s.last_poll)$('lt').textContent=s.last_poll;
    const net=s.net_pnl;
    $('hv').className='val '+(net>=0?'pos':'neg');
    $('hv').textContent=fmt(net);
    $('hs').textContent=fmtC(s.capital)+' capital · '+s.net_pct+'%';
    $('rp').textContent=s.win_rate+'%';
    $('rp').className='ring-pct '+(s.win_rate>=55?'pos':'neg');
    drawRing(s.win_rate);
    $('chips').innerHTML=[
      ['Today P&L',fmt(s.today_pnl),s.today_pnl>=0?'pos':'neg'],
      ['Trades',s.total_trades,''],
      ['Today',s.today_trades,''],
      ['Total Pips',s.total_pips,''],
      ['Avg Pip/Trade',s.avg_pips_per_trade,''],
      ['Spread Cost',fmt(s.total_spread_cost),'neg'],
    ].map(([l,v,c])=>`<div class=chip><div class="cv ${c}">${v}</div><div class=cl>${l}</div></div>`).join('');
    const pc=s.pair_config||{};
    const cfgParts=Object.entries(pc).map(([k,v])=>k+': '+v.spread+'pip spread, '+v.lot+' lot');
    $('cfginfo').innerHTML=cfgParts.join(' | ')+(s.started_at?' · started '+s.started_at:'');
    const today=s.now?.split(' ')[0];
    const todayTrades=trades.filter(t=>t.day===today);
    $('tc').textContent=todayTrades.length;
    renderTrades(todayTrades,$('todayTrades'));
    $('dc').textContent=daily.length;
    renderDaily(daily);
    drawEquity(equity);
    $('ac').textContent=s.total_trades;
    renderTrades(trades,$('allTrades'));
    if(s.errors&&s.errors.length){
      $('errSec').style.display='';
      $('errList').textContent=s.errors.join('\n');
    }else{$('errSec').style.display='none'}
  }catch(e){console.error(e)}
}

connectWS();
load();
setInterval(load,30000);
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)

"""Forex Paper Trading Dashboard — Multi-Strategy Portfolio v3.

Reads live state from forex_app_v3.py (JSON files) and serves a dashboard.
Mounted on main dashboard at /forex, or run standalone on port 8002.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

UTC = ZoneInfo("UTC")
IST = ZoneInfo("Asia/Kolkata")

app = FastAPI(title="Forex Portfolio v3", docs_url=None, redoc_url=None)

DATA_DIR = Path(os.path.expanduser("~/Trading-Buddy/data"))
TRADES_FILE = DATA_DIR / "forex_paper_trades_v3.json"
STATE_FILE = DATA_DIR / "forex_state_v3.json"
STARTING_CAPITAL = 150_000

_ws_clients: set[WebSocket] = set()
_stop_event = threading.Event()
_loop: asyncio.AbstractEventLoop | None = None

_state: dict[str, Any] = {
    "running": False,
    "capital": STARTING_CAPITAL,
    "peak_capital": STARTING_CAPITAL,
    "trades": [],
    "daily_pnl": {},
    "total_wins": 0,
    "total_losses": 0,
    "total_draws": 0,
    "max_drawdown": 0,
    "max_win_streak": 0,
    "max_loss_streak": 0,
    "started_at": None,
    "last_poll": None,
    "last_price": {},
    "errors": [],
}


def _load_state() -> None:
    trades = []
    if TRADES_FILE.exists():
        try:
            trades = json.loads(TRADES_FILE.read_text())
        except Exception:
            pass
    state_data = {}
    if STATE_FILE.exists():
        try:
            state_data = json.loads(STATE_FILE.read_text())
        except Exception:
            pass

    _state["trades"] = trades
    _state["running"] = len(state_data.get("open_positions", [])) > 0 or True
    _state["open_positions"] = state_data.get("open_positions", [])

    wins = sum(1 for t in trades if t.get("won"))
    losses = sum(1 for t in trades if not t.get("won"))
    _state["total_wins"] = wins
    _state["total_losses"] = losses

    daily = defaultdict(float)
    for t in trades:
        day = t.get("exit_time", "")[:10]
        if day:
            daily[day] += t.get("pnl", 0)
    _state["daily_pnl"] = dict(daily)

    total_pnl = sum(t.get("pnl", 0) for t in trades)
    _state["capital"] = STARTING_CAPITAL + total_pnl
    _state["peak_capital"] = max(STARTING_CAPITAL, _state["capital"])

    eq = 0
    peak = 0
    mdd = 0
    ws = 0
    ls = 0
    mws = 0
    mls = 0
    for t in trades:
        eq += t.get("pnl", 0)
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        if t.get("won"):
            ws += 1; ls = 0
        else:
            ls += 1; ws = 0
        mws = max(mws, ws)
        mls = max(mls, ls)
    _state["max_drawdown"] = mdd
    _state["max_win_streak"] = mws
    _state["max_loss_streak"] = mls
    _state["started_at"] = trades[0].get("entry_time", "")[:16] if trades else None


def _save_state() -> None:
    pass


def _poll_loop() -> None:
    while not _stop_event.is_set():
        _load_state()
        if _loop and _ws_clients:
            asyncio.run_coroutine_threadsafe(_ws_broadcast({"type": "tick"}), _loop)
        _stop_event.wait(30)


async def _ws_broadcast(data: dict) -> None:
    msg = json.dumps(data)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _ws_clients -= dead


# ── API ─────────────────────────────────────────────────────────────────────
@app.get("/api/forex/status")
def api_status() -> JSONResponse:
    _load_state()
    trades = _state["trades"]
    total = len(trades)
    wins = _state["total_wins"]
    wr = (wins / total * 100) if total else 0
    net = _state["capital"] - STARTING_CAPITAL
    day_key = datetime.now(IST).strftime("%Y-%m-%d")
    today_pnl = _state["daily_pnl"].get(day_key, 0)
    today_trades = sum(1 for t in trades if t.get("exit_time", "")[:10] == day_key)
    green_days = sum(1 for v in _state["daily_pnl"].values() if v > 0)
    red_days = sum(1 for v in _state["daily_pnl"].values() if v < 0)
    dd_pct = (_state["max_drawdown"] / _state["peak_capital"] * 100) if _state["peak_capital"] else 0

    open_pos = _state.get("open_positions", [])
    strat_perf = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0, "pips": 0})
    for t in trades:
        key = f"{t['strat']} {t['pair']} {t['tf']}"
        strat_perf[key]["trades"] += 1
        strat_perf[key]["pnl"] += t.get("pnl", 0)
        strat_perf[key]["pips"] += t.get("pips", 0)
        if t.get("won"):
            strat_perf[key]["wins"] += 1

    return JSONResponse({
        "running": _state["running"],
        "capital": round(_state["capital"]),
        "starting_capital": STARTING_CAPITAL,
        "net_pnl": round(net),
        "net_pct": round(net / STARTING_CAPITAL * 100, 1),
        "peak_capital": round(_state["peak_capital"]),
        "max_drawdown": round(_state["max_drawdown"]),
        "max_drawdown_pct": round(dd_pct, 1),
        "total_trades": total,
        "wins": wins,
        "losses": _state["total_losses"],
        "draws": 0,
        "win_rate": round(wr, 1),
        "max_win_streak": _state["max_win_streak"],
        "max_loss_streak": _state["max_loss_streak"],
        "today_pnl": round(today_pnl),
        "today_trades": today_trades,
        "last_price": _state.get("last_price", {}),
        "last_poll": _state.get("last_poll"),
        "green_days": green_days,
        "red_days": red_days,
        "total_days": green_days + red_days,
        "trade_amount": "1.0 lot",
        "payout_pct": 0,
        "daily_loss_cap": 0,
        "pair_daily_loss_cap": 0,
        "pairs": ["GBP/USD", "EUR/USD", "USD/JPY", "GBP/JPY"],
        "started_at": _state["started_at"],
        "errors": _state["errors"][-5:],
        "now": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "open_positions": open_pos,
        "strategy_perf": dict(strat_perf),
        "total_pips": round(sum(t.get("pips", 0) for t in trades), 1),
    })


@app.get("/api/forex/trades")
def api_trades(limit: int = 50) -> JSONResponse:
    _load_state()
    trades = _state["trades"][-limit:][::-1]
    clean = []
    for t in trades:
        clean.append({
            "direction": t.get("direction", ""),
            "strategy": f"{t.get('strat','')} {t.get('pair','')} {t.get('tf','')}",
            "ts": (t.get("exit_time", "")[:16]).replace("T", " "),
            "entry": str(round(t.get("entry_price", 0), 5)),
            "expiry": str(round(t.get("exit_price", 0), 5)),
            "pnl": round(t.get("pnl", 0)),
            "pips": round(t.get("pips", 0), 1),
            "result": "WIN" if t.get("won") else "LOSS",
            "reason": t.get("reason", ""),
            "day": t.get("exit_time", "")[:10],
            "candles_held": t.get("candles_held", 0),
        })
    return JSONResponse(clean)


@app.get("/api/forex/daily")
def api_daily() -> JSONResponse:
    _load_state()
    daily = []
    trades = _state["trades"]
    for day in sorted(_state["daily_pnl"].keys()):
        pnl = _state["daily_pnl"][day]
        day_trades = [t for t in trades if t.get("exit_time", "")[:10] == day]
        wins = sum(1 for t in day_trades if t.get("won"))
        losses = len(day_trades) - wins
        wr = (wins / (wins + losses) * 100) if (wins + losses) else 0
        daily.append({"day": day, "pnl": round(pnl), "trades": len(day_trades),
                      "wins": wins, "losses": losses, "win_rate": round(wr, 1)})
    return JSONResponse(daily[::-1])


@app.get("/api/forex/equity")
def api_equity() -> JSONResponse:
    _load_state()
    curve = []
    running = STARTING_CAPITAL
    for day in sorted(_state["daily_pnl"].keys()):
        running += _state["daily_pnl"][day]
        curve.append({"day": day, "capital": round(running)})
    return JSONResponse(curve)


@app.post("/api/forex/start")
def api_start() -> JSONResponse:
    return JSONResponse({"ok": True, "msg": "v3 bot runs independently — check logs/forex_v3.log"})

@app.post("/api/forex/stop")
def api_stop() -> JSONResponse:
    return JSONResponse({"ok": True, "msg": "SSH into VM and pkill -f forex_app_v3 to stop"})

@app.post("/api/forex/reset")
def api_reset() -> JSONResponse:
    return JSONResponse({"ok": False, "reason": "Reset not available — delete data/forex_paper_trades_v3.json manually"})


# ── Page ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _PAGE


_PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,user-scalable=no">
<title>Forex Portfolio v3</title>
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
@media(prefers-color-scheme:light){:root:not([data-theme=dark]){
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
.wrap{max-width:520px;margin:0 auto;padding:12px 16px 80px}
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
.sec{background:var(--sf);border:1px solid var(--bd);border-radius:12px;margin-bottom:12px;overflow:hidden}
.sec-h{padding:12px 14px 8px;font-size:13px;font-weight:700;display:flex;align-items:center;gap:6px}
.badge{background:var(--bld);color:var(--bl);font-size:10px;padding:2px 7px;border-radius:8px;font-weight:600}
.sec-body{padding:0 14px 12px}
.tcard{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--bd)}
.tcard:last-child{border-bottom:none}
.tcard .dir{font-size:10px;font-weight:700;padding:3px 8px;border-radius:6px;letter-spacing:.3px;flex-shrink:0}
.tcard .dir.CALL{background:var(--gd);color:var(--gn)}
.tcard .dir.PUT{background:var(--rdd);color:var(--rd)}
.tcard .info{flex:1;min-width:0}
.tcard .strat{font-size:12px;font-weight:600}
.tcard .meta{font-size:10px;color:var(--mt);font-family:var(--mn)}
.tcard .res{font-size:13px;font-weight:700;font-family:var(--mn);text-align:right}
.tcard .res.WIN{color:var(--gn)}.tcard .res.LOSS{color:var(--rd)}
.drow{display:flex;align-items:center;padding:8px 14px;border-bottom:1px solid var(--bd);font-size:12px}
.drow:last-child{border-bottom:none}
.drow .day{width:90px;font-weight:600;font-family:var(--mn)}
.drow .bar{flex:1;height:6px;border-radius:3px;background:var(--el);overflow:hidden;margin:0 10px}
.drow .bar-fill{height:100%;border-radius:3px}
.drow .dpnl{width:80px;text-align:right;font-weight:700;font-family:var(--mn);font-variant-numeric:tabular-nums}
.drow .dwr{width:40px;text-align:right;color:var(--mt);font-family:var(--mn)}
.chart-wrap{padding:8px 14px 14px}
.chart-wrap canvas{width:100%;height:160px}
.empty{color:var(--mt);font-size:13px;padding:16px 14px;text-align:center}
.bnav{position:fixed;bottom:0;left:0;right:0;background:var(--sf);border-top:1px solid var(--bd);
  display:flex;justify-content:center;padding:8px 0 max(8px,env(safe-area-inset-bottom));z-index:50}
.bnav a{color:var(--mt);text-decoration:none;font-size:10px;text-align:center;padding:4px 16px}
.bnav a.active{color:var(--cy)}
.bnav .nav-ico{font-size:18px;display:block}

/* Open positions */
.opos{padding:8px 14px;border-bottom:1px solid var(--bd);font-size:12px;font-family:var(--mn)}
.opos:last-child{border-bottom:none}
.opos .op-pair{font-weight:700;color:var(--cy)}
.opos .op-dir{font-size:10px;font-weight:700;padding:2px 6px;border-radius:4px;margin-left:6px}
.opos .op-dir.CALL{background:var(--gd);color:var(--gn)}
.opos .op-dir.PUT{background:var(--rdd);color:var(--rd)}
.opos .op-meta{color:var(--mt);margin-top:2px;font-size:10px}

/* Strategy perf table */
.srow{display:flex;align-items:center;padding:8px 14px;border-bottom:1px solid var(--bd);font-size:11px;font-family:var(--mn)}
.srow:last-child{border-bottom:none}
.srow .sname{flex:1;font-weight:600;font-size:12px}
.srow .spnl{width:80px;text-align:right;font-weight:700}
.srow .swr{width:40px;text-align:right;color:var(--mt)}
.srow .str{width:30px;text-align:right;color:var(--mt)}
.srow .spips{width:50px;text-align:right;color:var(--mt)}

@media(min-width:600px){.wrap{max-width:560px}.hero-pnl .val{font-size:36px}}
</style></head><body>

<div class=hdr>
  <div style="display:flex;align-items:center;gap:8px">
    <a href="/" class=back>&larr; Dashboard</a>
    <h1>Forex <span>Portfolio v3</span></h1>
  </div>
  <div class=hdr-right>
    <div class=live-dot id=sd></div>
    <span class=hdr-time id=ck></span>
  </div>
</div>

<div class=wrap>

<!-- Hero P&L -->
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

<!-- Stat chips -->
<div class=chips id=chips></div>

<!-- Open positions -->
<div class=sec>
  <div class=sec-h>Open Positions <span class=badge id=opc>0</span></div>
  <div id=openPos></div>
</div>

<!-- Strategy performance -->
<div class=sec>
  <div class=sec-h>Strategy Performance</div>
  <div id=stratPerf></div>
</div>

<!-- Equity curve -->
<div class=sec>
  <div class=sec-h>Equity Curve</div>
  <div class=chart-wrap><canvas id=cv></canvas></div>
</div>

<!-- Daily breakdown -->
<div class=sec>
  <div class=sec-h>Daily P&L <span class=badge id=dc>0</span></div>
  <div id=dailyRows></div>
</div>

<!-- Recent trades -->
<div class=sec>
  <div class=sec-h>Recent Trades <span class=badge id=ac>0</span></div>
  <div id=allTrades></div>
</div>

</div>

<div class=bnav>
  <a href="/"><span class=nav-ico>📈</span>Dashboard</a>
  <a href="/channel"><span class=nav-ico>📡</span>Channel</a>
  <a href="/forex" class=active><span class=nav-ico>💱</span>Forex</a>
</div>

<script>
const $=id=>document.getElementById(id);
const fmt=v=>{if(v==null)return'—';const a=Math.abs(v);
  if(a>=100000)return(v>=0?'+':'')+'₹'+(v/1000).toFixed(0)+'K';
  return(v>=0?'+':'')+'₹'+v.toLocaleString('en-IN')};
const fmtC=v=>'₹'+Math.round(v).toLocaleString('en-IN');

function drawRing(pct){
  const c=$('ring'),ctx=c.getContext('2d'),w=c.width,h=c.height,cx=w/2,cy=h/2,r=40,lw=8;
  ctx.clearRect(0,0,w,h);
  ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);
  ctx.strokeStyle=getComputedStyle(document.documentElement).getPropertyValue('--bd');
  ctx.lineWidth=lw;ctx.stroke();
  if(pct>0){
    ctx.beginPath();ctx.arc(cx,cy,r,-Math.PI/2,-Math.PI/2+Math.PI*2*pct/100);
    ctx.strokeStyle=pct>=50?getComputedStyle(document.documentElement).getPropertyValue('--gn')
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
  el.innerHTML=trades.map(t=>`<div class=tcard>
    <span class="dir ${t.direction}">${t.direction}</span>
    <div class=info><div class=strat>${t.strategy}</div>
      <div class=meta>${t.ts} · ${t.entry} → ${t.expiry} · ${t.pips}p · ${t.reason} · ${t.candles_held}c</div></div>
    <div class="res ${t.result}">${t.result==='WIN'?'+':''}₹${Math.abs(t.pnl).toLocaleString('en-IN')}</div>
  </div>`).join('');
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

function renderOpenPositions(positions){
  if(!positions||!positions.length){$('openPos').innerHTML='<div class=empty>No open positions</div>';return}
  $('openPos').innerHTML=positions.map(p=>`<div class=opos>
    <span class=op-pair>${p.pair}</span>
    <span class="op-dir ${p.direction}">${p.direction}</span>
    <span style="margin-left:6px;font-weight:600">${p.strat} ${p.tf}</span>
    <div class=op-meta>Entry ${p.entry_price?.toFixed(5)} · SL ${p.sl_price?.toFixed(5)} · TP ${p.tp_price?.toFixed(5)} · ${p.candles_held||0} candles held</div>
  </div>`).join('');
}

function renderStratPerf(perf){
  if(!perf||!Object.keys(perf).length){$('stratPerf').innerHTML='<div class=empty>No data yet</div>';return}
  const entries=Object.entries(perf).sort((a,b)=>b[1].pnl-a[1].pnl);
  $('stratPerf').innerHTML=entries.map(([name,s])=>{
    const wr=s.trades?Math.round(s.wins/s.trades*100):0;
    const cls=s.pnl>=0?'pos':'neg';
    return`<div class=srow>
      <span class=sname>${name}</span>
      <span class=str>${s.trades}</span>
      <span class=swr>${wr}%</span>
      <span class=spips>${s.pips?.toFixed(1)||0}p</span>
      <span class="spnl ${cls}">${fmt(s.pnl)}</span>
    </div>`
  }).join('');
}

let ws;
function connectWS(){
  const proto=location.protocol==='https:'?'wss:':'ws:';
  ws=new WebSocket(proto+'//'+location.host+'/ws');
  ws.onmessage=e=>{const d=JSON.parse(e.data);if(d.type==='tick'||d.type==='trade')load()};
  ws.onclose=()=>setTimeout(connectWS,3000);
  ws.onerror=()=>{};
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
    $('ck').textContent=s.now?.split(' ').slice(1).join(' ')||'';

    const net=s.net_pnl;
    $('hv').className='val '+(net>=0?'pos':'neg');
    $('hv').textContent=fmt(net);
    $('hs').textContent=fmtC(s.capital)+' capital · '+s.net_pct+'% · '+s.total_pips+'p total';

    $('rp').textContent=s.win_rate+'%';
    $('rp').className='ring-pct '+(s.win_rate>=50?'pos':'neg');
    drawRing(s.win_rate);

    $('chips').innerHTML=[
      ['Today P&L',fmt(s.today_pnl),s.today_pnl>=0?'pos':'neg'],
      ['Trades',s.total_trades,''],
      ['Today',s.today_trades,''],
      ['Green Days',s.green_days+'/'+s.total_days,'pos'],
      ['Max DD',fmtC(s.max_drawdown),'neg'],
      ['Streaks',s.max_win_streak+'W / '+s.max_loss_streak+'L',''],
    ].map(([l,v,c])=>`<div class=chip><div class="cv ${c}">${v}</div><div class=cl>${l}</div></div>`).join('');

    $('opc').textContent=s.open_positions?.length||0;
    renderOpenPositions(s.open_positions);
    renderStratPerf(s.strategy_perf);

    $('dc').textContent=daily.length;
    renderDaily(daily);
    drawEquity(equity);

    $('ac').textContent=s.total_trades;
    renderTrades(trades,$('allTrades'));
  }catch(e){console.error(e)}
}

try{connectWS()}catch(e){}
load();
setInterval(load,30000);
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)

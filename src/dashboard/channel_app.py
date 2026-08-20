"""Standalone channel trades dashboard — runs on port 8001.

Lightweight FastAPI app showing only channel signal trades.
Separate from the main dashboard (port 8000) for independent operation.

Run:  python -m src.dashboard.channel_app
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

import config
from src.storage import db
from src.utils import market_calendar as mc

load_dotenv()
app = FastAPI(title="Channel Trades", docs_url=None, redoc_url=None)

_CHANNEL_FILTER_BASE = "(symbol LIKE '% % CE' OR symbol LIKE '% % PE')"

# Channel 1 = old trades (channel IS NULL or 'ch1'), Channel 2 = 'ch2'
_CH_FILTERS = {
    "ch1": f"({_CHANNEL_FILTER_BASE} AND (channel IS NULL OR channel = 'ch1'))",
    "ch1b": f"({_CHANNEL_FILTER_BASE} AND channel = 'ch1b')",
    "ch2": f"({_CHANNEL_FILTER_BASE} AND channel = 'ch2' AND ts >= '2026-08-20')",
    "ch3": f"({_CHANNEL_FILTER_BASE} AND channel = 'ch3')",
    "ch5": f"({_CHANNEL_FILTER_BASE} AND channel = 'ch5')",
    "oeh": f"({_CHANNEL_FILTER_BASE} AND channel = 'oeh')",
    "ch1f": f"({_CHANNEL_FILTER_BASE} AND (channel IS NULL OR channel = 'ch1') AND filter_score >= 50)",
}


def _ch_filter(channel: str) -> str:
    return _CH_FILTERS.get(channel, _CH_FILTERS["ch1"])


def _rows(query: str, params: tuple = ()) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


@app.get("/api/trades")
def api_trades(channel: str = "ch1") -> JSONResponse:
    db.init_db()
    cf = _ch_filter(channel)
    rows = _rows(
        f"SELECT id, ts, symbol, side, qty, price, exit_price, pnl, mode, status, "
        f"stop_price, target_price, index_entry, broker_key, charges "
        f"FROM trades WHERE {cf} ORDER BY id DESC LIMIT 100")
    return JSONResponse(rows)


@app.get("/api/stats")
def api_stats(channel: str = "ch1") -> JSONResponse:
    db.init_db()
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


@app.get("/api/ltp")
def api_ltp(channel: str = "ch1") -> JSONResponse:
    """Fetch live LTP for all open channel trades that have a broker_key."""
    db.init_db()
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
    db.init_db()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, price, qty FROM trades WHERE id = ? AND status = 'OPEN'",
            (trade_id,)).fetchone()
        if not row:
            return JSONResponse({"closed": False, "reason": "Trade not found or already closed"}, status_code=404)
        conn.execute(
            "UPDATE trades SET status = 'CLOSED', exit_price = price, pnl = 0 WHERE id = ?",
            (trade_id,))
    return JSONResponse({"closed": True, "trade_id": trade_id, "pnl": 0})


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
  padding:10px 12px;margin-bottom:6px;display:flex;align-items:center;gap:10px}
.tc-icon{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;
  justify-content:center;font-size:14px;font-weight:800;flex-shrink:0}
.tc-icon.w{background:var(--gd);color:var(--gn)}
.tc-icon.l{background:var(--rdd);color:var(--rd)}
.tc-body{flex:1;min-width:0}
.tc-sym{font-size:12px;font-weight:700;font-family:var(--mn);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.tc-meta{font-size:10px;color:var(--mt)}
.tc-pnl{font-size:14px;font-weight:800;font-family:var(--mn);text-align:right;flex-shrink:0}

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

.pos{color:var(--gn)}.neg{color:var(--rd)}

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
  <button class="tab" onclick="switchCh('oeh')" id="tab-oeh"><span class=ico>H</span> OEH</button>
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

<!-- Bottom nav -->
<div class=bnav>
  <button class=active id=nav-trades onclick="switchView('trades')"><span class=nav-ico>&#9776;</span>Trades</button>
  <button id=nav-scan onclick="switchView('scan')"><span class=nav-ico>&#9881;</span>Scanner</button>
  <button id=nav-refresh onclick="load()"><span class=nav-ico>&#8635;</span>Refresh</button>
</div>

<script>
const $=id=>document.getElementById(id);
let AT=[],CF='all',LTP={},CH='ch1',VIEW='trades';
let _loading=false,_scanCache=null,_scanCacheTs=0,_refreshTimer=null;
const REFRESH_MS=30000,SCAN_CACHE_MS=300000;

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
    return '<div class=trade-card>'+
      '<div class="tc-icon '+(isW?'w':'l')+'">'+(isW?'W':'L')+'</div>'+
      '<div class=tc-body><div class=tc-sym>'+t.symbol+'</div>'+
        '<div class=tc-meta>'+tf(t.ts)+' | Qty '+t.qty+'</div></div>'+
      '<div class="tc-pnl '+pc(t.pnl)+'">'+inr(t.pnl)+'</div>'+
    '</div>'
  }).join('');
}

function sF(f){CF=f;rH(AT)}

function switchCh(ch){
  if(ch===CH)return;
  CH=ch;CF='all';
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  $('tab-'+ch).classList.add('active');
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

async function load(){
  if(_loading)return;
  _loading=true;
  try{
    const q='?channel='+CH;
    const [s,t]=await Promise.all([fetch('/api/stats'+q).then(r=>r.json()),fetch('/api/trades'+q).then(r=>r.json())]);
    AT=t;$('ck').textContent=s.now;$('sd').className='live-dot';
    rHero(s);rRing(s);rChips(s);rC(s.pnl_curve);rH(t);
    // LTP fetch is fire-and-forget — don't block render
    fetch('/api/ltp'+q).then(r=>r.json()).then(ltp=>{if(!ltp.error){LTP=ltp;rO(t)}}).catch(()=>{});
    rO(t);
  }catch(e){$('sd').className='live-dot off'}
  finally{_loading=false}
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

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
    "ch2": f"({_CHANNEL_FILTER_BASE} AND channel = 'ch2')",
    "ch3": f"({_CHANNEL_FILTER_BASE} AND channel = 'ch3')",
    "ch5": f"({_CHANNEL_FILTER_BASE} AND channel = 'ch5')",
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
    with db.get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM trades WHERE {cf}").fetchone()[0]
        open_count = conn.execute(f"SELECT COUNT(*) FROM trades WHERE {cf} AND status='OPEN'").fetchone()[0]
        closed = conn.execute(f"SELECT COUNT(*) FROM trades WHERE {cf} AND status LIKE 'CLOSED%'").fetchone()[0]
        wins = conn.execute(f"SELECT COUNT(*) FROM trades WHERE {cf} AND pnl > 0").fetchone()[0]
        losses = conn.execute(f"SELECT COUNT(*) FROM trades WHERE {cf} AND pnl IS NOT NULL AND pnl <= 0").fetchone()[0]
        total_pnl = conn.execute(f"SELECT COALESCE(SUM(pnl),0) FROM trades WHERE {cf} AND pnl IS NOT NULL").fetchone()[0]
        best = conn.execute(f"SELECT COALESCE(MAX(pnl),0) FROM trades WHERE {cf}").fetchone()[0]
        worst = conn.execute(f"SELECT COALESCE(MIN(pnl),0) FROM trades WHERE {cf} AND pnl IS NOT NULL").fetchone()[0]
        avg_pnl = conn.execute(f"SELECT COALESCE(AVG(pnl),0) FROM trades WHERE {cf} AND pnl IS NOT NULL").fetchone()[0]
        today_iso = mc.now_ist().date().isoformat()
        today_pnl = conn.execute(
            f"SELECT COALESCE(SUM(pnl),0) FROM trades WHERE {cf} AND pnl IS NOT NULL AND ts >= ?",
            (today_iso,)).fetchone()[0]
        today_count = conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE {cf} AND ts >= ?", (today_iso,)).fetchone()[0]
        total_charges = conn.execute(
            f"SELECT COALESCE(SUM(charges),0) FROM trades WHERE {cf} AND charges IS NOT NULL").fetchone()[0]
        today_charges = conn.execute(
            f"SELECT COALESCE(SUM(charges),0) FROM trades WHERE {cf} AND charges IS NOT NULL AND ts >= ?",
            (today_iso,)).fetchone()[0]
        pnl_series = [dict(r) for r in conn.execute(
            f"SELECT ts, pnl FROM trades WHERE {cf} AND pnl IS NOT NULL ORDER BY id"
        ).fetchall()]
        utilized = conn.execute(
            f"SELECT COALESCE(SUM(price * qty), 0) FROM trades WHERE {cf} AND status = 'OPEN'"
        ).fetchone()[0]

    capital = 100000
    win_rate = (wins / closed * 100) if closed > 0 else 0
    cumulative = []
    running = 0
    for row in pnl_series:
        running += row["pnl"]
        cumulative.append({"ts": row["ts"], "pnl": row["pnl"], "cumulative": round(running, 2)})

    return JSONResponse({
        "total": total, "open": open_count, "closed": closed,
        "wins": wins, "losses": losses, "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2), "best_trade": round(best, 2),
        "worst_trade": round(worst, 2), "avg_pnl": round(avg_pnl, 2),
        "today_pnl": round(today_pnl, 2), "today_count": today_count,
        "total_charges": round(total_charges, 2), "today_charges": round(today_charges, 2),
        "capital": capital, "utilized": round(utilized, 2),
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
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Channel Trades — Trading Buddy</title>
<style>
.tabs{display:flex;gap:0;margin-bottom:16px;border-bottom:2px solid var(--bd)}
.tab{padding:8px 20px;font-size:13px;font-weight:600;cursor:pointer;border:none;
  background:none;color:var(--mt);border-bottom:2px solid transparent;margin-bottom:-2px;
  font-family:var(--sn);transition:all .15s}
.tab:hover{color:var(--tx)}
.tab.active{color:var(--bl);border-bottom-color:var(--bl)}
.ch-label{font-size:11px;color:var(--mt);font-weight:400;margin-left:4px}
</style>
<style>
:root{
  --bg:#0b0f14;--sf:#121820;--el:#1a2230;--bd:#232d3d;
  --tx:#dfe6ee;--mt:#6b7a8d;--ft:#3a4858;
  --gn:#22c55e;--gd:rgba(34,197,94,.12);
  --rd:#ef4444;--rdd:rgba(239,68,68,.12);
  --bl:#3b82f6;--bld:rgba(59,130,246,.1);
  --am:#f59e0b;--amd:rgba(245,158,11,.12);
  --mn:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sn:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
}
@media(prefers-color-scheme:light){:root{
  --bg:#f5f7fa;--sf:#fff;--el:#fff;--bd:#e2e8f0;
  --tx:#1e293b;--mt:#64748b;--ft:#cbd5e1;
  --gn:#16a34a;--gd:rgba(22,163,74,.08);
  --rd:#dc2626;--rdd:rgba(220,38,38,.08);
  --bl:#2563eb;--bld:rgba(37,99,235,.06);
  --am:#d97706;--amd:rgba(217,119,6,.08);
}}
:root[data-theme=light]{
  --bg:#f5f7fa;--sf:#fff;--el:#fff;--bd:#e2e8f0;
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

*{box-sizing:border-box;margin:0}
body{font-family:var(--sn);background:var(--bg);color:var(--tx);padding:0;line-height:1.5}
.wrap{max-width:1120px;margin:0 auto;padding:14px 16px 32px}

.top-bar{display:flex;align-items:center;gap:10px;padding:10px 0 14px;flex-wrap:wrap}
.top-bar h1{font-size:17px;font-weight:700;letter-spacing:-.01em}
.dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.dot.on{background:var(--gn);box-shadow:0 0 5px var(--gn)}
.dot.off{background:var(--rd)}
.top-bar .ts{font-size:11px;color:var(--mt);margin-left:auto;font-family:var(--mn)}

.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:6px;margin-bottom:16px}
.s{background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:10px 12px}
.s .l{font-size:10px;color:var(--mt);text-transform:uppercase;letter-spacing:.05em}
.s .v{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;font-family:var(--mn);margin-top:2px}
.s .d{font-size:10px;color:var(--mt);margin-top:1px;font-variant-numeric:tabular-nums}
.pos{color:var(--gn)}.neg{color:var(--rd)}

.sec{margin-bottom:18px}
.sec-h{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;
  color:var(--mt);margin-bottom:8px;display:flex;align-items:center;gap:6px}
.badge{background:var(--bld);color:var(--bl);font-size:10px;padding:1px 6px;border-radius:8px;font-weight:700}

.cw{background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:12px;margin-bottom:14px;position:relative}
.cw canvas{width:100%;height:160px;display:block}
.cl{position:absolute;top:10px;right:12px;font-size:10px;color:var(--mt);font-family:var(--mn)}

.tw{overflow-x:auto;border:1px solid var(--bd);border-radius:8px;background:var(--sf)}
table{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
thead th{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;
  color:var(--mt);padding:8px 10px;text-align:left;border-bottom:1px solid var(--bd);
  position:sticky;top:0;background:var(--sf);z-index:1}
td{padding:7px 10px;border-bottom:1px solid var(--bd);white-space:nowrap;font-family:var(--mn);font-size:11px}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--bld)}

.pill{display:inline-block;padding:1px 7px;border-radius:5px;font-size:10px;font-weight:700}
.pill.op{background:var(--amd);color:var(--am)}
.pill.cl{background:var(--gd);color:var(--gn)}
.pill.sl{background:var(--rdd);color:var(--rd)}

.cb{padding:3px 8px;border:1px solid var(--bd);border-radius:5px;background:var(--sf);
  color:var(--rd);font-size:10px;font-weight:600;cursor:pointer}
.cb:hover{background:var(--rdd);border-color:var(--rd)}

.empty{padding:24px;text-align:center;color:var(--mt);font-size:12px}

.fb{display:flex;gap:4px;margin-bottom:8px}
.fb button{padding:4px 10px;border:1px solid var(--bd);border-radius:5px;
  background:var(--sf);color:var(--mt);font-size:11px;cursor:pointer;font-weight:500}
.fb button.a{background:var(--bld);color:var(--bl);border-color:var(--bl)}

@media(max-width:600px){
  .stats{grid-template-columns:repeat(2,1fr)}
  .s .v{font-size:17px}
}
</style></head><body>
<div class=wrap>

<div class=top-bar>
  <h1>Channel Trades</h1>
  <div class=dot id=sd></div>
  <span style="font-size:11px;color:var(--mt)" id=st>...</span>
  <span class=ts id=ck></span>
</div>

<div class=tabs>
  <button class="tab active" onclick="switchCh('ch1')" id="tab-ch1">Channel 1<span class=ch-label>Stock Options</span></button>
  <button class="tab" onclick="switchCh('ch1b')" id="tab-ch1b">Channel 1B<span class=ch-label>Stock Options (Free)</span></button>
  <button class="tab" onclick="switchCh('ch2')" id="tab-ch2">Channel 2<span class=ch-label>Index Options (3 lots)</span></button>
  <button class="tab" onclick="switchCh('ch3')" id="tab-ch3">Channel 3<span class=ch-label>Free Signals (3 lots)</span></button>
  <button class="tab" onclick="switchCh('ch5')" id="tab-ch5">Scanner<span class=ch-label>Auto Bot (1 lot)</span></button>
</div>

<div class=stats id=ss></div>

<div class=sec>
  <div class=sec-h>Equity Curve</div>
  <div class=cw><canvas id=cv></canvas><div class=cl id=cvl></div></div>
</div>

<div class=sec>
  <div class=sec-h>Open Positions <span class=badge id=oc>0</span></div>
  <div class=tw id=ow></div>
</div>

<div class=sec>
  <div class=sec-h>Trade History <span class=badge id=hc>0</span></div>
  <div class=fb id=fb></div>
  <div class=tw style="max-height:400px;overflow-y:auto" id=hw></div>
</div>

</div>
<script>
const $=id=>document.getElementById(id);
let AT=[],CF='all',LTP={},CH='ch1';

function inr(v){if(v==null||isNaN(v))return'-';return(v<0?'-':'')+'₹'+Math.abs(Math.round(v)).toLocaleString('en-IN')}
function pc(v){return v>0?'pos':v<0?'neg':''}
function sp(s){return s==='OPEN'?'<span class="pill op">OPEN</span>':s&&s.includes('SL')?'<span class="pill sl">SL</span>':'<span class="pill cl">CLOSED</span>'}
function tf(t){if(!t)return'-';const d=new Date(t),mm=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];return String(d.getDate()).padStart(2,'0')+' '+mm[d.getMonth()]+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')}

async function cp(id,sym){if(!confirm('Close '+sym+' at cost?'))return;await fetch('/api/close/'+id,{method:'POST'});load()}

function rS(s){
  const avail=s.capital-s.utilized;
  const I=[
    {l:'Capital',v:inr(s.capital),c:'',d:'available '+inr(avail)},
    {l:'Utilized',v:inr(s.utilized),c:s.utilized>0?'neg':'',d:Math.round(s.utilized/s.capital*100)+'% deployed'},
    {l:'Today P&L',v:inr(s.today_pnl),c:pc(s.today_pnl),d:s.today_count+' trades'},
    {l:'Total P&L',v:inr(s.total_pnl),c:pc(s.total_pnl),d:s.closed+' closed'},
    {l:'Win Rate',v:s.closed?s.win_rate+'%':'—',c:s.win_rate>=50?'pos':'neg',d:s.wins+'W '+s.losses+'L'},
    {l:'Avg Trade',v:inr(s.avg_pnl),c:pc(s.avg_pnl),d:'per closed'},
    {l:'Best',v:inr(s.best_trade),c:'pos',d:'single max'},
    {l:'Worst',v:inr(s.worst_trade),c:'neg',d:'single min'},
    {l:'Charges',v:inr(s.total_charges),c:'neg',d:'today '+inr(s.today_charges)},
    {l:'Open',v:String(s.open),c:'',d:'active now'},
  ];
  $('ss').innerHTML=I.map(i=>'<div class=s><div class=l>'+i.l+'</div><div class="v '+i.c+'">'+i.v+'</div><div class=d>'+i.d+'</div></div>').join('');
}

function rC(curve){
  const c=$('cv'),x=c.getContext('2d'),dp=devicePixelRatio||1,r=c.getBoundingClientRect();
  c.width=r.width*dp;c.height=r.height*dp;x.scale(dp,dp);
  const W=r.width,H=r.height;
  if(!curve||curve.length<2){x.fillStyle=getComputedStyle(document.documentElement).getPropertyValue('--mt');x.font='12px system-ui';x.textAlign='center';x.fillText('Waiting for closed trades',W/2,H/2);return}
  const V=curve.map(c=>c.cumulative),mn=Math.min(0,...V),mx=Math.max(0,...V),rg=mx-mn||1;
  const p={t:16,b:24,l:44,r:12},cw=W-p.l-p.r,ch=H-p.t-p.b;
  const X=i=>p.l+(i/(V.length-1))*cw, Y=v=>p.t+ch-(((v-mn)/rg)*ch);
  const cs=getComputedStyle(document.documentElement);
  const gn=cs.getPropertyValue('--gn').trim(),rd=cs.getPropertyValue('--rd').trim();
  const mt=cs.getPropertyValue('--mt').trim(),bd=cs.getPropertyValue('--bd').trim();
  const lv=V[V.length-1],lc=lv>=0?gn:rd;
  x.strokeStyle=bd;x.lineWidth=.5;
  for(let i=0;i<=3;i++){const yy=p.t+(ch/3)*i;x.beginPath();x.moveTo(p.l,yy);x.lineTo(W-p.r,yy);x.stroke();
    x.fillStyle=mt;x.font='9px system-ui';x.textAlign='right';x.fillText(Math.round(mx-((mx-mn)/3)*i).toLocaleString('en-IN'),p.l-4,yy+3)}
  if(mn<0&&mx>0){x.strokeStyle=mt;x.lineWidth=.8;x.setLineDash([3,3]);x.beginPath();x.moveTo(p.l,Y(0));x.lineTo(W-p.r,Y(0));x.stroke();x.setLineDash([])}
  x.beginPath();x.moveTo(X(0),Y(0));for(let i=0;i<V.length;i++)x.lineTo(X(i),Y(V[i]));x.lineTo(X(V.length-1),Y(0));x.closePath();
  const g=x.createLinearGradient(0,p.t,0,H-p.b);g.addColorStop(0,lc+'40');g.addColorStop(1,lc+'05');x.fillStyle=g;x.fill();
  x.beginPath();x.moveTo(X(0),Y(V[0]));for(let i=1;i<V.length;i++)x.lineTo(X(i),Y(V[i]));x.strokeStyle=lc;x.lineWidth=2;x.lineJoin='round';x.stroke();
  const ex=X(V.length-1),ey=Y(lv);x.beginPath();x.arc(ex,ey,3.5,0,Math.PI*2);x.fillStyle=lc;x.fill();
  $('cvl').textContent='Cumulative: '+inr(lv);$('cvl').style.color=lc;
}

function rO(T){
  const O=T.filter(t=>t.status==='OPEN');$('oc').textContent=O.length;
  if(!O.length){$('ow').innerHTML='<div class=empty>No open positions</div>';return}
  $('ow').innerHTML='<table><thead><tr><th>Symbol</th><th>Entry</th><th>CMP</th><th>P&L</th><th>SL</th><th>Target</th><th>Qty</th><th></th></tr></thead><tbody>'+
    O.map(t=>{
      const cmp=LTP[t.id];
      const cmpStr=cmp!=null?cmp.toFixed(2):'—';
      const upnl=cmp!=null?(cmp-t.price)*t.qty:null;
      const upnlStr=upnl!=null?inr(upnl):'—';
      const upnlCls=upnl!=null?pc(upnl):'';
      return'<tr><td style="color:var(--tx);font-weight:600">'+t.symbol+'</td><td>'+t.price+'</td><td>'+cmpStr+'</td><td class="'+upnlCls+'" style="font-weight:700">'+upnlStr+'</td><td>'+t.stop_price+'</td><td>'+(t.target_price||'-')+'</td><td>'+t.qty+'</td><td><button class=cb onclick="cp('+t.id+",'"+t.symbol.replace(/'/g,'')+"')\">Close</button></td></tr>"}).join('')+'</tbody></table>';
}

function rH(T){
  const C=T.filter(t=>t.status!=='OPEN');
  let F=C;if(CF==='w')F=C.filter(t=>t.pnl>0);else if(CF==='l')F=C.filter(t=>t.pnl!==null&&t.pnl<=0);
  const w=C.filter(t=>t.pnl>0).length,l=C.filter(t=>t.pnl!==null&&t.pnl<=0).length;
  $('hc').textContent=C.length;
  $('fb').innerHTML=[{k:'all',t:'All ('+C.length+')'},{k:'w',t:'Wins ('+w+')'},{k:'l',t:'Losses ('+l+')'}].map(f=>'<button class="'+(CF===f.k?'a':'')+'" onclick="sF(\''+f.k+'\')">'+f.t+'</button>').join('');
  if(!F.length){$('hw').innerHTML='<div class=empty>No trades</div>';return}
  $('hw').innerHTML='<table><thead><tr><th>Time</th><th>Symbol</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Charges</th><th>Net P&L</th><th>Status</th></tr></thead><tbody>'+
    F.map(t=>'<tr><td style="color:var(--mt)">'+tf(t.ts)+'</td><td style="color:var(--tx);font-weight:600">'+t.symbol+'</td><td>'+t.qty+'</td><td>'+t.price+'</td><td>'+(t.exit_price||'-')+'</td><td style="color:var(--mt)">'+(t.charges!=null?inr(t.charges):'-')+'</td><td class="'+pc(t.pnl)+'">'+inr(t.pnl)+'</td><td>'+sp(t.status)+'</td></tr>').join('')+'</tbody></table>';
}

function sF(f){CF=f;rH(AT)}

function switchCh(ch){
  CH=ch;CF='all';
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  $('tab-'+ch).classList.add('active');
  load();
}

async function load(){
  try{
    const q='?channel='+CH;
    const [s,t]=await Promise.all([fetch('/api/stats'+q).then(r=>r.json()),fetch('/api/trades'+q).then(r=>r.json())]);
    AT=t;$('ck').textContent=s.now;$('sd').className='dot on';$('st').textContent='Listener active';
    rS(s);rC(s.pnl_curve);rH(t);
    try{const ltp=await fetch('/api/ltp'+q).then(r=>r.json());if(!ltp.error)LTP=ltp}catch(e){}
    rO(t);
  }catch(e){$('sd').className='dot off';$('st').textContent='Error: '+e}
}
load();setInterval(load,15000);
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)

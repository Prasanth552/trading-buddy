"""Interactive web dashboard for Trading Buddy (FastAPI).

Read-only views of the SQLite DB (status, positions, trades, signals, news) plus
pause/resume controls. Mobile- and laptop-friendly. Auth via optional HTTP Basic
(DASHBOARD_PASSWORD); open on localhost if no password is set.

Run:  python main.py --dashboard   (serves on 0.0.0.0:DASHBOARD_PORT, default 8000)
"""
from __future__ import annotations

import base64
import os
import secrets
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import config
from src.notify import state
from src.storage import db
from src.utils import market_calendar as mc

load_dotenv()
app = FastAPI(title="Trading Buddy", docs_url=None, redoc_url=None)


def require_auth(request: Request) -> None:
    """HTTP Basic auth when DASHBOARD_PASSWORD is set; open otherwise (local dev)."""
    pw = os.getenv("DASHBOARD_PASSWORD")
    if not pw:
        return
    hdr = request.headers.get("Authorization", "")
    if hdr.startswith("Basic "):
        try:
            user, _, passwd = base64.b64decode(hdr[6:]).decode().partition(":")
            if (secrets.compare_digest(user, os.getenv("DASHBOARD_USER", "admin"))
                    and secrets.compare_digest(passwd, pw)):
                return
        except Exception:  # noqa: BLE001
            pass
    raise HTTPException(status_code=401, detail="Auth required",
                        headers={"WWW-Authenticate": "Basic"})


def _rows(query: str, params: tuple = ()) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


# Cached broker handles for live P&L + manual close (lazily created, reused).
_clients: dict[str, Any] = {}


def _kite() -> Any | None:
    """Return a cached Kite client (reuses today's token), or None if unavailable."""
    if "kite" not in _clients:
        try:
            from src.broker.session import ensure_session
            _clients["kite"] = ensure_session()
        except Exception as exc:  # noqa: BLE001 - dashboard must not crash
            _clients["kite"] = None
            _clients["kite_err"] = str(exc)
    return _clients.get("kite")


def _notifier() -> Any | None:
    if "notifier" not in _clients:
        try:
            from src.notify.telegram_bot import TelegramNotifier
            _clients["notifier"] = TelegramNotifier()
        except Exception:  # noqa: BLE001
            _clients["notifier"] = None
    return _clients.get("notifier")


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------
@app.get("/api/status")
def api_status(_: None = Depends(require_auth)) -> JSONResponse:
    db.init_db()
    today = mc.now_ist().date().isoformat()
    ds = dict(db.get_or_create_daily_state(today))
    return JSONResponse({
        "now": mc.now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        "mode": config.MODE,
        "broker": config.EXECUTION_BROKER,
        "market": mc.market_status(),
        "paused": state.is_paused(),
        "trades_count": ds["trades_count"],
        "max_trades": config.MAX_TRADES_PER_DAY,
        "realised_pnl": round(ds["realised_pnl"], 2),
        "kill_switch": bool(ds["kill_switch_tripped"]),
        "max_daily_loss": config.MAX_DAILY_LOSS,
    })


@app.get("/api/positions")
def api_positions(_: None = Depends(require_auth)) -> JSONResponse:
    """Open positions enriched with live current premium + unrealised P&L (₹).

    Falls back to entry-only data if no Kite session is available.
    """
    rows = _rows(
        "SELECT id, symbol, side, qty, price, stop_price, target_price, mode, status, ts "
        "FROM trades WHERE status='OPEN' AND side='BUY' ORDER BY id DESC")
    kite = _kite()
    from src.execution import executor
    total_unreal = 0.0
    for r in rows:
        r["current_premium"] = None
        r["unrealised_pnl"] = None
        r["profit_target"] = config.PROFIT_TARGET_RUPEES
        if kite is not None:
            try:
                live = executor.position_pnl(r, kite)
                r["current_premium"] = live["current_premium"]
                r["unrealised_pnl"] = live["unrealised_pnl"]
                total_unreal += live["unrealised_pnl"]
            except Exception as exc:  # noqa: BLE001 - one bad symbol shouldn't break the list
                r["error"] = str(exc)
    return JSONResponse({
        "positions": rows,
        "total_unrealised": round(total_unreal, 2),
        "live": kite is not None,
        "profit_target": config.PROFIT_TARGET_RUPEES,
    })


@app.post("/api/close/{trade_id}")
def api_close(trade_id: int, _: None = Depends(require_auth)) -> JSONResponse:
    """Manually close one open position now at the current market premium."""
    kite = _kite()
    if kite is None:
        return JSONResponse(
            {"closed": False, "reason": "no Kite session — cannot fetch price/close"},
            status_code=503)
    from src.execution import executor
    try:
        result = executor.manual_close(trade_id, kite_client=kite, notifier=_notifier())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"closed": False, "reason": str(exc)}, status_code=500)
    return JSONResponse(result, status_code=200 if result.get("closed") else 400)


@app.get("/api/trades")
def api_trades(_: None = Depends(require_auth)) -> JSONResponse:
    return JSONResponse(_rows(
        "SELECT ts, symbol, side, qty, price, exit_price, pnl, mode, status "
        "FROM trades ORDER BY id DESC LIMIT 30"))


@app.get("/api/signals")
def api_signals(_: None = Depends(require_auth)) -> JSONResponse:
    return JSONResponse(_rows(
        "SELECT ts, symbol, direction, entry, stop, target, status "
        "FROM signals ORDER BY id DESC LIMIT 20"))


@app.get("/api/news")
def api_news(_: None = Depends(require_auth)) -> JSONResponse:
    return JSONResponse(_rows(
        "SELECT ts, symbol, sentiment, confidence, headline "
        "FROM news_items ORDER BY id DESC LIMIT 20"))


@app.get("/api/performance")
def api_performance(_: None = Depends(require_auth)) -> JSONResponse:
    from src.review.performance import compute_stats
    return JSONResponse(compute_stats(db.closed_trades_with_context()))


@app.get("/api/attention")
def api_attention(_: None = Depends(require_auth)) -> JSONResponse:
    from src.utils.attention import attention_items
    try:
        return JSONResponse(attention_items())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse([{"level": "error", "msg": f"attention check failed: {exc}"}])


@app.post("/api/pause")
def api_pause(_: None = Depends(require_auth)) -> JSONResponse:
    state.set_paused(True)
    return JSONResponse({"paused": True})


@app.post("/api/resume")
def api_resume(_: None = Depends(require_auth)) -> JSONResponse:
    state.set_paused(False)
    return JSONResponse({"paused": False})


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(_: None = Depends(require_auth)) -> str:
    return _PAGE


_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Trading Buddy</title>
<style>
 :root{--bg:#0f1419;--card:#1a212b;--mut:#8b97a7;--grn:#27c281;--red:#ff5d5d;--acc:#4c9aff}
 *{box-sizing:border-box} body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;
  background:var(--bg);color:#e6edf3;padding:14px;max-width:900px;margin:auto}
 h1{font-size:20px;margin:4px 0 2px} .sub{color:var(--mut);font-size:12px;margin-bottom:14px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
 .card{background:var(--card);border-radius:12px;padding:12px 14px}
 .card .k{color:var(--mut);font-size:12px} .card .v{font-size:20px;font-weight:600;margin-top:3px}
 .pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600}
 .ok{background:rgba(39,194,129,.15);color:var(--grn)} .bad{background:rgba(255,93,93,.15);color:var(--red)}
 .neu{background:rgba(139,151,167,.15);color:var(--mut)}
 .pos{color:var(--grn)} .neg{color:var(--red)}
 h2{font-size:14px;color:var(--mut);margin:18px 0 8px;text-transform:uppercase;letter-spacing:.5px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #232c38;white-space:nowrap}
 th{color:var(--mut);font-weight:500} .wrap td{white-space:normal}
 .btns{display:flex;gap:10px;margin:6px 0 4px}
 button{flex:1;padding:11px;border:0;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer}
 .pause{background:#3a2a13;color:#ffb454} .resume{background:#13321f;color:var(--grn)}
 .scroll{overflow-x:auto} .empty{color:var(--mut);font-size:13px;padding:8px 0}
 .att{padding:9px 12px;border-radius:9px;margin-bottom:7px;font-size:13px;border-left:4px solid}
 .att.ok{background:rgba(39,194,129,.08);border-color:var(--grn)}
 .att.warn{background:rgba(255,180,84,.10);border-color:#ffb454}
 .att.error{background:rgba(255,93,93,.10);border-color:var(--red)}
 .note{font-size:12px;color:var(--mut);text-transform:none;letter-spacing:0;margin-left:6px}
 .close-btn{padding:6px 12px;border:0;border-radius:8px;background:#3a2a13;color:#ffb454;
   font-size:13px;font-weight:600;cursor:pointer;flex:none;width:auto}
</style></head><body>
<h1>📈 Trading Buddy</h1><div class=sub id=sub>loading…</div>
<div class=grid id=cards></div>
<div class=btns><button class=pause onclick="ctl('pause')">⏸ Pause</button>
 <button class=resume onclick="ctl('resume')">▶ Resume</button></div>
<h2>Performance (all closed trades)</h2><div class=grid id=perf></div>
<h2>Open positions <span id=posnote class=note></span></h2><div class=scroll id=positions></div>
<h2>Recent trades</h2><div class=scroll id=trades></div>
<h2>Recent signals</h2><div class=scroll id=signals></div>
<h2>Latest news</h2><div class=scroll id=news></div>
<h2>⚙️ Needs attention</h2><div id=attention></div>
<script>
const $=id=>document.getElementById(id);
function tbl(rows,cols,wrap){if(!rows||!rows.length)return '<div class=empty>none</div>';
 let h='<table'+(wrap?' class=wrap':'')+'><tr>'+cols.map(c=>'<th>'+c[0]+'</th>').join('')+'</tr>';
 for(const r of rows){h+='<tr>'+cols.map(c=>'<td>'+c[1](r)+'</td>').join('')+'</tr>'}return h+'</table>'}
function pnl(v){if(v==null)return '-';const c=v>=0?'pos':'neg';return '<span class='+c+'>₹'+v+'</span>'}
async function ctl(a){await fetch('/api/'+a,{method:'POST'});load()}
async function closePos(id,sym){
 if(!confirm('Close '+sym+' now at the current market price?'))return;
 const r=await fetch('/api/close/'+id,{method:'POST'});
 const d=await r.json();
 alert(d.closed?('✅ Closed '+d.symbol+' @ '+d.exit_price+'  P&L ₹'+d.pnl)
   :('⚠️ Could not close: '+(d.reason||'unknown')));
 load();
}
async function load(){
 try{
  const s=await (await fetch('/api/status')).json();
  $('sub').textContent=s.now+' · mode '+s.mode+' · execution: '+s.broker;
  const ks=s.kill_switch?'<span class="pill bad">TRIPPED</span>':'<span class="pill ok">armed</span>';
  const mk=s.market.startsWith('OPEN')?'<span class="pill ok">'+s.market+'</span>':'<span class="pill neu">'+s.market+'</span>';
  const pz=s.paused?'<span class="pill bad">PAUSED</span>':'<span class="pill ok">running</span>';
  $('cards').innerHTML=[
   ['Market',mk],['State',pz],
   ['Trades today',s.trades_count+' / '+s.max_trades],
   ["Today's P&L",pnl(s.realised_pnl)],
   ['Kill switch',ks],['Loss limit','₹'+s.max_daily_loss]
  ].map(c=>'<div class=card><div class=k>'+c[0]+'</div><div class=v>'+c[1]+'</div></div>').join('');
  const pf=await (await fetch('/api/performance')).json();
  $('perf').innerHTML=[
   ['Win rate', pf.n? (pf.win_rate*100).toFixed(0)+'%':'—'],
   ['Trades', pf.wins+'W / '+pf.losses+'L'],
   ['Total P&L', pnl(pf.total_pnl)],
   ['Per trade', pnl(pf.expectancy)]
  ].map(c=>'<div class=card><div class=k>'+c[0]+'</div><div class=v>'+c[1]+'</div></div>').join('');
  const P=await (await fetch('/api/positions')).json();
  const pos=P.positions||[];
  const liveTxt=P.live?('Live P&L: '+pnl(P.total_unrealised)+' · target ₹'+P.profit_target+'/trade')
    :'⚠️ no live price (Kite session needed) — showing entry only';
  $('posnote').innerHTML=liveTxt;
  $('positions').innerHTML=tbl(pos,[['Symbol',r=>r.symbol],['Qty',r=>r.qty],
    ['Entry',r=>r.price],['Now',r=>r.current_premium??'-'],
    ['Live P&L',r=>pnl(r.unrealised_pnl)],
    ['Stop',r=>r.stop_price],['Target',r=>r.target_price],
    ['Action',r=>'<button class=close-btn onclick="closePos('+r.id+',\\''+r.symbol+'\\')">✖ Close</button>']]);
  const T=await (await fetch('/api/trades')).json();
  $('trades').innerHTML=tbl(T,[['Time',r=>(r.ts||'').slice(5,16)],['Symbol',r=>r.symbol],['Side',r=>r.side],
    ['Qty',r=>r.qty],['Entry',r=>r.price],['Exit',r=>r.exit_price??'-'],['P&L',r=>pnl(r.pnl)],['Status',r=>r.status]]);
  const G=await (await fetch('/api/signals')).json();
  $('signals').innerHTML=tbl(G,[['Time',r=>(r.ts||'').slice(5,16)],['Symbol',r=>r.symbol],['Dir',r=>r.direction],
    ['Entry',r=>r.entry],['Stop',r=>r.stop],['Target',r=>r.target]]);
  const N=await (await fetch('/api/news')).json();
  $('news').innerHTML=tbl(N,[['Sent',r=>r.sentiment||'-'],['Sym',r=>r.symbol||'-'],['Headline',r=>(r.headline||'').slice(0,70)]],true);
  const A=await (await fetch('/api/attention')).json();
  $('attention').innerHTML=A.map(a=>'<div class="att '+a.level+'">'+(a.level==='error'?'⛔ ':a.level==='warn'?'⚠️ ':'✅ ')+a.msg+'</div>').join('');
 }catch(e){$('sub').textContent='error loading: '+e}
}
load();setInterval(load,20000);
</script></body></html>"""

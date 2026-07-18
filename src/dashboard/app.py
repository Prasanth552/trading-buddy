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
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
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


_KITE_TTL_SECONDS = 300  # re-validate the cached session every 5 minutes


def _kite() -> Any | None:
    """Return a Kite client, re-validating the session every few minutes.

    Kite tokens expire daily; a client cached forever goes stale and price
    fetches fail silently (showing '-' on the dashboard). Re-running
    ensure_session() on a TTL picks up the fresh daily token file written by
    the scheduler's morning auto-login. Failures are never cached.
    """
    import time as _time
    cli = _clients.get("kite")
    if cli is not None and _time.time() - _clients.get("kite_ts", 0) < _KITE_TTL_SECONDS:
        return cli
    try:
        from src.broker.session import ensure_session
        _clients["kite"] = ensure_session()  # validates the token via profile()
        _clients["kite_ts"] = _time.time()
        return _clients["kite"]
    except Exception as exc:  # noqa: BLE001 - dashboard must not crash
        _clients.pop("kite", None)
        _clients["kite_err"] = str(exc)
        return None


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
    with db.get_conn() as conn:
        # Capital deployed in open positions (premium paid x qty).
        utilized = conn.execute(
            "SELECT COALESCE(SUM(price*qty),0) FROM trades "
            "WHERE status='OPEN' AND side='BUY'").fetchone()[0]
        # Lifetime realised P&L -> current account value on sandbox capital.
        lifetime_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE pnl IS NOT NULL"
        ).fetchone()[0]
    capital = float(getattr(config, "CAPITAL", 0))
    account_value = capital + lifetime_pnl
    return JSONResponse({
        "capital": capital,
        "utilized": round(utilized, 2),
        "available": round(account_value - utilized, 2),
        "account_value": round(account_value, 2),
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
        "SELECT id, symbol, side, qty, price, stop_price, target_price, mode, status, ts, "
        "is_hedge, index_entry FROM trades WHERE status='OPEN' AND side='BUY' ORDER BY id DESC")
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
                if live["unrealised_pnl"] is not None:
                    total_unreal += live["unrealised_pnl"]
            except Exception as exc:  # noqa: BLE001 - one bad symbol shouldn't break the list
                r["error"] = str(exc)
                # Token likely went stale mid-day — drop the cached client so
                # the next refresh reconnects instead of erroring forever.
                _clients.pop("kite", None)
    return JSONResponse({
        "positions": rows,
        "total_unrealised": round(total_unreal, 2),
        "live": kite is not None,
        "kite_err": None if kite is not None else _clients.get("kite_err", "unknown"),
        "profit_target": config.PROFIT_TARGET_RUPEES,
        "exit_mode": getattr(config, "EXIT_MODE", "target"),
        "stops_enabled": getattr(config, "STOP_LOSS_ENABLED", True),
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
    """Recent trades; still-OPEN rows are enriched with live premium + P&L."""
    rows = _rows(
        "SELECT id, ts, symbol, side, qty, price, exit_price, pnl, mode, status, "
        "is_hedge, index_entry, stop_price, target_price "
        "FROM trades ORDER BY id DESC LIMIT 30")
    kite = _kite()
    if kite is not None:
        from src.execution import executor
        for r in rows:
            if r["status"] == "OPEN" and r["side"] == "BUY":
                try:
                    live = executor.position_pnl(r, kite)
                    r["current_premium"] = live["current_premium"]
                    r["unrealised_pnl"] = live["unrealised_pnl"]
                except Exception:  # noqa: BLE001 - display-only enrichment
                    pass
    return JSONResponse(rows)


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


# --------------------------------------------------------------------------
# Chart Analyzer
# --------------------------------------------------------------------------
_CHART_SYSTEM = """You are an expert technical analyst for the Indian stock market (NSE/BSE).
You analyse chart images — candlestick, line, or bar charts — and produce a
structured, actionable report. Cover ALL of the following:

1. **Chart type & timeframe** — what you see (candlestick/line, 1min/15min/daily/weekly, etc.).
2. **Trend identification** — primary trend (up/down/sideways), trend strength, and
   any trend-line breaks or channel boundaries visible.
3. **Key support & resistance levels** — mark every price level that has acted as
   S/R (round numbers, prior swing highs/lows, gap zones).
4. **Candlestick / price-action patterns** — e.g. hammer, engulfing, doji, inside bar,
   double top/bottom, head-and-shoulders, flags, wedges, cup-and-handle.
5. **Indicator readings** (if visible on the chart) — moving averages, RSI, MACD,
   Bollinger Bands, volume profile, Supertrend, etc.
6. **Volume analysis** — is volume confirming the move? Divergences?
7. **Prediction / next likely move** — based on everything above, give a SHORT-TERM
   (next 1-5 sessions) and MEDIUM-TERM (next 2-4 weeks) directional view with
   specific price targets and invalidation (stop-loss) levels.
8. **Recommended trade setup** — direction (long/short/wait), entry zone, stop-loss,
   target(s), and risk-reward ratio. If no clear setup exists, say "No trade — wait."

Be specific with numbers. Use ₹ for prices. If the chart is unclear or low quality,
say what you CAN and CANNOT determine. Never fabricate data you cannot see."""

_CHART_PROMPT_EN = "Analyse this chart image. Provide the full 8-section technical analysis and prediction."
_CHART_PROMPT_TA = (
    "Analyse this chart image. Provide the full 8-section technical analysis and prediction. "
    "IMPORTANT: Write your ENTIRE response in Tamil (தமிழ்). Use Tamil script throughout. "
    "Keep stock names, numbers, and ₹ symbols as-is, but all explanations and sentences must be in Tamil."
)


@app.post("/api/chart-analyze")
async def api_chart_analyze(
    file: UploadFile = File(...),
    request: Request = None,
    _: None = Depends(require_auth),
) -> JSONResponse:
    """Accept a chart image, send it to Claude vision, return the analysis."""
    content_type = file.content_type or "image/png"
    if not content_type.startswith("image/"):
        return JSONResponse({"error": "Upload an image file (PNG, JPG, WEBP)."}, status_code=400)
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        return JSONResponse({"error": "Image too large (max 20 MB)."}, status_code=400)
    img_b64 = base64.b64encode(data).decode()
    lang = (request.query_params.get("lang") or "en") if request else "en"
    prompt = _CHART_PROMPT_TA if lang == "ta" else _CHART_PROMPT_EN
    try:
        from src.llm.client import LLMClient, LLMError
        llm = LLMClient()
        analysis = llm.analyze_image(
            system=_CHART_SYSTEM,
            image_b64=img_b64,
            media_type=content_type,
            prompt=prompt,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"analysis": analysis})


@app.get("/chart", response_class=HTMLResponse)
def chart_page(_: None = Depends(require_auth)) -> str:
    return _CHART_PAGE


_CHART_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Chart Analyzer — Trading Buddy</title>
<style>
:root{--bg:#0f1419;--card:#1a212b;--mut:#8b97a7;--grn:#27c281;--red:#ff5d5d;--acc:#4c9aff;--txt:#e6edf3}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt);
 padding:14px;max-width:960px;margin:auto}
a{color:var(--acc);text-decoration:none}
h1{font-size:22px;margin:4px 0 2px}
.sub{color:var(--mut);font-size:13px;margin-bottom:18px}
.upload-zone{border:2px dashed #2d3748;border-radius:14px;padding:40px 20px;text-align:center;
 cursor:pointer;transition:border-color .2s,background .2s;margin-bottom:18px;position:relative}
.upload-zone:hover,.upload-zone.drag{border-color:var(--acc);background:rgba(76,154,255,.06)}
.upload-zone input{position:absolute;inset:0;opacity:0;cursor:pointer}
.upload-zone .icon{font-size:48px;margin-bottom:8px}
.upload-zone .label{font-size:15px;color:var(--mut)}
.preview-wrap{text-align:center;margin-bottom:16px}
.preview-wrap img{max-width:100%;max-height:400px;border-radius:10px;border:1px solid #2d3748}
.btn{display:inline-block;padding:12px 28px;border:0;border-radius:10px;font-size:15px;font-weight:600;
 cursor:pointer;background:var(--acc);color:#fff;transition:opacity .2s}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn.loading{position:relative;color:transparent}
.btn.loading::after{content:'';position:absolute;top:50%;left:50%;width:20px;height:20px;
 margin:-10px 0 0 -10px;border:3px solid rgba(255,255,255,.3);border-top-color:#fff;
 border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.result{background:var(--card);border-radius:14px;padding:20px 22px;margin-top:18px;
 line-height:1.7;font-size:14px;white-space:pre-wrap;word-wrap:break-word}
.result h1,.result h2,.result h3{color:var(--acc);margin:18px 0 6px}
.result h1{font-size:18px} .result h2{font-size:16px} .result h3{font-size:14px}
.result strong{color:var(--grn)}
.result em{color:#ffb454}
.error{color:var(--red);margin-top:14px;font-size:14px}
.back{display:inline-block;margin-bottom:12px;font-size:13px}
.lang-toggle{display:flex;gap:0;border-radius:8px;overflow:hidden;border:1px solid #2d3748;
 width:fit-content;margin-bottom:16px}
.lang-toggle button{padding:8px 18px;border:0;background:var(--card);color:var(--mut);
 font-size:13px;font-weight:600;cursor:pointer;transition:all .2s}
.lang-toggle button.active{background:var(--acc);color:#fff}
</style></head><body>
<a href="/" class=back>&larr; Dashboard</a>
<h1>📊 Chart Analyzer</h1>
<div class=sub>Upload a chart image (Nifty, Bank Nifty, any stock) — AI analyses patterns, S/R, and predicts the next move.</div>

<div class=lang-toggle>
 <button id=langEn class=active onclick="setLang('en')">English</button>
 <button id=langTa onclick="setLang('ta')">தமிழ்</button>
</div>

<div class=upload-zone id=dropzone>
 <input type=file id=fileInput accept="image/*">
 <div class=icon>📈</div>
 <div class=label>Drop chart image here or click to browse<br><small>PNG, JPG, WEBP — max 20 MB</small></div>
</div>
<div class=preview-wrap id=previewWrap style=display:none><img id=preview></div>
<div style=text-align:center>
 <button class=btn id=analyzeBtn disabled onclick=analyze()>Analyze Chart</button>
</div>
<div class=error id=error></div>
<div class=result id=result style=display:none></div>

<script>
const dropzone=$('dropzone'),fileInput=$('fileInput'),preview=$('preview'),
 previewWrap=$('previewWrap'),btn=$('analyzeBtn'),resultDiv=$('result'),errDiv=$('error');
let selectedFile=null;
let lang='en';
function $(id){return document.getElementById(id)}
function setLang(l){lang=l;
 $('langEn').classList.toggle('active',l==='en');
 $('langTa').classList.toggle('active',l==='ta');
}

['dragover','dragenter'].forEach(e=>dropzone.addEventListener(e,ev=>{ev.preventDefault();dropzone.classList.add('drag')}));
['dragleave','drop'].forEach(e=>dropzone.addEventListener(e,ev=>{ev.preventDefault();dropzone.classList.remove('drag')}));
dropzone.addEventListener('drop',ev=>{if(ev.dataTransfer.files.length)pickFile(ev.dataTransfer.files[0])});
fileInput.addEventListener('change',()=>{if(fileInput.files.length)pickFile(fileInput.files[0])});

function pickFile(f){
 if(!f.type.startsWith('image/')){errDiv.textContent='Please select an image file.';return}
 if(f.size>20*1024*1024){errDiv.textContent='File too large (max 20 MB).';return}
 selectedFile=f; errDiv.textContent=''; resultDiv.style.display='none';
 const r=new FileReader();
 r.onload=()=>{preview.src=r.result;previewWrap.style.display='';dropzone.style.display='none'};
 r.readAsDataURL(f);
 btn.disabled=false;
}

async function analyze(){
 if(!selectedFile)return;
 btn.disabled=true;btn.classList.add('loading');btn.textContent='Analyzing…';
 errDiv.textContent='';resultDiv.style.display='none';
 const fd=new FormData();fd.append('file',selectedFile);
 try{
  const r=await fetch('/api/chart-analyze?lang='+lang,{method:'POST',body:fd});
  const d=await r.json();
  if(d.error){errDiv.textContent=d.error;return}
  resultDiv.innerHTML=markdownToHtml(d.analysis);
  resultDiv.style.display='';
 }catch(e){errDiv.textContent='Request failed: '+e}
 finally{btn.disabled=false;btn.classList.remove('loading');btn.textContent='Analyze Chart'}
}

function markdownToHtml(md){
 var LT=String.fromCharCode(60),GT=String.fromCharCode(62);
 return md
  .replace(/&/g,'&amp;').replace(new RegExp(LT,'g'),'&lt;').replace(new RegExp(GT,'g'),'&gt;')
  .replace(/^### (.+)$/gm,LT+'h3>$1'+LT+'/h3>')
  .replace(/^## (.+)$/gm,LT+'h2>$1'+LT+'/h2>')
  .replace(/^# (.+)$/gm,LT+'h1>$1'+LT+'/h1>')
  .replace(/[*][*](.+?)[*][*]/g,LT+'strong>$1'+LT+'/strong>')
  .replace(/[*](.+?)[*]/g,LT+'em>$1'+LT+'/em>')
  .replace(/^[-] (.+)$/gm,'\\u2022 $1')
  .replace(/\\n\\n+/g,LT+'br>'+LT+'br>')
}
</script></body></html>"""


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
<h1>📈 Trading Buddy</h1><div class=sub id=sub>loading… · <a href="/chart" style="color:#4c9aff">📊 Chart Analyzer</a></div>
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
  const inr=v=>'₹'+Math.round(v).toLocaleString('en-IN');
  const utilPct=s.capital>0?Math.round(100*s.utilized/s.capital):0;
  $('cards').innerHTML=[
   ['Market',mk],['State',pz],
   ['Capital',inr(s.capital)],
   ['Account value',(s.account_value>=s.capital?'<span class=pos>':'<span class=neg>')+inr(s.account_value)+'</span>'],
   ['Utilized',inr(s.utilized)+' <span class=note>('+utilPct+'%)</span>'],
   ['Available',inr(s.available)],
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
  const exitTxt=P.stops_enabled===false
    ?('₹'+P.profit_target+' take-profit · NO auto stop — cut manually ⚠️')
    :((P.exit_mode&&P.exit_mode.startsWith('trail'))
      ?((P.profit_target>0?('₹'+P.profit_target+' take-profit + '):'')+'trailing stop')
      :('target ₹'+P.profit_target+'/trade'));
  const liveTxt=P.live?('Live P&L: '+pnl(P.total_unrealised)+' · exit: '+exitTxt)
    :'⚠️ no live price (Kite session needed) — showing entry only';
  $('posnote').innerHTML=liveTxt;
  const dirOf=r=>r.symbol&&r.symbol.toUpperCase().includes('CE')?'📈 CALL':'📉 PUT';
  const posCols=[['Symbol',r=>(r.is_hedge?'🛡️ ':'')+r.symbol],
    ['Dir',dirOf],
    ['Index @',r=>r.index_entry?Math.round(r.index_entry).toLocaleString('en-IN'):'-'],
    ['Qty',r=>r.qty],
    ['Prem In',r=>r.price],['Now',r=>r.current_premium??'-'],
    ['Live P&L',r=>pnl(r.unrealised_pnl)]];
  if(P.stops_enabled!==false){posCols.push(['Stop',r=>r.stop_price],['Target',r=>r.target_price]);}
  posCols.push(['Action',r=>'<button class=close-btn onclick="closePos('+r.id+',\\''+r.symbol+'\\')">✖ Close</button>']);
  $('positions').innerHTML=tbl(pos,posCols);
  const T=await (await fetch('/api/trades')).json();
  $('trades').innerHTML=tbl(T,[['Time',r=>(r.ts||'').slice(5,16)],
    ['Symbol',r=>(r.is_hedge?'🛡️ ':'')+r.symbol],
    ['Dir',dirOf],
    ['Index @',r=>r.index_entry?Math.round(r.index_entry).toLocaleString('en-IN'):'-'],
    ['Qty',r=>r.qty],['Prem In',r=>r.price],
    ['Prem Out',r=>r.exit_price??(r.current_premium!=null?('now '+r.current_premium):'-')],
    ['P&L',r=>r.pnl!=null?pnl(r.pnl):(r.unrealised_pnl!=null?pnl(r.unrealised_pnl):'-')],
    ['Status',r=>r.status]]);
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

"""Strategy sweep — TODAY ONLY (Oct 8, 2026)."""
from __future__ import annotations
import asyncio, json, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import websockets

UTC = ZoneInfo("UTC")
WS = "wss://api.derivws.com/trading/v1/options/ws/public"
HDRS = {"User-Agent": "Mozilla/5.0", "Origin": "https://app.deriv.com"}

def calc_rsi(closes, period=14):
    rsi = [None]*len(closes)
    if len(closes) < period+1: return rsi
    g = l = 0.0
    for i in range(1, period+1):
        d = closes[i]-closes[i-1]
        if d>0: g+=d
        else: l-=d
    ag, al = g/period, l/period
    rsi[period] = 100.0 if al==0 else 100-100/(1+ag/al)
    for i in range(period+1, len(closes)):
        d = closes[i]-closes[i-1]
        gn = d if d>0 else 0; ln = -d if d<0 else 0
        ag = (ag*(period-1)+gn)/period; al = (al*(period-1)+ln)/period
        rsi[i] = 100.0 if al==0 else 100-100/(1+ag/al)
    return rsi

def calc_ema(closes, period):
    ema = [None]*len(closes)
    if len(closes)<period: return ema
    ema[period-1] = sum(closes[:period])/period
    m = 2/(period+1)
    for i in range(period, len(closes)):
        ema[i] = (closes[i]-ema[i-1])*m + ema[i-1]
    return ema

def calc_bb(closes, period=20, std_mult=2.0):
    u=[None]*len(closes); l=[None]*len(closes); mid=[None]*len(closes)
    for i in range(period-1, len(closes)):
        w = closes[i-period+1:i+1]; m=sum(w)/period
        s = (sum((x-m)**2 for x in w)/period)**0.5
        mid[i]=m; u[i]=m+std_mult*s; l[i]=m-std_mult*s
    return mid,u,l

def calc_macd(closes, fast=12, slow=26, sig=9):
    ef=calc_ema(closes,fast); es=calc_ema(closes,slow)
    ml=[None]*len(closes)
    for i in range(len(closes)):
        if ef[i] is not None and es[i] is not None: ml[i]=ef[i]-es[i]
    vals=[v for v in ml if v is not None]
    sl=[None]*len(closes); hist=[None]*len(closes)
    if len(vals)<sig: return ml,sl,hist
    st=next(i for i,v in enumerate(ml) if v is not None)
    se=sum(vals[:sig])/sig; sl[st+sig-1]=se; m=2/(sig+1)
    for i in range(st+sig, len(closes)):
        if ml[i] is not None:
            se=(ml[i]-se)*m+se; sl[i]=se; hist[i]=ml[i]-se
    return ml,sl,hist

def strat_sr(candles, lb=25, th=0.005):
    c=[x["close"] for x in candles]; h=[x["high"] for x in candles]; l=[x["low"] for x in candles]
    sigs=[]
    for i in range(lb, len(candles)):
        res=max(h[i-lb:i]); sup=min(l[i-lb:i]); rng=res-sup
        if rng==0: continue
        pos=(c[i]-sup)/rng
        if pos<th: sigs.append((i,"CALL"))
        elif pos>(1-th): sigs.append((i,"PUT"))
    return sigs

def strat_linreg(candles, period=20, sm=2.0):
    closes=[x["close"] for x in candles]; sigs=[]
    for i in range(period, len(candles)):
        w=closes[i-period:i]; n=len(w)
        sx=n*(n-1)/2; sy=sum(w); sxx=sum(j*j for j in range(n)); sxy=sum(j*v for j,v in enumerate(w))
        d=n*sxx-sx*sx
        if d==0: continue
        sl=(n*sxy-sx*sy)/d; ic=(sy-sl*sx)/n; pred=ic+sl*(n-1)
        std=(sum((w[j]-(ic+sl*j))**2 for j in range(n))/n)**0.5
        if std==0: continue
        if closes[i]<pred-sm*std: sigs.append((i,"CALL"))
        elif closes[i]>pred+sm*std: sigs.append((i,"PUT"))
    return sigs

def strat_rsi(candles, period=14, os=30, ob=70):
    c=[x["close"] for x in candles]; rsi=calc_rsi(c,period); sigs=[]
    for i in range(len(candles)):
        if rsi[i] is None: continue
        if rsi[i]<os: sigs.append((i,"CALL"))
        elif rsi[i]>ob: sigs.append((i,"PUT"))
    return sigs

def strat_ema(candles, fast=5, slow=20):
    c=[x["close"] for x in candles]; ef=calc_ema(c,fast); es=calc_ema(c,slow); sigs=[]
    for i in range(1,len(candles)):
        if any(v is None for v in [ef[i],ef[i-1],es[i],es[i-1]]): continue
        if ef[i-1]<=es[i-1] and ef[i]>es[i]: sigs.append((i,"CALL"))
        elif ef[i-1]>=es[i-1] and ef[i]<es[i]: sigs.append((i,"PUT"))
    return sigs

def strat_bb(candles, period=20, sm=2.0):
    c=[x["close"] for x in candles]; _,u,l=calc_bb(c,period,sm); sigs=[]
    for i in range(len(candles)):
        if l[i] is None: continue
        if c[i]<=l[i]: sigs.append((i,"CALL"))
        elif c[i]>=u[i]: sigs.append((i,"PUT"))
    return sigs

def strat_macd(candles, f=12, s=26, sg=9):
    c=[x["close"] for x in candles]; _,_,hist=calc_macd(c,f,s,sg); sigs=[]
    for i in range(1,len(candles)):
        if hist[i] is None or hist[i-1] is None: continue
        if hist[i-1]<=0 and hist[i]>0: sigs.append((i,"CALL"))
        elif hist[i-1]>=0 and hist[i]<0: sigs.append((i,"PUT"))
    return sigs

def strat_sr_rsi(candles, lb=25, th=0.005, rp=14, ros=40, rob=60):
    c=[x["close"] for x in candles]; h=[x["high"] for x in candles]; l=[x["low"] for x in candles]
    rsi=calc_rsi(c,rp); sigs=[]
    for i in range(max(lb,rp+1), len(candles)):
        if rsi[i] is None: continue
        res=max(h[i-lb:i]); sup=min(l[i-lb:i]); rng=res-sup
        if rng==0: continue
        pos=(c[i]-sup)/rng
        if pos<th and rsi[i]<ros: sigs.append((i,"CALL"))
        elif pos>(1-th) and rsi[i]>rob: sigs.append((i,"PUT"))
    return sigs

def strat_bb_rsi(candles, bp=20, bs=2.0, rp=14, ros=30, rob=70):
    c=[x["close"] for x in candles]; _,u,l=calc_bb(c,bp,bs); rsi=calc_rsi(c,rp); sigs=[]
    for i in range(len(candles)):
        if l[i] is None or rsi[i] is None: continue
        if c[i]<=l[i] and rsi[i]<ros: sigs.append((i,"CALL"))
        elif c[i]>=u[i] and rsi[i]>rob: sigs.append((i,"PUT"))
    return sigs

def strat_lr_rsi(candles, lp=20, ls=2.0, rp=14, ros=40, rob=60):
    c=[x["close"] for x in candles]; rsi=calc_rsi(c,rp); sigs=[]
    for i in range(max(lp,rp+1), len(candles)):
        if rsi[i] is None: continue
        w=c[i-lp:i]; n=len(w)
        sx=n*(n-1)/2; sy=sum(w); sxx=sum(j*j for j in range(n)); sxy=sum(j*v for j,v in enumerate(w))
        d=n*sxx-sx*sx
        if d==0: continue
        sl=(n*sxy-sx*sy)/d; ic=(sy-sl*sx)/n; pred=ic+sl*(n-1)
        std=(sum((w[j]-(ic+sl*j))**2 for j in range(n))/n)**0.5
        if std==0: continue
        if c[i]<pred-ls*std and rsi[i]<ros: sigs.append((i,"CALL"))
        elif c[i]>pred+ls*std and rsi[i]>rob: sigs.append((i,"PUT"))
    return sigs

def strat_ema_rsi(candles, ef=5, es=20, rp=14, ros=45, rob=55):
    c=[x["close"] for x in candles]; emaf=calc_ema(c,ef); emas=calc_ema(c,es); rsi=calc_rsi(c,rp); sigs=[]
    for i in range(1,len(candles)):
        if any(v is None for v in [emaf[i],emaf[i-1],emas[i],emas[i-1],rsi[i]]): continue
        if emaf[i-1]<=emas[i-1] and emaf[i]>emas[i] and rsi[i]<ros: sigs.append((i,"CALL"))
        elif emaf[i-1]>=emas[i-1] and emaf[i]<emas[i] and rsi[i]>rob: sigs.append((i,"PUT"))
    return sigs

def strat_triple(candles, lb=25, th=0.005, rp=14, ros=40, rob=60, bp=20, bs=2.0):
    c=[x["close"] for x in candles]; h=[x["high"] for x in candles]; lo=[x["low"] for x in candles]
    rsi=calc_rsi(c,rp); _,bu,bl=calc_bb(c,bp,bs); sigs=[]
    for i in range(max(lb,rp+1,bp), len(candles)):
        if rsi[i] is None or bl[i] is None: continue
        res=max(h[i-lb:i]); sup=min(lo[i-lb:i]); rng=res-sup
        if rng==0: continue
        pos=(c[i]-sup)/rng
        if pos<th and rsi[i]<ros and c[i]<=bl[i]: sigs.append((i,"CALL"))
        elif pos>(1-th) and rsi[i]>rob and c[i]>=bu[i]: sigs.append((i,"PUT"))
    return sigs

def backtest_today(candles, signals, expiry_bars, today_start, stake=2.0, payout=0.80, cooldown=None):
    if cooldown is None: cooldown=expiry_bars
    w=l=d=0; pnl=0.0; last=-cooldown; trades=[]
    for idx, dir in signals:
        if candles[idx]["time"] < today_start: continue
        if idx+expiry_bars>=len(candles): continue
        if idx-last<cooldown: continue
        entry=candles[idx]["close"]; exp=candles[idx+expiry_bars]["close"]; last=idx
        t=datetime.fromtimestamp(candles[idx]["time"],UTC)
        if entry==exp:
            d+=1; res="DRAW"
        elif (dir=="CALL" and exp>entry) or (dir=="PUT" and exp<entry):
            pnl+=stake*payout; w+=1; res="WIN"
        else:
            pnl-=stake; l+=1; res="LOSS"
        trades.append({"time":t.strftime("%H:%M"),"dir":dir,"entry":f"{entry:.5f}","expiry":f"{exp:.5f}","result":res})
    tot=w+l+d; wr=w/(w+l)*100 if (w+l)>0 else 0
    return {"trades":tot,"wins":w,"losses":l,"draws":d,"wr":wr,"pnl":pnl,"details":trades}

async def fetch(ws, symbol, gran, count=5000):
    rid=int(time.time()*1000)%100000
    await ws.send(json.dumps({"ticks_history":symbol,"adjust_start_time":1,"count":min(count,5000),"end":"latest","granularity":gran,"style":"candles","req_id":rid}))
    resp=json.loads(await asyncio.wait_for(ws.recv(),timeout=30))
    return [{"time":c["epoch"],"open":float(c["open"]),"high":float(c["high"]),"low":float(c["low"]),"close":float(c["close"])} for c in resp.get("candles",[])]

async def main():
    print("Connecting to Deriv..."); ws=await asyncio.wait_for(websockets.connect(WS,additional_headers=HDRS,ping_interval=25),timeout=15)
    print("Connected!\n")
    PAYOUT=0.80; BE=100/(1+PAYOUT)

    # Today = Oct 8, 2026 00:00 UTC
    today_start = int(datetime(2026,10,8,0,0,0,tzinfo=UTC).timestamp())
    now = datetime.now(UTC)
    print(f"Current UTC time: {now:%Y-%m-%d %H:%M:%S}")
    print(f"Filtering signals from: {datetime.fromtimestamp(today_start,UTC):%Y-%m-%d %H:%M}")
    print(f"Breakeven WR: {BE:.1f}%\n")

    configs=[
        ("EURGBP 5m→15m London",  "frxEURGBP", 300, 3, {7,8,9,10,11,12,13,14,15}),
        ("EURGBP 5m→15m AllHrs",  "frxEURGBP", 300, 3, None),
        ("EURGBP 15m→15m London", "frxEURGBP", 900, 1, {7,8,9,10,11,12,13,14,15}),
        ("EURGBP 15m→30m London", "frxEURGBP", 900, 2, {7,8,9,10,11,12,13,14,15}),
        ("EURGBP 15m→45m London", "frxEURGBP", 900, 3, {7,8,9,10,11,12,13,14,15}),
        ("Vol10 1m→1m",  "1HZ10V", 60,  1, None),
        ("Vol10 1m→3m",  "1HZ10V", 60,  3, None),
        ("Vol10 1m→5m",  "1HZ10V", 60,  5, None),
        ("Vol10 5m→5m",  "1HZ10V", 300, 1, None),
        ("Vol10 5m→15m", "1HZ10V", 300, 3, None),
        ("Vol75 1m→1m",  "1HZ75V", 60,  1, None),
        ("Vol75 1m→3m",  "1HZ75V", 60,  3, None),
        ("Vol75 1m→5m",  "1HZ75V", 60,  5, None),
        ("Vol75 5m→5m",  "1HZ75V", 300, 1, None),
        ("Vol100 1m→1m", "R_100",  60,  1, None),
        ("Vol100 5m→5m", "R_100",  300, 1, None),
    ]

    strategies=[
        ("S/R(15,0.003)",     lambda c: strat_sr(c,15,0.003)),
        ("S/R(25,0.005)",     lambda c: strat_sr(c,25,0.005)),
        ("S/R(40,0.005)",     lambda c: strat_sr(c,40,0.005)),
        ("S/R(50,0.008)",     lambda c: strat_sr(c,50,0.008)),
        ("LinReg(14,1.5)",    lambda c: strat_linreg(c,14,1.5)),
        ("LinReg(20,2.0)",    lambda c: strat_linreg(c,20,2.0)),
        ("LinReg(30,2.5)",    lambda c: strat_linreg(c,30,2.5)),
        ("RSI(14,25,75)",     lambda c: strat_rsi(c,14,25,75)),
        ("RSI(14,30,70)",     lambda c: strat_rsi(c,14,30,70)),
        ("RSI(7,20,80)",      lambda c: strat_rsi(c,7,20,80)),
        ("RSI(14,20,80)",     lambda c: strat_rsi(c,14,20,80)),
        ("EMA(3,10)",         lambda c: strat_ema(c,3,10)),
        ("EMA(5,20)",         lambda c: strat_ema(c,5,20)),
        ("EMA(8,21)",         lambda c: strat_ema(c,8,21)),
        ("BB(20,2.0)",        lambda c: strat_bb(c,20,2.0)),
        ("BB(20,2.5)",        lambda c: strat_bb(c,20,2.5)),
        ("BB(14,1.5)",        lambda c: strat_bb(c,14,1.5)),
        ("MACD(12,26,9)",     lambda c: strat_macd(c,12,26,9)),
        ("MACD(5,13,4)",      lambda c: strat_macd(c,5,13,4)),
        ("S/R+RSI(25,.005,40,60)",   lambda c: strat_sr_rsi(c,25,0.005,14,40,60)),
        ("S/R+RSI(25,.005,35,65)",   lambda c: strat_sr_rsi(c,25,0.005,14,35,65)),
        ("S/R+RSI(40,.005,35,65)",   lambda c: strat_sr_rsi(c,40,0.005,14,35,65)),
        ("LR+RSI(20,2.0,40,60)",     lambda c: strat_lr_rsi(c,20,2.0,14,40,60)),
        ("LR+RSI(20,2.0,35,65)",     lambda c: strat_lr_rsi(c,20,2.0,14,35,65)),
        ("BB+RSI(20,2.0,30,70)",     lambda c: strat_bb_rsi(c,20,2.0,14,30,70)),
        ("BB+RSI(20,2.5,25,75)",     lambda c: strat_bb_rsi(c,20,2.5,14,25,75)),
        ("EMA+RSI(5,20,45,55)",      lambda c: strat_ema_rsi(c,5,20,14,45,55)),
        ("EMA+RSI(8,21,40,60)",      lambda c: strat_ema_rsi(c,8,21,14,40,60)),
        ("Triple(25,.005,40,60)",     lambda c: strat_triple(c,25,0.005,14,40,60,20,2.0)),
        ("Triple(40,.005,35,65)",     lambda c: strat_triple(c,40,0.005,14,35,65,20,2.5)),
    ]

    cache={}; results=[]
    for cl, sym, gran, eb, hrs in configs:
        ck=f"{sym}_{gran}"
        if ck not in cache:
            print(f"Fetching {sym} {gran//60}m...")
            cache[ck]=await fetch(ws,sym,gran,5000)
            cn=cache[ck]
            if cn:
                t0=datetime.fromtimestamp(cn[0]["time"],UTC); t1=datetime.fromtimestamp(cn[-1]["time"],UTC)
                today_count = sum(1 for c in cn if c["time"]>=today_start)
                print(f"  {len(cn)} total candles, {today_count} today | {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M}")
        candles=cache[ck]
        if len(candles)<50: continue

        # Filter hours for signal generation but use all candles for indicator warmup
        for sn, sf in strategies:
            sigs=sf(candles)
            if not sigs: continue
            # Only count today's signals
            if hrs is not None:
                today_sigs = [(i,d) for i,d in sigs if candles[i]["time"]>=today_start and datetime.fromtimestamp(candles[i]["time"],UTC).hour in hrs]
            else:
                today_sigs = [(i,d) for i,d in sigs if candles[i]["time"]>=today_start]
            if not today_sigs: continue

            r=backtest_today(candles,sigs,eb,today_start,payout=PAYOUT)
            if r["trades"]>0:
                results.append({"config":cl,"strategy":sn,**r})

    await ws.close()
    results.sort(key=lambda x: (-x["wr"],-x["pnl"]))

    print(f"\n{'='*100}")
    print(f"  TODAY (Oct 8, 2026) — {len(results)} combos had trades (breakeven = {BE:.1f}%)")
    print(f"{'='*100}")
    print(f"{'Config':<25s} {'Strategy':<28s} {'Tr':>3s} {'W':>3s} {'L':>3s} {'WR':>6s} {'P&L':>8s} {'Edge'}")
    print("-"*100)

    profitable=[]; losing=[]
    for r in results:
        v="✅" if r["wr"]>BE else "❌"
        if r["wr"]>BE: profitable.append(r)
        else: losing.append(r)
        print(f"{v} {r['config']:<23s} {r['strategy']:<28s} {r['trades']:>3d} {r['wins']:>3d} {r['losses']:>3d} {r['wr']:>5.1f}% ${r['pnl']:>7.2f}")

    # Show trade-by-trade for top 10 profitable
    print(f"\n{'='*100}")
    print(f"  TRADE-BY-TRADE — Top profitable combos")
    print(f"{'='*100}")
    for r in profitable[:10]:
        print(f"\n  {r['config']} | {r['strategy']} | WR {r['wr']:.1f}% | P&L ${r['pnl']:.2f}")
        for t in r["details"]:
            icon = "✅" if t["result"]=="WIN" else ("❌" if t["result"]=="LOSS" else "➖")
            print(f"    {icon} {t['time']} {t['dir']:4s} entry={t['entry']} expiry={t['expiry']} → {t['result']}")

    # Summary
    tot_w = sum(r["wins"] for r in results)
    tot_l = sum(r["losses"] for r in results)
    tot_pnl = sum(r["pnl"] for r in results)
    print(f"\n{'='*100}")
    print(f"  SUMMARY: {len(profitable)} profitable / {len(results)} total combos today")
    print(f"  Aggregate: {tot_w}W / {tot_l}L across all combos | Net P&L: ${tot_pnl:.2f}")
    print(f"{'='*100}")

if __name__=="__main__":
    asyncio.run(main())

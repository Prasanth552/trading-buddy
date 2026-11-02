"""Strategy sweep on real Deriv data — find profitable setups."""
from __future__ import annotations
import asyncio, json, time
from collections import defaultdict
from datetime import datetime
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
    """EMA cross confirmed by RSI trend direction."""
    c=[x["close"] for x in candles]; emaf=calc_ema(c,ef); emas=calc_ema(c,es); rsi=calc_rsi(c,rp); sigs=[]
    for i in range(1,len(candles)):
        if any(v is None for v in [emaf[i],emaf[i-1],emas[i],emas[i-1],rsi[i]]): continue
        if emaf[i-1]<=emas[i-1] and emaf[i]>emas[i] and rsi[i]<ros: sigs.append((i,"CALL"))
        elif emaf[i-1]>=emas[i-1] and emaf[i]<emas[i] and rsi[i]>rob: sigs.append((i,"PUT"))
    return sigs

def strat_triple(candles, lb=25, th=0.005, rp=14, ros=40, rob=60, bp=20, bs=2.0):
    """Triple confirmation: S/R + RSI + Bollinger."""
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

def backtest(candles, signals, expiry_bars, stake=2.0, payout=0.80, cooldown=None, hours=None):
    if cooldown is None: cooldown=expiry_bars
    w=l=d=0; pnl=0.0; peak=0.0; mdd=0.0; last=-cooldown
    for idx, dir in signals:
        if idx+expiry_bars>=len(candles): continue
        if idx-last<cooldown: continue
        if hours is not None:
            hr=datetime.fromtimestamp(candles[idx]["time"],UTC).hour
            if hr not in hours: continue
        entry=candles[idx]["close"]; exp=candles[idx+expiry_bars]["close"]; last=idx
        if entry==exp: d+=1
        elif (dir=="CALL" and exp>entry) or (dir=="PUT" and exp<entry): pnl+=stake*payout; w+=1
        else: pnl-=stake; l+=1
        peak=max(peak,pnl); mdd=max(mdd,peak-pnl)
    tot=w+l+d; wr=w/(w+l)*100 if (w+l)>0 else 0
    return {"trades":tot,"wins":w,"losses":l,"draws":d,"wr":wr,"pnl":pnl,"max_dd":mdd}

async def fetch(ws, symbol, gran, count=5000):
    rid=int(time.time()*1000)%100000
    await ws.send(json.dumps({"ticks_history":symbol,"adjust_start_time":1,"count":min(count,5000),"end":"latest","granularity":gran,"style":"candles","req_id":rid}))
    resp=json.loads(await asyncio.wait_for(ws.recv(),timeout=30))
    return [{"time":c["epoch"],"open":float(c["open"]),"high":float(c["high"]),"low":float(c["low"]),"close":float(c["close"])} for c in resp.get("candles",[])]

async def main():
    print("Connecting to Deriv..."); ws=await asyncio.wait_for(websockets.connect(WS,additional_headers=HDRS,ping_interval=25),timeout=15)
    print("Connected!\n")
    PAYOUT=0.80; BE=100/(1+PAYOUT)

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
                print(f"  {len(cn)} candles: {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M}")
        candles=cache[ck]
        if len(candles)<50: continue
        for sn, sf in strategies:
            sigs=sf(candles)
            if not sigs: continue
            r=backtest(candles,sigs,eb,payout=PAYOUT,hours=hrs)
            if r["trades"]>=10:
                results.append({"config":cl,"strategy":sn,**r})

    await ws.close()
    results.sort(key=lambda x: (-x["wr"],-x["pnl"]))

    print(f"\n{'='*95}")
    print(f"  STRATEGY SWEEP — {len(results)} combos tested (breakeven WR = {BE:.1f}%)")
    print(f"{'='*95}")
    print(f"{'Config':<25s} {'Strategy':<28s} {'Trades':>6s} {'WR':>6s} {'P&L':>8s} {'MaxDD':>8s} {'Edge'}")
    print("-"*95)

    profitable=[]
    for r in results:
        if r["wr"]<BE-3: continue
        v="✅" if r["wr"]>BE else "  "
        if r["wr"]>BE: profitable.append(r)
        print(f"{v} {r['config']:<23s} {r['strategy']:<28s} {r['trades']:>6d} {r['wr']:>5.1f}% ${r['pnl']:>7.2f} ${r['max_dd']:>7.2f}")

    print(f"\n{'='*95}")
    print(f"  TOP PROFITABLE (WR > {BE:.1f}%): {len(profitable)} found")
    print(f"{'='*95}")
    for r in profitable[:20]:
        edge=r["wr"]-BE
        print(f"  ✅ {r['config']:<23s} {r['strategy']:<28s} {r['trades']:>4d} tr  WR {r['wr']:>5.1f}%  edge +{edge:.1f}%  P&L ${r['pnl']:.2f}")

    print(f"\nTotal: {len(results)} combos | Profitable: {len(profitable)}")

if __name__=="__main__":
    asyncio.run(main())

"""30-day backtest using 5m + 15m candles (enough data coverage)."""
from __future__ import annotations
import asyncio, json, time
from datetime import datetime, timedelta
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
    se=sum(vals[:sig])/sig; sl[st+sig-1]=se; m2=2/(sig+1)
    for i in range(st+sig, len(closes)):
        if ml[i] is not None: se=(ml[i]-se)*m2+se; sl[i]=se; hist[i]=ml[i]-se
    return ml,sl,hist

def sig_ema(candles, fast, slow):
    c=[x["close"] for x in candles]; ef=calc_ema(c,fast); es=calc_ema(c,slow); s={}
    for i in range(1,len(candles)):
        if any(v is None for v in [ef[i],ef[i-1],es[i],es[i-1]]): continue
        if ef[i-1]<=es[i-1] and ef[i]>es[i]: s[i]="CALL"
        elif ef[i-1]>=es[i-1] and ef[i]<es[i]: s[i]="PUT"
    return s

def sig_macd(candles, f=12, sl=26, sg=9):
    c=[x["close"] for x in candles]; _,_,hist=calc_macd(c,f,sl,sg); s={}
    for i in range(1,len(candles)):
        if hist[i] is None or hist[i-1] is None: continue
        if hist[i-1]<=0 and hist[i]>0: s[i]="CALL"
        elif hist[i-1]>=0 and hist[i]<0: s[i]="PUT"
    return s

def sig_rsi(candles, period=14, os_val=30, ob_val=70):
    c=[x["close"] for x in candles]; rsi=calc_rsi(c,period); s={}
    for i in range(len(candles)):
        if rsi[i] is None: continue
        if rsi[i]<os_val: s[i]="CALL"
        elif rsi[i]>ob_val: s[i]="PUT"
    return s

def sig_bb(candles, period=20, sm=2.0):
    c=[x["close"] for x in candles]; _,u,l=calc_bb(c,period,sm); s={}
    for i in range(len(candles)):
        if l[i] is None: continue
        if c[i]<=l[i]: s[i]="CALL"
        elif c[i]>=u[i]: s[i]="PUT"
    return s

def sig_sr(candles, lb=25, th=0.005):
    c=[x["close"] for x in candles]; h=[x["high"] for x in candles]; lo=[x["low"] for x in candles]; s={}
    for i in range(lb, len(candles)):
        res=max(h[i-lb:i]); sup=min(lo[i-lb:i]); rng=res-sup
        if rng==0: continue
        pos=(c[i]-sup)/rng
        if pos<th: s[i]="CALL"
        elif pos>(1-th): s[i]="PUT"
    return s

def sig_linreg(candles, period=20, sm=2.0):
    closes=[x["close"] for x in candles]; s={}
    for i in range(period, len(candles)):
        w=closes[i-period:i]; n=len(w)
        sx=n*(n-1)/2; sy=sum(w); sxx=sum(j*j for j in range(n)); sxy=sum(j*v for j,v in enumerate(w))
        d=n*sxx-sx*sx
        if d==0: continue
        sl2=(n*sxy-sx*sy)/d; ic=(sy-sl2*sx)/n; pred=ic+sl2*(n-1)
        std=(sum((w[j]-(ic+sl2*j))**2 for j in range(n))/n)**0.5
        if std==0: continue
        if closes[i]<pred-sm*std: s[i]="CALL"
        elif closes[i]>pred+sm*std: s[i]="PUT"
    return s

def confluence_signals(strat_list, min_agree=3):
    all_idx = set()
    for ss in strat_list: all_idx |= set(ss.keys())
    result = {}
    for idx in sorted(all_idx):
        votes = {}
        for ss in strat_list:
            if idx in ss: d=ss[idx]; votes[d]=votes.get(d,0)+1
        for d,cnt in votes.items():
            if cnt>=min_agree: result[idx]=d; break
    return result

def get_strats(candles):
    return [
        sig_ema(candles,3,10), sig_ema(candles,5,20), sig_ema(candles,8,21),
        sig_macd(candles,12,26,9), sig_macd(candles,5,13,4),
        sig_rsi(candles,14,30,70), sig_rsi(candles,7,20,80), sig_rsi(candles,14,25,75),
        sig_bb(candles,20,2.0), sig_bb(candles,14,1.5),
        sig_sr(candles,25,0.005), sig_sr(candles,40,0.005),
        sig_linreg(candles,20,2.0),
    ]

def backtest(candles, sigs, eb, base=10.0, payout=0.80, cd=None, staking="flat", bal0=1000.0):
    if cd is None: cd=eb
    w=l=d=0; pnl=0.0; pk=0.0; mdd=0.0; last=-cd; streak=0; bal=bal0; daily={}
    for idx in sorted(sigs.keys()):
        if idx+eb>=len(candles): continue
        if idx-last<cd: continue
        dr=sigs[idx]; entry=candles[idx]["close"]; exp=candles[idx+eb]["close"]; last=idx
        day=datetime.fromtimestamp(candles[idx]["time"],UTC).strftime("%Y-%m-%d")
        if staking=="flat": stake=base
        elif staking=="flat25": stake=25.0
        elif staking=="flat50": stake=50.0
        elif staking=="kelly":
            if w+l>=10:
                wr=w/(w+l); kf=(wr*(1+payout)-1)/payout; kf=max(0.01,min(kf,0.10)); stake=bal*kf
            else: stake=base
        elif staking=="halfkelly":
            if w+l>=20:
                wr=w/(w+l); kf=(wr*(1+payout)-1)/payout; kf=max(0.005,min(kf*0.5,0.05)); stake=bal*kf
            else: stake=base
        elif staking=="antimart":
            stake=base*min(1+max(0,streak)*0.5, 4)
        else: stake=base
        stake=min(stake,bal*0.15)
        if stake<1: stake=1
        if entry==exp: d+=1
        elif (dr=="CALL" and exp>entry) or (dr=="PUT" and exp<entry):
            pnl+=stake*payout; bal+=stake*payout; w+=1; streak=max(0,streak)+1
        else:
            pnl-=stake; bal-=stake; l+=1; streak=min(0,streak)-1
        pk=max(pk,pnl); mdd=max(mdd,pk-pnl)
        if day not in daily: daily[day]={"w":0,"l":0,"pnl":0.0,"bal":bal}
        daily[day]["bal"]=bal
        if entry!=exp:
            if (dr=="CALL" and exp>entry) or (dr=="PUT" and exp<entry):
                daily[day]["w"]+=1; daily[day]["pnl"]+=stake*payout
            else: daily[day]["l"]+=1; daily[day]["pnl"]-=stake
        if bal<=0: break
    wr=w/(w+l)*100 if w+l>0 else 0
    return {"trades":w+l+d,"wins":w,"losses":l,"draws":d,"wr":wr,"pnl":pnl,"mdd":mdd,"bal":bal,"daily":daily}

async def fetch_chunked(ws, sym, gran, days=35):
    """Fetch data in 5000-candle chunks going back `days` days."""
    now_ts = int(datetime.now(UTC).timestamp())
    start_ts = now_ts - days*86400
    all_c = []
    # Work backwards from now in chunks
    end = now_ts
    chunk = 0
    while True:
        chunk += 1
        rid = int(time.time()*1000)%100000 + chunk
        msg = {"ticks_history":sym,"adjust_start_time":1,"count":5000,
               "end":end,"granularity":gran,"style":"candles","req_id":rid}
        await ws.send(json.dumps(msg))
        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        candles = resp.get("candles",[])
        if not candles: break
        batch = [{"time":c["epoch"],"open":float(c["open"]),"high":float(c["high"]),
                  "low":float(c["low"]),"close":float(c["close"])} for c in candles]
        all_c = batch + all_c  # prepend older data
        oldest = batch[0]["time"]
        t0=datetime.fromtimestamp(batch[0]["time"],UTC)
        t1=datetime.fromtimestamp(batch[-1]["time"],UTC)
        print(f"  Chunk {chunk}: {len(batch)} candles ({t0:%m-%d %H:%M} → {t1:%m-%d %H:%M})")
        if oldest <= start_ts: break
        end = oldest - 1
        await asyncio.sleep(0.3)
    # Dedupe
    seen=set(); out=[]
    for c in all_c:
        if c["time"] not in seen: seen.add(c["time"]); out.append(c)
    out.sort(key=lambda x:x["time"])
    # Trim to requested period
    out = [c for c in out if c["time"] >= start_ts]
    return out

async def main():
    print("Connecting to Deriv...")
    ws = await asyncio.wait_for(websockets.connect(WS,additional_headers=HDRS,ping_interval=25),timeout=15)
    print("Connected!\n")
    P=0.80; BE=100/(1+P)
    now=datetime.now(UTC)
    print(f"UTC: {now:%Y-%m-%d %H:%M} | Breakeven: {BE:.1f}%")
    print(f"Target: 30 days of data\n")

    # Fetch data
    markets = [
        ("Vol75 5m",  "1HZ75V", 300),   # 5000×5m = 17 days → 2 chunks = 34 days
        ("Vol10 5m",  "1HZ10V", 300),
        ("Vol100 5m", "R_100",  300),
        ("Vol75 15m", "1HZ75V", 900),   # 5000×15m = 52 days → 1 chunk enough
        ("Vol10 15m", "1HZ10V", 900),
        ("EURGBP 5m", "frxEURGBP", 300),
        ("EURGBP 15m","frxEURGBP", 900),
    ]

    data = {}
    for name, sym, gran in markets:
        print(f"Fetching {name}...")
        data[name] = await fetch_chunked(ws, sym, gran, days=35)
        c = data[name]
        if c:
            t0=datetime.fromtimestamp(c[0]["time"],UTC)
            t1=datetime.fromtimestamp(c[-1]["time"],UTC)
            d_cov = (t1-t0).total_seconds()/86400
            print(f"  TOTAL: {len(c)} candles, {d_cov:.1f} days ({t0:%Y-%m-%d} → {t1:%Y-%m-%d})\n")

    await ws.close()

    # ─── Configs to test ───
    # For 5m candles: eb=1 means 5m expiry, eb=3 means 15m, eb=6 means 30m
    # For 15m candles: eb=1 means 15m expiry, eb=2 means 30m, eb=3 means 45m
    configs = [
        # Synthetics 5m
        ("Vol75 5m→5m",    "Vol75 5m",  1, None),
        ("Vol75 5m→15m",   "Vol75 5m",  3, None),
        ("Vol75 5m→30m",   "Vol75 5m",  6, None),
        ("Vol10 5m→5m",    "Vol10 5m",  1, None),
        ("Vol10 5m→15m",   "Vol10 5m",  3, None),
        ("Vol100 5m→5m",   "Vol100 5m", 1, None),
        ("Vol100 5m→15m",  "Vol100 5m", 3, None),
        # Synthetics 15m
        ("Vol75 15m→15m",  "Vol75 15m", 1, None),
        ("Vol75 15m→30m",  "Vol75 15m", 2, None),
        ("Vol75 15m→45m",  "Vol75 15m", 3, None),
        ("Vol10 15m→15m",  "Vol10 15m", 1, None),
        # Forex
        ("EURGBP 5m→15m London",  "EURGBP 5m",  3, {7,8,9,10,11,12,13,14,15}),
        ("EURGBP 5m→15m All",     "EURGBP 5m",  3, None),
        ("EURGBP 5m→30m London",  "EURGBP 5m",  6, {7,8,9,10,11,12,13,14,15}),
        ("EURGBP 15m→15m London", "EURGBP 15m", 1, {7,8,9,10,11,12,13,14,15}),
        ("EURGBP 15m→30m London", "EURGBP 15m", 2, {7,8,9,10,11,12,13,14,15}),
        ("EURGBP 15m→45m London", "EURGBP 15m", 3, {7,8,9,10,11,12,13,14,15}),
    ]

    confs = [2, 3, 4, 5]
    staking_modes = [
        ("flat$10","flat",10), ("flat$25","flat25",25), ("flat$50","flat50",50),
        ("kelly","kelly",10), ("halfkelly","halfkelly",10), ("antimart","antimart",10),
    ]

    # ─── PART 1: Find best config + confluence ───
    print(f"\n{'='*120}")
    print(f"  30-DAY BACKTEST — ALL CONFIGS (flat $10)")
    print(f"{'='*120}\n")
    print(f"{'Config':<28s} {'Conf':>4s} {'Days':>4s} {'Tr':>5s} {'W':>5s} {'L':>5s} {'WR':>6s} {'P&L':>10s} {'MDD':>8s}")
    print("-"*90)

    all_r = []
    for label, mkt, eb, hrs in configs:
        if mkt not in data or len(data[mkt])<100: continue
        candles = data[mkt]
        strats = get_strats(candles)
        for mn in confs:
            csigs = confluence_signals(strats, mn)
            if hrs:
                csigs = {k:v for k,v in csigs.items()
                         if datetime.fromtimestamp(candles[k]["time"],UTC).hour in hrs}
            if not csigs: continue
            r = backtest(candles, csigs, eb, base=10, payout=P)
            if r["trades"]<5: continue
            days = len(r["daily"])
            v = "✅" if r["wr"]>BE else "  "
            tag = f"{label} c≥{mn}"
            print(f"{v} {tag:<26s} {mn:>4d} {days:>4d} {r['trades']:>5d} {r['wins']:>5d} {r['losses']:>5d} "
                  f"{r['wr']:>5.1f}% ${r['pnl']:>8.1f} ${r['mdd']:>6.1f}")
            all_r.append({"tag":tag,"label":label,"mkt":mkt,"eb":eb,"hrs":hrs,"mn":mn,**r})

    # Sort by WR for profitable ones
    prof = [x for x in all_r if x["wr"]>BE]
    prof.sort(key=lambda x: (-x["wr"], -x["pnl"]))

    print(f"\n{'='*120}")
    print(f"  TOP 20 PROFITABLE CONFIGS (WR > {BE:.1f}%)")
    print(f"{'='*120}\n")
    for i, r in enumerate(prof[:20]):
        days = len(r["daily"])
        edge = r["wr"]-BE
        print(f"  {i+1:>2d}. {r['tag']:<30s} {days:>3d}d {r['trades']:>4d}tr "
              f"WR {r['wr']:>5.1f}% (+{edge:.1f}) P&L ${r['pnl']:>8.1f} MDD ${r['mdd']:>.1f}")

    # ─── PART 2: Staking on top 5 configs ───
    print(f"\n{'='*120}")
    print(f"  STAKING COMPARISON — Top 5 configs")
    print(f"{'='*120}\n")

    top5 = prof[:5]
    for r in top5:
        candles = data[r["mkt"]]
        strats = get_strats(candles)
        csigs = confluence_signals(strats, r["mn"])
        if r["hrs"]:
            csigs = {k:v for k,v in csigs.items()
                     if datetime.fromtimestamp(candles[k]["time"],UTC).hour in r["hrs"]}

        print(f"  {r['tag']}")
        print(f"  {'Staking':<14s} {'Tr':>5s} {'WR':>6s} {'P&L':>12s} {'MDD':>10s} {'Final':>12s} {'ROI':>8s}")
        print("  "+"-"*75)
        for sn, sm, sb in staking_modes:
            rs = backtest(candles, csigs, r["eb"], base=sb, payout=P, staking=sm)
            roi = (rs["pnl"]/1000)*100
            print(f"  {sn:<14s} {rs['trades']:>5d} {rs['wr']:>5.1f}% ${rs['pnl']:>10.1f} "
                  f"${rs['mdd']:>8.1f} ${rs['bal']:>10.1f} {roi:>6.1f}%")
        print()

    # ─── PART 3: Day-by-day for #1 ───
    if prof:
        best = prof[0]
        candles = data[best["mkt"]]
        strats = get_strats(candles)
        csigs = confluence_signals(strats, best["mn"])
        if best["hrs"]:
            csigs = {k:v for k,v in csigs.items()
                     if datetime.fromtimestamp(candles[k]["time"],UTC).hour in best["hrs"]}

        print(f"\n{'='*120}")
        print(f"  DAY-BY-DAY: {best['tag']} (flat $10)")
        print(f"{'='*120}\n")

        for sn, sm, sb in [("flat$10","flat",10), ("flat$50","flat50",50)]:
            rs = backtest(candles, csigs, best["eb"], base=sb, payout=P, staking=sm)
            days = sorted(rs["daily"].keys())
            wd = sum(1 for d in days if rs["daily"][d]["pnl"]>0)
            ld = sum(1 for d in days if rs["daily"][d]["pnl"]<0)
            print(f"  {best['tag']} + {sn} | {rs['trades']}tr WR {rs['wr']:.1f}% P&L ${rs['pnl']:.1f}")
            print(f"  {'Date':<12s} {'W':>3s} {'L':>3s} {'WR':>6s} {'DayP&L':>10s} {'Balance':>10s}")
            print("  "+"-"*55)
            for day in days:
                dd = rs["daily"][day]
                tot=dd["w"]+dd["l"]; dwr=dd["w"]/tot*100 if tot>0 else 0
                icon = "✅" if dd["pnl"]>0 else ("❌" if dd["pnl"]<0 else "➖")
                print(f"  {icon} {day} {dd['w']:>3d} {dd['l']:>3d} {dwr:>5.1f}% ${dd['pnl']:>8.1f} ${dd['bal']:>8.1f}")
            print(f"  Win days: {wd}/{wd+ld} ({wd/(wd+ld)*100:.0f}%) | "
                  f"Avg daily: ${rs['pnl']/len(days):.1f}\n")

    # ─── FINAL ───
    print(f"\n{'='*120}")
    print("  HONEST 30-DAY VERDICT")
    print(f"{'='*120}")
    if prof:
        b=prof[0]; days=sorted(b["daily"].keys())
        wd=sum(1 for d in days if b["daily"][d]["pnl"]>0)
        print(f"\n  Best config: {b['tag']}")
        print(f"  Period: {days[0]} → {days[-1]} ({len(days)} trading days)")
        print(f"  Trades: {b['trades']} | WR: {b['wr']:.1f}% | Edge: +{b['wr']-BE:.1f}%")
        print(f"  P&L (flat $10): ${b['pnl']:.1f}")
        print(f"  Max Drawdown: ${b['mdd']:.1f}")
        print(f"  Winning days: {wd}/{len(days)} ({wd/len(days)*100:.0f}%)")
        print(f"  Avg daily P&L: ${b['pnl']/len(days):.1f}")
    n_total = len(all_r)
    n_prof = len(prof)
    print(f"\n  Configs tested: {n_total} | Profitable: {n_prof} ({n_prof/n_total*100:.0f}%)")
    print()

if __name__=="__main__":
    asyncio.run(main())

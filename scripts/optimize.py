"""Optimize SL/TP ratios and lot sizes for top strategies."""
import random
from datetime import datetime
from zoneinfo import ZoneInfo
import yfinance as yf

UTC = ZoneInfo("UTC")

# ── Indicators (same as strategy_lab) ───────────────────────────────────────
def ema(values, period):
    result=[None]*len(values)
    if len(values)<period: return result
    k=2/(period+1); result[period-1]=sum(values[:period])/period
    for i in range(period,len(values)): result[i]=values[i]*k+result[i-1]*(1-k)
    return result

def rsi(closes, period=14):
    result=[None]*len(closes)
    if len(closes)<period+1: return result
    gains,losses=[],[]
    for i in range(1,period+1):
        d=closes[i]-closes[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/period; al=sum(losses)/period
    result[period]=100.0 if al==0 else 100-100/(1+ag/al)
    for i in range(period+1,len(closes)):
        d=closes[i]-closes[i-1]; ag=(ag*(period-1)+max(d,0))/period; al=(al*(period-1)+max(-d,0))/period
        result[i]=100.0 if al==0 else 100-100/(1+ag/al)
    return result

def atr(candles, period=14):
    result=[None]*len(candles)
    if len(candles)<period+1: return result
    trs=[]
    for i in range(1,len(candles)):
        h,l,pc=candles[i]["high"],candles[i]["low"],candles[i-1]["close"]
        trs.append(max(h-l,abs(h-pc),abs(l-pc)))
    if len(trs)<period: return result
    result[period]=sum(trs[:period])/period
    for i in range(period,len(trs)): result[i+1]=(result[i]*(period-1)+trs[i])/period
    return result

def macd(closes, fast=12, slow=26, signal=9):
    ef=ema(closes,fast); es=ema(closes,slow)
    ml=[None]*len(closes)
    for i in range(len(closes)):
        if ef[i] is not None and es[i] is not None: ml[i]=ef[i]-es[i]
    valid=[v for v in ml if v is not None]; sig=ema(valid,signal)
    sl=[None]*len(closes); j=0
    for i in range(len(closes)):
        if ml[i] is not None: sl[i]=sig[j] if j<len(sig) else None; j+=1
    return ml, sl

def donchian(candles, period=20):
    upper,lower=[None]*len(candles),[None]*len(candles)
    for i in range(period,len(candles)):
        upper[i]=max(c["high"] for c in candles[i-period:i])
        lower[i]=min(c["low"] for c in candles[i-period:i])
    return upper, lower

# ── Signal generators (parametric SL/TP) ────────────────────────────────────
def sig_ema_cross(candles, fast_p, slow_p, sl, tp):
    closes=[c["close"] for c in candles]; f=ema(closes,fast_p); s=ema(closes,slow_p); sigs=[]
    for i in range(1,len(candles)):
        if f[i] is None or f[i-1] is None or s[i] is None or s[i-1] is None: continue
        if f[i-1]<=s[i-1] and f[i]>s[i]: sigs.append((i,"CALL",sl,tp))
        elif f[i-1]>=s[i-1] and f[i]<s[i]: sigs.append((i,"PUT",sl,tp))
    return sigs

def sig_triple_ema(candles, p1, p2, p3, sl, tp):
    closes=[c["close"] for c in candles]; e1=ema(closes,p1); e2=ema(closes,p2); e3=ema(closes,p3); sigs=[]
    for i in range(1,len(candles)):
        if e1[i] is None or e2[i] is None or e3[i] is None or e1[i-1] is None or e2[i-1] is None or e3[i-1] is None: continue
        pu=e1[i-1]>e2[i-1] and e2[i-1]>e3[i-1]; cu=e1[i]>e2[i] and e2[i]>e3[i]
        pd=e1[i-1]<e2[i-1] and e2[i-1]<e3[i-1]; cd=e1[i]<e2[i] and e2[i]<e3[i]
        if not pu and cu: sigs.append((i,"CALL",sl,tp))
        elif not pd and cd: sigs.append((i,"PUT",sl,tp))
    return sigs

def sig_macd(candles, sl, tp):
    closes=[c["close"] for c in candles]; ml,sl_line=macd(closes); sigs=[]
    for i in range(1,len(candles)):
        if ml[i] is None or ml[i-1] is None or sl_line[i] is None or sl_line[i-1] is None: continue
        if ml[i-1]<=sl_line[i-1] and ml[i]>sl_line[i]: sigs.append((i,"CALL",sl,tp))
        elif ml[i-1]>=sl_line[i-1] and ml[i]<sl_line[i]: sigs.append((i,"PUT",sl,tp))
    return sigs

def sig_rsi(candles, sl, tp):
    closes=[c["close"] for c in candles]; r=rsi(closes,14); sigs=[]
    for i in range(1,len(candles)):
        if r[i] is None or r[i-1] is None: continue
        if r[i-1]<30 and r[i]>=30: sigs.append((i,"CALL",sl,tp))
        elif r[i-1]>70 and r[i]<=70: sigs.append((i,"PUT",sl,tp))
    return sigs

def sig_donchian(candles, sl, tp):
    upper,lower=donchian(candles,20); sigs=[]
    for i in range(1,len(candles)):
        if upper[i] is None: continue
        if candles[i]["close"]>upper[i]: sigs.append((i,"CALL",sl,tp))
        elif candles[i]["close"]<lower[i]: sigs.append((i,"PUT",sl,tp))
    return sigs

# ── Trade simulator ─────────────────────────────────────────────────────────
def simulate(candles, signals, spread_pips, pip_size, pip_value, lot_size, max_hold=50):
    atr_vals=atr(candles,14); trades=[]
    i=0
    while i<len(signals):
        idx,direction,sl_mult,tp_mult=signals[i]
        if idx+1>=len(candles) or atr_vals[idx] is None: i+=1; continue
        cur_atr=atr_vals[idx]; slippage=random.uniform(0,0.5)*pip_size
        sh=(spread_pips*pip_size)/2; entry_idx=idx+1; raw_entry=candles[entry_idx]["open"]
        if direction=="CALL":
            ep=raw_entry+sh+slippage; sl_p=ep-sl_mult*cur_atr; tp_p=ep+tp_mult*cur_atr
        else:
            ep=raw_entry-sh-slippage; sl_p=ep+sl_mult*cur_atr; tp_p=ep-tp_mult*cur_atr
        exit_p=None; reason="timeout"
        for j in range(entry_idx,min(entry_idx+max_hold,len(candles))):
            c=candles[j]
            if direction=="CALL":
                if c["low"]<=sl_p: exit_p=sl_p-sh; reason="SL"; break
                if c["high"]>=tp_p: exit_p=tp_p-sh; reason="TP"; break
            else:
                if c["high"]>=sl_p: exit_p=sl_p+sh; reason="SL"; break
                if c["low"]<=tp_p: exit_p=tp_p+sh; reason="TP"; break
        if exit_p is None:
            lj=min(entry_idx+max_hold-1,len(candles)-1)
            exit_p=candles[lj]["close"]-sh if direction=="CALL" else candles[lj]["close"]+sh
        pm=((exit_p-ep) if direction=="CALL" else (ep-exit_p))/pip_size
        pnl=pm*pip_value*lot_size
        trades.append({"pnl":pnl,"won":pnl>0,"reason":reason})
        hold_end=entry_idx+max_hold; i+=1
        while i<len(signals) and signals[i][0]<hold_end: i+=1
    return trades

def stats(trades):
    if not trades: return 0,0,0,0,0,0
    wins=sum(1 for t in trades if t["won"]); total=len(trades)
    wr=wins/total*100; pnl=sum(t["pnl"] for t in trades)
    wp=[t["pnl"] for t in trades if t["won"]]; lp=[t["pnl"] for t in trades if not t["won"]]
    aw=sum(wp)/len(wp) if wp else 0; al=abs(sum(lp)/len(lp)) if lp else 0
    gw=sum(wp); gl=abs(sum(lp)); pf=gw/gl if gl else 99
    eq=0; pk=0; mdd=0
    for t in trades: eq+=t["pnl"]; pk=max(pk,eq); mdd=max(mdd,pk-eq)
    return total,wr,pnl,pf,mdd,aw

# ── Main optimization ──────────────────────────────────────────────────────
PAIRS = {
    "GBP/USD": {"symbol":"GBPUSD=X","spread":1.0,"pip":0.0001,"pv":830},
    "EUR/USD": {"symbol":"EURUSD=X","spread":0.8,"pip":0.0001,"pv":830},
    "USD/JPY": {"symbol":"USDJPY=X","spread":0.8,"pip":0.01,"pv":55},
    "GBP/JPY": {"symbol":"GBPJPY=X","spread":1.5,"pip":0.01,"pv":55},
}

# Top combos to optimize
COMBOS = [
    ("Triple EMA","GBP/USD","1h",lambda c,sl,tp: sig_triple_ema(c,8,13,21,sl,tp)),
    ("EMA 9/21","EUR/USD","1h",lambda c,sl,tp: sig_ema_cross(c,9,21,sl,tp)),
    ("EMA 9/21","GBP/USD","1h",lambda c,sl,tp: sig_ema_cross(c,9,21,sl,tp)),
    ("Donchian","GBP/USD","1h",lambda c,sl,tp: sig_donchian(c,sl,tp)),
    ("EMA 20/50","GBP/JPY","1h",lambda c,sl,tp: sig_ema_cross(c,20,50,sl,tp)),
    ("MACD","EUR/USD","15m",lambda c,sl,tp: sig_macd(c,sl,tp)),
    ("MACD","GBP/JPY","15m",lambda c,sl,tp: sig_macd(c,sl,tp)),
    ("RSI","USD/JPY","1h",lambda c,sl,tp: sig_rsi(c,sl,tp)),
    ("RSI","USD/JPY","15m",lambda c,sl,tp: sig_rsi(c,sl,tp)),
    ("Donchian","USD/JPY","15m",lambda c,sl,tp: sig_donchian(c,sl,tp)),
]

SL_RANGE = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
TP_RANGE = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
LOT_SIZES = [0.5, 1.0, 1.5, 2.0, 3.0]

# Cache candle data
candle_cache = {}
def get_candles(symbol, interval):
    key=f"{symbol}_{interval}"
    if key not in candle_cache:
        df=yf.Ticker(symbol).history(period="1mo",interval=interval)
        candle_cache[key]=[{"time":int(ts.timestamp()),"open":float(row["Open"]),
            "high":float(row["High"]),"low":float(row["Low"]),"close":float(row["Close"])}
            for ts,row in df.iterrows()]
    return candle_cache[key]

print("="*90)
print("OPTIMIZATION SWEEP — Finding best SL/TP ratios for top strategies")
print("="*90)

all_results = []

for sname,pair,tf,sfn in COMBOS:
    pcfg=PAIRS[pair]
    candles=get_candles(pcfg["symbol"],tf)
    sep=[c for c in candles if datetime.fromtimestamp(c["time"],UTC).month==9]
    if len(sep)<50: continue
    
    best_pnl=-999999; best_cfg=None
    sweep_results = []
    
    for sl in SL_RANGE:
        for tp in TP_RANGE:
            if tp <= sl*0.5: continue  # skip absurd R:R
            random.seed(42)
            signals=sfn(sep,sl,tp)
            trades=simulate(sep,signals,pcfg["spread"],pcfg["pip"],pcfg["pv"],0.5)
            total,wr,pnl,pf,mdd,aw=stats(trades)
            if total >= 3:
                sweep_results.append((sl,tp,total,wr,pnl,pf,mdd))
                if pnl>best_pnl: best_pnl=pnl; best_cfg=(sl,tp,total,wr,pnl,pf,mdd)
    
    if not best_cfg: continue
    sl,tp,total,wr,pnl,pf,mdd = best_cfg
    
    print(f"\n{'─'*70}")
    print(f"  {sname} | {pair} | {tf}")
    print(f"  Best SL={sl} TP={tp} → {total} trades, WR {wr:.1f}%, PF {pf:.2f}, P&L ₹{pnl:,.0f}, MaxDD ₹{mdd:,.0f}")
    
    # Now test lot sizes with the best SL/TP
    print(f"  Lot size scaling (SL={sl}, TP={tp}):")
    print(f"    {'Lot':>5} {'P&L':>12} {'MaxDD':>10} {'Risk%':>7}")
    for lot in LOT_SIZES:
        random.seed(42)
        signals=sfn(sep,sl,tp)
        trades=simulate(sep,signals,pcfg["spread"],pcfg["pip"],pcfg["pv"],lot)
        t,w,p,pf2,m,aw2=stats(trades)
        risk_pct=m/150000*100
        marker=" <<<" if p>0 and risk_pct<15 else " (risky)" if risk_pct>20 else ""
        print(f"    {lot:>5.1f} ₹{p:>11,.0f} ₹{m:>9,.0f} {risk_pct:>6.1f}%{marker}")
    
    # Top 5 SL/TP combos
    sweep_results.sort(key=lambda x: x[4], reverse=True)
    print(f"  Top 5 SL/TP combos:")
    print(f"    {'SL':>5} {'TP':>5} {'Trades':>6} {'WR':>6} {'PF':>6} {'P&L':>12} {'MaxDD':>10}")
    for sl2,tp2,t2,w2,p2,pf2,m2 in sweep_results[:5]:
        print(f"    {sl2:>5.2f} {tp2:>5.1f} {t2:>6} {w2:>5.1f}% {pf2:>5.2f} ₹{p2:>11,.0f} ₹{m2:>9,.0f}")
    
    all_results.append((sname,pair,tf,best_cfg))

# Portfolio simulation
print(f"\n{'='*90}")
print("PORTFOLIO — Running all optimized strategies together (Sep)")
print(f"{'='*90}")

portfolio_pnl = 0; portfolio_trades = 0; portfolio_wins = 0; portfolio_maxdd = 0
for sname,pair,tf,cfg in all_results:
    sl,tp,total,wr,pnl,pf,mdd = cfg
    # Use 1.0 lot for portfolio
    pcfg=PAIRS[pair]; candles=get_candles(pcfg["symbol"],tf)
    sep=[c for c in candles if datetime.fromtimestamp(c["time"],UTC).month==9]
    random.seed(42)
    sfn_map = {
        "Triple EMA": lambda c,sl,tp: sig_triple_ema(c,8,13,21,sl,tp),
        "EMA 9/21": lambda c,sl,tp: sig_ema_cross(c,9,21,sl,tp),
        "EMA 20/50": lambda c,sl,tp: sig_ema_cross(c,20,50,sl,tp),
        "Donchian": lambda c,sl,tp: sig_donchian(c,sl,tp),
        "MACD": lambda c,sl,tp: sig_macd(c,sl,tp),
        "RSI": lambda c,sl,tp: sig_rsi(c,sl,tp),
    }
    signals = sfn_map[sname](sep,sl,tp)
    trades = simulate(sep,signals,pcfg["spread"],pcfg["pip"],pcfg["pv"],1.0)
    t,w,p,pf2,m,aw2 = stats(trades)
    portfolio_pnl += p; portfolio_trades += t; portfolio_wins += sum(1 for tr in trades if tr["won"])
    print(f"  {sname:<12} {pair:<10} {tf:<4} SL={sl:.1f} TP={tp:.1f} → {t} trades, P&L ₹{p:>8,.0f}")

print(f"\n  PORTFOLIO TOTAL (1.0 lot each):")
print(f"    Trades: {portfolio_trades}")
print(f"    Wins: {portfolio_wins}")
print(f"    WR: {portfolio_wins/portfolio_trades*100:.1f}%" if portfolio_trades else "")
print(f"    Combined P&L: ₹{portfolio_pnl:,.0f}")
print(f"    Per day avg: ₹{portfolio_pnl/22:,.0f} (22 trading days)")

# With 2.0 lots
print(f"\n  With 2.0 lots each: ₹{portfolio_pnl*2:,.0f}/month = ₹{portfolio_pnl*2/22:,.0f}/day")
print(f"  With 3.0 lots each: ₹{portfolio_pnl*3:,.0f}/month = ₹{portfolio_pnl*3/22:,.0f}/day")

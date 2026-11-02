"""Backtest v3 strategies on this week (Oct 6-9, 2026)."""
import random
from datetime import datetime
from zoneinfo import ZoneInfo
import yfinance as yf

UTC = ZoneInfo("UTC")

def ema(values, period):
    r=[None]*len(values)
    if len(values)<period: return r
    k=2/(period+1); r[period-1]=sum(values[:period])/period
    for i in range(period,len(values)): r[i]=values[i]*k+r[i-1]*(1-k)
    return r

def rsi(closes, period=14):
    r=[None]*len(closes)
    if len(closes)<period+1: return r
    gains,losses=[],[]
    for i in range(1,period+1):
        d=closes[i]-closes[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/period; al=sum(losses)/period
    r[period]=100.0 if al==0 else 100-100/(1+ag/al)
    for i in range(period+1,len(closes)):
        d=closes[i]-closes[i-1]; ag=(ag*(period-1)+max(d,0))/period; al=(al*(period-1)+max(-d,0))/period
        r[i]=100.0 if al==0 else 100-100/(1+ag/al)
    return r

def atr(candles, period=14):
    r=[None]*len(candles)
    if len(candles)<period+1: return r
    trs=[]
    for i in range(1,len(candles)):
        h,l,pc=candles[i]["high"],candles[i]["low"],candles[i-1]["close"]
        trs.append(max(h-l,abs(h-pc),abs(l-pc)))
    if len(trs)<period: return r
    r[period]=sum(trs[:period])/period
    for i in range(period,len(trs)): r[i+1]=(r[i]*(period-1)+trs[i])/period
    return r

def macd_ind(closes, fast=12, slow=26, signal=9):
    ef=ema(closes,fast); es=ema(closes,slow)
    ml=[None]*len(closes)
    for i in range(len(closes)):
        if ef[i] is not None and es[i] is not None: ml[i]=ef[i]-es[i]
    valid=[v for v in ml if v is not None]; sig=ema(valid,signal)
    sl=[None]*len(closes); j=0
    for i in range(len(closes)):
        if ml[i] is not None: sl[i]=sig[j] if j<len(sig) else None; j+=1
    return ml, sl

def donchian_ind(candles, period=20):
    upper,lower=[None]*len(candles),[None]*len(candles)
    for i in range(period,len(candles)):
        upper[i]=max(c["high"] for c in candles[i-period:i])
        lower[i]=min(c["low"] for c in candles[i-period:i])
    return upper, lower

def gen_triple_ema(candles):
    closes=[c["close"] for c in candles]; e1=ema(closes,8); e2=ema(closes,13); e3=ema(closes,21); sigs=[]
    for i in range(1,len(candles)):
        if any(x is None for x in [e1[i],e2[i],e3[i],e1[i-1],e2[i-1],e3[i-1]]): continue
        pu=e1[i-1]>e2[i-1] and e2[i-1]>e3[i-1]; cu=e1[i]>e2[i] and e2[i]>e3[i]
        pd=e1[i-1]<e2[i-1] and e2[i-1]<e3[i-1]; cd=e1[i]<e2[i] and e2[i]<e3[i]
        if not pu and cu: sigs.append((i,"CALL"))
        elif not pd and cd: sigs.append((i,"PUT"))
    return sigs

def gen_ema_cross(candles, fast_p, slow_p):
    closes=[c["close"] for c in candles]; f=ema(closes,fast_p); s=ema(closes,slow_p); sigs=[]
    for i in range(1,len(candles)):
        if f[i] is None or f[i-1] is None or s[i] is None or s[i-1] is None: continue
        if f[i-1]<=s[i-1] and f[i]>s[i]: sigs.append((i,"CALL"))
        elif f[i-1]>=s[i-1] and f[i]<s[i]: sigs.append((i,"PUT"))
    return sigs

def gen_macd(candles):
    closes=[c["close"] for c in candles]; ml,sl=macd_ind(closes); sigs=[]
    for i in range(1,len(candles)):
        if ml[i] is None or ml[i-1] is None or sl[i] is None or sl[i-1] is None: continue
        if ml[i-1]<=sl[i-1] and ml[i]>sl[i]: sigs.append((i,"CALL"))
        elif ml[i-1]>=sl[i-1] and ml[i]<sl[i]: sigs.append((i,"PUT"))
    return sigs

def gen_rsi(candles):
    closes=[c["close"] for c in candles]; r=rsi(closes,14); sigs=[]
    for i in range(1,len(candles)):
        if r[i] is None or r[i-1] is None: continue
        if r[i-1]<30 and r[i]>=30: sigs.append((i,"CALL"))
        elif r[i-1]>70 and r[i]<=70: sigs.append((i,"PUT"))
    return sigs

def gen_donchian(candles):
    upper,lower=donchian_ind(candles,20); sigs=[]
    for i in range(1,len(candles)):
        if upper[i] is None: continue
        if candles[i]["close"]>upper[i]: sigs.append((i,"CALL"))
        elif candles[i]["close"]<lower[i]: sigs.append((i,"PUT"))
    return sigs

def simulate(candles, signals, sl_mult, tp_mult, spread_pips, pip_size, pip_value, lot_size, max_hold=50):
    atr_vals=atr(candles,14); trades=[]
    i=0
    while i<len(signals):
        idx,direction=signals[i]
        if idx+1>=len(candles) or atr_vals[idx] is None: i+=1; continue
        cur_atr=atr_vals[idx]; slippage=random.uniform(0,0.5)*pip_size
        sh=(spread_pips*pip_size)/2; entry_idx=idx+1; raw_entry=candles[entry_idx]["open"]
        if direction=="CALL":
            ep=raw_entry+sh+slippage; sl_p=ep-sl_mult*cur_atr; tp_p=ep+tp_mult*cur_atr
        else:
            ep=raw_entry-sh-slippage; sl_p=ep+sl_mult*cur_atr; tp_p=ep-tp_mult*cur_atr
        exit_p=None; reason="TIMEOUT"; exit_idx=entry_idx
        for j in range(entry_idx,min(entry_idx+max_hold,len(candles))):
            c=candles[j]
            if direction=="CALL":
                if c["low"]<=sl_p: exit_p=sl_p-sh; reason="SL"; exit_idx=j; break
                if c["high"]>=tp_p: exit_p=tp_p-sh; reason="TP"; exit_idx=j; break
            else:
                if c["high"]>=sl_p: exit_p=sl_p+sh; reason="SL"; exit_idx=j; break
                if c["low"]<=tp_p: exit_p=tp_p+sh; reason="TP"; exit_idx=j; break
            exit_idx=j
        if exit_p is None:
            lj=min(entry_idx+max_hold-1,len(candles)-1)
            exit_p=candles[lj]["close"]-sh if direction=="CALL" else candles[lj]["close"]+sh
        pm=((exit_p-ep) if direction=="CALL" else (ep-exit_p))/pip_size
        pnl=pm*pip_value*lot_size
        day=datetime.fromtimestamp(candles[entry_idx]["time"],UTC).strftime("%Y-%m-%d")
        trades.append({"pnl":pnl,"pips":pm,"won":pnl>0,"reason":reason,"dir":direction,
                       "day":day,"held":exit_idx-entry_idx+1,"entry":ep,"exit":exit_p})
        hold_end=exit_idx+1; i+=1
        while i<len(signals) and signals[i][0]<hold_end: i+=1
    return trades

PAIRS = {
    "GBP/USD": {"symbol":"GBPUSD=X","spread":1.0,"pip":0.0001,"pv":830},
    "EUR/USD": {"symbol":"EURUSD=X","spread":0.8,"pip":0.0001,"pv":830},
    "USD/JPY": {"symbol":"USDJPY=X","spread":0.8,"pip":0.01,"pv":55},
    "GBP/JPY": {"symbol":"GBPJPY=X","spread":1.5,"pip":0.01,"pv":55},
}

STRATEGIES = [
    {"name":"Triple EMA",  "pair":"GBP/USD","tf":"1h", "sl":0.75,"tp":1.5, "fn":lambda c: gen_triple_ema(c)},
    {"name":"EMA 9/21",    "pair":"EUR/USD","tf":"1h", "sl":1.25,"tp":2.0, "fn":lambda c: gen_ema_cross(c,9,21)},
    {"name":"EMA 9/21",    "pair":"GBP/USD","tf":"1h", "sl":0.75,"tp":5.0, "fn":lambda c: gen_ema_cross(c,9,21)},
    {"name":"Donchian",    "pair":"GBP/USD","tf":"1h", "sl":0.75,"tp":5.0, "fn":lambda c: gen_donchian(c)},
    {"name":"EMA 20/50",   "pair":"GBP/JPY","tf":"1h", "sl":1.25,"tp":4.0, "fn":lambda c: gen_ema_cross(c,20,50)},
    {"name":"MACD",        "pair":"EUR/USD","tf":"15m","sl":3.0, "tp":5.0, "fn":lambda c: gen_macd(c)},
    {"name":"MACD",        "pair":"GBP/JPY","tf":"15m","sl":1.25,"tp":1.0, "fn":lambda c: gen_macd(c)},
    {"name":"RSI",         "pair":"USD/JPY","tf":"1h", "sl":1.0, "tp":1.5, "fn":lambda c: gen_rsi(c)},
    {"name":"RSI",         "pair":"USD/JPY","tf":"15m","sl":1.0, "tp":5.0, "fn":lambda c: gen_rsi(c)},
    {"name":"Donchian",    "pair":"USD/JPY","tf":"15m","sl":1.0, "tp":2.0, "fn":lambda c: gen_donchian(c)},
]

LOT = 1.0
random.seed(42)

candle_cache = {}
def get_candles(symbol, interval):
    key=f"{symbol}_{interval}"
    if key not in candle_cache:
        df=yf.Ticker(symbol).history(period="5d",interval=interval)
        candle_cache[key]=[{"time":int(ts.timestamp()),"open":float(row["Open"]),
            "high":float(row["High"]),"low":float(row["Low"]),"close":float(row["Close"])}
            for ts,row in df.iterrows()]
    return candle_cache[key]

print("="*80)
print("THIS WEEK BACKTEST — Oct 6-9, 2026 — All 10 v3 strategies @ 1.0 lot")
print("="*80)

all_trades = []
for s in STRATEGIES:
    pcfg = PAIRS[s["pair"]]
    candles = get_candles(pcfg["symbol"], s["tf"])
    print(f"\n  {s['name']:<12} {s['pair']:<10} {s['tf']:<4} | {len(candles)} candles", end="")
    if len(candles) < 30:
        print(" — SKIP (not enough data)")
        continue
    signals = s["fn"](candles)
    random.seed(42)
    trades = simulate(candles, signals, s["sl"], s["tp"], pcfg["spread"], pcfg["pip"], pcfg["pv"], LOT)
    wins = sum(1 for t in trades if t["won"])
    pnl = sum(t["pnl"] for t in trades)
    pips = sum(t["pips"] for t in trades)
    wr = wins/len(trades)*100 if trades else 0
    print(f" | {len(trades)} trades | {wins}W | WR {wr:.0f}% | {pips:+.1f}p | Rs {pnl:+,.0f}")
    for t in trades:
        t["strat"] = f"{s['name']} {s['pair']} {s['tf']}"
        all_trades.append(t)

# Summary
print(f"\n{'='*80}")
print("PORTFOLIO SUMMARY")
print(f"{'='*80}")
total = len(all_trades)
wins = sum(1 for t in all_trades if t["won"])
pnl = sum(t["pnl"] for t in all_trades)
pips = sum(t["pips"] for t in all_trades)
wr = wins/total*100 if total else 0
print(f"  Total trades: {total}")
print(f"  Wins: {wins} | Losses: {total-wins}")
print(f"  Win Rate: {wr:.1f}%")
print(f"  Total Pips: {pips:+.1f}")
print(f"  Total P&L: Rs {pnl:+,.0f}")
if total:
    print(f"  Avg P&L/trade: Rs {pnl/total:+,.0f}")

# Equity curve
eq = 0; peak = 0; mdd = 0
for t in sorted(all_trades, key=lambda x: x["day"]):
    eq += t["pnl"]; peak = max(peak, eq); mdd = max(mdd, peak - eq)
print(f"  Max Drawdown: Rs {mdd:,.0f}")

# Daily breakdown
print(f"\n  DAILY BREAKDOWN")
print(f"  {'Day':<12} {'Trades':>6} {'Wins':>5} {'WR':>6} {'Pips':>8} {'P&L':>12}")
print(f"  {'─'*52}")
daily = {}
for t in all_trades:
    d = t["day"]
    if d not in daily: daily[d] = {"trades":0,"wins":0,"pnl":0,"pips":0}
    daily[d]["trades"] += 1
    daily[d]["pnl"] += t["pnl"]
    daily[d]["pips"] += t["pips"]
    if t["won"]: daily[d]["wins"] += 1
for day in sorted(daily):
    d = daily[day]
    w = d["wins"]/d["trades"]*100 if d["trades"] else 0
    mark = "  +++" if d["pnl"]>5000 else "  +" if d["pnl"]>0 else "  ---" if d["pnl"]<-5000 else "  -" if d["pnl"]<0 else ""
    print(f"  {day:<12} {d['trades']:>6} {d['wins']:>5} {w:>5.0f}% {d['pips']:>+7.1f} Rs {d['pnl']:>+10,.0f}{mark}")

# Per strategy
print(f"\n  BY STRATEGY")
print(f"  {'Strategy':<30} {'Tr':>4} {'W':>3} {'WR':>5} {'Pips':>8} {'P&L':>12}")
print(f"  {'─'*65}")
strats = {}
for t in all_trades:
    k = t["strat"]
    if k not in strats: strats[k] = {"trades":0,"wins":0,"pnl":0,"pips":0}
    strats[k]["trades"] += 1
    strats[k]["pnl"] += t["pnl"]
    strats[k]["pips"] += t["pips"]
    if t["won"]: strats[k]["wins"] += 1
for k in sorted(strats, key=lambda x: strats[x]["pnl"], reverse=True):
    s = strats[k]
    w = s["wins"]/s["trades"]*100 if s["trades"] else 0
    print(f"  {k:<30} {s['trades']:>4} {s['wins']:>3} {w:>4.0f}% {s['pips']:>+7.1f} Rs {s['pnl']:>+10,.0f}")

# Individual trades
print(f"\n  ALL TRADES")
print(f"  {'Day':<12} {'Strategy':<28} {'Dir':<5} {'Reason':<4} {'Pips':>7} {'P&L':>10} {'Held':>4}")
print(f"  {'─'*75}")
for t in sorted(all_trades, key=lambda x: x["day"]):
    w = "W" if t["won"] else "L"
    print(f"  {t['day']:<12} {t['strat']:<28} {t['dir']:<5} {t['reason']:<4} {t['pips']:>+6.1f} Rs {t['pnl']:>+9,.0f} {t['held']:>4} [{w}]")

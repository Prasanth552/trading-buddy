"""Backtest S/R + LinReg on 5m, 15m, 1h candles with realistic CFD model."""
import random
from datetime import datetime
from zoneinfo import ZoneInfo
import yfinance as yf

UTC = ZoneInfo("UTC")
BEST_HOURS_UTC = {0,1,2,3,4,19,21,22,23}

PAIR_CONFIG = {
    "EUR/GBP": {"symbol":"EURGBP=X","spread_pips":1.5,"pip_size":0.0001,"pip_value_inr":1100,"lot_size":0.5},
    "CAD/CHF": {"symbol":"CADCHF=X","spread_pips":2.5,"pip_size":0.0001,"pip_value_inr":950,"lot_size":0.5},
}

def strategy_sr(candles, lookback=25, threshold_pct=0.0005):
    closes=[c["close"] for c in candles]; highs=[c["high"] for c in candles]; lows=[c["low"] for c in candles]
    signals=[]
    for i in range(lookback, len(candles)):
        res=max(highs[i-lookback:i]); sup=min(lows[i-lookback:i]); price=closes[i]; rng=res-sup
        if rng==0: continue
        if (price-sup)/rng < threshold_pct*10: signals.append((i,"CALL"))
        elif (res-price)/rng < threshold_pct*10: signals.append((i,"PUT"))
    return signals

def strategy_linreg(candles, period=20, std_mult=2.0):
    closes=[c["close"] for c in candles]; signals=[]
    for i in range(period, len(candles)):
        w=closes[i-period:i]; n=len(w)
        sx=n*(n-1)/2; sy=sum(w); sxx=sum(j*j for j in range(n)); sxy=sum(j*v for j,v in enumerate(w))
        d=n*sxx-sx*sx
        if d==0: continue
        slope=(n*sxy-sx*sy)/d; intercept=(sy-slope*sx)/n
        pred=intercept+slope*(n-1)
        residuals=[w[j]-(intercept+slope*j) for j in range(n)]
        std=(sum(r**2 for r in residuals)/n)**0.5
        if std==0: continue
        price=closes[i]
        if price < pred - std_mult*std: signals.append((i,"CALL"))
        elif price > pred + std_mult*std: signals.append((i,"PUT"))
    return signals

def fetch_candles(symbol, interval, period):
    df = yf.Ticker(symbol).history(period=period, interval=interval)
    if df.empty: return []
    candles = []
    for ts, row in df.iterrows():
        candles.append({"time":int(ts.timestamp()),"open":float(row["Open"]),
            "high":float(row["High"]),"low":float(row["Low"]),"close":float(row["Close"])})
    return candles

def run_realistic(candles, pair_name, with_hour_filter):
    cfg=PAIR_CONFIG[pair_name]; pip_size=cfg["pip_size"]; spread_pips=cfg["spread_pips"]
    pip_value=cfg["pip_value_inr"]; lot_size=cfg["lot_size"]
    trades=[]; total_pips=0
    for name, fn in [("S/R",strategy_sr),("LinReg",strategy_linreg)]:
        signals=fn(candles)
        for idx, direction in signals:
            if idx >= len(candles)-1: continue
            dt_utc=datetime.fromtimestamp(candles[idx]["time"], UTC)
            if with_hour_filter and dt_utc.hour not in BEST_HOURS_UTC: continue
            entry_c=candles[idx+1]; raw_entry=entry_c["open"]; raw_exit=entry_c["close"]
            slippage=random.uniform(0,0.5); sp_price=spread_pips*pip_size; sl_price=slippage*pip_size
            if direction=="CALL":
                entry=raw_entry+(sp_price/2)+sl_price; exit_p=raw_exit-(sp_price/2)
                pip_move=(exit_p-entry)/pip_size
            else:
                entry=raw_entry-(sp_price/2)-sl_price; exit_p=raw_exit+(sp_price/2)
                pip_move=(entry-exit_p)/pip_size
            pnl=pip_move*pip_value*lot_size; total_pips+=pip_move
            trades.append({"strategy":name,"direction":direction,"pips":round(pip_move,1),
                "won":pnl>0,"pnl":round(pnl,0),"day":dt_utc.strftime("%Y-%m-%d")})
    wins=sum(1 for t in trades if t["won"]); total=len(trades)
    wr=wins/total*100 if total else 0; total_pnl=sum(t["pnl"] for t in trades)
    spread_cost=total*spread_pips*pip_value*lot_size
    # daily
    daily={}
    for t in trades:
        d=t["day"]
        if d not in daily: daily[d]={"pnl":0,"trades":0,"wins":0,"pips":0}
        daily[d]["pnl"]+=t["pnl"]; daily[d]["trades"]+=1; daily[d]["pips"]+=t["pips"]
        if t["won"]: daily[d]["wins"]+=1
    # avg win/loss
    win_pnls=[t["pnl"] for t in trades if t["won"]]
    loss_pnls=[t["pnl"] for t in trades if not t["won"]]
    avg_win=sum(win_pnls)/len(win_pnls) if win_pnls else 0
    avg_loss=sum(loss_pnls)/len(loss_pnls) if loss_pnls else 0
    return {"total":total,"wins":wins,"losses":total-wins,"wr":wr,"pnl":total_pnl,
        "pips":round(total_pips,1),"spread_cost":round(spread_cost,0),"daily":daily,
        "avg_win":round(avg_win,0),"avg_loss":round(avg_loss,0),"trades":trades}

TIMEFRAMES = [
    ("5m", "5m", "7d"),
    ("15m", "15m", "1mo"),
    ("1h", "1h", "1mo"),
]

print("=" * 70)
print("MULTI-TIMEFRAME REALISTIC BACKTEST")
print("Entry: next candle OPEN + spread + slippage | Exit: that candle CLOSE")
print("=" * 70)

for tf_name, interval, period in TIMEFRAMES:
    print(f"\n{'#' * 70}")
    print(f"  TIMEFRAME: {tf_name} candles (period={period})")
    print(f"{'#' * 70}")
    
    for pair_name, cfg in PAIR_CONFIG.items():
        random.seed(42)
        candles = fetch_candles(cfg["symbol"], interval, period)
        if len(candles) < 30:
            print(f"  {pair_name}: not enough candles ({len(candles)})"); continue
        print(f"\n  {pair_name} | {len(candles)} candles | spread {cfg['spread_pips']} pips")
        
        # With hour filter
        r = run_realistic(candles, pair_name, True)
        print(f"    WITH hour filter:  {r['total']:>4} trades | W {r['wins']:>3} L {r['losses']:>3} | WR {r['wr']:>5.1f}% | P&L ₹{r['pnl']:>10,.0f} | Pips {r['pips']:>7.1f} | Spread ₹{r['spread_cost']:>8,.0f}")
        print(f"                       Avg win ₹{r['avg_win']:>6,.0f} | Avg loss ₹{r['avg_loss']:>6,.0f} | Reward:Risk {abs(r['avg_win']/r['avg_loss']) if r['avg_loss'] else 0:.2f}")
        
        # Without hour filter (all hours)
        random.seed(42)
        r2 = run_realistic(candles, pair_name, False)
        print(f"    NO hour filter:   {r2['total']:>4} trades | W {r2['wins']:>3} L {r2['losses']:>3} | WR {r2['wr']:>5.1f}% | P&L ₹{r2['pnl']:>10,.0f} | Pips {r2['pips']:>7.1f}")
        
        # Daily for the better one
        best = r if r["pnl"] > r2["pnl"] else r2
        tag = "(hour filter)" if r["pnl"] > r2["pnl"] else "(all hours)"
        print(f"\n    Daily {tag}:")
        print(f"    {'Day':<12} {'Trades':>6} {'Wins':>5} {'WR':>6} {'Pips':>7} {'P&L':>10}")
        for day in sorted(best["daily"].keys()):
            d=best["daily"][day]; wr=d["wins"]/d["trades"]*100 if d["trades"] else 0
            m=" +" if d["pnl"]>0 else " -" if d["pnl"]<0 else ""
            print(f"    {day:<12} {d['trades']:>6} {d['wins']:>5} {wr:>5.1f}% {d['pips']:>7.1f} ₹{d['pnl']:>9,.0f}{m}")

# Summary table
print(f"\n{'=' * 70}")
print("SUMMARY: Which timeframe + pair is profitable?")
print(f"{'=' * 70}")
print(f"{'TF':<5} {'Pair':<10} {'Filter':<8} {'Trades':>6} {'WR':>6} {'P&L':>12} {'Pips':>8} {'Avg W':>8} {'Avg L':>8} {'R:R':>5}")
print("-"*75)
for tf_name, interval, period in TIMEFRAMES:
    for pair_name, cfg in PAIR_CONFIG.items():
        random.seed(42)
        candles=fetch_candles(cfg["symbol"],interval,period)
        if len(candles)<30: continue
        for filt, fname in [(True,"hours"),(False,"all")]:
            random.seed(42)
            r=run_realistic(candles,pair_name,filt)
            if r["total"]==0: continue
            rr=abs(r["avg_win"]/r["avg_loss"]) if r["avg_loss"] else 0
            marker="<<<" if r["pnl"]>0 else ""
            print(f"{tf_name:<5} {pair_name:<10} {fname:<8} {r['total']:>6} {r['wr']:>5.1f}% ₹{r['pnl']:>11,.0f} {r['pips']:>7.1f} ₹{r['avg_win']:>7,.0f} ₹{r['avg_loss']:>7,.0f} {rr:>5.2f} {marker}")

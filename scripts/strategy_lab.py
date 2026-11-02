"""Strategy Lab — 8 proven strategies, realistic CFD backtest on September data.

Strategies implemented:
1. EMA Crossover (20/50) — trend following
2. EMA Crossover (9/21) — faster trend following 
3. Donchian Breakout (20) — channel breakout
4. RSI Oversold/Overbought (14, 30/70) — mean reversion with RSI
5. RSI + EMA Filter — RSI signals only in trend direction
6. Bollinger Band Mean Reversion — fade BB extremes
7. MACD Crossover — momentum
8. Triple EMA (8/13/21) — short-term trend

All use:
  - Entry at NEXT candle OPEN + spread + slippage
  - ATR-based stop loss and take profit (hold multiple candles)
  - Realistic spread and slippage
"""
import random, math
from datetime import datetime
from zoneinfo import ZoneInfo
import yfinance as yf

UTC = ZoneInfo("UTC")
IST = ZoneInfo("Asia/Kolkata")

PAIR_CONFIG = {
    "EUR/USD": {"symbol":"EURUSD=X","spread_pips":0.8,"pip_size":0.0001,"pip_value_inr":830,"lot_size":0.5},
    "GBP/JPY": {"symbol":"GBPJPY=X","spread_pips":1.5,"pip_size":0.01,"pip_value_inr":55,"lot_size":1.0},
    "GBP/USD": {"symbol":"GBPUSD=X","spread_pips":1.0,"pip_size":0.0001,"pip_value_inr":830,"lot_size":0.5},
    "USD/JPY": {"symbol":"USDJPY=X","spread_pips":0.8,"pip_size":0.01,"pip_value_inr":55,"lot_size":1.0},
    "EUR/GBP": {"symbol":"EURGBP=X","spread_pips":1.5,"pip_size":0.0001,"pip_value_inr":1100,"lot_size":0.5},
    "CAD/CHF": {"symbol":"CADCHF=X","spread_pips":2.5,"pip_size":0.0001,"pip_value_inr":950,"lot_size":0.5},
}

# ── Indicators ──────────────────────────────────────────────────────────────

def ema(values, period):
    result = [None]*len(values)
    if len(values) < period: return result
    k = 2/(period+1)
    result[period-1] = sum(values[:period])/period
    for i in range(period, len(values)):
        result[i] = values[i]*k + result[i-1]*(1-k)
    return result

def sma(values, period):
    result = [None]*len(values)
    for i in range(period-1, len(values)):
        result[i] = sum(values[i-period+1:i+1])/period
    return result

def rsi(closes, period=14):
    result = [None]*len(closes)
    if len(closes) < period+1: return result
    gains, losses = [], []
    for i in range(1, period+1):
        d = closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    avg_gain = sum(gains)/period; avg_loss = sum(losses)/period
    if avg_loss == 0: result[period] = 100.0
    else: result[period] = 100 - 100/(1+avg_gain/avg_loss)
    for i in range(period+1, len(closes)):
        d = closes[i]-closes[i-1]
        avg_gain = (avg_gain*(period-1)+max(d,0))/period
        avg_loss = (avg_loss*(period-1)+max(-d,0))/period
        if avg_loss == 0: result[i] = 100.0
        else: result[i] = 100 - 100/(1+avg_gain/avg_loss)
    return result

def atr(candles, period=14):
    result = [None]*len(candles)
    if len(candles) < period+1: return result
    trs = []
    for i in range(1, len(candles)):
        h,l,pc = candles[i]["high"],candles[i]["low"],candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    # trs[0] corresponds to candles[1]
    if len(trs) < period: return result
    result[period] = sum(trs[:period])/period
    for i in range(period, len(trs)):
        result[i+1] = (result[i]*(period-1)+trs[i])/period
    return result

def bollinger(closes, period=20, mult=2.0):
    upper, lower, mid = [None]*len(closes), [None]*len(closes), [None]*len(closes)
    for i in range(period-1, len(closes)):
        w = closes[i-period+1:i+1]
        m = sum(w)/period
        std = (sum((x-m)**2 for x in w)/period)**0.5
        mid[i]=m; upper[i]=m+mult*std; lower[i]=m-mult*std
    return upper, lower, mid

def macd(closes, fast=12, slow=26, signal=9):
    ema_fast = ema(closes, fast); ema_slow = ema(closes, slow)
    macd_line = [None]*len(closes)
    for i in range(len(closes)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i]-ema_slow[i]
    valid = [v for v in macd_line if v is not None]
    sig = ema(valid, signal)
    # map signal back
    signal_line = [None]*len(closes)
    j=0
    for i in range(len(closes)):
        if macd_line[i] is not None:
            signal_line[i] = sig[j] if j < len(sig) else None
            j+=1
    return macd_line, signal_line

def donchian(candles, period=20):
    upper, lower = [None]*len(candles), [None]*len(candles)
    for i in range(period, len(candles)):
        upper[i] = max(c["high"] for c in candles[i-period:i])
        lower[i] = min(c["low"] for c in candles[i-period:i])
    return upper, lower

# ── Strategy Signal Generators ──────────────────────────────────────────────
# Each returns list of (candle_index, direction, sl_atr_mult, tp_atr_mult)

def strat_ema_cross(candles, fast_p, slow_p, sl_mult=1.5, tp_mult=2.0):
    closes = [c["close"] for c in candles]
    fast = ema(closes, fast_p); slow = ema(closes, slow_p)
    signals = []
    for i in range(1, len(candles)):
        if fast[i] is None or fast[i-1] is None or slow[i] is None or slow[i-1] is None: continue
        if fast[i-1] <= slow[i-1] and fast[i] > slow[i]:
            signals.append((i, "CALL", sl_mult, tp_mult))
        elif fast[i-1] >= slow[i-1] and fast[i] < slow[i]:
            signals.append((i, "PUT", sl_mult, tp_mult))
    return signals

def strat_donchian(candles, period=20, sl_mult=1.0, tp_mult=2.0):
    upper, lower = donchian(candles, period)
    signals = []
    for i in range(1, len(candles)):
        if upper[i] is None: continue
        if candles[i]["close"] > upper[i]:
            signals.append((i, "CALL", sl_mult, tp_mult))
        elif candles[i]["close"] < lower[i]:
            signals.append((i, "PUT", sl_mult, tp_mult))
    return signals

def strat_rsi(candles, period=14, oversold=30, overbought=70, sl_mult=1.5, tp_mult=2.0):
    closes = [c["close"] for c in candles]
    r = rsi(closes, period)
    signals = []
    for i in range(1, len(candles)):
        if r[i] is None or r[i-1] is None: continue
        if r[i-1] < oversold and r[i] >= oversold:
            signals.append((i, "CALL", sl_mult, tp_mult))
        elif r[i-1] > overbought and r[i] <= overbought:
            signals.append((i, "PUT", sl_mult, tp_mult))
    return signals

def strat_rsi_ema(candles, rsi_period=14, ema_period=50, oversold=30, overbought=70, sl_mult=1.5, tp_mult=2.0):
    closes = [c["close"] for c in candles]
    r = rsi(closes, rsi_period)
    e = ema(closes, ema_period)
    signals = []
    for i in range(1, len(candles)):
        if r[i] is None or r[i-1] is None or e[i] is None: continue
        if r[i-1] < oversold and r[i] >= oversold and closes[i] > e[i]:
            signals.append((i, "CALL", sl_mult, tp_mult))
        elif r[i-1] > overbought and r[i] <= overbought and closes[i] < e[i]:
            signals.append((i, "PUT", sl_mult, tp_mult))
    return signals

def strat_bollinger(candles, period=20, mult=2.0, sl_mult=1.5, tp_mult=1.5):
    closes = [c["close"] for c in candles]
    upper, lower, mid = bollinger(closes, period, mult)
    signals = []
    for i in range(1, len(candles)):
        if upper[i] is None: continue
        if lower[i] is not None and lower[i-1] is not None and closes[i-1] > lower[i-1] and closes[i] <= lower[i]:
            signals.append((i, "CALL", sl_mult, tp_mult))
        elif upper[i] is not None and upper[i-1] is not None and closes[i-1] < upper[i-1] and closes[i] >= upper[i]:
            signals.append((i, "PUT", sl_mult, tp_mult))
    return signals

def strat_macd_cross(candles, fast=12, slow=26, sig=9, sl_mult=1.5, tp_mult=2.0):
    closes = [c["close"] for c in candles]
    ml, sl = macd(closes, fast, slow, sig)
    signals = []
    for i in range(1, len(candles)):
        if ml[i] is None or ml[i-1] is None or sl[i] is None or sl[i-1] is None: continue
        if ml[i-1] <= sl[i-1] and ml[i] > sl[i]:
            signals.append((i, "CALL", sl_mult, tp_mult))
        elif ml[i-1] >= sl[i-1] and ml[i] < sl[i]:
            signals.append((i, "PUT", sl_mult, tp_mult))
    return signals

def strat_triple_ema(candles, p1=8, p2=13, p3=21, sl_mult=1.0, tp_mult=1.5):
    closes = [c["close"] for c in candles]
    e1=ema(closes,p1); e2=ema(closes,p2); e3=ema(closes,p3)
    signals = []
    for i in range(1, len(candles)):
        if e1[i] is None or e2[i] is None or e3[i] is None: continue
        if e1[i-1] is None or e2[i-1] is None or e3[i-1] is None: continue
        prev_aligned_up = e1[i-1]>e2[i-1] and e2[i-1]>e3[i-1]
        curr_aligned_up = e1[i]>e2[i] and e2[i]>e3[i]
        prev_aligned_dn = e1[i-1]<e2[i-1] and e2[i-1]<e3[i-1]
        curr_aligned_dn = e1[i]<e2[i] and e2[i]<e3[i]
        if not prev_aligned_up and curr_aligned_up:
            signals.append((i, "CALL", sl_mult, tp_mult))
        elif not prev_aligned_dn and curr_aligned_dn:
            signals.append((i, "PUT", sl_mult, tp_mult))
    return signals

# ── Realistic Trade Simulator (with SL/TP, multi-candle hold) ───────────────

def simulate_trades(candles, signals, pair_name, max_hold=50):
    cfg = PAIR_CONFIG[pair_name]
    pip_size=cfg["pip_size"]; spread_pips=cfg["spread_pips"]
    pip_value=cfg["pip_value_inr"]; lot_size=cfg["lot_size"]
    atr_vals = atr(candles, 14)
    
    trades = []
    i = 0
    while i < len(signals):
        idx, direction, sl_mult, tp_mult = signals[i]
        # Enter at next candle's open
        if idx+1 >= len(candles): i+=1; continue
        if atr_vals[idx] is None: i+=1; continue
        
        current_atr = atr_vals[idx]
        slippage = random.uniform(0, 0.5) * pip_size
        spread_half = (spread_pips * pip_size) / 2
        
        entry_idx = idx + 1
        raw_entry = candles[entry_idx]["open"]
        
        if direction == "CALL":
            entry_price = raw_entry + spread_half + slippage
            sl_price = entry_price - sl_mult * current_atr
            tp_price = entry_price + tp_mult * current_atr
        else:
            entry_price = raw_entry - spread_half - slippage
            sl_price = entry_price + sl_mult * current_atr
            tp_price = entry_price - tp_mult * current_atr
        
        # Walk forward candle by candle
        exit_price = None
        exit_reason = "timeout"
        for j in range(entry_idx, min(entry_idx + max_hold, len(candles))):
            c = candles[j]
            if direction == "CALL":
                if c["low"] <= sl_price:
                    exit_price = sl_price - spread_half  # exit at bid
                    exit_reason = "SL"; break
                if c["high"] >= tp_price:
                    exit_price = tp_price - spread_half
                    exit_reason = "TP"; break
            else:
                if c["high"] >= sl_price:
                    exit_price = sl_price + spread_half
                    exit_reason = "SL"; break
                if c["low"] <= tp_price:
                    exit_price = tp_price + spread_half
                    exit_reason = "TP"; break
        
        if exit_price is None:
            # Timeout: exit at last candle close
            last_j = min(entry_idx + max_hold - 1, len(candles)-1)
            if direction == "CALL":
                exit_price = candles[last_j]["close"] - spread_half
            else:
                exit_price = candles[last_j]["close"] + spread_half
        
        if direction == "CALL":
            pip_move = (exit_price - entry_price) / pip_size
        else:
            pip_move = (entry_price - exit_price) / pip_size
        
        pnl = pip_move * pip_value * lot_size
        dt_utc = datetime.fromtimestamp(candles[idx]["time"], UTC)
        
        trades.append({
            "direction": direction,
            "pips": round(pip_move, 1),
            "pnl": round(pnl, 0),
            "won": pnl > 0,
            "exit_reason": exit_reason,
            "day": dt_utc.strftime("%Y-%m-%d"),
            "atr": round(current_atr/pip_size, 1),
        })
        
        # Skip signals during this trade's hold period
        hold_end = entry_idx + max_hold
        i += 1
        while i < len(signals) and signals[i][0] < hold_end:
            i += 1
    
    return trades

def summarize(trades, name):
    if not trades:
        return {"name":name,"total":0,"wins":0,"losses":0,"wr":0,"pnl":0,
                "avg_win":0,"avg_loss":0,"pf":0,"max_dd":0,"tp_pct":0,"sl_pct":0}
    wins = sum(1 for t in trades if t["won"])
    losses = len(trades)-wins
    wr = wins/len(trades)*100
    total_pnl = sum(t["pnl"] for t in trades)
    win_pnls = [t["pnl"] for t in trades if t["won"]]
    loss_pnls = [t["pnl"] for t in trades if not t["won"]]
    avg_win = sum(win_pnls)/len(win_pnls) if win_pnls else 0
    avg_loss = abs(sum(loss_pnls)/len(loss_pnls)) if loss_pnls else 0
    gross_win = sum(win_pnls)
    gross_loss = abs(sum(loss_pnls))
    pf = gross_win/gross_loss if gross_loss else 999
    
    # Max drawdown
    equity = 0; peak = 0; max_dd = 0
    for t in trades:
        equity += t["pnl"]; peak = max(peak, equity)
        max_dd = max(max_dd, peak-equity)
    
    tp_count = sum(1 for t in trades if t["exit_reason"]=="TP")
    sl_count = sum(1 for t in trades if t["exit_reason"]=="SL")
    
    return {"name":name,"total":len(trades),"wins":wins,"losses":losses,"wr":round(wr,1),
            "pnl":round(total_pnl,0),"avg_win":round(avg_win,0),"avg_loss":round(avg_loss,0),
            "pf":round(pf,2),"max_dd":round(max_dd,0),"tp_pct":round(tp_count/len(trades)*100,0),
            "sl_pct":round(sl_count/len(trades)*100,0)}

# ── All Strategy Configs ────────────────────────────────────────────────────

STRATEGIES = [
    ("EMA 20/50",     lambda c: strat_ema_cross(c, 20, 50, 1.5, 2.0)),
    ("EMA 9/21",      lambda c: strat_ema_cross(c, 9, 21, 1.0, 1.5)),
    ("Donchian 20",   lambda c: strat_donchian(c, 20, 1.0, 2.0)),
    ("RSI 14",        lambda c: strat_rsi(c, 14, 30, 70, 1.5, 2.0)),
    ("RSI+EMA",       lambda c: strat_rsi_ema(c, 14, 50, 30, 70, 1.5, 2.0)),
    ("Bollinger",     lambda c: strat_bollinger(c, 20, 2.0, 1.5, 1.5)),
    ("MACD",          lambda c: strat_macd_cross(c, 12, 26, 9, 1.5, 2.0)),
    ("Triple EMA",    lambda c: strat_triple_ema(c, 8, 13, 21, 1.0, 1.5)),
]

TIMEFRAMES = [
    ("15m", "15m", "1mo"),
    ("1h",  "1h",  "1mo"),
]

def main():
    random.seed(42)
    print("=" * 80)
    print("STRATEGY LAB — 8 Proven Strategies, Realistic CFD Backtest")
    print("Entry: next candle OPEN + spread + slippage")
    print("Exit: ATR-based SL/TP (multi-candle hold) or 50-candle timeout")
    print("=" * 80)
    
    results = []
    
    for tf_name, interval, period in TIMEFRAMES:
        print(f"\n{'#'*80}")
        print(f"  TIMEFRAME: {tf_name}")
        print(f"{'#'*80}")
        
        for pair_name, cfg in PAIR_CONFIG.items():
            symbol = cfg["symbol"]
            df = yf.Ticker(symbol).history(period=period, interval=interval)
            if df.empty:
                print(f"  {pair_name}: no data"); continue
            candles = [{"time":int(ts.timestamp()),"open":float(row["Open"]),
                "high":float(row["High"]),"low":float(row["Low"]),"close":float(row["Close"])}
                for ts, row in df.iterrows()]
            
            # Filter to September only
            sep_candles = [c for c in candles 
                if datetime.fromtimestamp(c["time"],UTC).month == 9]
            
            if len(sep_candles) < 50:
                print(f"  {pair_name} {tf_name}: only {len(sep_candles)} Sep candles, skipping")
                continue
            
            print(f"\n  {pair_name} | {tf_name} | {len(sep_candles)} Sep candles | spread {cfg['spread_pips']}pip")
            print(f"  {'Strategy':<16} {'Trades':>6} {'Wins':>5} {'WR':>6} {'P&L':>12} {'AvgW':>8} {'AvgL':>8} {'PF':>6} {'MaxDD':>10} {'TP%':>5} {'SL%':>5}")
            print(f"  {'-'*94}")
            
            for sname, sfn in STRATEGIES:
                random.seed(42)
                signals = sfn(sep_candles)
                trades = simulate_trades(sep_candles, signals, pair_name)
                s = summarize(trades, sname)
                marker = " <<<" if s["pnl"] > 0 else ""
                print(f"  {sname:<16} {s['total']:>6} {s['wins']:>5} {s['wr']:>5.1f}% ₹{s['pnl']:>11,.0f} ₹{s['avg_win']:>7,.0f} ₹{s['avg_loss']:>7,.0f} {s['pf']:>5.2f} ₹{s['max_dd']:>9,.0f} {s['tp_pct']:>4.0f}% {s['sl_pct']:>4.0f}%{marker}")
                results.append({**s, "pair":pair_name, "tf":tf_name})
    
    # Final leaderboard
    print(f"\n{'='*80}")
    print("LEADERBOARD — Sorted by P&L (profitable strategies marked <<<)")
    print(f"{'='*80}")
    results.sort(key=lambda x: x["pnl"], reverse=True)
    print(f"{'Rank':<5} {'Strategy':<16} {'Pair':<10} {'TF':<5} {'Trades':>6} {'WR':>6} {'PF':>6} {'P&L':>12} {'MaxDD':>10}")
    print("-"*76)
    for rank, r in enumerate(results[:20], 1):
        marker = " <<<" if r["pnl"] > 0 else ""
        print(f"{rank:<5} {r['name']:<16} {r['pair']:<10} {r['tf']:<5} {r['total']:>6} {r['wr']:>5.1f}% {r['pf']:>5.2f} ₹{r['pnl']:>11,.0f} ₹{r['max_dd']:>9,.0f}{marker}")

if __name__ == "__main__":
    main()

"""Profit optimizer — confluence, staking, parallel markets."""
from __future__ import annotations
import asyncio, json, time
from datetime import datetime
from zoneinfo import ZoneInfo
import websockets

UTC = ZoneInfo("UTC")
WS = "wss://api.derivws.com/trading/v1/options/ws/public"
HDRS = {"User-Agent": "Mozilla/5.0", "Origin": "https://app.deriv.com"}

# ─── Indicators ───
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
        if ml[i] is not None:
            se=(ml[i]-se)*m2+se; sl[i]=se; hist[i]=ml[i]-se
    return ml,sl,hist

# ─── Strategy signal generators (return dict: idx → direction) ───
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

# ─── Confluence: require N strategies to agree ───
def confluence_signals(strat_signals_list, min_agree=2):
    all_idx = set()
    for ss in strat_signals_list:
        all_idx |= set(ss.keys())
    result = {}
    for idx in sorted(all_idx):
        votes = {}
        for ss in strat_signals_list:
            if idx in ss:
                d = ss[idx]
                votes[d] = votes.get(d, 0) + 1
        for d, cnt in votes.items():
            if cnt >= min_agree:
                result[idx] = d
                break
    return result

# ─── Backtest with staking modes ───
def backtest(candles, signals_dict, expiry_bars, base_stake=10.0, payout=0.80,
             cooldown=None, staking="flat", max_streak_mult=4):
    if cooldown is None: cooldown = expiry_bars
    w=l=d=0; pnl=0.0; peak=0.0; mdd=0.0; last=-cooldown
    streak=0; balance=1000.0; trades=[]

    for idx in sorted(signals_dict.keys()):
        if idx+expiry_bars >= len(candles): continue
        if idx - last < cooldown: continue
        dir = signals_dict[idx]
        entry = candles[idx]["close"]; exp = candles[idx+expiry_bars]["close"]
        last = idx
        t = datetime.fromtimestamp(candles[idx]["time"], UTC)

        # Determine stake
        if staking == "flat":
            stake = base_stake
        elif staking == "anti_martingale":
            mult = min(1 + streak * 0.5, max_streak_mult)
            stake = base_stake * mult
        elif staking == "martingale_light":
            if streak < 0:
                stake = base_stake * min(2 ** abs(streak), max_streak_mult)
            else:
                stake = base_stake
        elif staking == "compound":
            stake = max(base_stake, balance * 0.02)  # 2% of balance
        elif staking == "kelly":
            # Simplified Kelly: use running WR
            if w+l >= 10:
                wr_est = w/(w+l)
                kelly_f = (wr_est * (1+payout) - 1) / payout
                kelly_f = max(0.01, min(kelly_f, 0.10))  # cap 1-10%
                stake = balance * kelly_f
            else:
                stake = base_stake
        else:
            stake = base_stake

        stake = min(stake, balance * 0.15)  # never risk >15% of balance
        if stake < 1: stake = 1

        if entry == exp:
            d += 1; res = "DRAW"
        elif (dir=="CALL" and exp>entry) or (dir=="PUT" and exp<entry):
            pnl += stake * payout; balance += stake * payout
            w += 1; res = "WIN"
            streak = max(0, streak) + 1
        else:
            pnl -= stake; balance -= stake
            l += 1; res = "LOSS"
            streak = min(0, streak) - 1

        peak = max(peak, pnl); mdd = max(mdd, peak - pnl)
        trades.append({"time": t.strftime("%H:%M"), "dir": dir, "stake": f"${stake:.1f}",
                        "entry": f"{entry:.5f}", "expiry": f"{exp:.5f}", "result": res,
                        "pnl": f"${pnl:.1f}", "bal": f"${balance:.1f}"})

        if balance <= 0: break

    tot = w+l+d; wr = w/(w+l)*100 if (w+l)>0 else 0
    return {"trades":tot,"wins":w,"losses":l,"draws":d,"wr":wr,"pnl":pnl,
            "max_dd":mdd,"final_bal":balance,"details":trades}

async def fetch(ws, symbol, gran, count=5000):
    rid = int(time.time()*1000) % 100000
    await ws.send(json.dumps({"ticks_history":symbol,"adjust_start_time":1,
                               "count":min(count,5000),"end":"latest",
                               "granularity":gran,"style":"candles","req_id":rid}))
    resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
    return [{"time":c["epoch"],"open":float(c["open"]),"high":float(c["high"]),
             "low":float(c["low"]),"close":float(c["close"])} for c in resp.get("candles",[])]

async def main():
    print("Connecting to Deriv...")
    ws = await asyncio.wait_for(websockets.connect(WS, additional_headers=HDRS, ping_interval=25), timeout=15)
    print("Connected!\n")
    PAYOUT = 0.80; BE = 100/(1+PAYOUT)

    today_start = int(datetime(2026,10,8,0,0,0,tzinfo=UTC).timestamp())
    now = datetime.now(UTC)
    print(f"UTC: {now:%Y-%m-%d %H:%M} | Breakeven: {BE:.1f}%\n")

    # ─── Fetch data ───
    markets = {
        "Vol10_1m":  ("1HZ10V", 60),
        "Vol75_1m":  ("1HZ75V", 60),
        "Vol100_1m": ("R_100", 60),
        "Vol10_5m":  ("1HZ10V", 300),
        "Vol75_5m":  ("1HZ75V", 300),
        "EURGBP_5m": ("frxEURGBP", 300),
        "EURGBP_15m":("frxEURGBP", 900),
    }
    data = {}
    for name, (sym, gran) in markets.items():
        print(f"Fetching {name}...")
        data[name] = await fetch(ws, sym, gran, 5000)
        c = data[name]
        if c:
            today_n = sum(1 for x in c if x["time"] >= today_start)
            print(f"  {len(c)} candles ({today_n} today)")

    print(f"\n{'='*110}")
    print("  PART 1: CONFLUENCE TESTING — How many strategies must agree?")
    print(f"{'='*110}\n")

    # Test confluence on each market
    market_configs = [
        ("Vol10_1m", 3, None),   # 1m candles, 3-bar expiry = 3min
        ("Vol75_1m", 3, None),
        ("Vol100_1m", 1, None),
        ("Vol10_1m", 5, None),
        ("Vol75_1m", 5, None),
        ("Vol75_5m", 1, None),
        ("EURGBP_5m", 3, {7,8,9,10,11,12,13,14,15}),
        ("EURGBP_15m", 1, {7,8,9,10,11,12,13,14,15}),
    ]

    # Strategy pool per market
    def get_strat_signals(candles, mkt):
        strats = {}
        strats["EMA(3,10)"] = sig_ema(candles, 3, 10)
        strats["EMA(5,20)"] = sig_ema(candles, 5, 20)
        strats["EMA(8,21)"] = sig_ema(candles, 8, 21)
        strats["MACD(12,26,9)"] = sig_macd(candles, 12, 26, 9)
        strats["MACD(5,13,4)"] = sig_macd(candles, 5, 13, 4)
        strats["RSI(14,30,70)"] = sig_rsi(candles, 14, 30, 70)
        strats["RSI(7,20,80)"] = sig_rsi(candles, 7, 20, 80)
        strats["RSI(14,25,75)"] = sig_rsi(candles, 14, 25, 75)
        strats["BB(20,2.0)"] = sig_bb(candles, 20, 2.0)
        strats["BB(14,1.5)"] = sig_bb(candles, 14, 1.5)
        strats["S/R(25,0.005)"] = sig_sr(candles, 25, 0.005)
        strats["S/R(40,0.005)"] = sig_sr(candles, 40, 0.005)
        strats["LR(20,2.0)"] = sig_linreg(candles, 20, 2.0)
        return strats

    confluence_results = []
    for mkt, eb, hrs in market_configs:
        candles = data[mkt]
        if len(candles) < 50: continue
        strats = get_strat_signals(candles, mkt)
        strat_list = list(strats.values())

        for min_agree in [1, 2, 3, 4, 5]:
            csigs = confluence_signals(strat_list, min_agree)
            if hrs:
                csigs = {k:v for k,v in csigs.items()
                         if datetime.fromtimestamp(candles[k]["time"],UTC).hour in hrs}
            # Filter today only
            csigs_today = {k:v for k,v in csigs.items() if candles[k]["time"] >= today_start}
            if not csigs_today: continue

            r = backtest(candles, csigs, eb, base_stake=10, payout=PAYOUT)
            # Also get today-only result
            rt = backtest(candles, csigs_today, eb, base_stake=10, payout=PAYOUT)
            if rt["trades"] > 0:
                label = f"{mkt} eb={eb} agree≥{min_agree}"
                confluence_results.append({"label": label, "min_agree": min_agree, **rt})

    confluence_results.sort(key=lambda x: (-x["wr"], -x["pnl"]))
    print(f"{'Config':<35s} {'Tr':>4s} {'W':>4s} {'L':>4s} {'WR':>6s} {'P&L':>10s} {'MaxDD':>8s}")
    print("-"*80)
    for r in confluence_results:
        v = "✅" if r["wr"] > BE else "  "
        print(f"{v} {r['label']:<33s} {r['trades']:>4d} {r['wins']:>4d} {r['losses']:>4d} "
              f"{r['wr']:>5.1f}% ${r['pnl']:>8.1f} ${r['max_dd']:>7.1f}")

    print(f"\n{'='*110}")
    print("  PART 2: STAKING STRATEGIES — Which staking method maximizes profit?")
    print(f"{'='*110}\n")

    # Use the best confluence combos and test different staking
    best_combos = [
        ("Vol10_1m", 3, None, 2, "Vol10 1m→3m conf≥2"),
        ("Vol75_1m", 3, None, 2, "Vol75 1m→3m conf≥2"),
        ("Vol75_1m", 1, None, 2, "Vol75 1m→1m conf≥2"),
        ("Vol100_1m", 1, None, 2, "Vol100 1m→1m conf≥2"),
        ("Vol10_1m", 3, None, 3, "Vol10 1m→3m conf≥3"),
        ("Vol75_1m", 3, None, 3, "Vol75 1m→3m conf≥3"),
    ]

    staking_modes = [
        ("flat $10", "flat", 10),
        ("flat $25", "flat", 25),
        ("flat $50", "flat", 50),
        ("anti-mart $10", "anti_martingale", 10),
        ("anti-mart $25", "anti_martingale", 25),
        ("mart-light $10", "martingale_light", 10),
        ("compound 2%", "compound", 10),
        ("kelly", "kelly", 10),
    ]

    print(f"{'Combo':<25s} {'Staking':<18s} {'Tr':>4s} {'WR':>6s} {'P&L':>10s} {'FinalBal':>10s} {'MaxDD':>8s}")
    print("-"*95)

    staking_results = []
    for mkt, eb, hrs, min_ag, label in best_combos:
        candles = data[mkt]
        if len(candles) < 50: continue
        strats = get_strat_signals(candles, mkt)
        csigs = confluence_signals(list(strats.values()), min_ag)
        if hrs:
            csigs = {k:v for k,v in csigs.items()
                     if datetime.fromtimestamp(candles[k]["time"],UTC).hour in hrs}
        csigs_today = {k:v for k,v in csigs.items() if candles[k]["time"] >= today_start}
        if not csigs_today: continue

        for sname, smode, base in staking_modes:
            r = backtest(candles, csigs_today, eb, base_stake=base, payout=PAYOUT,
                         staking=smode)
            if r["trades"] > 0:
                v = "✅" if r["wr"] > BE else "  "
                print(f"{v} {label:<23s} {sname:<18s} {r['trades']:>4d} {r['wr']:>5.1f}% "
                      f"${r['pnl']:>8.1f} ${r['final_bal']:>8.1f} ${r['max_dd']:>7.1f}")
                staking_results.append({"combo": label, "staking": sname, **r})

    print(f"\n{'='*110}")
    print("  PART 3: PARALLEL MARKETS — Running best combos simultaneously")
    print(f"{'='*110}\n")

    # Simulate running multiple markets in parallel
    parallel_configs = [
        [("Vol10_1m", 3, None, 2), ("Vol75_1m", 3, None, 2)],
        [("Vol10_1m", 3, None, 2), ("Vol75_1m", 3, None, 2), ("Vol100_1m", 1, None, 2)],
        [("Vol10_1m", 3, None, 3), ("Vol75_1m", 3, None, 3)],
        [("Vol10_1m", 3, None, 2), ("Vol75_1m", 3, None, 2), ("EURGBP_5m", 3, {7,8,9,10,11,12,13,14,15}, 2)],
        [("Vol10_1m", 3, None, 2), ("Vol75_1m", 1, None, 2), ("Vol75_1m", 3, None, 2), ("Vol100_1m", 1, None, 2)],
    ]

    for stakes_label, smode, base in [("flat $10","flat",10), ("flat $25","flat",25),
                                        ("anti-mart $10","anti_martingale",10),
                                        ("compound 2%","compound",10), ("kelly","kelly",10)]:
        print(f"\n  Staking: {stakes_label}")
        print(f"  {'Markets':<60s} {'Tr':>4s} {'W':>4s} {'L':>4s} {'WR':>6s} {'P&L':>10s} {'FinalBal':>10s}")
        print("  "+"-"*100)

        for pconfig in parallel_configs:
            # Merge all signals from all markets (they don't overlap since different symbols)
            all_trades = []
            for mkt, eb, hrs, min_ag in pconfig:
                candles = data[mkt]
                if len(candles) < 50: continue
                strats = get_strat_signals(candles, mkt)
                csigs = confluence_signals(list(strats.values()), min_ag)
                if hrs:
                    csigs = {k:v for k,v in csigs.items()
                             if datetime.fromtimestamp(candles[k]["time"],UTC).hour in hrs}
                # Today only
                for idx in sorted(csigs.keys()):
                    if candles[idx]["time"] < today_start: continue
                    if idx + eb >= len(candles): continue
                    entry = candles[idx]["close"]; exp = candles[idx+eb]["close"]
                    t = candles[idx]["time"]
                    won = (csigs[idx]=="CALL" and exp>entry) or (csigs[idx]=="PUT" and exp<entry)
                    draw = entry == exp
                    all_trades.append((t, csigs[idx], entry, exp, won, draw))

            # Sort by time and simulate
            all_trades.sort(key=lambda x: x[0])
            w=l=d=0; pnl=0.0; balance=1000.0; streak=0
            for t, dir, entry, exp, won, draw in all_trades:
                if smode == "flat": stake = base
                elif smode == "anti_martingale": stake = base * min(1 + streak*0.5, 4)
                elif smode == "compound": stake = max(base, balance * 0.02)
                elif smode == "kelly":
                    if w+l >= 10:
                        wr_e = w/(w+l); kf = max(0.01, min((wr_e*(1+PAYOUT)-1)/PAYOUT, 0.10))
                        stake = balance * kf
                    else: stake = base
                else: stake = base
                stake = min(stake, balance * 0.15)
                if stake < 1: stake = 1

                if draw: d += 1
                elif won:
                    pnl += stake*PAYOUT; balance += stake*PAYOUT; w+=1
                    streak = max(0,streak)+1
                else:
                    pnl -= stake; balance -= stake; l+=1
                    streak = min(0,streak)-1
                if balance <= 0: break

            tot = w+l+d; wr = w/(w+l)*100 if w+l>0 else 0
            names = "+".join(f"{m}({a})" for m,_,_,a in pconfig)
            v = "✅" if wr > BE else "  "
            print(f"  {v} {names:<58s} {tot:>4d} {w:>4d} {l:>4d} {wr:>5.1f}% ${pnl:>8.1f} ${balance:>8.1f}")

    print(f"\n{'='*110}")
    print("  PART 4: BEST SINGLE-MARKET COMBOS — Trade-by-trade detail")
    print(f"{'='*110}\n")

    # Show trade-by-trade for top 3 combos with $25 flat
    top3 = [
        ("Vol10_1m", 3, None, 2, "Vol10 1m→3m conf≥2"),
        ("Vol75_1m", 3, None, 2, "Vol75 1m→3m conf≥2"),
        ("Vol75_1m", 1, None, 2, "Vol75 1m→1m conf≥2"),
    ]
    for mkt, eb, hrs, min_ag, label in top3:
        candles = data[mkt]
        strats = get_strat_signals(candles, mkt)
        csigs = confluence_signals(list(strats.values()), min_ag)
        csigs_today = {k:v for k,v in csigs.items() if candles[k]["time"] >= today_start}
        r = backtest(candles, csigs_today, eb, base_stake=25, payout=PAYOUT, staking="anti_martingale")
        print(f"  {label} | Anti-Mart $25 | {r['trades']}tr WR {r['wr']:.1f}% | P&L ${r['pnl']:.1f} | Final ${r['final_bal']:.1f}")
        for t in r["details"][:30]:
            icon = "✅" if t["result"]=="WIN" else ("❌" if t["result"]=="LOSS" else "➖")
            print(f"    {icon} {t['time']} {t['dir']:4s} stake={t['stake']} → {t['result']:4s} | P&L {t['pnl']} Bal {t['bal']}")
        if len(r["details"]) > 30:
            print(f"    ... +{len(r['details'])-30} more trades")
        print()

    # ─── FINAL RECOMMENDATION ───
    print(f"\n{'='*110}")
    print("  FINAL RECOMMENDATION")
    print(f"{'='*110}")

    # Find best overall
    best_single = max(staking_results, key=lambda x: x["pnl"]) if staking_results else None
    if best_single:
        print(f"\n  Best single-market today:")
        print(f"    {best_single['combo']} + {best_single['staking']}")
        print(f"    {best_single['trades']} trades | WR {best_single['wr']:.1f}% | P&L ${best_single['pnl']:.1f} | Final ${best_single['final_bal']:.1f}")

    print(f"\n  Recommended setup for max daily profit:")
    print(f"    1. Run Vol10 + Vol75 + Vol100 in PARALLEL (3 markets)")
    print(f"    2. Confluence filter: ≥2 strategies must agree")
    print(f"    3. Anti-martingale staking: $25 base, +50% per win streak (cap 4x)")
    print(f"    4. Max risk per trade: 15% of balance")
    print(f"    5. Starting balance: $1000")
    print()

    await ws.close()

if __name__ == "__main__":
    asyncio.run(main())

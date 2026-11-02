"""One-month backtest — Vol75 1m→3m conf≥3 + multiple staking modes."""
from __future__ import annotations
import asyncio, json, time
from datetime import datetime, timedelta
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
        if ml[i] is not None: se=(ml[i]-se)*m2+se; sl[i]=se; hist[i]=ml[i]-se
    return ml,sl,hist

# ─── Signal generators ───
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

def confluence_signals(strat_signals_list, min_agree=3):
    all_idx = set()
    for ss in strat_signals_list: all_idx |= set(ss.keys())
    result = {}
    for idx in sorted(all_idx):
        votes = {}
        for ss in strat_signals_list:
            if idx in ss:
                d = ss[idx]; votes[d] = votes.get(d, 0) + 1
        for d, cnt in votes.items():
            if cnt >= min_agree:
                result[idx] = d; break
    return result

def get_all_strat_signals(candles):
    return [
        sig_ema(candles, 3, 10), sig_ema(candles, 5, 20), sig_ema(candles, 8, 21),
        sig_macd(candles, 12, 26, 9), sig_macd(candles, 5, 13, 4),
        sig_rsi(candles, 14, 30, 70), sig_rsi(candles, 7, 20, 80), sig_rsi(candles, 14, 25, 75),
        sig_bb(candles, 20, 2.0), sig_bb(candles, 14, 1.5),
        sig_sr(candles, 25, 0.005), sig_sr(candles, 40, 0.005),
        sig_linreg(candles, 20, 2.0),
    ]

def backtest_period(candles, signals_dict, expiry_bars, base_stake=10.0, payout=0.80,
                    cooldown=None, staking="flat", start_bal=1000.0):
    if cooldown is None: cooldown = expiry_bars
    w=l=d=0; pnl=0.0; peak=0.0; mdd=0.0; last=-cooldown
    streak=0; balance=start_bal; daily_pnl={}

    for idx in sorted(signals_dict.keys()):
        if idx+expiry_bars >= len(candles): continue
        if idx - last < cooldown: continue
        dir = signals_dict[idx]
        entry = candles[idx]["close"]; exp = candles[idx+expiry_bars]["close"]
        last = idx
        t = datetime.fromtimestamp(candles[idx]["time"], UTC)
        day = t.strftime("%Y-%m-%d")

        # Stake calc
        if staking == "flat":
            stake = base_stake
        elif staking == "kelly":
            if w+l >= 10:
                wr_est = w/(w+l)
                kf = (wr_est * (1+payout) - 1) / payout
                kf = max(0.01, min(kf, 0.10))
                stake = balance * kf
            else:
                stake = base_stake
        elif staking == "flat_25":
            stake = 25.0
        elif staking == "flat_50":
            stake = 50.0
        elif staking == "conservative_kelly":
            if w+l >= 20:
                wr_est = w/(w+l)
                kf = (wr_est * (1+payout) - 1) / payout
                kf = max(0.005, min(kf * 0.5, 0.05))  # half-kelly, cap 5%
                stake = balance * kf
            else:
                stake = base_stake
        elif staking == "anti_martingale":
            mult = min(1 + max(0,streak) * 0.5, 4)
            stake = base_stake * mult
        else:
            stake = base_stake

        stake = min(stake, balance * 0.15)
        if stake < 1: stake = 1

        if entry == exp:
            d += 1
        elif (dir=="CALL" and exp>entry) or (dir=="PUT" and exp<entry):
            pnl += stake*payout; balance += stake*payout; w+=1
            streak = max(0,streak)+1
        else:
            pnl -= stake; balance -= stake; l+=1
            streak = min(0,streak)-1

        peak = max(peak, pnl); mdd = max(mdd, peak-pnl)
        if day not in daily_pnl: daily_pnl[day] = {"w":0,"l":0,"pnl":0.0,"bal":balance}
        daily_pnl[day]["bal"] = balance
        if entry != exp:
            if (dir=="CALL" and exp>entry) or (dir=="PUT" and exp<entry):
                daily_pnl[day]["w"] += 1
                daily_pnl[day]["pnl"] += stake*payout
            else:
                daily_pnl[day]["l"] += 1
                daily_pnl[day]["pnl"] -= stake

        if balance <= 0: break

    tot=w+l+d; wr=w/(w+l)*100 if (w+l)>0 else 0
    return {"trades":tot,"wins":w,"losses":l,"draws":d,"wr":wr,"pnl":pnl,
            "max_dd":mdd,"final_bal":balance,"daily":daily_pnl}

async def fetch_chunk(ws, symbol, gran, start_epoch, end_epoch):
    rid = int(time.time()*1000) % 100000
    msg = {"ticks_history":symbol,"adjust_start_time":1,"count":5000,
           "start":start_epoch,"end":end_epoch,
           "granularity":gran,"style":"candles","req_id":rid}
    await ws.send(json.dumps(msg))
    resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
    candles = resp.get("candles", [])
    return [{"time":c["epoch"],"open":float(c["open"]),"high":float(c["high"]),
             "low":float(c["low"]),"close":float(c["close"])} for c in candles]

async def fetch_month(ws, symbol, gran, days=30):
    """Fetch ~30 days of data by chunking requests."""
    now = datetime.now(UTC)
    end = int(now.timestamp())
    start = int((now - timedelta(days=days)).timestamp())

    all_candles = []
    chunk_start = start
    chunk_num = 0

    while chunk_start < end:
        chunk_num += 1
        candles = await fetch_chunk(ws, symbol, gran, chunk_start, end)
        if not candles:
            break
        all_candles.extend(candles)
        # Next chunk starts after last candle
        last_time = candles[-1]["time"]
        if last_time <= chunk_start:
            break
        chunk_start = last_time + gran
        print(f"  Chunk {chunk_num}: {len(candles)} candles "
              f"({datetime.fromtimestamp(candles[0]['time'],UTC):%m-%d %H:%M} → "
              f"{datetime.fromtimestamp(candles[-1]['time'],UTC):%m-%d %H:%M})")
        await asyncio.sleep(0.5)  # rate limit

    # Dedupe by time
    seen = set(); deduped = []
    for c in all_candles:
        if c["time"] not in seen:
            seen.add(c["time"]); deduped.append(c)
    deduped.sort(key=lambda x: x["time"])
    return deduped

async def main():
    print("Connecting to Deriv...")
    ws = await asyncio.wait_for(websockets.connect(WS, additional_headers=HDRS, ping_interval=25), timeout=15)
    print("Connected!\n")
    PAYOUT = 0.80; BE = 100/(1+PAYOUT)
    now = datetime.now(UTC)
    print(f"UTC: {now:%Y-%m-%d %H:%M}")
    print(f"Fetching 30 days of data...\n")

    # ─── Fetch 30 days for multiple markets ───
    markets = {
        "Vol75_1m": ("1HZ75V", 60),
        "Vol10_1m": ("1HZ10V", 60),
        "Vol75_5m": ("1HZ75V", 300),
    }

    data = {}
    for name, (sym, gran) in markets.items():
        print(f"Fetching {name} ({sym}, {gran//60}m candles)...")
        data[name] = await fetch_month(ws, sym, gran, days=30)
        c = data[name]
        if c:
            t0 = datetime.fromtimestamp(c[0]["time"], UTC)
            t1 = datetime.fromtimestamp(c[-1]["time"], UTC)
            days_covered = (t1-t0).total_seconds() / 86400
            print(f"  TOTAL: {len(c)} candles over {days_covered:.1f} days "
                  f"({t0:%Y-%m-%d} → {t1:%Y-%m-%d})\n")

    await ws.close()

    # ─── Run backtests ───
    configs = [
        ("Vol75 1m→3m conf≥3", "Vol75_1m", 3, 3),
        ("Vol75 1m→3m conf≥2", "Vol75_1m", 3, 2),
        ("Vol75 1m→3m conf≥4", "Vol75_1m", 3, 4),
        ("Vol75 1m→3m conf≥5", "Vol75_1m", 3, 5),
        ("Vol75 1m→1m conf≥3", "Vol75_1m", 1, 3),
        ("Vol75 1m→5m conf≥3", "Vol75_1m", 5, 3),
        ("Vol10 1m→3m conf≥3", "Vol10_1m", 3, 3),
        ("Vol10 1m→3m conf≥2", "Vol10_1m", 3, 2),
        ("Vol75 5m→5m conf≥3", "Vol75_5m", 1, 3),
    ]

    staking_modes = [
        ("flat $10", "flat", 10),
        ("flat $25", "flat_25", 25),
        ("flat $50", "flat_50", 50),
        ("kelly", "kelly", 10),
        ("half-kelly", "conservative_kelly", 10),
        ("anti-mart $10", "anti_martingale", 10),
    ]

    print(f"\n{'='*120}")
    print(f"  30-DAY BACKTEST RESULTS (breakeven = {BE:.1f}%)")
    print(f"{'='*120}\n")

    # Part 1: Find best market/confluence combo with flat $10
    print("─── PART 1: Best market + confluence level (flat $10 stake) ───\n")
    print(f"{'Config':<28s} {'Trades':>6s} {'W':>5s} {'L':>5s} {'WR':>6s} {'P&L':>10s} {'MaxDD':>10s} {'Final':>10s} {'Days':>5s}")
    print("-"*100)

    best_combo = None; best_pnl = -999999
    for label, mkt, eb, min_ag in configs:
        if mkt not in data or len(data[mkt]) < 100: continue
        candles = data[mkt]
        strats = get_all_strat_signals(candles)
        csigs = confluence_signals(strats, min_ag)
        r = backtest_period(candles, csigs, eb, base_stake=10, payout=PAYOUT, staking="flat")
        days = len(r["daily"])
        v = "✅" if r["wr"] > BE else "❌"
        print(f"{v} {label:<26s} {r['trades']:>6d} {r['wins']:>5d} {r['losses']:>5d} "
              f"{r['wr']:>5.1f}% ${r['pnl']:>8.1f} ${r['max_dd']:>8.1f} ${r['final_bal']:>8.1f} {days:>5d}")
        if r["pnl"] > best_pnl and r["wr"] > BE:
            best_pnl = r["pnl"]; best_combo = (label, mkt, eb, min_ag)

    # Part 2: Test staking modes on top combos
    print(f"\n─── PART 2: Staking comparison on best combos ───\n")
    test_combos = [
        ("Vol75 1m→3m conf≥3", "Vol75_1m", 3, 3),
        ("Vol75 1m→3m conf≥4", "Vol75_1m", 3, 4),
        ("Vol75 1m→3m conf≥5", "Vol75_1m", 3, 5),
        ("Vol10 1m→3m conf≥3", "Vol10_1m", 3, 3),
    ]

    print(f"{'Config':<28s} {'Staking':<16s} {'Tr':>5s} {'WR':>6s} {'P&L':>12s} {'MaxDD':>10s} {'Final':>12s}")
    print("-"*105)

    all_results = []
    for label, mkt, eb, min_ag in test_combos:
        if mkt not in data or len(data[mkt]) < 100: continue
        candles = data[mkt]
        strats = get_all_strat_signals(candles)
        csigs = confluence_signals(strats, min_ag)

        for sname, smode, base in staking_modes:
            r = backtest_period(candles, csigs, eb, base_stake=base, payout=PAYOUT, staking=smode)
            if r["trades"] == 0: continue
            v = "✅" if r["wr"] > BE else "❌"
            print(f"{v} {label:<26s} {sname:<16s} {r['trades']:>5d} {r['wr']:>5.1f}% "
                  f"${r['pnl']:>10.1f} ${r['max_dd']:>8.1f} ${r['final_bal']:>10.1f}")
            all_results.append({"label":label,"staking":sname,"smode":smode,**r})

    # Part 3: Day-by-day breakdown for the best combo
    print(f"\n─── PART 3: Day-by-day breakdown ───\n")

    # Show for top 3 profitable combos
    for label, mkt, eb, min_ag in test_combos[:2]:
        candles = data[mkt]
        strats = get_all_strat_signals(candles)
        csigs = confluence_signals(strats, min_ag)

        for sname, smode, base in [("flat $10","flat",10), ("flat $50","flat_50",50),
                                     ("half-kelly","conservative_kelly",10)]:
            r = backtest_period(candles, csigs, eb, base_stake=base, payout=PAYOUT, staking=smode)
            if r["trades"] == 0: continue

            print(f"  {label} + {sname} | {r['trades']}tr WR {r['wr']:.1f}% | P&L ${r['pnl']:.1f}")
            print(f"  {'Date':<12s} {'W':>3s} {'L':>3s} {'WR':>6s} {'DayP&L':>10s} {'Balance':>10s}")
            print("  "+"-"*55)
            win_days = 0; lose_days = 0
            for day in sorted(r["daily"].keys()):
                dd = r["daily"][day]
                tot = dd["w"]+dd["l"]
                wr = dd["w"]/tot*100 if tot>0 else 0
                icon = "✅" if dd["pnl"]>0 else ("❌" if dd["pnl"]<0 else "➖")
                print(f"  {icon} {day:<10s} {dd['w']:>3d} {dd['l']:>3d} {wr:>5.1f}% ${dd['pnl']:>8.1f} ${dd['bal']:>8.1f}")
                if dd["pnl"]>0: win_days+=1
                elif dd["pnl"]<0: lose_days+=1
            print(f"  Winning days: {win_days}/{win_days+lose_days} ({win_days/(win_days+lose_days)*100:.0f}%)\n")

    # ─── FINAL SUMMARY ───
    print(f"\n{'='*120}")
    print("  FINAL 30-DAY SUMMARY")
    print(f"{'='*120}")

    # Find overall best
    profitable = [r for r in all_results if r["wr"] > BE]
    profitable.sort(key=lambda x: -x["pnl"])

    print(f"\n  Total configs tested: {len(all_results)}")
    print(f"  Profitable (WR > {BE:.1f}%): {len(profitable)}")

    if profitable:
        print(f"\n  TOP 10 by P&L:")
        for i, r in enumerate(profitable[:10]):
            roi = (r["pnl"]/1000)*100
            print(f"  {i+1}. {r['label']:<26s} {r['staking']:<16s} "
                  f"{r['trades']}tr WR {r['wr']:.1f}% P&L ${r['pnl']:.1f} "
                  f"MaxDD ${r['max_dd']:.1f} ROI {roi:.1f}%")

        best = profitable[0]
        print(f"\n  ★ BEST OVERALL: {best['label']} + {best['staking']}")
        print(f"    Trades: {best['trades']} | WR: {best['wr']:.1f}% | P&L: ${best['pnl']:.1f}")
        print(f"    Max DD: ${best['max_dd']:.1f} | Final Balance: ${best['final_bal']:.1f}")
        print(f"    ROI: {(best['pnl']/1000)*100:.1f}% over 30 days")
        if best["daily"]:
            days = sorted(best["daily"].keys())
            win_d = sum(1 for d in days if best["daily"][d]["pnl"]>0)
            print(f"    Winning days: {win_d}/{len(days)} ({win_d/len(days)*100:.0f}%)")
            avg_daily = best["pnl"]/len(days)
            print(f"    Avg daily P&L: ${avg_daily:.1f}")

    print()

if __name__ == "__main__":
    asyncio.run(main())

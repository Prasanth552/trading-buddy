"""
BSQ backtest for Oct 22 — simulate what trades would have been taken.
BB(14,2), squeeze pctile<=35%, breakout above/below band.
"""
import time, json, math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from src.broker.upstox_data import UpstoxData, load_cached_token
import config

IST = ZoneInfo(config.TIMEZONE)
token = load_cached_token()
ud = UpstoxData(access_token=token)

CAPITAL = 300000
SL_PCT = 0.30
MAX_SL = 5000
FLOOR_LEVELS = [2000, 3500, 5000, 6500, 8000, 9500, 11000, 12500]
BB_PERIOD = 14
BB_STD = 2.0
SQUEEZE_PCTILE = 35
BLOCKLIST = {"GODREJCP", "GRASIM"}
LOSS_CAP = 20000
PROFIT_CAP = 30000
MAX_ENTRIES_PER_SYM = 2
RESCAN_INTERVAL = 300  # 5 min

trade_date = datetime(2026, 10, 22).date()

# Load master
print("Loading master...")
master = ud._load_master()

fo_stocks = set()
opt_keys = {}
lot_sizes = {}
for inst in master:
    if inst.get("segment") != "NSE_FO":
        continue
    sym = str(inst.get("asset_symbol", "")).upper()
    it = str(inst.get("instrument_type", "")).upper()
    if not sym or it not in ("CE", "PE"):
        continue
    fo_stocks.add(sym)
    strike = inst.get("strike_price", 0)
    expiry = inst.get("expiry", 0)
    key = inst.get("instrument_key", "")
    lot = inst.get("lot_size", 0)
    if key and strike:
        opt_keys[(sym, expiry, float(strike), it)] = key
    if sym not in lot_sizes and lot:
        lot_sizes[sym] = int(lot)

eq_keys = {}
for inst in master:
    if inst.get("segment") == "NSE_EQ":
        tsym = str(inst.get("trading_symbol", "")).upper()
        if tsym in fo_stocks and inst.get("instrument_key"):
            eq_keys[tsym] = inst["instrument_key"]

print(f"Stocks: {len(eq_keys)}, Options: {len(opt_keys)}")

def find_nearest_expiry(symbol):
    trade_ms = int(datetime(trade_date.year, trade_date.month, trade_date.day, tzinfo=IST).timestamp() * 1000)
    expiries = sorted(e for (s,e,st,o) in opt_keys if s == symbol and e >= trade_ms)
    return expiries[0] if expiries else None

def find_opt_key(symbol, expiry_ms, strike, ot):
    for off in [0, 5, -5, 10, -10, 20, -20, 50, -50, 100, -100, 2.5, -2.5]:
        k = opt_keys.get((symbol, expiry_ms, float(strike + off), ot))
        if k:
            return k, strike + off
    return None, strike

def simulate_trade(candles, entry_price, qty):
    sl_pct_price = entry_price * (1 - SL_PCT)
    sl_cap_price = entry_price - (MAX_SL / qty) if qty > 0 else 0
    sl = max(sl_pct_price, sl_cap_price)
    peak_net = 0
    for c in candles:
        net_high = (c["high"] - entry_price) * qty
        if net_high > peak_net:
            peak_net = net_high
        if c["low"] <= sl:
            return (sl - entry_price) * qty, "SL", peak_net
        net_close = (c["close"] - entry_price) * qty
        stepped = 0
        for fl in FLOOR_LEVELS:
            if peak_net >= fl:
                stepped = fl
        if stepped > 0 and net_close <= stepped:
            return net_close, "FLOOR", peak_net
    if candles:
        return (candles[-1]["close"] - entry_price) * qty, "EOD", peak_net
    return 0, "NODATA", 0

def bb_squeeze_check(candles_5m, ci):
    """Check if BB is in squeeze at candle index ci."""
    if ci < BB_PERIOD:
        return False, None, None, None
    window = candles_5m[ci - BB_PERIOD + 1:ci + 1]
    closes = [c["close"] for c in window]
    sma = sum(closes) / len(closes)
    std = (sum((x - sma)**2 for x in closes) / len(closes)) ** 0.5
    if std <= 0:
        return False, None, None, None
    upper = sma + BB_STD * std
    lower = sma - BB_STD * std
    bandwidth = (upper - lower) / sma * 100

    # Calculate percentile of current bandwidth vs recent history
    min_lookback = max(BB_PERIOD, 10)
    if ci < min_lookback + BB_PERIOD:
        return False, None, None, None
    recent_bws = []
    for j in range(max(0, ci - 50), ci + 1):
        if j < BB_PERIOD:
            continue
        w = candles_5m[j - BB_PERIOD + 1:j + 1]
        cs = [c["close"] for c in w]
        s = sum(cs) / len(cs)
        st = (sum((x - s)**2 for x in cs) / len(cs)) ** 0.5
        if s > 0 and st > 0:
            recent_bws.append((w[-1]["close"] - s - BB_STD * st, (s + BB_STD * st - (s - BB_STD * st)) / s * 100))

    if len(recent_bws) < 10:
        return False, None, None, None

    bw_values = [b[1] for b in recent_bws]
    pctile = sum(1 for b in bw_values if b <= bandwidth) / len(bw_values) * 100

    if pctile <= SQUEEZE_PCTILE:
        return True, upper, lower, sma
    return False, None, None, None

# Fetch 5-min candles for all F&O stocks on Oct 22
print("Fetching 5-min candles for Oct 22...")
from_dt = datetime(2026, 10, 22, 9, 15, tzinfo=IST)
to_dt = datetime(2026, 10, 22, 15, 30, tzinfo=IST)

five_min_cache = {}
fc = 0
for sym in sorted(eq_keys.keys()):
    if sym in BLOCKLIST:
        continue
    ek = eq_keys.get(sym)
    if not ek:
        continue
    try:
        candles = ud.historical_data(ek, from_dt, to_dt, "5minute")
        time.sleep(0.25)
    except:
        continue
    if candles and len(candles) >= BB_PERIOD + 5:
        five_min_cache[sym] = candles
    fc += 1
    if fc % 50 == 0:
        print(f"  ... {fc}/{len(eq_keys)}")

print(f"Fetched: {len(five_min_cache)} stocks with enough candles")

# Scan for BSQ signals
print("\nScanning for BSQ signals...")
all_signals = []

for sym, candles_5m in five_min_cache.items():
    for ci in range(BB_PERIOD + 10, len(candles_5m)):
        c = candles_5m[ci]
        ctime = c["date"]
        try:
            ct = datetime.fromisoformat(ctime.replace("Z",""))
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=IST)
        except:
            continue

        # Only scan from 09:45
        if ct.hour < 9 or (ct.hour == 9 and ct.minute < 45):
            continue
        # Cutoff at 15:15
        if ct.hour > 15 or (ct.hour == 15 and ct.minute > 15):
            break

        in_squeeze, upper, lower, sma = bb_squeeze_check(candles_5m, ci - 1)  # prev candle in squeeze
        if not in_squeeze:
            continue

        # Current candle breaks out
        if c["close"] > upper:
            all_signals.append({"sym": sym, "ot": "CE", "time": ctime, "close": c["close"], "upper": upper, "lower": lower})
        elif c["close"] < lower:
            all_signals.append({"sym": sym, "ot": "PE", "time": ctime, "close": c["close"], "upper": upper, "lower": lower})

# Sort by time
all_signals.sort(key=lambda x: x["time"])
print(f"Found {len(all_signals)} BSQ signals")

# Simulate trades
cap = CAPITAL
day_pnl = 0
sym_entries = {}
trades = []

for sig in all_signals:
    if cap < 5000:
        continue
    if day_pnl >= PROFIT_CAP:
        break
    if day_pnl <= -LOSS_CAP:
        break
    sym = sig["sym"]
    if sym_entries.get(sym, 0) >= MAX_ENTRIES_PER_SYM:
        continue

    spot = sig["close"]
    ot = sig["ot"]
    exp = find_nearest_expiry(sym)
    if not exp:
        continue
    lot = lot_sizes.get(sym, 0)
    if lot <= 0:
        continue

    atm = round(spot / 5) * 5 if spot < 500 else round(spot / 10) * 10 if spot < 2000 else round(spot / 50) * 50 if spot < 10000 else round(spot / 100) * 100
    ok, astrike = find_opt_key(sym, exp, atm, ot)
    if not ok:
        continue

    # Fetch 1-min option candles from signal time to EOD
    try:
        sig_time = datetime.fromisoformat(sig["time"].replace("Z",""))
        if sig_time.tzinfo is None:
            sig_time = sig_time.replace(tzinfo=IST)
    except:
        continue
    exit_dt = datetime(2026, 10, 22, 15, 30, tzinfo=IST)
    try:
        oc = ud.historical_data(ok, sig_time, exit_dt, "1minute")
        time.sleep(0.3)
    except:
        continue
    if not oc or len(oc) < 2:
        continue

    ep = oc[0]["open"]
    if ep <= 0:
        continue
    cpl = ep * lot
    ml = int(cap / cpl) if cpl > 0 else 0
    if ml <= 0:
        continue
    lots = min(ml, 3)
    qty = lots * lot
    cap -= ep * qty

    pnl, reason, peak = simulate_trade(oc[1:], ep, qty)
    day_pnl += pnl
    cap += ep * qty + pnl
    sym_entries[sym] = sym_entries.get(sym, 0) + 1
    trades.append({
        "sym": sym, "ot": ot, "entry": ep, "pnl": pnl, "reason": reason,
        "peak": peak, "time": sig["time"][:16], "strike": astrike, "qty": qty
    })

# Results
print(f"\n{'='*80}")
print(f"  BSQ BACKTEST — Oct 22, 2026")
print(f"{'='*80}")
wins = sum(1 for t in trades if t["pnl"] > 0)
losses = len(trades) - wins
wr = (wins/len(trades)*100) if trades else 0
print(f"  Trades: {len(trades)} | Wins: {wins} | Losses: {losses} | WR: {wr:.0f}%")
print(f"  Total P&L: {day_pnl:>+,.0f}")
print(f"  Avg Win: {sum(t['pnl'] for t in trades if t['pnl']>0)/max(wins,1):>+,.0f}")
print(f"  Avg Loss: {sum(t['pnl'] for t in trades if t['pnl']<0)/max(losses,1):>+,.0f}")

print(f"\n  {'Time':16s} {'Symbol':15s} {'Strike':>6s} {'Type':4s} {'Entry':>8s} {'Qty':>5s} {'Peak':>8s} {'P&L':>8s} {'Exit':6s}")
print(f"  {'-'*80}")
for t in trades:
    print(f"  {t['time']:16s} {t['sym']:15s} {t['strike']:>6.0f} {t['ot']:4s} {t['entry']:>8.2f} {t['qty']:>5d} {t['peak']:>+8,.0f} {t['pnl']:>+8,.0f} {t['reason']:6s}")

# CE vs PE breakdown
ce_trades = [t for t in trades if t["ot"] == "CE"]
pe_trades = [t for t in trades if t["ot"] == "PE"]
print(f"\n  CE: {len(ce_trades)}t P&L={sum(t['pnl'] for t in ce_trades):>+,.0f}")
print(f"  PE: {len(pe_trades)}t P&L={sum(t['pnl'] for t in pe_trades):>+,.0f}")

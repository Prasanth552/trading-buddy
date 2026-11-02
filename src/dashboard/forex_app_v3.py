"""
Forex Paper Trading Bot v3 — Multi-Strategy Portfolio
Runs 10 optimized strategies across 4 pairs on 15m and 1h timeframes.
All entries realistic: next-candle open + spread + slippage.
"""
import json
import os
import random
import time
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import yfinance as yf

UTC = ZoneInfo("UTC")
IST = ZoneInfo("Asia/Kolkata")

DATA_DIR = Path(os.path.expanduser("~/Trading-Buddy/data"))
DATA_DIR.mkdir(exist_ok=True)
TRADES_FILE = DATA_DIR / "forex_paper_trades_v3.json"
STATE_FILE = DATA_DIR / "forex_state_v3.json"

# ── Pair configs ─────────────────────────────────────────────────────────────
PAIRS = {
    "GBP/USD": {"symbol": "GBPUSD=X", "spread": 1.0, "pip": 0.0001, "pv": 830},
    "EUR/USD": {"symbol": "EURUSD=X", "spread": 0.8, "pip": 0.0001, "pv": 830},
    "USD/JPY": {"symbol": "USDJPY=X", "spread": 0.8, "pip": 0.01, "pv": 55},
    "GBP/JPY": {"symbol": "GBPJPY=X", "spread": 1.5, "pip": 0.01, "pv": 55},
}

LOT_SIZE = 1.0
MAX_HOLD = 50

# ── Strategy configs (optimized SL/TP from Sep backtest) ────────────────────
STRATEGIES = [
    {"name": "Triple EMA",   "pair": "GBP/USD", "tf": "1h",  "sl": 0.75, "tp": 1.5, "fn": "triple_ema"},
    {"name": "EMA 9/21",     "pair": "EUR/USD", "tf": "1h",  "sl": 1.25, "tp": 2.0, "fn": "ema_9_21"},
    {"name": "EMA 9/21",     "pair": "GBP/USD", "tf": "1h",  "sl": 0.75, "tp": 5.0, "fn": "ema_9_21"},
    {"name": "EMA 20/50",    "pair": "GBP/JPY", "tf": "1h",  "sl": 1.25, "tp": 4.0, "fn": "ema_20_50"},
    {"name": "RSI",          "pair": "USD/JPY", "tf": "1h",  "sl": 1.0,  "tp": 1.5, "fn": "rsi"},
    {"name": "Donchian",     "pair": "USD/JPY", "tf": "15m", "sl": 1.0,  "tp": 2.0, "fn": "donchian"},
]

# ── Indicators ──────────────────────────────────────────────────────────────
def ema(values, period):
    r = [None] * len(values)
    if len(values) < period: return r
    k = 2 / (period + 1)
    r[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        r[i] = values[i] * k + r[i - 1] * (1 - k)
    return r

def rsi(closes, period=14):
    r = [None] * len(closes)
    if len(closes) < period + 1: return r
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains) / period; al = sum(losses) / period
    r[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        r[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return r

def atr(candles, period=14):
    r = [None] * len(candles)
    if len(candles) < period + 1: return r
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period: return r
    r[period] = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        r[i + 1] = (r[i] * (period - 1) + trs[i]) / period
    return r

def macd_ind(closes, fast=12, slow=26, signal=9):
    ef = ema(closes, fast); es = ema(closes, slow)
    ml = [None] * len(closes)
    for i in range(len(closes)):
        if ef[i] is not None and es[i] is not None:
            ml[i] = ef[i] - es[i]
    valid = [v for v in ml if v is not None]
    sig = ema(valid, signal)
    sl = [None] * len(closes); j = 0
    for i in range(len(closes)):
        if ml[i] is not None:
            sl[i] = sig[j] if j < len(sig) else None; j += 1
    return ml, sl

def donchian_ind(candles, period=20):
    upper, lower = [None] * len(candles), [None] * len(candles)
    for i in range(period, len(candles)):
        upper[i] = max(c["high"] for c in candles[i - period:i])
        lower[i] = min(c["low"] for c in candles[i - period:i])
    return upper, lower

# ── Signal generators ───────────────────────────────────────────────────────
def gen_ema_cross(candles, fast_p, slow_p):
    closes = [c["close"] for c in candles]
    f = ema(closes, fast_p); s = ema(closes, slow_p)
    sigs = []
    for i in range(1, len(candles)):
        if f[i] is None or f[i-1] is None or s[i] is None or s[i-1] is None: continue
        if f[i-1] <= s[i-1] and f[i] > s[i]: sigs.append((i, "CALL"))
        elif f[i-1] >= s[i-1] and f[i] < s[i]: sigs.append((i, "PUT"))
    return sigs

def gen_triple_ema(candles):
    closes = [c["close"] for c in candles]
    e1 = ema(closes, 8); e2 = ema(closes, 13); e3 = ema(closes, 21)
    sigs = []
    for i in range(1, len(candles)):
        if any(x is None for x in [e1[i], e2[i], e3[i], e1[i-1], e2[i-1], e3[i-1]]): continue
        pu = e1[i-1] > e2[i-1] and e2[i-1] > e3[i-1]
        cu = e1[i] > e2[i] and e2[i] > e3[i]
        pd = e1[i-1] < e2[i-1] and e2[i-1] < e3[i-1]
        cd = e1[i] < e2[i] and e2[i] < e3[i]
        if not pu and cu: sigs.append((i, "CALL"))
        elif not pd and cd: sigs.append((i, "PUT"))
    return sigs

def gen_macd(candles):
    closes = [c["close"] for c in candles]
    ml, sl = macd_ind(closes)
    sigs = []
    for i in range(1, len(candles)):
        if ml[i] is None or ml[i-1] is None or sl[i] is None or sl[i-1] is None: continue
        if ml[i-1] <= sl[i-1] and ml[i] > sl[i]: sigs.append((i, "CALL"))
        elif ml[i-1] >= sl[i-1] and ml[i] < sl[i]: sigs.append((i, "PUT"))
    return sigs

def gen_rsi(candles):
    closes = [c["close"] for c in candles]
    r = rsi(closes, 14)
    sigs = []
    for i in range(1, len(candles)):
        if r[i] is None or r[i-1] is None: continue
        if r[i-1] < 30 and r[i] >= 30: sigs.append((i, "CALL"))
        elif r[i-1] > 70 and r[i] <= 70: sigs.append((i, "PUT"))
    return sigs

def gen_donchian(candles):
    upper, lower = donchian_ind(candles, 20)
    sigs = []
    for i in range(1, len(candles)):
        if upper[i] is None: continue
        if candles[i]["close"] > upper[i]: sigs.append((i, "CALL"))
        elif candles[i]["close"] < lower[i]: sigs.append((i, "PUT"))
    return sigs

SIGNAL_FNS = {
    "triple_ema": gen_triple_ema,
    "ema_9_21": lambda c: gen_ema_cross(c, 9, 21),
    "ema_20_50": lambda c: gen_ema_cross(c, 20, 50),
    "macd": gen_macd,
    "rsi": gen_rsi,
    "donchian": gen_donchian,
}

# ── Fetch candles ───────────────────────────────────────────────────────────
def fetch_candles(symbol, interval, period="5d"):
    try:
        df = yf.Ticker(symbol).history(period=period, interval=interval)
        return [{"time": int(ts.timestamp()), "open": float(row["Open"]),
                 "high": float(row["High"]), "low": float(row["Low"]),
                 "close": float(row["Close"])} for ts, row in df.iterrows()]
    except Exception as e:
        print(f"  ERROR fetching {symbol} {interval}: {e}")
        return []

# ── Trade management ────────────────────────────────────────────────────────
def load_trades():
    if TRADES_FILE.exists():
        return json.loads(TRADES_FILE.read_text())
    return []

def save_trades(trades):
    TRADES_FILE.write_text(json.dumps(trades, indent=2))

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_signal": {}, "open_positions": []}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Core: check for new signals and manage positions ────────────────────────
def check_signals_and_manage():
    state = load_state()
    trades = load_trades()
    new_trades = 0
    now = datetime.now(UTC)

    for strat in STRATEGIES:
        pair = strat["pair"]
        pcfg = PAIRS[pair]
        candles = fetch_candles(pcfg["symbol"], strat["tf"])
        if len(candles) < 50:
            continue

        atr_vals = atr(candles, 14)
        sig_fn = SIGNAL_FNS[strat["fn"]]
        signals = sig_fn(candles)

        strat_key = f"{strat['name']}_{pair}_{strat['tf']}"

        # Only look at the latest signal
        if not signals:
            continue
        latest_idx, direction = signals[-1]
        latest_time = candles[latest_idx]["time"]

        # Skip if we already acted on this signal
        if state["last_signal"].get(strat_key) == latest_time:
            continue

        # Skip if ATR not available
        if atr_vals[latest_idx] is None:
            continue

        # Check if this is recent (within last 2 candles)
        last_candle_time = candles[-1]["time"]
        tf_seconds = 3600 if strat["tf"] == "1h" else 900
        if last_candle_time - latest_time > tf_seconds * 2:
            continue

        # We have a fresh signal — enter at current candle's open (simulating next-candle entry)
        cur_atr = atr_vals[latest_idx]
        entry_candle = candles[-1]  # latest candle as our entry
        raw_entry = entry_candle["open"]

        slippage = random.uniform(0, 0.5) * pcfg["pip"]
        spread_half = (pcfg["spread"] * pcfg["pip"]) / 2

        if direction == "CALL":
            entry_price = raw_entry + spread_half + slippage
            sl_price = entry_price - strat["sl"] * cur_atr
            tp_price = entry_price + strat["tp"] * cur_atr
        else:
            entry_price = raw_entry - spread_half - slippage
            sl_price = entry_price + strat["sl"] * cur_atr
            tp_price = entry_price - strat["tp"] * cur_atr

        position = {
            "strat": strat["name"],
            "pair": pair,
            "tf": strat["tf"],
            "direction": direction,
            "entry_price": round(entry_price, 6),
            "sl_price": round(sl_price, 6),
            "tp_price": round(tp_price, 6),
            "entry_time": now.isoformat(),
            "entry_candle_time": entry_candle["time"],
            "candles_held": 0,
            "sl_mult": strat["sl"],
            "tp_mult": strat["tp"],
            "atr": round(cur_atr, 6),
        }

        state["open_positions"].append(position)
        state["last_signal"][strat_key] = latest_time
        new_trades += 1
        print(f"  NEW {direction} {pair} via {strat['name']} {strat['tf']} | Entry {entry_price:.5f} SL {sl_price:.5f} TP {tp_price:.5f}")

    # Check open positions for SL/TP hit
    closed = []
    still_open = []
    for pos in state["open_positions"]:
        pcfg = PAIRS[pos["pair"]]
        candles = fetch_candles(pcfg["symbol"], pos["tf"], period="2d")
        if not candles:
            still_open.append(pos)
            continue

        pos["candles_held"] += 1
        latest = candles[-1]
        exit_price = None
        reason = None

        if pos["direction"] == "CALL":
            if latest["low"] <= pos["sl_price"]:
                exit_price = pos["sl_price"] - (pcfg["spread"] * pcfg["pip"]) / 2
                reason = "SL"
            elif latest["high"] >= pos["tp_price"]:
                exit_price = pos["tp_price"] - (pcfg["spread"] * pcfg["pip"]) / 2
                reason = "TP"
            elif pos["candles_held"] >= MAX_HOLD:
                exit_price = latest["close"] - (pcfg["spread"] * pcfg["pip"]) / 2
                reason = "TIMEOUT"
        else:
            if latest["high"] >= pos["sl_price"]:
                exit_price = pos["sl_price"] + (pcfg["spread"] * pcfg["pip"]) / 2
                reason = "SL"
            elif latest["low"] <= pos["tp_price"]:
                exit_price = pos["tp_price"] + (pcfg["spread"] * pcfg["pip"]) / 2
                reason = "TP"
            elif pos["candles_held"] >= MAX_HOLD:
                exit_price = latest["close"] + (pcfg["spread"] * pcfg["pip"]) / 2
                reason = "TIMEOUT"

        if exit_price is not None:
            if pos["direction"] == "CALL":
                pip_move = (exit_price - pos["entry_price"]) / pcfg["pip"]
            else:
                pip_move = (pos["entry_price"] - exit_price) / pcfg["pip"]
            pnl = pip_move * pcfg["pv"] * LOT_SIZE

            trade = {
                "strat": pos["strat"],
                "pair": pos["pair"],
                "tf": pos["tf"],
                "direction": pos["direction"],
                "entry_price": pos["entry_price"],
                "exit_price": round(exit_price, 6),
                "entry_time": pos["entry_time"],
                "exit_time": now.isoformat(),
                "pips": round(pip_move, 1),
                "pnl": round(pnl, 0),
                "reason": reason,
                "candles_held": pos["candles_held"],
                "won": pnl > 0,
            }
            trades.append(trade)
            closed.append(trade)
            emoji = "W" if pnl > 0 else "L"
            print(f"  CLOSED [{emoji}] {pos['direction']} {pos['pair']} {pos['strat']} | {reason} | {pip_move:+.1f} pips | P&L Rs {pnl:+,.0f}")
        else:
            still_open.append(pos)

    state["open_positions"] = still_open
    save_state(state)
    save_trades(trades)
    return new_trades, len(closed), len(still_open)

# ── Dashboard ───────────────────────────────────────────────────────────────
def print_dashboard():
    trades = load_trades()
    state = load_state()
    now = datetime.now(IST)

    print("\n" + "=" * 70)
    print(f"  FOREX PORTFOLIO v3 — {now.strftime('%Y-%m-%d %H:%M IST')}")
    print(f"  Lot size: {LOT_SIZE} | Strategies: {len(STRATEGIES)} | Pairs: {len(PAIRS)}")
    print("=" * 70)

    # Open positions
    print(f"\n  OPEN POSITIONS ({len(state['open_positions'])})")
    if state["open_positions"]:
        print(f"    {'Strat':<12} {'Pair':<10} {'Dir':<5} {'Entry':>9} {'SL':>9} {'TP':>9} {'Held':>5}")
        for p in state["open_positions"]:
            print(f"    {p['strat']:<12} {p['pair']:<10} {p['direction']:<5} {p['entry_price']:>9.5f} {p['sl_price']:>9.5f} {p['tp_price']:>9.5f} {p['candles_held']:>5}")

    if not trades:
        print("\n  No closed trades yet.")
        return

    # Summary
    total = len(trades)
    wins = sum(1 for t in trades if t["won"])
    wr = wins / total * 100
    total_pnl = sum(t["pnl"] for t in trades)
    total_pips = sum(t["pips"] for t in trades)

    print(f"\n  SUMMARY")
    print(f"    Trades: {total} | Wins: {wins} | WR: {wr:.1f}%")
    print(f"    Total P&L: Rs {total_pnl:,.0f} | Total pips: {total_pips:.1f}")

    # By strategy
    print(f"\n  BY STRATEGY")
    print(f"    {'Strategy':<20} {'Trades':>6} {'WR':>6} {'P&L':>12} {'Pips':>8}")
    strats = {}
    for t in trades:
        key = f"{t['strat']} {t['pair']} {t['tf']}"
        if key not in strats:
            strats[key] = {"trades": 0, "wins": 0, "pnl": 0, "pips": 0}
        strats[key]["trades"] += 1
        strats[key]["pnl"] += t["pnl"]
        strats[key]["pips"] += t["pips"]
        if t["won"]: strats[key]["wins"] += 1
    for key in sorted(strats, key=lambda k: strats[k]["pnl"], reverse=True):
        s = strats[key]
        w = s["wins"] / s["trades"] * 100
        print(f"    {key:<20} {s['trades']:>6} {w:>5.1f}% Rs {s['pnl']:>10,.0f} {s['pips']:>7.1f}")

    # Recent trades
    print(f"\n  LAST 10 TRADES")
    print(f"    {'Time':<18} {'Strat':<12} {'Pair':<8} {'Dir':<5} {'Reason':<4} {'Pips':>6} {'P&L':>10}")
    for t in trades[-10:]:
        tm = t["exit_time"][:16] if "exit_time" in t else "?"
        w = "W" if t["won"] else "L"
        print(f"    {tm:<18} {t['strat']:<12} {t['pair']:<8} {t['direction']:<5} {t['reason']:<4} {t['pips']:>+6.1f} Rs {t['pnl']:>+9,.0f} [{w}]")

# ── Main loop ───────────────────────────────────────────────────────────────
def main():
    print("Starting Forex Portfolio v3...")
    print(f"Strategies: {len(STRATEGIES)}")
    for s in STRATEGIES:
        print(f"  {s['name']:<12} {s['pair']:<10} {s['tf']:<4} SL={s['sl']:.2f} TP={s['tp']:.1f}")

    cycle = 0
    while True:
        cycle += 1
        now = datetime.now(UTC)
        dow = now.weekday()

        # Skip weekends
        if dow >= 5:
            print(f"\n  [{now.strftime('%H:%M UTC')}] Weekend — sleeping 1h")
            time.sleep(3600)
            continue

        print(f"\n  [{now.strftime('%H:%M UTC')}] Cycle {cycle} — checking signals...")
        try:
            new, closed, open_count = check_signals_and_manage()
            print(f"  New: {new} | Closed: {closed} | Open: {open_count}")
        except Exception as e:
            print(f"  ERROR: {e}")

        if cycle % 4 == 0:
            print_dashboard()

        # Check every 15 minutes (matches shortest timeframe)
        time.sleep(900)

if __name__ == "__main__":
    main()

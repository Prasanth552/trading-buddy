#!/usr/bin/env python3
"""Verify DT paid channel stock option signals using STOCK equity candles.
Since expired option candles aren't available via Upstox, we use the underlying
stock's 1-min candles to determine if the directional bet was correct.

Logic:
- BUY X 3000 PE = betting stock goes DOWN from current level
- BUY X 3000 CE = betting stock goes UP from current level
- We track stock price from signal time and see if the move was favorable
- We estimate option P&L using delta approximation (ATM delta ~0.5)
"""
import json, re, os, sys, time
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
import requests

IST = ZoneInfo("Asia/Kolkata")

with open("data/ch1_oct_messages.json") as f:
    messages = json.load(f)

# ── Parse signals ─────────────────────────────────────────────────
SIGNAL_RE = re.compile(
    r'BUY\s+([A-Z&\s]+?)\s*\n+\s*(\d+(?:\.\d+)?)\s+(CE|PE)\s+(?:ABOVE|BELOW|NEAR|@|AT)\s+(\d+(?:\.\d+)?)\s*\n+\s*SL\s+(\d+(?:\.\d+)?)\s+TARGET\s+([\d.\s]+)',
    re.IGNORECASE | re.MULTILINE
)
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}

# Symbol name mapping (channel names to Upstox equity symbols)
SYMBOL_MAP = {
    "M&M": "M&M",
    "HEROMOTO": "HEROMOTOCO",
    "BAJAJ AUTO": "BAJAJ-AUTO",
    "BAJAJAUTO": "BAJAJ-AUTO",
    "NYKAA": "NYKAA",
    "KALYANJIL": "KALYANKJIL",
    "PERSEISTENT": "PERSISTENT",
    "PERSISTENT": "PERSISTENT",
    "ETERNAL": "ETERNAL",
    "TATACONSUN": "TATACONSUM",
}

signals = []
for m in messages:
    match = SIGNAL_RE.search(m["text"].strip())
    if not match:
        continue
    symbol = match.group(1).upper().strip().replace("  ", " ")
    clean_sym = symbol.replace(" ", "")
    if clean_sym in INDEX_NAMES:
        continue
    strike = float(match.group(2))
    opt_type = match.group(3).upper()
    entry_price = float(match.group(4))
    sl = float(match.group(5))
    targets = [float(x) for x in re.findall(r'[\d.]+', match.group(6).strip())]
    has_wait = "WAIT TO ACTIVATE" in m["text"].upper()

    signals.append({
        "msg_id": m["id"],
        "ts": m["ts"],
        "symbol": clean_sym,
        "symbol_raw": symbol.strip(),
        "strike": strike,
        "option_type": opt_type,
        "entry_price": entry_price,
        "sl": sl,
        "targets": targets,
        "has_wait": has_wait,
    })

# Collect follow-ups
msg_by_id = {m["id"]: m for m in messages}
for s in signals:
    s["followups"] = []
for m in messages:
    if m["reply_to"]:
        for s in signals:
            if s["msg_id"] == m["reply_to"]:
                s["followups"].append({"ts": m["ts"], "text": m["text"]})

# Determine channel-reported outcome
for s in signals:
    s["ch_outcome"] = "UNKNOWN"
    s["ch_exit"] = None
    for fu in s["followups"]:
        txt = fu["text"].upper()
        if "SL HIT" in txt or "SL TRIGGERED" in txt:
            s["ch_outcome"] = "SL_HIT"
        elif "BOOK" in txt or "TRAIL" in txt:
            s["ch_outcome"] = "PROFIT"
            nums = re.findall(r'[\d.]+', fu["text"])
            if nums:
                s["ch_exit"] = float(nums[0])
        elif "COST" in txt and "SL" in txt:
            s["ch_outcome"] = "BREAKEVEN"
        elif re.match(r'^\s*[\d.]+\s*$', fu["text"].strip()):
            price = float(fu["text"].strip())
            if price > s["entry_price"]:
                s["ch_outcome"] = "PROFIT"
                s["ch_exit"] = price

print(f"Parsed {len(signals)} stock option signals")

# ── Load Upstox instrument master for equity keys ─────────────────
import gzip

eq_url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE_EQ.json.gz"
r = requests.get(eq_url)
if r.status_code != 200:
    # Try complete
    eq_url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
    r = requests.get(eq_url)

try:
    eq_master = json.loads(gzip.decompress(r.content))
except:
    eq_master = json.loads(r.content)

print(f"Master: {len(eq_master)} instruments")

# Build symbol -> instrument_key map for equities
eq_keys = {}
for inst in eq_master:
    if not isinstance(inst, dict):
        continue
    seg = inst.get("segment", "")
    if seg != "NSE_EQ":
        continue
    sym = (inst.get("trading_symbol") or "").upper()
    ikey = inst.get("instrument_key", "")
    if sym and ikey:
        eq_keys[sym] = ikey
        # Also map asset_symbol
        asym = (inst.get("asset_symbol") or "").upper()
        if asym:
            eq_keys[asym] = ikey

print(f"Equity symbols mapped: {len(eq_keys)}")

def get_equity_key(symbol):
    """Resolve channel symbol to Upstox equity instrument key."""
    # Direct match
    if symbol in eq_keys:
        return eq_keys[symbol]
    # Try mapped name
    mapped = SYMBOL_MAP.get(symbol, symbol)
    if mapped in eq_keys:
        return eq_keys[mapped]
    # Try with EQ suffix
    if f"{symbol}EQ" in eq_keys:
        return eq_keys[f"{symbol}EQ"]
    return None

# ── Fetch candles and analyze ─────────────────────────────────────
candle_cache = {}
api_calls = 0

def fetch_stock_candles(ikey, date_str):
    global api_calls
    cache_key = f"{ikey}:{date_str}"
    if cache_key in candle_cache:
        return candle_cache[cache_key]

    enc = urllib.parse.quote(ikey, safe="")
    url = f"https://api.upstox.com/v2/historical-candle/{enc}/1minute/{date_str}/{date_str}"
    try:
        r = requests.get(url, headers={"Accept": "application/json"})
        api_calls += 1
        if api_calls % 3 == 0:
            time.sleep(0.3)
        d = r.json()
        candles = d.get("data", {}).get("candles", [])
        candle_cache[cache_key] = candles
        return candles
    except Exception as e:
        print(f"  API error for {ikey}: {e}")
        return []

results = []
no_key = set()

for i, sig in enumerate(signals):
    ikey = get_equity_key(sig["symbol"])
    if not ikey:
        no_key.add(sig["symbol"])
        results.append({**sig, "verdict": "NO_EQUITY_KEY", "stock_move": 0, "est_pnl": 0})
        continue

    sig_dt = datetime.fromisoformat(sig["ts"])
    date_str = sig_dt.strftime("%Y-%m-%d")
    sig_time = sig_dt.strftime("%H:%M")

    candles = fetch_stock_candles(ikey, date_str)
    if not candles:
        results.append({**sig, "verdict": "NO_CANDLES", "stock_move": 0, "est_pnl": 0})
        continue

    # Find stock price at signal time and track movement
    stock_at_signal = None
    stock_high_after = None
    stock_low_after = None
    stock_at_close = None

    for c in candles:
        c_ts = c[0]
        c_open, c_high, c_low, c_close = c[1], c[2], c[3], c[4]
        try:
            c_time = datetime.fromisoformat(c_ts.replace("Z", "+00:00")).astimezone(IST).strftime("%H:%M")
        except:
            continue

        if c_time >= sig_time and stock_at_signal is None:
            stock_at_signal = c_open

        if stock_at_signal is not None:
            if stock_high_after is None or c_high > stock_high_after:
                stock_high_after = c_high
            if stock_low_after is None or c_low < stock_low_after:
                stock_low_after = c_low
            stock_at_close = c_close

    if stock_at_signal is None:
        results.append({**sig, "verdict": "NO_PRICE_AT_TIME", "stock_move": 0, "est_pnl": 0})
        continue

    # For PE: profit when stock drops. For CE: profit when stock rises.
    if sig["option_type"] == "PE":
        favorable_move = stock_at_signal - stock_low_after  # stock dropped this much
        adverse_move = stock_high_after - stock_at_signal  # stock rose this much
    else:  # CE
        favorable_move = stock_high_after - stock_at_signal  # stock rose
        adverse_move = stock_at_signal - stock_low_after  # stock dropped

    # Estimate: option premium moves ~50-70% of stock move for ATM options
    # Use the channel's own entry/SL/target to determine risk:reward
    risk = sig["entry_price"] - sig["sl"]  # risk per lot
    reward = sig["targets"][0] - sig["entry_price"]  # reward to T1

    # Stock move needed for SL: risk / 0.5 (delta approximation)
    # Stock move needed for T1: reward / 0.5
    delta_est = 0.5  # ATM delta
    stock_move_for_sl = risk / delta_est
    stock_move_for_t1 = reward / delta_est

    # Walk candles minute by minute to see which hits first
    sl_hit_first = False
    t1_hit_first = False
    exit_stock = stock_at_close

    cum_fav = 0
    cum_adv = 0
    prev_close = stock_at_signal

    for c in candles:
        c_ts = c[0]
        c_open, c_high, c_low, c_close = c[1], c[2], c[3], c[4]
        try:
            c_time = datetime.fromisoformat(c_ts.replace("Z", "+00:00")).astimezone(IST).strftime("%H:%M")
        except:
            continue
        if c_time < sig_time:
            continue

        if sig["option_type"] == "PE":
            # Stock dropping is good
            if stock_at_signal - c_low >= stock_move_for_t1 and not sl_hit_first:
                t1_hit_first = True
                break
            if c_high - stock_at_signal >= stock_move_for_sl and not t1_hit_first:
                sl_hit_first = True
                break
        else:  # CE
            if c_high - stock_at_signal >= stock_move_for_t1 and not sl_hit_first:
                t1_hit_first = True
                break
            if stock_at_signal - c_low >= stock_move_for_sl and not t1_hit_first:
                sl_hit_first = True
                break

    if t1_hit_first:
        verdict = "T1_HIT"
        est_pnl = reward  # option premium gain
    elif sl_hit_first:
        verdict = "SL_HIT"
        est_pnl = -risk  # option premium loss
    else:
        # Neither hit — estimate from stock close
        if sig["option_type"] == "PE":
            stock_delta = stock_at_signal - stock_at_close
        else:
            stock_delta = stock_at_close - stock_at_signal
        est_pnl = stock_delta * delta_est  # approximate option P&L
        verdict = "EOD_WIN" if est_pnl > 0 else "EOD_LOSS"

    results.append({
        **sig,
        "verdict": verdict,
        "stock_at_signal": stock_at_signal,
        "stock_move_fav": favorable_move,
        "stock_move_adv": adverse_move,
        "est_pnl": est_pnl,
        "stock_for_sl": stock_move_for_sl,
        "stock_for_t1": stock_move_for_t1,
    })

    if (i + 1) % 20 == 0:
        print(f"  Processed {i+1}/{len(signals)}... (API calls: {api_calls})")

# ── Print results ─────────────────────────────────────────────────
print(f"\n{'='*110}")
print("  CANDLE-VERIFIED RESULTS — DT PAID CHANNEL OCT 2026")
print(f"{'='*110}")

if no_key:
    print(f"\n  Could not resolve equity key for: {no_key}")

by_date = defaultdict(list)
for r in results:
    by_date[r["ts"][:10]].append(r)

total_pnl = 0
wins = losses = errors = 0

for day in sorted(by_date):
    day_results = by_date[day]
    day_pnl = sum(r["est_pnl"] for r in day_results)
    total_pnl += day_pnl
    print(f"\n  ── {day} ({len(day_results)} signals, est day PnL: {day_pnl:+.1f} pts) ──")
    for r in day_results:
        ts = r["ts"][11:16]
        v = r["verdict"]
        ch = r["ch_outcome"]
        pnl = r["est_pnl"]
        wait = " [W]" if r["has_wait"] else ""
        stock_info = ""
        if "stock_at_signal" in r:
            stock_info = f" stock@{r['stock_at_signal']:.0f}"

        if v in ("T1_HIT", "EOD_WIN"):
            wins += 1
        elif v in ("SL_HIT", "EOD_LOSS"):
            losses += 1
        else:
            errors += 1

        match = "✓" if (v in ("T1_HIT", "EOD_WIN") and ch == "PROFIT") or (v == "SL_HIT" and ch == "SL_HIT") else "✗" if ch != "UNKNOWN" else "?"
        print(f"    {ts} | {r['symbol']:<12} {r['strike']:>6} {r['option_type']} | "
              f"E:{r['entry_price']:>6} SL:{r['sl']:>6} T1:{r['targets'][0]:>6} | "
              f"Candle:{v:<10} Ch:{ch:<10} | PnL:{pnl:>+7.1f}{wait}{stock_info} {match}")

# ── Summary ───────────────────────────────────────────────────────
print(f"\n{'='*110}")
print("  SUMMARY")
print(f"{'='*110}")
print(f"  Total signals: {len(signals)}")
print(f"  Verified: {wins + losses} (wins: {wins}, losses: {losses})")
print(f"  Errors/unresolved: {errors}")
if (wins + losses) > 0:
    print(f"  Win rate (candle-verified): {wins*100/(wins+losses):.1f}%")
print(f"  Estimated total P&L (points): {total_pnl:+,.1f}")

# Channel vs Reality comparison
ch_profit = sum(1 for r in results if r["ch_outcome"] == "PROFIT")
ch_sl = sum(1 for r in results if r["ch_outcome"] == "SL_HIT")
ch_unknown = sum(1 for r in results if r["ch_outcome"] == "UNKNOWN")
actual_win = sum(1 for r in results if r["verdict"] in ("T1_HIT", "EOD_WIN"))
actual_loss = sum(1 for r in results if r["verdict"] in ("SL_HIT", "EOD_LOSS"))

print(f"\n  Channel says: {ch_profit} profit, {ch_sl} SL, {ch_unknown} unreported")
print(f"  Candles say:  {actual_win} wins, {actual_loss} losses")

# Among the UNKNOWN ones (unreported by channel), what happened?
unknown_results = [r for r in results if r["ch_outcome"] == "UNKNOWN"]
unk_wins = sum(1 for r in unknown_results if r["verdict"] in ("T1_HIT", "EOD_WIN"))
unk_losses = sum(1 for r in unknown_results if r["verdict"] in ("SL_HIT", "EOD_LOSS"))
unk_pnl = sum(r["est_pnl"] for r in unknown_results)
print(f"\n  UNREPORTED signals ({len(unknown_results)}):")
print(f"    Wins: {unk_wins}, Losses: {unk_losses}, PnL: {unk_pnl:+,.1f} pts")
if (unk_wins + unk_losses) > 0:
    print(f"    Win rate: {unk_wins*100/(unk_wins+unk_losses):.1f}%")

# Daily breakdown
print(f"\n  Daily breakdown:")
running = 0
win_days = loss_days = 0
for day in sorted(by_date):
    dr = [r for r in by_date[day] if r["verdict"] not in ("NO_EQUITY_KEY", "NO_CANDLES", "NO_PRICE_AT_TIME")]
    dp = sum(r["est_pnl"] for r in dr)
    dw = sum(1 for r in dr if r["est_pnl"] > 0)
    dl = sum(1 for r in dr if r["est_pnl"] <= 0)
    running += dp
    if dp > 0: win_days += 1
    elif dp < 0: loss_days += 1
    print(f"    {day}  {len(dr):>2} traded  W:{dw} L:{dl}  PnL:{dp:>+8.1f}  Running:{running:>+10.1f}")

print(f"\n  Win days: {win_days}/{win_days+loss_days}, Loss days: {loss_days}")
if len(by_date) > 0:
    print(f"  Avg daily PnL: {total_pnl/len(by_date):+.1f} pts")

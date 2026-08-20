#!/usr/bin/env python3
"""Verify channel signals using UNDERLYING spot/index candle data.

Expired option contracts are removed from Upstox master, so we verify
using the underlying instrument (NIFTY index, stock price, etc.).

For CE: did the underlying go UP enough after signal time?
For PE: did the underlying go DOWN enough after signal time?

Option P&L estimated from spot movement x delta.

Usage: .venv/bin/python3 scripts/verify_underlying.py /tmp/channel_msgs.txt
"""
import sys, os, re, time as _time, json, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    import config
    from src.broker.upstox_data import UpstoxData, load_cached_token
except ImportError:
    print("ERROR: Run from Trading-Buddy root with .venv/bin/python3")
    sys.exit(1)

IST = ZoneInfo("Asia/Kolkata")
LOTS = 3

LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 40,
    "MIDCPNIFTY": 50,
    "RELIANCE": 250, "TCS": 175, "INFY": 600, "INDIGO": 300,
    "TRENT": 625, "PAYTM": 1600, "KEI": 200, "BSE": 250,
    "TITAN": 375, "BRITANNIA": 200, "ABB": 250, "HAL": 300,
    "EICHERMOT": 150, "SIEMENS": 275, "MCX": 900, "POLYCAB": 200,
    "LTM": 200, "LTIM": 200, "PERSISTENT": 200, "DIXON": 200,
    "APOLLOHOSP": 250, "BAJAJAUTO": 250, "CUMMINSIND": 400,
    "MFSL": 1600, "PIIND": 300, "LT": 300, "MARUTI": 100,
    "RADICO": 1200, "BHARTIARTL": 475,
    "AMBER": 200, "SUPREMEIND": 300,
}
DEFAULT_LOT = 400

# Underlying instrument keys (always available, never expire)
UNDERLYING_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
}

# --- Parse signals from channel dump ---
msg_file = sys.argv[1] if len(sys.argv) > 1 else "/tmp/channel_msgs.txt"
print(f"Reading messages from {msg_file}...")

with open(msg_file, "r") as f:
    raw = f.read()

blocks = raw.split("\n---\n")
messages = []
for block in blocks:
    block = block.strip()
    if not block:
        continue
    m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] (.*)", block, re.DOTALL)
    if m:
        messages.append({"ts": m.group(1), "text": m.group(2).strip()})

print(f"Parsed {len(messages)} messages")

SIGNAL_RE = re.compile(
    r"^(?:BANK\s*NIFTY|NIFTY|SENSEX|FINNIFTY|MIDCPNIFTY|"
    r"[A-Z]{2,20})\s+\d+\s+(?:CE|PE)\s*$",
    re.MULTILINE,
)
ENTRY_RE = re.compile(r"(?:ABOVE|NEAR|Above|Near)\s+(\d+[\.\d]*)", re.IGNORECASE)
TGT_RE = re.compile(r"(?:TGT|TARGET|Tgt)\s+(\d+[\s/\d\+\.\-]+)", re.IGNORECASE)
SL_RE = re.compile(r"(?:SL|Sl|sl|STOPLOSS|Stop\s*loss)\s+(?:below\s+)?(\d+[\.\d]*)", re.IGNORECASE)


def parse_symbol(raw_sym):
    parts = raw_sym.strip().split()
    if parts[0] == "BANK" and len(parts) >= 4:
        return "BANKNIFTY", int(parts[2]), parts[3]
    elif len(parts) >= 3:
        return parts[0], int(parts[1]), parts[2]
    return None, None, None


signals = []
for i in range(len(messages)):
    text = messages[i]["text"]
    clean = re.sub(r"[*\U0001F600-\U0001FAFF☀-➿❤]+", "", text).strip()

    if SIGNAL_RE.search(clean):
        sig_match = SIGNAL_RE.search(clean)
        symbol = sig_match.group(0).strip()
        ts = messages[i]["ts"]

        entry = None
        tgts = []
        sl = None

        window = [messages[i]["text"]]
        for j in range(1, min(8, len(messages) - i)):
            window.append(messages[i + j]["text"])
        combined = "\n".join(window)

        em = ENTRY_RE.search(combined)
        if em:
            entry = float(em.group(1))

        tm = TGT_RE.search(combined)
        if tm:
            tgt_str = tm.group(1)
            tgts = [float(x) for x in re.findall(r"(\d+[\.\d]*)", tgt_str)]

        sm = SL_RE.search(combined)
        if sm:
            sl_val = float(sm.group(1))
            if entry and sl_val < 30:
                sl = entry - sl_val
            else:
                sl = sl_val

        if entry and tgts and sl:
            underlying, strike, opt_type = parse_symbol(symbol)
            if underlying and strike and opt_type:
                signals.append({
                    "ts": ts, "date": ts[:10], "symbol": symbol,
                    "underlying": underlying, "strike": strike,
                    "opt_type": opt_type, "entry": entry,
                    "tgts": tgts, "sl": sl,
                })

# Deduplicate
seen = set()
unique = []
for s in signals:
    key = f"{s['ts']}_{s['symbol']}"
    if key not in seen:
        seen.add(key)
        unique.append(s)
signals = unique

# Skip commodities (no underlying in Upstox index)
COMMODITY = {"CRUDEOIL", "CRUDE", "GOLD", "SILVER", "NATURALGAS"}
signals = [s for s in signals if s["underlying"] not in COMMODITY]

# 1 signal per underlying per day (like real trading)
day_taken = defaultdict(set)
filtered = []
for s in signals:
    if s["underlying"] not in day_taken[s["date"]]:
        day_taken[s["date"]].add(s["underlying"])
        filtered.append(s)
signals = filtered

print(f"\n{len(signals)} equity signals to verify (1 per underlying per day, no commodities)")

# --- Connect to Upstox ---
token = load_cached_token()
if not token:
    print("\nERROR: No Upstox token found. Session may need refresh.")
    print("Run auto-login or check data/upstox_token.json")
    sys.exit(1)

client = UpstoxData()
print("Upstox connected. Loading instrument master...")
master = client._load_master()
print(f"Master loaded: {len(master)} instruments")

# Build stock key lookup
stock_keys = {}
for inst in master:
    if inst.get("segment") == "NSE_EQ":
        tsym = (inst.get("trading_symbol") or "").upper()
        if tsym:
            stock_keys[tsym] = inst.get("instrument_key")

candle_cache = {}


def get_candles(inst_key, date_str):
    cache_key = f"{inst_key}|{date_str}"
    if cache_key in candle_cache:
        return candle_cache[cache_key]

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    from_dt = dt.replace(hour=9, minute=0)
    to_dt = dt.replace(hour=15, minute=35)

    try:
        candles = client.historical_data(inst_key, from_dt, to_dt, "minute")
        _time.sleep(0.3)  # rate limit
    except Exception as e:
        print(f"    API error for {inst_key} on {date_str}: {e}")
        candles = []

    candle_cache[cache_key] = candles
    return candles


def parse_candle_time(c):
    """Extract datetime from candle, handling various formats."""
    ct = c.get("date") or c.get("timestamp") or c.get("time")
    if isinstance(ct, str):
        try:
            ct_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            if ct_dt.tzinfo:
                ct_dt = ct_dt.astimezone(IST).replace(tzinfo=None)
            return ct_dt
        except Exception:
            return None
    elif isinstance(ct, datetime):
        return ct
    return None


def verify_signal(s, candles):
    """Verify option signal outcome using underlying candles + delta approximation.

    For CE: underlying UP → option premium rises
    For PE: underlying DOWN → option premium rises
    """
    signal_dt = datetime.strptime(s["ts"], "%Y-%m-%d %H:%M")
    opt_type = s["opt_type"]
    entry = s["entry"]
    tgt1 = s["tgts"][0]
    sl = s["sl"]
    strike = s["strike"]

    pts_to_tgt = tgt1 - entry
    pts_to_sl = entry - sl

    if pts_to_tgt <= 0 or pts_to_sl <= 0:
        return "BAD_SIGNAL", 0, "invalid entry/tgt/sl"

    # Find spot at signal time
    spot_at_signal = None
    signal_idx = -1
    for idx, c in enumerate(candles):
        ct_dt = parse_candle_time(c)
        if ct_dt is None:
            continue
        if ct_dt >= signal_dt:
            spot_at_signal = c["close"]
            signal_idx = idx
            break

    if spot_at_signal is None or signal_idx < 0:
        # Try the candle just before signal time
        for idx in range(len(candles) - 1, -1, -1):
            ct_dt = parse_candle_time(candles[idx])
            if ct_dt and ct_dt <= signal_dt:
                spot_at_signal = candles[idx]["close"]
                signal_idx = idx
                break

    if spot_at_signal is None:
        return "NO_DATA", 0, f"no candle near signal time"

    # Estimate delta from moneyness
    moneyness = spot_at_signal - strike
    if opt_type == "PE":
        moneyness = -moneyness

    if abs(moneyness) < 100:
        delta = 0.50
    elif moneyness > 0:
        delta = min(0.80, 0.50 + moneyness / 400)
    else:
        delta = max(0.15, 0.50 + moneyness / 400)

    # Walk candles from signal time to EOD
    first_hit = None

    for c in candles[signal_idx:]:
        high = c["high"]
        low = c["low"]

        if opt_type == "CE":
            favorable = (high - spot_at_signal) * delta
            adverse = (spot_at_signal - low) * delta
        else:
            favorable = (spot_at_signal - low) * delta
            adverse = (high - spot_at_signal) * delta

        if adverse >= pts_to_sl and first_hit is None:
            first_hit = "SL_HIT"

        if favorable >= pts_to_tgt and first_hit is None:
            first_hit = "TGT1_HIT"
            break

        if first_hit == "SL_HIT":
            break

    if first_hit is None:
        last_c = candles[-1]
        if opt_type == "CE":
            eod_move = (last_c["close"] - spot_at_signal) * delta
        else:
            eod_move = (spot_at_signal - last_c["close"]) * delta
        return "EOD", eod_move, f"spot={spot_at_signal:.0f} Δ={delta:.2f} eod_move={eod_move:+.0f}"

    if first_hit == "TGT1_HIT":
        return "TGT1_HIT", pts_to_tgt, f"spot={spot_at_signal:.0f} Δ={delta:.2f}"
    else:
        return "SL_HIT", -pts_to_sl, f"spot={spot_at_signal:.0f} Δ={delta:.2f}"


# --- Run verification ---
print()
print("=" * 110)
print("SIGNAL VERIFICATION — UNDERLYING SPOT/INDEX CANDLE DATA")
print("=" * 110)
print()
print("Method: Fetch 1-min candles of underlying (NIFTY spot, stock price).")
print("        Estimate option P&L using delta approximation.")
print("        CE → spot UP = win. PE → spot DOWN = win.")
print()

header = (f"  {'#':<4} {'Time':<17} {'Symbol':<25} {'Type':>4} {'Entry':>6} {'TGT1':>6} "
          f"{'SL':>6} {'Result':<10} {'Pts':>7} {'P&L':>10} {'Note'}")
print(header)
print("  " + "─" * 108)

daily = defaultdict(lambda: {"pnl": 0, "cap_pnl": 0, "trades": 0, "wins": 0, "losses": 0})
total_verified = 0
total_wins = 0
total_losses = 0
total_pnl = 0
total_cap_pnl = 0
no_data = 0

for idx, s in enumerate(signals):
    underlying = s["underlying"]

    # Get underlying instrument key
    if underlying in UNDERLYING_KEYS:
        inst_key = UNDERLYING_KEYS[underlying]
    else:
        inst_key = stock_keys.get(underlying)
        if not inst_key:
            print(f"  {idx+1:<4} {s['ts']:<17} {s['symbol']:<25} {s['opt_type']:>4} "
                  f"{s['entry']:>6.0f} {s['tgts'][0]:>6.0f} {s['sl']:>6.0f} "
                  f"{'SKIP':<10} {'':>7} {'':>10} underlying not found")
            no_data += 1
            continue

    candles = get_candles(inst_key, s["date"])
    if not candles:
        print(f"  {idx+1:<4} {s['ts']:<17} {s['symbol']:<25} {s['opt_type']:>4} "
              f"{s['entry']:>6.0f} {s['tgts'][0]:>6.0f} {s['sl']:>6.0f} "
              f"{'NO_DATA':<10} {'':>7} {'':>10} no candles returned")
        no_data += 1
        continue

    result, points, note = verify_signal(s, candles)

    lot_size = LOT_SIZES.get(underlying, DEFAULT_LOT)
    qty = lot_size * LOTS
    pnl = points * qty

    if result == "TGT1_HIT":
        cap_pnl = min(pnl, 2000)
    elif result == "SL_HIT":
        cap_pnl = max(pnl, -8000)  # ₹8K per-trade cap
    else:
        cap_pnl = min(pnl, 2000) if pnl > 0 else max(pnl, -8000)

    won = result == "TGT1_HIT"
    lost = result == "SL_HIT"
    icon = "W" if won else ("L" if lost else "~")

    total_verified += 1
    total_pnl += pnl
    total_cap_pnl += cap_pnl
    if won:
        total_wins += 1
    if lost:
        total_losses += 1

    d = daily[s["date"]]
    d["pnl"] += pnl
    d["cap_pnl"] += cap_pnl
    d["trades"] += 1
    if won:
        d["wins"] += 1
    if lost:
        d["losses"] += 1

    print(f"  {idx+1:<4} {s['ts']:<17} {s['symbol']:<25} {s['opt_type']:>4} "
          f"{s['entry']:>6.0f} {s['tgts'][0]:>6.0f} {s['sl']:>6.0f} "
          f"[{icon}] {result:<8} {points:>+6.0f} ₹{pnl:>+9,.0f} {note}")

# --- Summary ---
print()
print("=" * 110)
print("VERIFIED RESULTS (Underlying Spot Data + Delta Estimation)")
print("=" * 110)
print()
print(f"  Total signals parsed:  {len(signals)}")
print(f"  Verified with data:    {total_verified}")
print(f"  No data / skipped:     {no_data}")
print()

if total_verified > 0:
    wr = total_wins / total_verified * 100
    print(f"  TGT1 hit (WIN):        {total_wins} ({wr:.0f}%)")
    print(f"  SL hit (LOSS):         {total_losses} ({total_losses/total_verified*100:.0f}%)")
    print(f"  EOD exit (neutral):    {total_verified - total_wins - total_losses}")
    print()
    n_days = len(daily)
    print(f"  {'Metric':<30} {'Full Ride':>15} {'₹2K Cap':>15}")
    print(f"  {'─'*30} {'─'*15} {'─'*15}")
    print(f"  {'Total P&L':<30} {'₹{:+,}'.format(int(total_pnl)):>15} {'₹{:+,}'.format(int(total_cap_pnl)):>15}")
    if n_days > 0:
        print(f"  {'Avg P&L/day':<30} {'₹{:+,}'.format(int(total_pnl/n_days)):>15} {'₹{:+,}'.format(int(total_cap_pnl/n_days)):>15}")
    print()

    print(f"  DAILY BREAKDOWN")
    print(f"  {'Date':<12} {'Trades':>7} {'Wins':>6} {'Loss':>6} {'Full P&L':>12} {'₹2K Cap':>12}")
    print(f"  {'─'*12} {'─'*7} {'─'*6} {'─'*6} {'─'*12} {'─'*12}")

    green_full = 0
    green_cap = 0
    for date in sorted(daily.keys()):
        d = daily[date]
        mark = "+" if d["pnl"] > 0 else "-"
        if d["pnl"] > 0:
            green_full += 1
        if d["cap_pnl"] > 0:
            green_cap += 1
        print(f"  {date:<12} {d['trades']:>7} {d['wins']:>6} {d['losses']:>6} "
              f"{'₹{:+,}'.format(int(d['pnl'])):>12} "
              f"{'₹{:+,}'.format(int(d['cap_pnl'])):>12} {mark}")

    print()
    print(f"  Green days (Full):   {green_full}/{n_days} ({green_full/n_days*100:.0f}%)")
    print(f"  Green days (₹2K):    {green_cap}/{n_days} ({green_cap/n_days*100:.0f}%)")

print()
print("  NOTE: This uses delta approximation from underlying spot data.")
print("  Real option prices depend on IV, time decay, and liquidity.")
print("  Treat these as directional estimates, not exact P&L.")
print("=" * 110)

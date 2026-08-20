#!/usr/bin/env python3
"""Verify today's CH2 signals against actual option candle data.

For each signal parsed from the channel, fetch the real 1-min candles
for that specific option contract and check if TGT1 or SL was hit first.

Usage: .venv/bin/python3 scripts/verify_today_ch2.py /tmp/channel_msgs.txt
"""
import sys, os, re, time as _time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config
    from src.broker.upstox_data import UpstoxData, load_cached_token
except ImportError:
    print("ERROR: Run from Trading-Buddy root with .venv/bin/python3")
    sys.exit(1)

IST = ZoneInfo("Asia/Kolkata")
TODAY = datetime.now(IST).strftime("%Y-%m-%d")

LOTS = 3
PROFIT_CAP = 2000
MAX_LOSS_CAP = 8000

LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 40,
    "MIDCPNIFTY": 50, "MFSL": 1600, "MUTHOOTFIN": 1000,
    "RELIANCE": 250, "TCS": 175, "INFY": 600, "INDIGO": 300,
    "TRENT": 625, "PAYTM": 1600, "KEI": 200, "BSE": 250,
    "TITAN": 375, "BRITANNIA": 200, "ABB": 250, "HAL": 300,
}
DEFAULT_LOT = 400

# --- Parser (same as backtest_ch2.py) ---
SKIP_COMMODITIES = {"CRUDEOIL", "CRUDE", "GOLD", "SILVER", "NATURALGAS", "GOLF"}
SKIP_NOISE = {"DISCLAIMER", "WATCH LIST", "IMPORTANT", "FAKE ALERT",
              "OFFER", "APPLICATION", "FOLLOW THIS", "PLS READ",
              "PERFORMANCE", "MEMBERS SEND", "CONGRATULATIONS"}

SYMBOL_RE = re.compile(
    r'(?:(?:Intra|positional|Hazing|Note)[/\s]*)*'
    r'((?:BANK\s*NIFTY|NIFTY|SENSEX|FINNIFTY|MIDCPNIFTY|[A-Za-z&]{2,20}))'
    r'\s+(\d+)\s+(CE|PE)',
    re.IGNORECASE | re.MULTILINE,
)
ENTRY_RE = re.compile(
    r'(?:ABOVE|NEAR|Entry\s+near|BUY\s*@|CMP)\s*[:\-]?\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)
TGT_RE = re.compile(r'(?:TGT|TARGET)\s*[:\-]?\s*([\d\s,/.+\-]+)', re.IGNORECASE)
SL_RE = re.compile(
    r'(?:^|[^A-Z])(?:SL|Stop\s*loss)\s*(?:bel\w*\s*|use\s*)?(\d+(?:\.\d+)?(?:\s*[-–]\s*\d+)?)\s*(point)?',
    re.IGNORECASE,
)
NOT_ACTIVE_RE = re.compile(r'not\s+active\s+avoid', re.I)


def extract_sl(sl_match, trigger):
    raw = sl_match.group(1).strip()
    is_pts = sl_match.group(2) is not None
    if '-' in raw or '–' in raw:
        parts = re.split(r'[-–]', raw)
        val = float(parts[0].strip())
    else:
        val = float(raw)
    if is_pts and trigger > 0:
        return trigger - val
    if val <= 25 and trigger > 50:
        return trigger - val
    return val


def extract_targets(tgt_match):
    raw = tgt_match.group(1)
    nums = re.findall(r'\d+(?:\.\d+)?', raw)
    return [float(n) for n in nums if float(n) > 0]


# --- Read messages ---
msg_file = sys.argv[1] if len(sys.argv) > 1 else "/tmp/channel_msgs.txt"
with open(msg_file) as f:
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

# Filter to today only
today_msgs = [m for m in messages if m["ts"].startswith(TODAY)]
print(f"Total messages: {len(messages)}, today: {len(today_msgs)}")

# --- Parse today's signals ---
signals = []
pending = None

for msg in today_msgs:
    text = msg["text"]
    ts = msg["ts"]
    clean = text.replace("**", "")
    clean = re.sub(r'[\U0001F600-\U0001FAFF☀-➿❤️‍]+', ' ', clean).strip()

    if len(clean) < 5:
        continue
    upper = clean.upper()
    if any(skip in upper for skip in SKIP_NOISE):
        continue
    if NOT_ACTIVE_RE.search(upper):
        pending = None
        continue

    sym_match = SYMBOL_RE.search(clean)
    entry_match = ENTRY_RE.search(clean)
    tgt_match = TGT_RE.search(clean)
    sl_match = SL_RE.search(clean)

    has_sym = sym_match is not None
    has_tgt = tgt_match is not None
    has_sl = sl_match is not None

    if has_sym:
        raw_sym = sym_match.group(1).upper().strip()
        raw_sym = re.sub(r'\s+', ' ', raw_sym)
        if raw_sym == "BANK NIFTY":
            raw_sym = "BANKNIFTY"
        if raw_sym in SKIP_COMMODITIES:
            continue

        strike = float(sym_match.group(2))
        opt_type = sym_match.group(3).upper()
        trigger = float(entry_match.group(1)) if entry_match else 0.0

        if has_tgt and has_sl:
            targets = extract_targets(tgt_match)
            sl = extract_sl(sl_match, trigger)
            if sl <= 0 or not targets or trigger <= 0:
                continue
            signals.append({
                "ts": ts, "symbol": raw_sym, "strike": strike,
                "opt_type": opt_type, "entry": trigger,
                "targets": targets, "sl": sl,
            })
            pending = None
        else:
            pending = {
                "ts": ts, "symbol": raw_sym, "strike": strike,
                "opt_type": opt_type, "trigger": trigger,
            }
        continue

    if not has_sym and pending and (has_tgt or has_sl):
        if has_tgt and has_sl:
            targets = extract_targets(tgt_match)
            trigger = pending["trigger"]
            if not trigger and entry_match:
                trigger = float(entry_match.group(1))
            sl = extract_sl(sl_match, trigger)
            if sl > 0 and targets and trigger > 0:
                signals.append({
                    "ts": pending["ts"], "symbol": pending["symbol"],
                    "strike": pending["strike"], "opt_type": pending["opt_type"],
                    "entry": trigger, "targets": targets, "sl": sl,
                })
            pending = None

# Dedup
seen = set()
unique = []
for s in signals:
    key = f"{s['ts']}_{s['symbol']}_{s['strike']}_{s['opt_type']}"
    if key not in seen:
        seen.add(key)
        unique.append(s)
signals = unique

print(f"Parsed {len(signals)} signals for today ({TODAY})")
if not signals:
    print("No signals found for today!")
    sys.exit(0)

# --- Connect Upstox ---
token = load_cached_token()
if not token:
    print("ERROR: No Upstox token")
    sys.exit(1)

client = UpstoxData()
print("Loading instrument master...")
master = client._load_master()

# Build option lookup: symbol|strike|CE/PE -> instrument_key
EXCHANGE_MAP = {
    "NIFTY": "NSE_FO", "BANKNIFTY": "NSE_FO", "FINNIFTY": "NSE_FO",
    "MIDCPNIFTY": "NSE_FO", "SENSEX": "BSE_FO",
}

opt_keys = {}
for inst in master:
    seg = inst.get("segment", "")
    if seg not in ("NSE_FO", "BSE_FO"):
        continue
    if inst.get("instrument_type") not in ("CE", "PE"):
        continue
    tsym = (inst.get("trading_symbol") or "").upper()
    strike = inst.get("strike_price", 0)
    opt_type = inst.get("instrument_type", "")
    name = inst.get("name", "").upper().replace(" ", "")
    if not name:
        name = re.sub(r'\d.*', '', tsym)
    key = f"{name}|{int(strike)}|{opt_type}"
    opt_keys[key] = inst.get("instrument_key")

print(f"Master: {len(master)} instruments, {len(opt_keys)} option contracts")

# --- Verify each signal ---
print()
print("=" * 120)
print(f"CH2 SIGNAL VERIFICATION — {TODAY} — ACTUAL OPTION CANDLES")
print("=" * 120)
print()

header = (f"  {'#':<3} {'Time':<6} {'Symbol':<12} {'Strike':>7} {'Type':>3} "
          f"{'Entry':>6} {'TGT1':>6} {'SL':>6} "
          f"{'High':>6} {'Low':>6} {'Result':<8} {'P&L':>10}")
print(header)
print("  " + "─" * 118)

total_pnl = 0
total_cap = 0
wins = 0
losses = 0
no_data = 0

for idx, s in enumerate(signals):
    sym = s["symbol"]
    strike = int(s["strike"])
    opt_type = s["opt_type"]
    entry = s["entry"]
    tgt1 = s["targets"][0]
    sl = s["sl"]
    sig_time = s["ts"]

    # Find instrument key
    lookup = f"{sym}|{strike}|{opt_type}"
    inst_key = opt_keys.get(lookup)

    if not inst_key:
        # Try alternate name formats
        alt_names = [sym]
        if sym == "BANKNIFTY":
            alt_names.append("BANKNIFTY")
        for alt in alt_names:
            alt_key = f"{alt}|{strike}|{opt_type}"
            if alt_key in opt_keys:
                inst_key = opt_keys[alt_key]
                break

    if not inst_key:
        print(f"  {idx+1:<3} {sig_time[11:]:<6} {sym:<12} {strike:>7} {opt_type:>3} "
              f"{entry:>6.0f} {tgt1:>6.0f} {sl:>6.0f} "
              f"{'':>6} {'':>6} {'NO_INST':<8} {'':>10}")
        no_data += 1
        continue

    # Parse signal time
    sig_hour, sig_min = int(sig_time[11:13]), int(sig_time[14:16])
    dt_today = datetime.now(IST).replace(hour=sig_hour, minute=sig_min, second=0, microsecond=0)

    # Fetch 1-min candles from signal time to 15:30
    from_dt = dt_today
    to_dt = datetime.now(IST).replace(hour=15, minute=30, second=0, microsecond=0)

    try:
        candles = client.historical_data(inst_key, from_dt, to_dt, "1minute")
        _time.sleep(0.3)
    except Exception as e:
        err = str(e)
        if "429" in err or "rate" in err.lower():
            _time.sleep(3)
            try:
                candles = client.historical_data(inst_key, from_dt, to_dt, "1minute")
            except Exception:
                candles = None
        else:
            candles = None

    if not candles:
        print(f"  {idx+1:<3} {sig_time[11:]:<6} {sym:<12} {strike:>7} {opt_type:>3} "
              f"{entry:>6.0f} {tgt1:>6.0f} {sl:>6.0f} "
              f"{'':>6} {'':>6} {'NO_DATA':<8} {'':>10}")
        no_data += 1
        continue

    # Walk candles to find if TGT1 or SL hit first
    max_high = 0
    min_low = 999999
    result = "OPEN"
    exit_price = entry

    for c in candles:
        h = c["high"]
        l = c["low"]
        max_high = max(max_high, h)
        min_low = min(min_low, l)

        if h >= tgt1 and l <= sl:
            # Both hit in same candle — use open to guess order
            if c["open"] >= entry:
                result = "TGT1"
                exit_price = tgt1
            else:
                result = "SL_HIT"
                exit_price = sl
            break
        elif h >= tgt1:
            result = "TGT1"
            exit_price = tgt1
            break
        elif l <= sl:
            result = "SL_HIT"
            exit_price = sl
            break

    if result == "OPEN":
        # Neither hit — use last candle close
        last_close = candles[-1]["close"]
        exit_price = last_close
        if last_close > entry:
            result = "PROFIT"
        else:
            result = "LOSS"

    # P&L
    lot_size = LOT_SIZES.get(sym, DEFAULT_LOT)
    qty = lot_size * LOTS
    pnl = (exit_price - entry) * qty

    if pnl > 0:
        cap_pnl = min(pnl, PROFIT_CAP)
    else:
        cap_pnl = max(pnl, -MAX_LOSS_CAP)

    total_pnl += pnl
    total_cap += cap_pnl

    if result in ("TGT1", "PROFIT"):
        wins += 1
        icon = "W"
    else:
        losses += 1
        icon = "L"

    print(f"  {idx+1:<3} {sig_time[11:]:<6} {sym:<12} {strike:>7} {opt_type:>3} "
          f"{entry:>6.0f} {tgt1:>6.0f} {sl:>6.0f} "
          f"{max_high:>6.0f} {min_low:>6.0f} [{icon}] {result:<5} ₹{pnl:>+9,.0f}")

# --- Summary ---
total = wins + losses
print()
print("=" * 120)
print(f"  TODAY'S RESULTS ({TODAY})")
print("=" * 120)
print()
print(f"  Total signals:   {len(signals)}")
print(f"  Verified:        {total}")
print(f"  No data:         {no_data}")
print()
if total > 0:
    wr = wins / total * 100
    print(f"  Winners:         {wins} ({wr:.0f}%)")
    print(f"  Losers:          {losses} ({100-wr:.0f}%)")
    print(f"  Win Rate:        {wr:.1f}%")
    print()
    print(f"  Full P&L:        ₹{total_pnl:+,.0f}")
    print(f"  Capped P&L:      ₹{total_cap:+,.0f}")
    print(f"  Avg P&L/trade:   ₹{total_pnl/total:+,.0f}")
print()
print("  Verified against ACTUAL 1-min option candle data from Upstox.")
print("=" * 120)

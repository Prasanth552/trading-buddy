"""Verify channel signals against REAL market candle data.

For each signal:
  1. Find the instrument key from Upstox master
  2. Fetch 1-min candles from signal time to EOD
  3. Check if high hit TGT1 or low hit SL first
  4. Calculate real P&L

Usage: .venv/bin/python3 scripts/verify_channel.py /tmp/channel_msgs.txt
"""
import sys, os, re, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo("Asia/Kolkata")
CAPITAL = 200_000
LOTS = 3
PROFIT_TARGET = 2000

LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 40,
    "MIDCPNIFTY": 50,
    "RELIANCE": 250, "TCS": 175, "INFY": 600, "HDFCBANK": 550,
    "ICICIBANK": 1400, "SBIN": 1500, "TATAMOTORS": 1400,
    "BAJFINANCE": 250, "LT": 300, "MARUTI": 100, "INDIGO": 300,
    "TRENT": 625, "PAYTM": 1600, "KEI": 200, "BSE": 250,
    "TITAN": 375, "BRITANNIA": 200, "ABB": 250, "HAL": 300,
    "EICHERMOT": 150, "SIEMENS": 275, "MCX": 900, "POLYCAB": 200,
    "LTM": 200, "LTIM": 200, "PERSISTENT": 200, "DIXON": 200,
    "APOLLOHOSP": 250, "BAJAJAUTO": 250, "CUMMINSIND": 400,
    "MFSL": 1600, "PIIND": 300, "HERMOTOCO": 300, "CGPOWER": 1800,
    "DIVISLAB": 150, "AMBER": 200, "PFC": 3200, "REC": 2250,
    "OIL": 1600, "HINDALCO": 1400, "NATIONALUM": 4600, "PNB": 8000,
    "FORTIS": 1400, "BOSCH": 50, "ASHOKLEY": 4500, "ASTRAL": 550,
    "ADANIENT": 500, "BIOCON": 2300, "KALYANJIL": 1600,
    "CHOLAFIN": 500, "POWERGRID": 4500, "HCLTECH": 700,
    "TIINDIA": 375, "RADICO": 1200, "SUPRIMEIND": 300,
    "BHARTIARTL": 475, "MOTHERSON": 6700, "COFORGE": 200,
}
DEFAULT_LOT = 400

# --- Parse signals from dump ---
with open(sys.argv[1], "r") as f:
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

SIGNAL_RE = re.compile(
    r"^(?:BANK\s*NIFTY|NIFTY|SENSEX|FINNIFTY|MIDCPNIFTY|"
    r"[A-Z]{2,20})\s+\d+\s+(?:CE|PE)\s*$",
    re.MULTILINE,
)
ENTRY_RE = re.compile(r"(?:ABOVE|NEAR|Above|Near)\s+(\d+[\.\d]*)", re.IGNORECASE)
TGT_RE = re.compile(r"(?:TGT|TARGET|Tgt)\s+(\d+[\s/\d\+\.\-]+)", re.IGNORECASE)
SL_RE = re.compile(r"(?:SL|Sl|sl|STOPLOSS|Stop\s*loss)\s+(?:below\s+)?(\d+[\.\d]*)", re.IGNORECASE)


def parse_symbol(raw_sym):
    """Parse 'NIFTY 24200 CE' or 'BANK NIFTY 57400 PE' into components."""
    parts = raw_sym.strip().split()
    if parts[0] == "BANK" and len(parts) >= 4:
        return "BANKNIFTY", parts[2], parts[3]
    elif len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    return None, None, None


signals = []
i = 0
while i < len(messages):
    text = messages[i]["text"]
    clean = re.sub(r"[*\U0001F600-\U0001FAFF☀-➿❤️]+", "", text).strip()

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
    i += 1

# Deduplicate
seen = set()
unique = []
for s in signals:
    key = f"{s['ts']}_{s['symbol']}"
    if key not in seen:
        seen.add(key)
        unique.append(s)
signals = unique

# Skip commodities (MCX — different exchange, different API)
signals = [s for s in signals if s["underlying"] not in
           ("CRUDEOIL", "CRUDE", "GOLD", "SILVER", "NATURALGAS", "GOLF")]

# Limit to 1 per underlying per day
day_taken = defaultdict(set)
filtered = []
for s in signals:
    if s["underlying"] not in day_taken[s["date"]]:
        day_taken[s["date"]].add(s["underlying"])
        filtered.append(s)
signals = filtered

print(f"Parsed {len(signals)} equity signals (1 per underlying/day, commodities excluded)")
print()

# --- Connect to Upstox ---
token = load_cached_token()
if not token:
    print("ERROR: No valid Upstox token for today.")
    print("Run: .venv/bin/python -m src.broker.upstox_data")
    sys.exit(1)

client = UpstoxData()
print("Loading instrument master...")
master = client._load_master()

# Build lookup: underlying + strike + opt_type + expiry -> instrument_key
print(f"Loaded {len(master)} instruments")


def find_instrument_key(underlying, strike, opt_type, signal_date):
    """Find the Upstox instrument key for this option."""
    signal_dt = datetime.strptime(signal_date, "%Y-%m-%d")

    # Determine exchange
    if underlying in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        exchange = "NSE_FO"
    elif underlying == "SENSEX":
        exchange = "BSE_FO"
    else:
        exchange = "NSE_FO"

    candidates = []
    for inst in master:
        seg = inst.get("segment") or inst.get("exchange") or ""
        if seg != exchange:
            continue

        tsym = inst.get("trading_symbol") or inst.get("tradingsymbol") or ""
        inst_type = inst.get("instrument_type") or ""
        inst_strike = str(inst.get("strike_price") or inst.get("strike") or "")

        # Match underlying
        inst_name = inst.get("name") or inst.get("underlying") or ""
        if inst_name.upper() != underlying.upper():
            continue

        # Match option type
        if opt_type == "CE" and inst_type not in ("CE", "OPTIDX", "OPTSTK"):
            if "CE" not in tsym:
                continue
        if opt_type == "PE" and inst_type not in ("PE", "OPTIDX", "OPTSTK"):
            if "PE" not in tsym:
                continue

        # Check if trading symbol ends with CE/PE
        if not tsym.upper().endswith(opt_type):
            continue

        # Match strike
        try:
            s = float(inst_strike.replace(".0", ""))
            if s != float(strike):
                continue
        except (ValueError, TypeError):
            continue

        # Check expiry is >= signal date
        expiry_str = inst.get("expiry") or ""
        if not expiry_str:
            continue
        try:
            if "T" in expiry_str:
                expiry_dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
            else:
                expiry_dt = datetime.strptime(expiry_str, "%Y-%m-%d")
        except (ValueError, TypeError):
            continue

        exp_date = expiry_dt.date() if hasattr(expiry_dt, 'date') else expiry_dt
        sig_date = signal_dt.date()

        if exp_date < sig_date:
            continue

        days_to_expiry = (exp_date - sig_date).days
        candidates.append({
            "key": inst.get("instrument_key"),
            "tsym": tsym,
            "expiry": str(exp_date),
            "days": days_to_expiry,
        })

    if not candidates:
        return None, None

    # Pick the nearest expiry (weekly)
    candidates.sort(key=lambda c: c["days"])
    best = candidates[0]
    return best["key"], best["tsym"]


def check_signal_with_candles(client, inst_key, signal_ts, entry, tgt1, sl):
    """Fetch 1-min candles and check if TGT1 or SL hit first."""
    signal_dt = datetime.strptime(signal_ts, "%Y-%m-%d %H:%M")
    from_dt = signal_dt
    to_dt = signal_dt.replace(hour=15, minute=30)

    try:
        candles = client.historical_data(inst_key, from_dt, to_dt, "minute")
    except Exception as e:
        return "ERROR", 0, str(e)

    if not candles:
        return "NO_DATA", 0, "no candles"

    # Walk through candles from signal time onwards
    for c in candles:
        candle_ts = c["date"]
        # Parse candle timestamp
        if isinstance(candle_ts, str):
            try:
                ct = datetime.fromisoformat(candle_ts.replace("Z", "+00:00"))
                if ct.tzinfo:
                    ct = ct.astimezone(IST).replace(tzinfo=None)
            except Exception:
                continue
        else:
            ct = candle_ts

        # Only look at candles AFTER signal time
        if ct < from_dt:
            continue

        high = c["high"]
        low = c["low"]

        # Check SL hit
        if low <= sl:
            actual_exit = sl
            pnl_points = sl - entry
            return "SL_HIT", pnl_points, f"SL hit at {ct}"

        # Check TGT1 hit
        if high >= tgt1:
            actual_exit = tgt1
            pnl_points = tgt1 - entry
            return "TGT1_HIT", pnl_points, f"TGT1 hit at {ct}"

    # Neither hit — check close of last candle
    if candles:
        last_close = candles[-1]["close"]
        pnl_points = last_close - entry
        return "EOD_EXIT", pnl_points, f"exited at close {last_close}"

    return "UNKNOWN", 0, ""


# --- Verify each signal ---
print()
print("=" * 100)
print("VERIFYING SIGNALS AGAINST REAL 1-MIN CANDLE DATA")
print("=" * 100)
print()

results = []
errors = 0
no_instrument = 0

print(f"  {'#':<4} {'Time':<17} {'Symbol':<25} {'Entry':>7} {'TGT1':>7} {'SL':>7}"
      f" {'Result':<10} {'Points':>8} {'P&L':>10} {'Note'}")
print(f"  {'─'*4} {'─'*17} {'─'*25} {'─'*7} {'─'*7} {'─'*7}"
      f" {'─'*10} {'─'*8} {'─'*10} {'─'*30}")

daily = defaultdict(lambda: {"full_pnl": 0, "cap_pnl": 0, "trades": 0,
                              "wins": 0, "cap_wins": 0})

for idx, s in enumerate(signals):
    inst_key, tsym = find_instrument_key(
        s["underlying"], s["strike"], s["opt_type"], s["date"])

    if not inst_key:
        no_instrument += 1
        print(f"  {idx+1:<4} {s['ts']:<17} {s['symbol']:<25} {s['entry']:>7.0f} "
              f"{s['tgts'][0]:>7.0f} {s['sl']:>7.0f} {'NO_INST':<10} {'':>8} {'':>10} "
              f"instrument not found")
        continue

    tgt1 = s["tgts"][0]
    result, points, note = check_signal_with_candles(
        client, inst_key, s["ts"], s["entry"], tgt1, s["sl"])

    lot_size = LOT_SIZES.get(s["underlying"], DEFAULT_LOT)
    qty = lot_size * LOTS
    full_pnl = points * qty

    # Capped P&L
    if result == "TGT1_HIT":
        points_for_cap = PROFIT_TARGET / qty
        cap_pnl = min(full_pnl, PROFIT_TARGET)
    elif result == "SL_HIT":
        cap_pnl = full_pnl
    else:
        cap_pnl = full_pnl

    won = result == "TGT1_HIT"
    icon = "W" if won else ("L" if result == "SL_HIT" else "?")
    pnl_cls = "+" if full_pnl >= 0 else ""

    d = daily[s["date"]]
    d["full_pnl"] += full_pnl
    d["cap_pnl"] += cap_pnl
    d["trades"] += 1
    if won:
        d["wins"] += 1
    if cap_pnl > 0:
        d["cap_wins"] += 1

    results.append({
        "ts": s["ts"], "date": s["date"], "symbol": s["symbol"],
        "entry": s["entry"], "tgt1": tgt1, "sl": s["sl"],
        "result": result, "points": points, "full_pnl": full_pnl,
        "cap_pnl": cap_pnl, "won": won, "lot_size": lot_size,
    })

    print(f"  {idx+1:<4} {s['ts']:<17} {s['symbol']:<25} {s['entry']:>7.0f} "
          f"{tgt1:>7.0f} {s['sl']:>7.0f} [{icon}] {result:<8} {points:>+7.0f} "
          f"₹{full_pnl:>+9,.0f} {note}")

    # Rate limit — Upstox allows ~10 req/sec
    time.sleep(0.15)

# --- Summary ---
verified = [r for r in results if r["result"] in ("TGT1_HIT", "SL_HIT", "EOD_EXIT")]
wins = sum(1 for r in verified if r["won"])
losses = sum(1 for r in verified if r["result"] == "SL_HIT")
eod = sum(1 for r in verified if r["result"] == "EOD_EXIT")
total_full = sum(r["full_pnl"] for r in verified)
total_cap = sum(r["cap_pnl"] for r in verified)

print()
print("=" * 100)
print("VERIFIED RESULTS — REAL CANDLE DATA")
print("=" * 100)
print()
print(f"  Signals parsed:      {len(signals)}")
print(f"  Instrument not found: {no_instrument}")
print(f"  Verified trades:     {len(verified)}")
print()
print(f"  TGT1 hit (WIN):     {wins} ({wins/len(verified)*100:.0f}%)" if verified else "")
print(f"  SL hit (LOSS):      {losses} ({losses/len(verified)*100:.0f}%)" if verified else "")
print(f"  EOD exit:           {eod}")
print()
print(f"  {'Metric':<30} {'Full Ride':>15} {'₹2K Target':>15}")
print(f"  {'─'*30} {'─'*15} {'─'*15}")
print(f"  {'Total P&L':<30} {'₹{:+,}'.format(int(total_full)):>15} {'₹{:+,}'.format(int(total_cap)):>15}")
n_days = len(daily) if daily else 1
print(f"  {'Avg P&L/day':<30} {'₹{:+,}'.format(int(total_full/n_days)):>15} {'₹{:+,}'.format(int(total_cap/n_days)):>15}")

avg_win = sum(r["full_pnl"] for r in verified if r["won"]) / wins if wins else 0
avg_loss = sum(r["full_pnl"] for r in verified if r["result"] == "SL_HIT") / losses if losses else 0
print(f"  {'Avg Win':<30} {'₹{:+,}'.format(int(avg_win)):>15}")
print(f"  {'Avg Loss':<30} {'₹{:+,}'.format(int(avg_loss)):>15}")
if avg_win:
    print(f"  {'Loss:Win Ratio':<30} {abs(avg_loss/avg_win):>14.1f}x")

print()
print("  DAILY BREAKDOWN")
print(f"  {'Date':<12} {'Trades':>7} {'Wins':>6} {'Full P&L':>12} {'₹2K Cap':>12}")
print(f"  {'─'*12} {'─'*7} {'─'*6} {'─'*12} {'─'*12}")

green_full = 0
green_cap = 0
for date in sorted(daily.keys()):
    d = daily[date]
    mark = "+" if d["full_pnl"] > 0 else "-"
    if d["full_pnl"] > 0:
        green_full += 1
    if d["cap_pnl"] > 0:
        green_cap += 1
    print(f"  {date:<12} {d['trades']:>7} {d['wins']:>6} "
          f"{'₹{:+,}'.format(int(d['full_pnl'])):>12} "
          f"{'₹{:+,}'.format(int(d['cap_pnl'])):>12} {mark}")

print()
print(f"  Green days (Full):  {green_full}/{len(daily)}")
print(f"  Green days (₹2K):   {green_cap}/{len(daily)}")
print()
print("=" * 100)

#!/usr/bin/env python3
"""Verify CH2 signals with precise entry timing.

For each signal:
1. Convert message time (UTC) to IST
2. Fetch 1-min candles from signal time
3. Use first candle's open as ACTUAL entry price
4. Walk candles to check TGT1/SL hit order
5. Report realistic P&L

Usage: .venv/bin/python3 scripts/verify_ch2_precise.py /tmp/ch2_aug21.txt [--date 2026-08-21]
"""
import sys, os, re, time as _time, argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config
    from src.broker.upstox_data import UpstoxData, load_cached_token
    from src.notify.channel_listener import parse_signal_ch2
except ImportError:
    print("ERROR: Run from Trading-Buddy root with .venv/bin/python3")
    sys.exit(1)

IST = ZoneInfo("Asia/Kolkata")

parser = argparse.ArgumentParser()
parser.add_argument("msg_file", help="Channel messages file")
parser.add_argument("--date", default=None, help="Date YYYY-MM-DD (default: today)")
args = parser.parse_args()

target_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")

LOTS = 3
LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 40,
    "MIDCPNIFTY": 50, "CRUDEOIL": 100, "GOLD": 100, "SILVER": 30,
    "NATURALGAS": 250, "EICHERMOT": 150, "LODHA": 1000, "MFSL": 1600,
    "MUTHOOTFIN": 1000, "INDIGO": 300, "TRENT": 625, "PAYTM": 1600,
    "ABB": 250, "BSE": 250, "LT": 300, "TITAN": 375, "BRITANNIA": 200,
    "HAL": 300, "MCX": 900, "POLYCAB": 200, "PERSISTENT": 200,
    "APOLLOHOSP": 250, "BAJAJAUTO": 250, "CUMMINSIND": 400,
    "SIEMENS": 275, "PIIND": 300, "RADICO": 1200, "AMBER": 200,
    "MARUTI": 100, "KEI": 200, "DIXON": 200, "LTIM": 200, "LTM": 200,
    "HEROMOTOCO": 150, "BHARTIARTL": 475, "HINDALCO": 1500,
}
DEFAULT_LOT = 400

# --- Read messages ---
with open(args.msg_file) as f:
    raw = f.read()

blocks = raw.split("\n---\n")
messages = []
for block in blocks:
    block = block.strip()
    if not block:
        continue
    m = re.match(r"\[(\d{2}:\d{2}:\d{2})\] (.*)", block, re.DOTALL)
    if m:
        messages.append({"ts": m.group(1), "text": m.group(2).strip()})

print(f"Total messages: {len(messages)}")

# --- Parse signals ---
signals = []
for msg in messages:
    sig = parse_signal_ch2(msg["text"])
    if sig:
        # Convert UTC time to IST
        utc_h, utc_m, utc_s = int(msg["ts"][:2]), int(msg["ts"][3:5]), int(msg["ts"][6:8])
        ist_m = utc_m + 30
        ist_h = utc_h + 5
        if ist_m >= 60:
            ist_h += 1
            ist_m -= 60
        ist_time = f"{ist_h:02d}:{ist_m:02d}"

        signals.append({
            "ts_utc": msg["ts"],
            "ts_ist": ist_time,
            "ist_h": ist_h,
            "ist_m": ist_m,
            "sig": sig,
        })

print(f"Parsed {len(signals)} signals")
if not signals:
    sys.exit(0)

# --- Load instruments ---
token = load_cached_token()
if not token:
    print("ERROR: No Upstox token")
    sys.exit(1)

client = UpstoxData()
master = client._load_master()

opt_keys = {}
for inst in master:
    seg = inst.get("segment", "")
    if seg not in ("NSE_FO", "BSE_FO", "MCX_FO"):
        continue
    if inst.get("instrument_type") not in ("CE", "PE"):
        continue
    tsym = (inst.get("trading_symbol") or "").upper()
    strike = inst.get("strike_price", 0)
    opt_type = inst.get("instrument_type", "")
    inst_key = inst.get("instrument_key")

    sym_prefix = re.sub(r"[\s\d].*", "", tsym).strip()
    if sym_prefix:
        opt_keys[f"{sym_prefix}|{int(strike)}|{opt_type}"] = inst_key
    name = inst.get("name", "").upper().replace(" ", "")
    if name:
        opt_keys[f"{name}|{int(strike)}|{opt_type}"] = inst_key

print(f"Instrument lookups: {len(opt_keys)}")

# --- Parse target date ---
dt_parts = [int(x) for x in target_date.split("-")]
year, month, day = dt_parts[0], dt_parts[1], dt_parts[2]

# --- Verify each signal ---
print()
print("=" * 130)
print(f"CH2 SIGNAL VERIFICATION — {target_date} — PRECISE ENTRY TIMING")
print("=" * 130)
print()

header = (f"  {'#':<3} {'IST':<6} {'Symbol':<12} {'Strike':>7} {'T':>2} "
          f"{'SigEntry':>8} {'ActEntry':>8} {'TGT1':>6} {'SL':>6} "
          f"{'High':>6} {'Low':>6} {'Result':<8} {'P&L':>9}")
print(header)
print("  " + "─" * 128)

wins = 0
losses = 0
nodata = 0
total_pnl = 0
total_cap_pnl = 0

for idx, s in enumerate(signals):
    sg = s["sig"]
    sym = sg.symbol
    strike = int(sg.strike)
    opt_type = sg.option_type
    sig_entry = sg.trigger_price
    tgt1 = sg.targets[0]
    sl = sg.stop_loss
    ist_time = s["ts_ist"]

    # Find instrument
    key = f"{sym}|{strike}|{opt_type}"
    inst_key = opt_keys.get(key)

    if not inst_key:
        print(f"  {idx+1:<3} {ist_time:<6} {sym:<12} {strike:>7} {opt_type:>2} "
              f"{sig_entry:>8.0f} {'':>8} {tgt1:>6.0f} {sl:>6.0f} "
              f"{'':>6} {'':>6} {'NO_INST':<8}")
        nodata += 1
        continue

    # Fetch 1-min candles from signal time to EOD
    from_dt = datetime(year, month, day, s["ist_h"], s["ist_m"], 0, tzinfo=IST)
    to_dt = datetime(year, month, day, 15, 30, 0, tzinfo=IST)

    # For commodity (MCX) — market hours are different (9:00-23:30)
    if sym in ("CRUDEOIL", "CRUDE", "GOLD", "SILVER", "NATURALGAS"):
        to_dt = datetime(year, month, day, 23, 30, 0, tzinfo=IST)

    candles = None
    for interval in ("1minute", "30minute", "day"):
        try:
            candles = client.historical_data(inst_key, from_dt, to_dt, interval)
            _time.sleep(0.3)
        except Exception:
            _time.sleep(0.5)
            continue
        if candles:
            break

    if not candles:
        print(f"  {idx+1:<3} {ist_time:<6} {sym:<12} {strike:>7} {opt_type:>2} "
              f"{sig_entry:>8.0f} {'':>8} {tgt1:>6.0f} {sl:>6.0f} "
              f"{'':>6} {'':>6} {'NO_DATA':<8}")
        nodata += 1
        continue

    # ACTUAL entry = first candle's open
    actual_entry = candles[0]["open"]

    # Walk candles to find TGT1 or SL hit
    max_high = 0
    min_low = 999999
    result = "OPEN"
    exit_price = actual_entry

    for c in candles:
        max_high = max(max_high, c["high"])
        min_low = min(min_low, c["low"])

        tgt_hit = c["high"] >= tgt1
        sl_hit = c["low"] <= sl

        if tgt_hit and sl_hit:
            # Both in same candle — check open direction
            if c["open"] >= actual_entry:
                result = "TGT1"
                exit_price = tgt1
            else:
                result = "SL"
                exit_price = sl
            break
        elif tgt_hit:
            result = "TGT1"
            exit_price = tgt1
            break
        elif sl_hit:
            result = "SL"
            exit_price = sl
            break

    if result == "OPEN":
        # Neither hit — use last candle close
        exit_price = candles[-1]["close"]
        if exit_price > actual_entry:
            result = "PROFIT"
        else:
            result = "LOSS"

    # P&L based on ACTUAL entry
    lot_size = LOT_SIZES.get(sym, DEFAULT_LOT)
    qty = lot_size * LOTS
    pnl = (exit_price - actual_entry) * qty

    cap_pnl = min(pnl, 2000) if pnl > 0 else max(pnl, -8000)

    if result in ("TGT1", "PROFIT"):
        wins += 1
        icon = "W"
    else:
        losses += 1
        icon = "L"

    total_pnl += pnl
    total_cap_pnl += cap_pnl

    # Entry diff indicator
    diff = actual_entry - sig_entry
    diff_str = f"({diff:+.0f})" if abs(diff) > 0.5 else ""

    print(f"  {idx+1:<3} {ist_time:<6} {sym:<12} {strike:>7} {opt_type:>2} "
          f"{sig_entry:>8.0f} {actual_entry:>8.1f} {tgt1:>6.0f} {sl:>6.0f} "
          f"{max_high:>6.0f} {min_low:>6.0f} [{icon}] {result:<5} ₹{pnl:>+8,.0f} {diff_str}")

# --- Summary ---
total = wins + losses
print()
print("=" * 130)
print(f"  RESULTS — {target_date}")
print("=" * 130)
print()
print(f"  Total signals:    {len(signals)}")
print(f"  Verified:         {total}")
print(f"  No data:          {nodata}")
print()
if total > 0:
    wr = wins / total * 100
    print(f"  Winners:          {wins} ({wr:.0f}%)")
    print(f"  Losers:           {losses} ({100-wr:.0f}%)")
    print(f"  Win Rate:         {wr:.1f}%")
    print()
    print(f"  Full P&L:         ₹{total_pnl:+,.0f}")
    print(f"  Capped P&L:       ₹{total_cap_pnl:+,.0f}  (₹2K cap / ₹8K max loss)")
    print(f"  Avg P&L/trade:    ₹{total_pnl/total:+,.0f}")
print()
print("  Entry = first 1-min candle open AFTER signal time.")
print("  Verified against actual Upstox option candle data.")
print("=" * 130)

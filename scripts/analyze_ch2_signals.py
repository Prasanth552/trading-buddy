#!/usr/bin/env python3
"""Analyze CH2 signals from chat dump vs actual candles.

Takes raw Telegram dump, parses signals, matches to DB instrument keys,
fetches 5-min candles at exact signal time, walks to TGT/SL.

Usage: .venv/bin/python3 scripts/analyze_ch2_signals.py /tmp/ch2_aug21.txt --date 2026-08-21
"""
import sys, os, re, time as _time, argparse, sqlite3
from datetime import datetime
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
parser.add_argument("msg_file", help="Channel messages dump file")
parser.add_argument("--date", default=None, help="Date YYYY-MM-DD (default: today)")
args = parser.parse_args()

target_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")
target_dt = datetime.strptime(target_date, "%Y-%m-%d").date()
dt_parts = [int(x) for x in target_date.split("-")]
year, month, day = dt_parts

# --- Read messages from dump ---
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
        messages.append({"ts_utc": m.group(1), "text": m.group(2).strip()})

print(f"Messages in dump: {len(messages)}")

# --- Parse signals ---
signals = []
for msg in messages:
    sig = parse_signal_ch2(msg["text"])
    if sig:
        utc_h, utc_m = int(msg["ts_utc"][:2]), int(msg["ts_utc"][3:5])
        ist_m = utc_m + 30
        ist_h = utc_h + 5
        if ist_m >= 60:
            ist_h += 1
            ist_m -= 60
        signals.append({
            "ts_utc": msg["ts_utc"],
            "ist": f"{ist_h:02d}:{ist_m:02d}",
            "ist_h": ist_h, "ist_m": ist_m,
            "sig": sig,
            "raw": msg["text"][:80],
        })

print(f"Parsed signals: {len(signals)}")

# --- Load DB trades to get real broker keys ---
db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "trading_buddy.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
db_trades = conn.execute("""
    SELECT id, ts, symbol, price, stop_price, target_price, broker_key, status, pnl
    FROM trades WHERE ts >= ? AND ts < ? AND channel='ch2' ORDER BY ts
""", (f"{target_date}T00:00:00", f"{target_date}T23:59:59")).fetchall()

# Build lookup: "SYMBOL_APPROX" -> broker_key
db_keys = {}
for t in db_trades:
    sym = t["symbol"].upper().replace(" ", "")
    db_keys[sym] = t["broker_key"]

print(f"DB trades with broker keys: {len(db_keys)}")

# --- Build instrument index from master (fallback) ---
token = load_cached_token()
if not token:
    print("ERROR: No Upstox token"); sys.exit(1)

client = UpstoxData()
master = client._load_master()

from collections import defaultdict
_candidates = defaultdict(list)
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
    expiry = inst.get("expiry", "")
    try:
        if isinstance(expiry, (int, float)) or (isinstance(expiry, str) and expiry.isdigit()):
            exp_date = datetime.fromtimestamp(int(expiry) / 1000, tz=IST).date()
        elif isinstance(expiry, str) and "-" in expiry[:10]:
            exp_date = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
        else:
            exp_date = None
    except Exception:
        exp_date = None

    sym_prefix = re.sub(r"[\s\d].*", "", tsym).strip()
    if sym_prefix:
        _candidates[f"{sym_prefix}|{int(strike)}|{opt_type}"].append((exp_date, inst_key))

opt_keys = {}
for key, cands in _candidates.items():
    valid = [(e, k) for e, k in cands if e and e >= target_dt]
    if valid:
        valid.sort()
        opt_keys[key] = valid[0][1]
    elif cands:
        opt_keys[key] = cands[-1][1]

# --- Resolve instrument key for each signal ---
def resolve_key(sig):
    sym = sig.symbol.upper().replace(" ", "")
    strike = int(sig.strike)
    ot = sig.option_type
    # Try DB key first (exact match)
    db_sym = f"{sym}{strike}{ot}"
    if db_sym in db_keys:
        return db_keys[db_sym], "DB"
    # Try with common name variations
    for k, v in db_keys.items():
        if str(strike) in k and ot in k and any(s in k for s in sym.split()):
            return v, "DB"
    # Fallback to master
    master_key = opt_keys.get(f"{sym}|{strike}|{ot}")
    if master_key:
        return master_key, "MST"
    return None, None

# --- Analyze each signal ---
print()
print("=" * 150)
print(f"CH2 SIGNALS vs ACTUAL CANDLES — {target_date}")
print("=" * 150)
print()

hdr = (f"  {'#':<3} {'IST':<6} {'Symbol':<14} {'Strk':>6} {'T':>2} "
       f"{'SigEntry':>8} {'CandleO':>8} {'Slip':>5} "
       f"{'TGT1':>6} {'SL':>6} {'High':>7} {'Low':>7} "
       f"{'Result':<10} {'P&L':>9} {'Src':<3}")
print(hdr)
print("  " + "─" * 148)

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
    "MARUTI": 100, "KEI": 200, "DIXON": 200, "LTIM": 200,
    "HEROMOTOCO": 150, "BHARTIARTL": 475, "HINDALCO": 1500,
}
DEFAULT_LOT = 400

wins = losses = nodata = uncertain_ct = 0
total_pnl = 0

for idx, s in enumerate(signals):
    sg = s["sig"]
    sym = sg.symbol
    strike = int(sg.strike)
    ot = sg.option_type
    sig_entry = sg.trigger_price
    tgt1 = sg.targets[0]
    sl = sg.stop_loss
    ist = s["ist"]

    inst_key, src = resolve_key(sg)
    if not inst_key:
        print(f"  {idx+1:<3} {ist:<6} {sym:<14} {strike:>6} {ot:>2} "
              f"{sig_entry:>8.0f} {'':>8} {'':>5} {tgt1:>6.0f} {sl:>6.0f} "
              f"{'':>7} {'':>7} {'NO_INST':<10}")
        nodata += 1
        continue

    # Fetch candles — full day, then filter to signal time
    from_dt = datetime(year, month, day, 9, 15, 0, tzinfo=IST)
    if sym in ("CRUDEOIL", "CRUDE", "GOLD", "SILVER", "NATURALGAS"):
        to_dt = datetime(year, month, day, 23, 30, 0, tzinfo=IST)
    else:
        to_dt = datetime(year, month, day, 15, 30, 0, tzinfo=IST)

    candles = None
    for interval in ("5minute", "15minute", "day"):
        try:
            candles = client.historical_data(inst_key, from_dt, to_dt, interval)
            _time.sleep(0.25)
        except Exception:
            _time.sleep(0.5)
            continue
        if candles:
            break

    if not candles:
        print(f"  {idx+1:<3} {ist:<6} {sym:<14} {strike:>6} {ot:>2} "
              f"{sig_entry:>8.0f} {'':>8} {'':>5} {tgt1:>6.0f} {sl:>6.0f} "
              f"{'':>7} {'':>7} {'NO_DATA':<10}")
        nodata += 1
        continue

    # For pre-market signals (before 09:15), use 09:15 candle
    sig_ts = ist if ist >= "09:15" else "09:15"
    filtered = [c for c in candles if c["date"][11:16] >= sig_ts]
    if not filtered:
        filtered = candles

    candle_open = filtered[0]["open"]
    slip = candle_open - sig_entry

    # Walk candles
    max_high = 0
    min_low = 999999
    result = "OPEN"
    exit_price = candle_open

    for c in filtered:
        max_high = max(max_high, c["high"])
        min_low = min(min_low, c["low"])

        tgt_hit = c["high"] >= tgt1
        sl_hit = c["low"] <= sl

        if tgt_hit and sl_hit:
            result = "BOTH"
            # Can't determine order from 5-min candle
            exit_price = tgt1 if abs(c["open"] - tgt1) < abs(c["open"] - sl) else sl
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
        exit_price = filtered[-1]["close"]
        result = "EOD"

    # P&L using CANDLE OPEN as entry (what bot would get at market)
    lot_size = LOT_SIZES.get(sym, DEFAULT_LOT)
    qty = lot_size * LOTS
    pnl = (exit_price - candle_open) * qty

    if result == "BOTH":
        uncertain_ct += 1
        icon = "?"
    elif pnl >= 0:
        wins += 1
        icon = "W"
    else:
        losses += 1
        icon = "L"

    total_pnl += pnl

    print(f"  {idx+1:<3} {ist:<6} {sym:<14} {strike:>6} {ot:>2} "
          f"{sig_entry:>8.0f} {candle_open:>8.1f} {slip:>+5.0f} "
          f"{tgt1:>6.0f} {sl:>6.0f} {max_high:>7.1f} {min_low:>7.1f} "
          f"[{icon}] {result:<7} ₹{pnl:>+8,.0f} {src:<3}")

# --- Summary ---
total = wins + losses + uncertain_ct
print()
print("=" * 150)
print(f"  RESULTS — {target_date}")
print("=" * 150)
print()
print(f"  Signals parsed:   {len(signals)}")
print(f"  Verified:         {total}")
print(f"  No data:          {nodata}")
print(f"  Uncertain (both): {uncertain_ct}")
print()
if total > 0:
    print(f"  Winners:          {wins}")
    print(f"  Losers:           {losses}")
    print(f"  Win Rate:         {wins}/{wins+losses} = {wins/(wins+losses)*100:.0f}%" if (wins+losses) else "")
    print()
    print(f"  Total P&L:        ₹{total_pnl:+,.0f}")
    print(f"  Avg P&L/trade:    ₹{total_pnl/total:+,.0f}")
print()
print("  Entry = 5-min candle open at signal time (09:15 for pre-market signals)")
print("  Src: DB = broker key from actual trades, MST = resolved from master")
print("  BOTH = TGT and SL hit in same 5-min candle (uncertain outcome)")
print("=" * 150)

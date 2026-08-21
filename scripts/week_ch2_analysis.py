#!/usr/bin/env python3
"""Weekly CH2 analysis: parse signals from dumps, verify with candles, ₹5K/day cap.

Usage: .venv/bin/python3 scripts/week_ch2_analysis.py /tmp/ch2_20260820.txt /tmp/ch2_20260821.txt
"""
import sys, os, re, time as _time, argparse, sqlite3, glob
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

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
parser.add_argument("files", nargs="+", help="Message dump files (one per day)")
parser.add_argument("--daily-cap", type=float, default=10000, help="Stop trading after this daily profit (default: 10000)")
args = parser.parse_args()

# --- Load instruments ---
token = load_cached_token()
if not token:
    print("ERROR: No Upstox token"); sys.exit(1)

client = UpstoxData()
master = client._load_master()

# --- Load DB broker keys ---
db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "trading_buddy.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
db_all = conn.execute("""
    SELECT symbol, broker_key FROM trades WHERE channel='ch2'
""").fetchall()

db_keys = {}
for t in db_all:
    sym = t["symbol"].upper().replace(" ", "")
    db_keys[sym] = t["broker_key"]

# --- Build master index (nearest expiry per symbol|strike|type) ---
def build_master_index(target_date_str):
    target_dt = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    _cands = defaultdict(list)
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
            _cands[f"{sym_prefix}|{int(strike)}|{opt_type}"].append((exp_date, inst_key))
    idx = {}
    for key, cands in _cands.items():
        valid = [(e, k) for e, k in cands if e and e >= target_dt]
        if valid:
            valid.sort()
            idx[key] = valid[0][1]
        elif cands:
            idx[key] = cands[-1][1]
    return idx

def resolve_key(sig, master_idx):
    sym = sig.symbol.upper().replace(" ", "")
    strike = int(sig.strike)
    ot = sig.option_type
    db_sym = f"{sym}{strike}{ot}"
    if db_sym in db_keys:
        return db_keys[db_sym], "DB"
    for k, v in db_keys.items():
        if str(strike) in k and ot in k:
            parts = sym.split()
            if any(s in k for s in parts):
                return v, "DB"
    mkey = master_idx.get(f"{sym}|{strike}|{ot}")
    if mkey:
        return mkey, "MST"
    return None, None

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
LOTS = 3

# --- Process each day ---
def extract_date_from_filename(filepath):
    base = os.path.basename(filepath)
    m = re.search(r"(\d{8})", base)
    if m:
        d = m.group(1)
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    m = re.search(r"(\d{4}-\d{2}-\d{2})", base)
    if m:
        return m.group(1)
    # Try aug21 pattern
    m = re.search(r"aug(\d{2})", base, re.I)
    if m:
        return f"2026-08-{m.group(1)}"
    return None

def parse_messages(filepath):
    with open(filepath) as f:
        raw = f.read()
    blocks = raw.split("\n---\n")
    msgs = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        m = re.match(r"\[(\d{2}:\d{2}:\d{2})\] (.*)", block, re.DOTALL)
        if m:
            msgs.append({"ts_utc": m.group(1), "text": m.group(2).strip()})
    return msgs

def parse_signals(messages):
    # Reset parser state between days
    import src.notify.channel_listener as cl
    cl._ch2_pending = None
    cl._ch2_pending_ts = 0

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
            })
    return signals

def analyze_day(filepath, date_str):
    msgs = parse_messages(filepath)
    signals = parse_signals(msgs)
    master_idx = build_master_index(date_str)
    dt = [int(x) for x in date_str.split("-")]
    year, month, day = dt

    results = []
    day_pnl = 0
    trades_taken = 0

    for idx, s in enumerate(signals):
        sg = s["sig"]
        sym = sg.symbol
        strike = int(sg.strike)
        ot = sg.option_type
        sig_entry = sg.trigger_price
        tgt1 = sg.targets[0]
        sl = sg.stop_loss
        ist = s["ist"]

        # --- FILTERS ---
        skip_reason = None

        # Daily cap reached
        if day_pnl >= args.daily_cap:
            skip_reason = "CAP_HIT"

        if skip_reason:
            results.append({
                "idx": idx+1, "ist": ist, "sym": sym, "strike": strike,
                "ot": ot, "sig_entry": sig_entry, "tgt1": tgt1, "sl": sl,
                "skip": skip_reason,
            })
            continue

        # Resolve instrument
        inst_key, src = resolve_key(sg, master_idx)
        if not inst_key:
            results.append({
                "idx": idx+1, "ist": ist, "sym": sym, "strike": strike,
                "ot": ot, "sig_entry": sig_entry, "tgt1": tgt1, "sl": sl,
                "skip": "NO_INST",
            })
            continue

        # Fetch candles
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
            results.append({
                "idx": idx+1, "ist": ist, "sym": sym, "strike": strike,
                "ot": ot, "sig_entry": sig_entry, "tgt1": tgt1, "sl": sl,
                "skip": "NO_DATA",
            })
            continue

        # Filter to signal time (pre-market → 09:15)
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
                exit_price = sl  # conservative: assume SL hit first
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

        lot_size = LOT_SIZES.get(sym, DEFAULT_LOT)
        qty = lot_size * LOTS
        pnl = (exit_price - candle_open) * qty

        trades_taken += 1
        day_pnl += pnl

        results.append({
            "idx": idx+1, "ist": ist, "sym": sym, "strike": strike,
            "ot": ot, "sig_entry": sig_entry, "tgt1": tgt1, "sl": sl,
            "candle_open": candle_open, "slip": slip,
            "max_high": max_high, "min_low": min_low,
            "result": result, "exit_price": exit_price,
            "pnl": pnl, "day_pnl": day_pnl,
            "src": src, "traded": True,
        })

    return results, day_pnl, trades_taken

# === MAIN ===
print()
print("=" * 150)
print(f"CH2 WEEKLY ANALYSIS — ₹{args.daily_cap:,.0f}/day profit cap (only filter)")
print("=" * 150)

week_pnl = 0
week_trades = 0
week_wins = 0
week_losses = 0
week_skipped = 0
day_summaries = []

for filepath in args.files:
    date_str = extract_date_from_filename(filepath)
    if not date_str:
        print(f"WARNING: Can't extract date from {filepath}, skipping")
        continue

    if not os.path.exists(filepath):
        print(f"WARNING: {filepath} not found, skipping")
        continue

    results, day_pnl, day_trades = analyze_day(filepath, date_str)

    # Day header
    weekday = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
    print(f"\n{'─'*150}")
    print(f"  {date_str} ({weekday}) — {len(results)} signals parsed")
    print(f"{'─'*150}")
    print(f"  {'#':<3} {'IST':<6} {'Symbol':<14} {'Strk':>6} {'T':>2} "
          f"{'SigE':>7} {'ActE':>7} {'Slip':>5} {'TGT1':>6} {'SL':>6} "
          f"{'High':>7} {'Low':>7} {'Result':<7} {'P&L':>8} {'DayP&L':>8} {'Note':<10}")

    d_wins = 0
    d_losses = 0
    d_skipped = 0

    for r in results:
        if "skip" in r:
            d_skipped += 1
            print(f"  {r['idx']:<3} {r['ist']:<6} {r['sym']:<14} {r['strike']:>6} {r['ot']:>2} "
                  f"{r['sig_entry']:>7.0f} {'':>7} {'':>5} {r['tgt1']:>6.0f} {r['sl']:>6.0f} "
                  f"{'':>7} {'':>7} {'':>7} {'':>8} {'':>8} {r['skip']:<10}")
        else:
            pnl = r["pnl"]
            if pnl >= 0:
                d_wins += 1
                icon = "W"
            else:
                d_losses += 1
                icon = "L"
            print(f"  {r['idx']:<3} {r['ist']:<6} {r['sym']:<14} {r['strike']:>6} {r['ot']:>2} "
                  f"{r['sig_entry']:>7.0f} {r['candle_open']:>7.1f} {r['slip']:>+5.0f} "
                  f"{r['tgt1']:>6.0f} {r['sl']:>6.0f} "
                  f"{r['max_high']:>7.1f} {r['min_low']:>7.1f} "
                  f"[{icon}] {r['result']:<4} {pnl:>+8,.0f} {r['day_pnl']:>+8,.0f} {r['src']:<10}")

    print(f"\n  Day: {d_wins}W/{d_losses}L, {d_skipped} skipped, P&L = ₹{day_pnl:+,.0f}")
    week_pnl += day_pnl
    week_trades += day_trades
    week_wins += d_wins
    week_losses += d_losses
    week_skipped += d_skipped
    day_summaries.append((date_str, weekday, d_wins, d_losses, d_skipped, day_pnl, day_trades))

# --- Weekly Summary ---
print()
print("=" * 150)
print("  WEEKLY SUMMARY")
print("=" * 150)
print()
print(f"  {'Date':<12} {'Day':<10} {'W':>3} {'L':>3} {'Skip':>5} {'Trades':>6} {'P&L':>10}")
print(f"  {'─'*50}")
for ds, wd, w, l, sk, dp, dt in day_summaries:
    print(f"  {ds:<12} {wd:<10} {w:>3} {l:>3} {sk:>5} {dt:>6} ₹{dp:>+9,.0f}")
print(f"  {'─'*50}")
print(f"  {'TOTAL':<12} {'':10} {week_wins:>3} {week_losses:>3} {week_skipped:>5} {week_trades:>6} ₹{week_pnl:>+9,.0f}")
print()
if week_wins + week_losses > 0:
    wr = week_wins / (week_wins + week_losses) * 100
    print(f"  Win Rate:         {wr:.0f}%")
    print(f"  Avg P&L/trade:    ₹{week_pnl/week_trades:+,.0f}" if week_trades else "")
    print(f"  Avg P&L/day:      ₹{week_pnl/len(day_summaries):+,.0f}")
print()
print(f"  Rules: ₹{args.daily_cap:,.0f}/day profit cap (stop trading after cap hit)")
print(f"  BOTH = TGT & SL in same candle → conservative (assume SL hit)")
print("=" * 150)

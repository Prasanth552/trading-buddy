#!/usr/bin/env python3
"""Analyze ALL channel trades today with correct settings:
- CH1: September (monthly) expiry, 1 lot
- CH2/CH3: nearest expiry, 3 lots index / 2 lots stocks
- ₹1,500 profit floor (ride to TGT, exit if dips back to ₹1,500)

Usage: .venv/bin/python3 scripts/analyze_today_all.py [--date 2026-08-24]
"""
import sys, os, re, time as _time, argparse, sqlite3, calendar
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config
    from src.broker.upstox_data import UpstoxData, load_cached_token
    from src.broker.upstox_client import _expiry_to_date
except ImportError:
    print("ERROR: Run from Trading-Buddy root with .venv/bin/python3")
    sys.exit(1)

IST = ZoneInfo("Asia/Kolkata")
PROFIT_FLOOR = 1500

parser = argparse.ArgumentParser()
parser.add_argument("--date", default=None)
args = parser.parse_args()

target_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")
dt_parts = [int(x) for x in target_date.split("-")]
year, month, day = dt_parts
today_d = date(year, month, day)

# Load DB trades
db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "trading_buddy.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

token = load_cached_token()
if not token:
    print("ERROR: No Upstox token"); sys.exit(1)
client = UpstoxData()
master = client._load_master()

LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 40,
    "MIDCPNIFTY": 50, "CRUDEOIL": 100, "GOLD": 100, "SILVER": 30,
    "NATURALGAS": 250, "EICHERMOT": 150, "LODHA": 1000, "MFSL": 1600,
    "MUTHOOTFIN": 1000, "MANAPPURAM": 4000, "INDIGO": 300, "TRENT": 625,
    "PAYTM": 1600, "ABB": 250, "BSE": 250, "LT": 300, "TITAN": 375,
    "BRITANNIA": 200, "HAL": 300, "MCX": 900, "POLYCAB": 200,
    "PERSISTENT": 200, "APOLLOHOSP": 250, "BAJAJAUTO": 250,
    "CUMMINSIND": 400, "SIEMENS": 275, "PIIND": 300, "RADICO": 1200,
    "AMBER": 200, "MARUTI": 100, "KEI": 200, "DIXON": 200, "LTIM": 200,
    "HEROMOTOCO": 150, "BHARTIARTL": 475, "HINDALCO": 1500,
    "ULTRACEMCO": 100, "LTF": 2816, "CANBK": 2700,
}
DEFAULT_LOT = 400
INDEX_SYMS = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}


def get_base_symbol(symbol):
    m = re.match(r"([A-Z&]+)", symbol.upper().replace(" ", ""))
    return m.group(1) if m else symbol.upper()


def resolve_instrument(symbol_str, monthly=False):
    parts = symbol_str.strip().split()
    if len(parts) < 3:
        return None, None, None
    opt_type = parts[-1]
    strike = float(parts[-2])
    sym = "".join(parts[:-2]).upper()

    candidates = []
    for inst in master:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO", "MCX_FO"):
            continue
        asym = inst.get("asset_symbol", "").upper()
        if asym != sym:
            continue
        if inst.get("instrument_type") != opt_type:
            continue
        if abs(float(inst.get("strike_price", -1)) - strike) > 0.01:
            continue
        exp = _expiry_to_date(inst.get("expiry"))
        if exp is None or exp < today_d:
            continue
        candidates.append((exp, inst))

    if not candidates:
        return None, None, None

    if monthly and len(candidates) > 1:
        min_exp = today_d + timedelta(days=7)
        monthly_cands = [(e, i) for e, i in candidates if e >= min_exp]
        if monthly_cands:
            monthly_cands.sort(key=lambda x: x[0])
            inst = monthly_cands[0][1]
            return inst.get("instrument_key"), int(inst.get("lot_size", 1)) or 1, monthly_cands[0][0]

    candidates.sort(key=lambda x: x[0])
    inst = candidates[0][1]
    return inst.get("instrument_key"), int(inst.get("lot_size", 1)) or 1, candidates[0][0]


def walk_candles_floor(candles, entry, sl, ch_tgt, qty):
    """Walk candles with ₹1,500 profit floor logic.

    1. Channel TGT hit → exit at TGT
    2. SL hit → exit at SL
    3. Peak P&L crossed ₹1,500 then low dips back → floor exit
    4. EOD → exit at last close
    """
    peak_pnl = 0
    max_high = 0
    min_low = 999999
    floor_armed = False

    for c in candles:
        max_high = max(max_high, c["high"])
        min_low = min(min_low, c["low"])

        candle_peak_pnl = (c["high"] - entry) * qty
        peak_pnl = max(peak_pnl, candle_peak_pnl)

        if peak_pnl >= PROFIT_FLOOR:
            floor_armed = True

        tgt_hit = ch_tgt and c["high"] >= ch_tgt
        sl_hit = sl and c["low"] <= sl
        low_pnl = (c["low"] - entry) * qty

        if tgt_hit and sl_hit:
            return ch_tgt, "BOTH_TGT", max_high, min_low
        elif tgt_hit:
            return ch_tgt, "TGT", max_high, min_low
        elif sl_hit:
            return sl, "SL", max_high, min_low
        elif floor_armed and low_pnl <= PROFIT_FLOOR:
            floor_price = entry + (PROFIT_FLOOR / qty)
            return floor_price, "FLOOR", max_high, min_low

    return candles[-1]["close"], "EOD", max_high, min_low


# ===== MAIN =====
grand_total = 0
grand_db_total = 0

for ch in ("ch1", "ch2", "ch3"):
    rows = conn.execute("""
        SELECT id, ts, symbol, price, stop_price, target_price, exit_price, pnl, status, broker_key, qty
        FROM trades WHERE ts >= ? AND ts < ? AND channel=? ORDER BY ts
    """, (f"{target_date}T00:00:00", f"{target_date}T23:59:59", ch)).fetchall()

    if not rows:
        print(f"\n--- {ch.upper()}: No trades today ---")
        continue

    is_monthly = ch in ("ch1", "ch1b")

    print()
    print("=" * 160)
    ch_label = {"ch1": "CH1 Paid (Sep expiry, 1 lot)", "ch2": "CH2 G Prime (3L idx / 2L stk)",
                "ch3": "CH3 Free (3L idx / 2L stk)"}.get(ch, ch.upper())
    print(f"  {ch_label} — {target_date} — ₹{PROFIT_FLOOR:,} profit floor")
    print("=" * 160)
    print(f"  {'#':<4} {'Time':<6} {'Symbol':<24} {'Entry':>7} {'SL':>7} {'TGT':>7} "
          f"{'Lots':>4} {'Qty':>5} {'Peak':>7} {'Low':>7} {'Result':<8} {'Floor P&L':>10} {'DB P&L':>8}")
    print("  " + "─" * 158)

    ch_pnl = 0
    ch_db_pnl = 0
    ch_wins = 0
    ch_losses = 0
    ch_nodata = 0

    for row in rows:
        trade_id = row["id"]
        ts = row["ts"]
        symbol = row["symbol"]
        db_entry = row["price"]
        sl = row["stop_price"]
        ch_tgt = row["target_price"]
        db_pnl = row["pnl"] or 0
        ch_db_pnl += db_pnl

        entry_time = (ts or "")[11:16]

        base_sym = get_base_symbol(symbol)
        is_index = base_sym in INDEX_SYMS

        # Determine lots
        if ch == "ch1":
            lots = 1
        elif ch in ("ch2", "ch3"):
            lots = 3 if is_index else 2
        else:
            lots = 1

        # Resolve instrument
        inst_key, master_lot, exp_date = resolve_instrument(symbol, monthly=is_monthly)

        if not inst_key:
            print(f"  {trade_id:<4} {entry_time:<6} {symbol:<24} {db_entry:>7.1f} {sl:>7.1f} {ch_tgt:>7.1f} "
                  f"{'':>4} {'':>5} {'':>7} {'':>7} {'NO_INST':<8} {'':>10} {db_pnl:>+8,.0f}")
            ch_nodata += 1
            continue

        lot_size = LOT_SIZES.get(base_sym, master_lot or DEFAULT_LOT)
        qty = lot_size * lots

        # Fetch candles
        from_dt = datetime(year, month, day, 9, 15, 0, tzinfo=IST)
        if base_sym in ("CRUDEOIL", "CRUDE", "GOLD", "SILVER", "NATURALGAS"):
            to_dt = datetime(year, month, day, 23, 30, 0, tzinfo=IST)
        else:
            to_dt = datetime(year, month, day, 15, 30, 0, tzinfo=IST)

        candles = None
        for interval in ("5minute", "15minute"):
            try:
                candles = client.historical_data(inst_key, from_dt, to_dt, interval)
                _time.sleep(0.25)
            except Exception:
                _time.sleep(0.5)
                continue
            if candles:
                break

        if not candles:
            print(f"  {trade_id:<4} {entry_time:<6} {symbol:<24} {db_entry:>7.1f} {sl:>7.1f} {ch_tgt:>7.1f} "
                  f"{lots:>4} {qty:>5} {'':>7} {'':>7} {'NO_DATA':<8} {'':>10} {db_pnl:>+8,.0f}")
            ch_nodata += 1
            continue

        # Filter from entry time
        filtered = [c for c in candles if c["date"][11:16] >= entry_time]
        if not filtered:
            filtered = candles

        entry = filtered[0]["open"]

        # Walk with floor logic
        exit_price, result, max_high, min_low = walk_candles_floor(filtered, entry, sl, ch_tgt, qty)
        pnl = (exit_price - entry) * qty

        if pnl >= 0:
            ch_wins += 1
            icon = "W"
        else:
            ch_losses += 1
            icon = "L"

        ch_pnl += pnl

        exp_str = f" ({exp_date.strftime('%d%b')})" if exp_date and is_monthly else ""

        print(f"  {trade_id:<4} {entry_time:<6} {symbol:<24} {entry:>7.1f} {sl:>7.1f} {ch_tgt:>7.1f} "
              f"{lots:>4} {qty:>5} {max_high:>7.1f} {min_low:>7.1f} [{icon}] {result:<5} {pnl:>+10,.0f} {db_pnl:>+8,.0f}{exp_str}")

    total = ch_wins + ch_losses
    print(f"\n  {ch.upper()}: {ch_wins}W/{ch_losses}L ({ch_nodata} no data), "
          f"Floor P&L = ₹{ch_pnl:+,.0f}, DB P&L = ₹{ch_db_pnl:+,.0f}, "
          f"Diff = ₹{ch_pnl - ch_db_pnl:+,.0f}")

    grand_total += ch_pnl
    grand_db_total += ch_db_pnl

# Grand total
print()
print("=" * 160)
print(f"  GRAND TOTAL — {target_date}")
print("=" * 160)
print(f"  With correct settings:  ₹{grand_total:+,.0f}")
print(f"  Actual DB result:       ₹{grand_db_total:+,.0f}")
print(f"  Missed profit:          ₹{grand_total - grand_db_total:+,.0f}")
print()
print(f"  Floor = ₹{PROFIT_FLOOR:,} | CH1 = 1 lot Sep expiry | CH2/CH3 = 3L index, 2L stocks")
print("=" * 160)

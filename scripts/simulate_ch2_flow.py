#!/usr/bin/env python3
"""Simulate the improved CH2 parser flow against actual channel messages.

Fetches messages, processes them through the new WAIT/Active/cancel/re-entry
flow, and compares results with DB trades.

Must kill channel_listener first (Telethon session lock).

Usage: .venv/bin/python3 scripts/simulate_ch2_flow.py [--date 2026-08-25]
"""
import sys, os, re, asyncio, argparse, sqlite3, time as _time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
PROFIT_FLOOR = 1500

parser = argparse.ArgumentParser()
parser.add_argument("--date", default=None)
parser.add_argument("--limit", type=int, default=600)
args = parser.parse_args()

target_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")
year, month, day = [int(x) for x in target_date.split("-")]
today_d = date(year, month, day)
day_start = datetime(year, month, day, 0, 0, 0, tzinfo=IST)
day_end = day_start + timedelta(days=1)

# --- Import parser components ---
try:
    import config
    from src.notify.channel_listener import (
        ParsedSignal, parse_signal_ch2,
        _CH2_SYMBOL_RE, _CH2_ENTRY_RE, _CH2_TGT_RE, _CH2_SL_RE,
        _ch2_extract_targets, _ch2_extract_sl,
    )
    import src.notify.channel_listener as _cl
    from src.broker.upstox_data import UpstoxData, load_cached_token
    from src.broker.upstox_client import _expiry_to_date
except ImportError as e:
    print(f"ERROR: {e}\nRun from Trading-Buddy root with .venv/bin/python3")
    sys.exit(1)

# --- Telegram setup ---
api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
api_hash = os.getenv("TELEGRAM_API_HASH", "")
ch2_id = int(os.getenv("SIGNAL_CHANNEL2_ID", "0"))
session_path = os.path.join(os.path.dirname(__file__), "..", "data", "telegram_user.session")

# --- DB ---
db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "trading_buddy.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# --- Upstox ---
token = load_cached_token()
if not token:
    print("ERROR: No Upstox token"); sys.exit(1)
uclient = UpstoxData()
master = uclient._load_master()

INDEX_SYMS = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}
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


def resolve_instrument(symbol_str):
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
        if inst.get("asset_symbol", "").upper() != sym:
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
    candidates.sort(key=lambda x: x[0])
    inst = candidates[0][1]
    return inst.get("instrument_key"), int(inst.get("lot_size", 1)) or 1, candidates[0][0]


def walk_candles_floor(candles, entry, sl, ch_tgt, qty):
    peak_pnl = 0
    floor_armed = False
    tgt_valid = ch_tgt and ch_tgt > entry
    sl_valid = sl and sl < entry
    inverted = ""
    if ch_tgt and ch_tgt <= entry:
        inverted += "TGT<E "
    if sl and sl >= entry:
        inverted += "SL>E"

    for c in candles:
        tgt_hit = tgt_valid and c["high"] >= ch_tgt
        sl_hit = sl_valid and c["low"] <= sl
        low_pnl = (c["low"] - entry) * qty

        if tgt_hit and sl_hit:
            return ch_tgt, "BOTH_TGT", inverted
        elif tgt_hit:
            return ch_tgt, "TGT", inverted
        elif sl_hit:
            return sl, "SL", inverted
        elif floor_armed and low_pnl <= PROFIT_FLOOR:
            floor_price = entry + (PROFIT_FLOOR / qty)
            return floor_price, "FLOOR", inverted

        candle_peak_pnl = (c["high"] - entry) * qty
        peak_pnl = max(peak_pnl, candle_peak_pnl)
        if peak_pnl >= PROFIT_FLOOR:
            floor_armed = True

    return candles[-1]["close"], "EOD", inverted


def simulate_trade(sig, entry_time_str):
    """Resolve instrument, fetch candles, walk with floor logic."""
    sym_str = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"
    base_sym = re.match(r"([A-Z&]+)", sig.symbol.upper().replace(" ", "")).group(1)
    is_index = base_sym in INDEX_SYMS
    lots = 3 if is_index else 2

    inst_key, master_lot, exp_date = resolve_instrument(sym_str)
    if not inst_key:
        return None, "NO_INST", 0, ""

    lot_size = LOT_SIZES.get(base_sym, master_lot or DEFAULT_LOT)
    qty = lot_size * lots

    from_dt = datetime(year, month, day, 9, 15, 0, tzinfo=IST)
    if base_sym in ("CRUDEOIL", "CRUDE", "GOLD", "SILVER", "NATURALGAS"):
        to_dt = datetime(year, month, day, 23, 30, 0, tzinfo=IST)
    else:
        to_dt = datetime(year, month, day, 15, 30, 0, tzinfo=IST)

    candles = None
    for interval in ("5minute", "15minute"):
        try:
            candles = uclient.historical_data(inst_key, from_dt, to_dt, interval)
            _time.sleep(0.25)
        except Exception:
            _time.sleep(0.5)
            continue
        if candles:
            break

    if not candles:
        return None, "NO_DATA", 0, ""

    filtered = [c for c in candles if c["date"][11:16] >= entry_time_str]
    if not filtered:
        filtered = candles

    entry = filtered[0]["open"]
    exit_price, result, inverted = walk_candles_floor(filtered, entry, sig.stop_loss, sig.targets[0], qty)
    pnl = (exit_price - entry) * qty

    return {
        "entry": entry, "exit": exit_price, "qty": qty, "lots": lots,
        "pnl": pnl, "result": result, "inverted": inverted,
    }, result, pnl, inverted


# ============================================================
# Main simulation
# ============================================================
async def main():
    from telethon import TelegramClient

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: Telethon session not authorized")
        return

    entity = ch2_id
    if ch2_id < 0 and not str(ch2_id).startswith("-100"):
        entity = int(f"-100{abs(ch2_id)}")

    print(f"Fetching CH2 messages for {target_date}...")
    messages = []
    async for msg in client.iter_messages(entity, limit=args.limit, offset_date=day_end):
        if msg.date.astimezone(IST) < day_start:
            break
        messages.append(msg)
    messages.reverse()
    print(f"Fetched {len(messages)} messages\n")
    await client.disconnect()

    # --- Build message lookup for replies ---
    msg_by_id = {m.id: m for m in messages}

    # --- Simulation state ---
    queued_signal = None
    queued_ts = 0.0
    queued_msg_id = 0
    trigger_held = None
    trigger_held_msg_id = 0

    executed = []     # signals that would be executed
    cancelled = []    # signals that were cancelled
    held = []         # signals currently held (should be empty at end)
    skipped = []      # messages that were skipped/ignored
    reentries = []    # re-entry signals

    # Reset parser state
    _cl._ch2_pending = None
    _cl._ch2_pending_ts = 0.0

    DELAY_SECS = 5

    out_lines = []
    def out(s=""):
        out_lines.append(s)
        print(s)

    out(f"{'='*120}")
    out(f"  CH2 Parser Simulation v2 — {target_date}")
    out(f"  ABOVE → auto-hold until Active | NEAR → 5s delay | Re-entry: tight SL, validated TGT")
    out(f"{'='*120}")
    out()

    for msg in messages:
        if not msg.text:
            continue

        text = msg.text.strip()
        ts = msg.date.astimezone(IST)
        ts_str = ts.strftime("%H:%M:%S")
        ts_epoch = ts.timestamp()
        upper = text.upper()

        # --- Check if queued signal's delay has expired ---
        if queued_signal and (ts_epoch - queued_ts) > DELAY_SECS:
            out(f"  ⏱  {DELAY_SECS}s elapsed — executing queued signal:")
            out(f"      {queued_signal.symbol} {int(queued_signal.strike)} {queued_signal.option_type} "
                f"trigger={queued_signal.trigger_price} SL={queued_signal.stop_loss} "
                f"TGT={queued_signal.targets[0]}")
            executed.append({"signal": queued_signal, "ts": queued_ts, "reason": "near_exec",
                             "entry_time": datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M")})
            queued_signal = None

        # --- Control messages ---
        # WAIT FOR TRIGGER
        if re.search(r'WAIT\s+FOR\s+TRIGGER', upper):
            if queued_signal:
                trigger_held = queued_signal
                trigger_held_msg_id = queued_msg_id
                held_sym = f"{queued_signal.symbol} {int(queued_signal.strike)} {queued_signal.option_type}"
                out(f"  ⏸  #{msg.id} @ {ts_str}: WAIT FOR TRIGGER — holding {held_sym}")
                queued_signal = None
            elif trigger_held:
                out(f"  ⏸  #{msg.id} @ {ts_str}: WAIT FOR TRIGGER (already holding)")
            else:
                out(f"  ⏸  #{msg.id} @ {ts_str}: WAIT FOR TRIGGER (nothing queued)")
            continue

        # Active / Actt
        if re.search(r'\bACTIVE\b|\bACTT\b', upper) and trigger_held:
            held_sym = f"{trigger_held.symbol} {int(trigger_held.strike)} {trigger_held.option_type}"
            out(f"  ▶  #{msg.id} @ {ts_str}: ACTIVE — executing held {held_sym}")
            executed.append({"signal": trigger_held, "ts": ts_epoch, "reason": "active_trigger",
                             "entry_time": ts.strftime("%H:%M")})
            trigger_held = None
            continue

        # Not active avoid
        if re.search(r'NOT\s+ACTIVE', upper):
            if queued_signal:
                sym = f"{queued_signal.symbol} {int(queued_signal.strike)} {queued_signal.option_type}"
                out(f"  ❌ #{msg.id} @ {ts_str}: NOT ACTIVE — cancelled queued {sym}")
                cancelled.append({"signal": queued_signal, "reason": "not_active"})
                queued_signal = None
            elif trigger_held:
                sym = f"{trigger_held.symbol} {int(trigger_held.strike)} {trigger_held.option_type}"
                out(f"  ❌ #{msg.id} @ {ts_str}: NOT ACTIVE — cancelled held {sym}")
                cancelled.append({"signal": trigger_held, "reason": "not_active"})
                trigger_held = None
            else:
                out(f"  ❌ #{msg.id} @ {ts_str}: NOT ACTIVE (nothing to cancel)")
            continue

        # Re-entry via reply with "again" — log only (disabled)
        if msg.reply_to and msg.reply_to.reply_to_msg_id and re.search(r'\bAGAIN\b', upper):
            reply_id = msg.reply_to.reply_to_msg_id
            orig = msg_by_id.get(reply_id)
            if orig and orig.text:
                sym_m = _CH2_SYMBOL_RE.search(orig.text)
                if sym_m:
                    raw_sym = sym_m.group(1).upper().strip()
                    raw_sym = re.sub(r'\s+', ' ', raw_sym)
                    if raw_sym == "BANK NIFTY":
                        raw_sym = "BANKNIFTY"
                    trade_sym = f"{raw_sym} {int(float(sym_m.group(2)))} {sym_m.group(3).upper()}"
                    out(f"  ℹ  #{msg.id} @ {ts_str}: RE-ENTRY detected (not executing) {trade_sym}")
                    out(f"      \"{text[:70]}\"")
                    reentries.append(trade_sym)
                    continue

        # --- Parse signal ---
        sig = parse_signal_ch2(text)

        if sig:
            sym = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"
            is_above = bool(re.search(r'\bABOVE\b', text, re.I)) or _cl._ch2_last_is_above

            if is_above:
                # ABOVE signal → auto-hold until Active
                if trigger_held:
                    old = f"{trigger_held.symbol} {int(trigger_held.strike)} {trigger_held.option_type}"
                    out(f"  ⚠  Replacing held {old}")
                trigger_held = sig
                trigger_held_msg_id = msg.id
                out(f"  🔒 #{msg.id} @ {ts_str}: ABOVE SIGNAL (auto-held) {sym} "
                    f"trigger={sig.trigger_price} SL={sig.stop_loss} TGT={sig.targets[0]}")
                continue

            # NEAR signal → queue with short delay
            out(f"  📊 #{msg.id} @ {ts_str}: NEAR SIGNAL {sym} trigger={sig.trigger_price} "
                f"SL={sig.stop_loss} TGT={sig.targets[0]}")

            if queued_signal:
                old = f"{queued_signal.symbol} {int(queued_signal.strike)} {queued_signal.option_type}"
                out(f"      (replacing queued {old})")
            queued_signal = sig
            queued_ts = ts_epoch
            queued_msg_id = msg.id
            out(f"      → Queued ({DELAY_SECS}s delay)")
            continue

        # Anything else — skip silently (LTP updates, commentary, etc.)

    # --- Flush any remaining queued signal ---
    if queued_signal:
        out(f"\n  ⏱  End of messages — executing remaining queued signal:")
        sym = f"{queued_signal.symbol} {int(queued_signal.strike)} {queued_signal.option_type}"
        out(f"      {sym}")
        executed.append({"signal": queued_signal, "ts": queued_ts, "reason": "end_flush",
                         "entry_time": datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M")})

    if trigger_held:
        sym = f"{trigger_held.symbol} {int(trigger_held.strike)} {trigger_held.option_type}"
        out(f"\n  ⚠  Signal still held at end (never activated): {sym}")

    # ============================================================
    # Results: simulate P&L for each executed signal
    # ============================================================
    out(f"\n{'='*120}")
    out(f"  EXECUTION RESULTS")
    out(f"{'='*120}")
    out(f"  {'#':<3} {'Time':<6} {'Symbol':<24} {'Trigger':>7} {'Entry':>7} {'SL':>7} {'TGT':>7} "
        f"{'Lots':>4} {'Qty':>5} {'Result':<8} {'P&L':>10} {'Reason':<12} {'Warn'}")
    out(f"  {'─'*118}")

    total_pnl = 0
    wins = 0
    losses = 0
    no_data = 0

    for i, ex in enumerate(executed, 1):
        sig = ex["signal"]
        entry_time = ex["entry_time"]
        reason = ex["reason"]
        sym_str = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"

        trade_info, result, pnl, inverted = simulate_trade(sig, entry_time)

        if trade_info is None:
            out(f"  {i:<3} {entry_time:<6} {sym_str:<24} {sig.trigger_price:>7.0f} {'':>7} "
                f"{sig.stop_loss:>7.0f} {sig.targets[0]:>7.0f} {'':>4} {'':>5} {result:<8} "
                f"{'':>10} {reason:<12}")
            no_data += 1
            continue

        entry = trade_info["entry"]
        lots = trade_info["lots"]
        qty = trade_info["qty"]
        warn = f"⚠{inverted.strip()}" if inverted else ""

        if pnl >= 0:
            wins += 1
            icon = "W"
        else:
            losses += 1
            icon = "L"
        total_pnl += pnl

        out(f"  {i:<3} {entry_time:<6} {sym_str:<24} {sig.trigger_price:>7.0f} {entry:>7.1f} "
            f"{sig.stop_loss:>7.0f} {sig.targets[0]:>7.0f} {lots:>4} {qty:>5} [{icon}] {result:<5} "
            f"{pnl:>+10,.0f} {reason:<12} {warn}")

    out(f"\n  {'─'*118}")
    out(f"  Total: {wins}W/{losses}L ({no_data} no data) | Simulated P&L: ₹{total_pnl:+,.0f}")

    # --- Compare with DB ---
    out(f"\n{'='*120}")
    out(f"  DB COMPARISON (actual trades today)")
    out(f"{'='*120}")

    db_rows = conn.execute("""
        SELECT id, ts, symbol, price, stop_price, target_price, pnl, status
        FROM trades WHERE ts >= ? AND ts < ? AND channel='ch2' ORDER BY ts
    """, (f"{target_date}T00:00:00", f"{target_date}T23:59:59")).fetchall()

    db_total = 0
    out(f"  {'ID':<5} {'Time':<6} {'Symbol':<24} {'Entry':>7} {'SL':>7} {'TGT':>7} {'P&L':>10} {'Status'}")
    out(f"  {'─'*90}")
    for row in db_rows:
        db_pnl = row["pnl"] or 0
        db_total += db_pnl
        ts = (row["ts"] or "")[11:16]
        out(f"  {row['id']:<5} {ts:<6} {row['symbol']:<24} {row['price']:>7.1f} "
            f"{row['stop_price']:>7.0f} {row['target_price']:>7.0f} {db_pnl:>+10,.0f} {row['status']}")

    out(f"\n  DB Total: ₹{db_total:+,.0f} ({len(db_rows)} trades)")

    # --- Cancelled signals ---
    if cancelled:
        out(f"\n{'='*120}")
        out(f"  CANCELLED SIGNALS ({len(cancelled)})")
        out(f"{'='*120}")
        for c in cancelled:
            s = c["signal"]
            out(f"  {s.symbol} {int(s.strike)} {s.option_type} — {c['reason']}")

    # --- Summary ---
    out(f"\n{'='*120}")
    out(f"  SUMMARY")
    out(f"{'='*120}")
    out(f"  New parser signals executed: {len(executed)}")
    out(f"  Signals cancelled:          {len(cancelled)}")
    out(f"  Re-entries detected:        {len(reentries)}")
    out(f"  Simulated P&L:              ₹{total_pnl:+,.0f}")
    out(f"  Actual DB P&L:              ₹{db_total:+,.0f}")
    out(f"  Difference:                 ₹{total_pnl - db_total:+,.0f}")
    out(f"{'='*120}")

    # --- Save to file ---
    out_file = os.path.join(os.path.dirname(__file__), "..", "data",
                            f"ch2_sim_{target_date}.txt")
    with open(out_file, "w") as f:
        f.write("\n".join(out_lines))
    print(f"\nResults saved to: {out_file}")


asyncio.run(main())

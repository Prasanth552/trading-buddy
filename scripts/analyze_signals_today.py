#!/usr/bin/env python3
"""Fetch today's CH1+CH2 Telegram messages, parse signals, simulate with actual candles.

Must kill channel_listener first (Telethon session lock).

Usage: .venv/bin/python3 scripts/analyze_signals_today.py [--date 2026-08-26]
"""
import sys, os, re, asyncio, argparse, sqlite3, time as _time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
PROFIT_FLOOR = 1500

parser = argparse.ArgumentParser()
parser.add_argument("--date", default=None)
parser.add_argument("--limit", type=int, default=600)
parser.add_argument("--max-loss", type=float, default=0,
                    help="Cap max loss per trade (dynamic qty sizing). 0 = fixed lots")
args = parser.parse_args()
MAX_LOSS = args.max_loss

target_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")
year, month, day = [int(x) for x in target_date.split("-")]
today_d = date(year, month, day)
day_start = datetime(year, month, day, 0, 0, 0, tzinfo=IST)
day_end = day_start + timedelta(days=1)

try:
    import config
    from src.notify.channel_listener import (
        ParsedSignal, parse_signal, parse_signal_ch2, _parse_signal_regex,
        _CH2_SYMBOL_RE, _CH2_ENTRY_RE, _CH2_TGT_RE, _CH2_SL_RE,
        _ch2_extract_targets, _ch2_extract_sl,
    )
    import src.notify.channel_listener as _cl
    from src.broker.upstox_data import UpstoxData, load_cached_token
    from src.broker.upstox_client import _expiry_to_date
except ImportError as e:
    print(f"ERROR: {e}\nRun from Trading-Buddy root with .venv/bin/python3")
    sys.exit(1)

# --- Telegram ---
api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
api_hash = os.getenv("TELEGRAM_API_HASH", "")
ch1_id = int(os.getenv("SIGNAL_CHANNEL_ID", "0"))
ch2_id = int(os.getenv("SIGNAL_CHANNEL2_ID", "0"))
import shutil
_data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
session_path = os.path.join(_data_dir, "telegram_reader.session")
_main_session = os.path.join(_data_dir, "telegram_user.session")
if not os.path.exists(session_path) and os.path.exists(_main_session):
    shutil.copy2(_main_session, session_path)

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


def _norm_channel_id(raw_id):
    if raw_id > 0:
        return int(f"-100{raw_id}")
    elif not str(raw_id).startswith("-100"):
        return int(f"-100{abs(raw_id)}")
    return raw_id


def resolve_instrument(symbol_str, use_monthly=False):
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

    if use_monthly and len(candidates) > 1:
        min_exp = today_d + timedelta(days=7)
        monthly_cands = [(e, i) for e, i in candidates if e >= min_exp]
        if monthly_cands:
            monthly_cands.sort(key=lambda x: x[0])
            inst = monthly_cands[0][1]
            return inst.get("instrument_key"), int(inst.get("lot_size", 1)) or 1, monthly_cands[0][0]

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

        if MAX_LOSS > 0 and low_pnl <= -MAX_LOSS:
            exit_price = entry - (MAX_LOSS / qty)
            return exit_price, "MAX_SL", inverted
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


def simulate_trade(sig, entry_time_str, lots_override=None, use_monthly=False):
    sym_str = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"
    base_sym = re.match(r"([A-Z&]+)", sig.symbol.upper().replace(" ", "")).group(1)
    is_index = base_sym in INDEX_SYMS

    if lots_override:
        lots = lots_override
    else:
        lots = 3 if is_index else 2

    inst_key, master_lot, exp_date = resolve_instrument(sym_str, use_monthly=use_monthly)
    if not inst_key:
        return None, "NO_INST", 0, "", exp_date

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
        return None, "NO_DATA", 0, "", exp_date

    filtered = [c for c in candles if c["date"][11:16] >= entry_time_str]
    if not filtered:
        filtered = candles

    entry = filtered[0]["open"]
    exit_price, result, inverted = walk_candles_floor(filtered, entry, sig.stop_loss, sig.targets[0], qty)
    pnl = (exit_price - entry) * qty

    return {
        "entry": entry, "exit": exit_price, "qty": qty, "lots": lots,
        "pnl": pnl, "result": result, "inverted": inverted,
    }, result, pnl, inverted, exp_date


async def main():
    from telethon import TelegramClient

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: Telethon session not authorized")
        return

    ch1_entity = _norm_channel_id(ch1_id)
    ch2_entity = _norm_channel_id(ch2_id)

    # --- Fetch CH1 ---
    print(f"Fetching CH1 messages for {target_date}...")
    ch1_msgs = []
    async for msg in client.iter_messages(ch1_entity, limit=args.limit, offset_date=day_end):
        if msg.date.astimezone(IST) < day_start:
            break
        ch1_msgs.append(msg)
    ch1_msgs.reverse()
    print(f"  CH1: {len(ch1_msgs)} messages")

    # --- Fetch CH2 ---
    print(f"Fetching CH2 messages for {target_date}...")
    ch2_msgs = []
    async for msg in client.iter_messages(ch2_entity, limit=args.limit, offset_date=day_end):
        if msg.date.astimezone(IST) < day_start:
            break
        ch2_msgs.append(msg)
    ch2_msgs.reverse()
    print(f"  CH2: {len(ch2_msgs)} messages")

    await client.disconnect()

    out_lines = []
    def out(s=""):
        out_lines.append(s)
        print(s)

    # ============================================================
    # CH1 Analysis — parse each message, simulate with monthly expiry, 2 lots
    # ============================================================
    out(f"\n{'='*130}")
    sl_label = f", ₹{MAX_LOSS:,.0f} hard SL" if MAX_LOSS else ""
    out(f"  CH1 SIGNALS — {target_date} — Monthly expiry, 2 lots{sl_label}, ₹{PROFIT_FLOOR:,} floor")
    out(f"{'='*130}")

    ch1_signals = []
    for msg in ch1_msgs:
        if not msg.text:
            continue
        sig = parse_signal(msg.text)
        if not sig:
            sig = _parse_signal_regex(msg.text)
        if sig:
            ts = msg.date.astimezone(IST)
            ch1_signals.append({"signal": sig, "msg": msg, "ts": ts,
                                "entry_time": ts.strftime("%H:%M")})

    out(f"  Parsed {len(ch1_signals)} signals from {len(ch1_msgs)} messages\n")
    out(f"  {'#':<3} {'Time':<6} {'Symbol':<28} {'Trigger':>7} {'Entry':>7} {'SL':>7} {'TGT':>7} "
        f"{'Lots':>4} {'Qty':>5} {'Result':<8} {'P&L':>10} {'Expiry':<12} {'Warn'}")
    out(f"  {'─'*130}")

    ch1_total = 0
    ch1_wins = 0
    ch1_losses = 0
    ch1_nodata = 0

    for i, item in enumerate(ch1_signals, 1):
        sig = item["signal"]
        entry_time = item["entry_time"]
        msg_id = item["msg"].id
        sym_str = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"

        trade_info, result, pnl, inverted, exp_date = simulate_trade(
            sig, entry_time, lots_override=2, use_monthly=True
        )

        exp_str = exp_date.strftime("%d%b") if exp_date else ""

        if trade_info is None:
            out(f"  {msg_id:<5} {entry_time:<6} {sym_str:<28} {sig.trigger_price:>7.0f} {'':>7} "
                f"{sig.stop_loss:>7.0f} {sig.targets[0]:>7.0f} {'':>4} {'':>5} {result:<8} "
                f"{'':>10} {exp_str:<12}")
            ch1_nodata += 1
            continue

        entry = trade_info["entry"]
        lots = trade_info["lots"]
        qty = trade_info["qty"]
        warn = f"⚠{inverted.strip()}" if inverted else ""

        if pnl >= 0:
            ch1_wins += 1
            icon = "W"
        else:
            ch1_losses += 1
            icon = "L"
        ch1_total += pnl

        out(f"  {msg_id:<5} {entry_time:<6} {sym_str:<28} {sig.trigger_price:>7.0f} {entry:>7.1f} "
            f"{sig.stop_loss:>7.0f} {sig.targets[0]:>7.0f} {lots:>4} {qty:>5} [{icon}] {result:<5} "
            f"{pnl:>+10,.0f} {exp_str:<12} {warn}")

    out(f"\n  CH1: {ch1_wins}W/{ch1_losses}L ({ch1_nodata} no data) | P&L: ₹{ch1_total:+,.0f}")

    # ============================================================
    # CH2 Analysis — full state machine (ABOVE/NEAR/WAIT/Active)
    # ============================================================
    out(f"\n{'='*130}")
    sl_label2 = f", ₹{MAX_LOSS:,.0f} hard SL" if MAX_LOSS else ""
    out(f"  CH2 SIGNALS — {target_date} — Index only, 3L idx{sl_label2}, ₹{PROFIT_FLOOR:,} floor")
    out(f"  ABOVE → auto-hold until Active | NEAR → 5s delay")
    out(f"{'='*130}")

    msg_by_id = {m.id: m for m in ch2_msgs}
    queued_signal = None
    queued_ts = 0.0
    queued_msg_id = 0
    trigger_held = None
    trigger_held_msg_id = 0
    last_executed_sig = None
    executed = []
    cancelled = []
    reentries = []

    _RE_REENTRY = re.compile(
        r'(?:'
        r'(?:ABOVE|NEAR)\s+(?:HIGH|SAME\s+RANGE|(\d+))\s*(?:AGAIN|NEW\s+BUY|FOCUS\s+WITH)'
        r'|SAME\s+RANGE\s+AGAIN'
        r'|NEAR\s+SAME\s+RANGE'
        r'|ABOVE\s+(\d+)\s+(?:NEW\s+BUY|AGAIN|FOCUS\s+WITH)'
        r'|ABOVE\s+HIGH\s+AGAIN'
        r'|ABOVE\s+(\d+)\s+(?:PE|CE)\s+SIDE'
        r'|BELOW\s+DAY\s+LOW\s+NEW\s+BUY'
        r')',
        re.IGNORECASE,
    )

    _cl._ch2_pending = None
    _cl._ch2_pending_ts = 0.0
    DELAY_SECS = 5

    for msg in ch2_msgs:
        if not msg.text:
            continue
        text = msg.text.strip()
        ts = msg.date.astimezone(IST)
        ts_str = ts.strftime("%H:%M:%S")
        ts_epoch = ts.timestamp()
        upper = text.upper()

        if queued_signal and (ts_epoch - queued_ts) > DELAY_SECS:
            out(f"  ⏱  {DELAY_SECS}s elapsed — executing queued signal:")
            out(f"      {queued_signal.symbol} {int(queued_signal.strike)} {queued_signal.option_type}")
            executed.append({"signal": queued_signal, "ts": queued_ts, "reason": "near_exec",
                             "entry_time": datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M")})
            last_executed_sig = queued_signal
            queued_signal = None

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

        if re.search(r'\bACTIVE\b|\bACTT\b', upper) and trigger_held:
            held_sym = f"{trigger_held.symbol} {int(trigger_held.strike)} {trigger_held.option_type}"
            out(f"  ▶  #{msg.id} @ {ts_str}: ACTIVE — executing held {held_sym}")
            executed.append({"signal": trigger_held, "ts": ts_epoch, "reason": "active_trigger",
                             "entry_time": ts.strftime("%H:%M")})
            last_executed_sig = trigger_held
            trigger_held = None
            continue

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

        # Re-entry: "Above X again", "same range again", "new buy", "Above High again"
        reentry_m = _RE_REENTRY.search(upper)
        if reentry_m and last_executed_sig:
            last = last_executed_sig
            re_sym = last.symbol.replace(" ", "").upper()
            if re_sym in INDEX_SYMS:
                new_entry = last.trigger_price
                for g in reentry_m.groups():
                    if g:
                        new_entry = float(g)
                        break
                side_m = re.search(r'(CE|PE)\s+SIDE', upper)
                opt_type = side_m.group(1) if side_m else last.option_type
                re_sig = ParsedSignal(
                    action="BUY", symbol=last.symbol, strike=last.strike,
                    option_type=opt_type, trigger_price=new_entry,
                    stop_loss=round(new_entry * 0.90), targets=last.targets,
                )
                sym_label = f"{last.symbol} {int(last.strike)} {opt_type}"
                has_above = bool(re.search(r'\bABOVE\b', upper))
                if has_above:
                    trigger_held = re_sig
                    trigger_held_msg_id = msg.id
                    out(f"  🔄 #{msg.id} @ {ts_str}: RE-ENTRY ABOVE (held) {sym_label} @ {new_entry}")
                else:
                    out(f"  🔄 #{msg.id} @ {ts_str}: RE-ENTRY {sym_label} @ {new_entry}")
                    executed.append({"signal": re_sig, "ts": ts_epoch, "reason": "re-entry",
                                     "entry_time": ts.strftime("%H:%M")})
                    last_executed_sig = re_sig
                    reentries.append(sym_label)
                continue

        if msg.reply_to and msg.reply_to.reply_to_msg_id and re.search(r'\bAGAIN\b', upper):
            reply_id = msg.reply_to.reply_to_msg_id
            orig = msg_by_id.get(reply_id)
            if orig and orig.text:
                orig_sig = parse_signal_ch2(orig.text)
                if orig_sig:
                    re_sym = orig_sig.symbol.replace(" ", "").upper()
                    sym_label = f"{orig_sig.symbol} {int(orig_sig.strike)} {orig_sig.option_type}"
                    if re_sym not in INDEX_SYMS:
                        out(f"  ⊘  #{msg.id} @ {ts_str}: RE-ENTRY SKIP non-index {sym_label}")
                        continue
                    reply_sig = parse_signal_ch2(text)
                    if reply_sig and reply_sig.stop_loss and reply_sig.targets:
                        orig_sig = reply_sig
                    out(f"  🔄 #{msg.id} @ {ts_str}: RE-ENTRY {sym_label} "
                        f"SL={orig_sig.stop_loss} TGT={orig_sig.targets[0]}")
                    executed.append({"signal": orig_sig, "ts": ts_epoch, "reason": "re-entry",
                                     "entry_time": ts.strftime("%H:%M")})
                    last_executed_sig = orig_sig
                    reentries.append(sym_label)
                    continue

        sig = parse_signal_ch2(text)
        if sig:
            ch2_sym = sig.symbol.replace(" ", "").upper()
            if ch2_sym not in INDEX_SYMS:
                out(f"  ⊘  #{msg.id} @ {ts_str}: SKIP non-index {sig.symbol} {int(sig.strike)} {sig.option_type}")
                continue

            sym = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"
            is_above = bool(re.search(r'\bABOVE\b', text, re.I)) or _cl._ch2_last_is_above

            if is_above:
                if trigger_held:
                    old = f"{trigger_held.symbol} {int(trigger_held.strike)} {trigger_held.option_type}"
                    out(f"  ⚠  Replacing held {old}")
                trigger_held = sig
                trigger_held_msg_id = msg.id
                out(f"  🔒 #{msg.id} @ {ts_str}: ABOVE (auto-held) {sym}")
                continue

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

    if queued_signal:
        sym = f"{queued_signal.symbol} {int(queued_signal.strike)} {queued_signal.option_type}"
        out(f"\n  ⏱  End — executing remaining queued: {sym}")
        executed.append({"signal": queued_signal, "ts": queued_ts, "reason": "end_flush",
                         "entry_time": datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M")})
        last_executed_sig = queued_signal

    if trigger_held:
        sym = f"{trigger_held.symbol} {int(trigger_held.strike)} {trigger_held.option_type}"
        out(f"\n  ⚠  Signal still held (never activated): {sym}")

    # --- CH2 Results ---
    out(f"\n  {'#':<3} {'Time':<6} {'Symbol':<24} {'Entry':>7} {'SL':>7} {'TGT':>7} "
        f"{'Lots':>4} {'Qty':>5} {'Result':<8} {'P&L':>10} {'Reason':<12} {'Warn'}")
    out(f"  {'─'*118}")

    ch2_total = 0
    ch2_wins = 0
    ch2_losses = 0
    ch2_nodata = 0

    for i, ex in enumerate(executed, 1):
        sig = ex["signal"]
        entry_time = ex["entry_time"]
        reason = ex["reason"]
        sym_str = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"

        trade_info, result, pnl, inverted, _ = simulate_trade(sig, entry_time)

        if trade_info is None:
            out(f"  {i:<3} {entry_time:<6} {sym_str:<24} {'':>7} "
                f"{sig.stop_loss:>7.0f} {sig.targets[0]:>7.0f} {'':>4} {'':>5} {result:<8} "
                f"{'':>10} {reason:<12}")
            ch2_nodata += 1
            continue

        entry = trade_info["entry"]
        lots = trade_info["lots"]
        qty = trade_info["qty"]
        warn = f"⚠{inverted.strip()}" if inverted else ""

        if pnl >= 0:
            ch2_wins += 1
            icon = "W"
        else:
            ch2_losses += 1
            icon = "L"
        ch2_total += pnl

        out(f"  {i:<3} {entry_time:<6} {sym_str:<24} {entry:>7.1f} "
            f"{sig.stop_loss:>7.0f} {sig.targets[0]:>7.0f} {lots:>4} {qty:>5} [{icon}] {result:<5} "
            f"{pnl:>+10,.0f} {reason:<12} {warn}")

    out(f"\n  CH2: {ch2_wins}W/{ch2_losses}L ({ch2_nodata} no data) | P&L: ₹{ch2_total:+,.0f}")

    # --- DB comparison ---
    out(f"\n{'='*130}")
    out(f"  DB TRADES — {target_date}")
    out(f"{'='*130}")
    for ch_label in ("ch1", "ch2"):
        db_rows = conn.execute("""
            SELECT id, ts, symbol, price, stop_price, target_price, pnl, status, channel
            FROM trades WHERE ts >= ? AND ts < ? AND channel=? ORDER BY ts
        """, (f"{target_date}T00:00:00", f"{target_date}T23:59:59", ch_label)).fetchall()
        db_pnl = sum(r["pnl"] or 0 for r in db_rows)
        out(f"  {ch_label.upper()}: {len(db_rows)} trades, DB P&L: ₹{db_pnl:+,.0f}")

    # --- Grand total ---
    grand = ch1_total + ch2_total
    out(f"\n{'='*130}")
    out(f"  GRAND TOTAL")
    out(f"{'='*130}")
    out(f"  CH1: {ch1_wins}W/{ch1_losses}L = ₹{ch1_total:+,.0f}")
    out(f"  CH2: {ch2_wins}W/{ch2_losses}L = ₹{ch2_total:+,.0f}")
    out(f"  Combined: ₹{grand:+,.0f}")
    if cancelled:
        out(f"  Cancelled: {len(cancelled)}")
    if reentries:
        out(f"  Re-entries (skipped): {len(reentries)}")
    out(f"{'='*130}")

    out_file = os.path.join(os.path.dirname(__file__), "..", "data",
                            f"signals_{target_date}.txt")
    with open(out_file, "w") as f:
        f.write("\n".join(out_lines))
    print(f"\nResults saved to: {out_file}")


asyncio.run(main())

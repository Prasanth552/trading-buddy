#!/usr/bin/env python3
"""Extract all CH2 messages for a given day, run through the full state machine,
simulate with actual candles, and produce a detailed report.

Monkey-patches time.time() so the split-message buffer works correctly
in replay mode (buffer uses wall-clock for timeout).

Usage:
  .venv/bin/python3 scripts/extract_ch2_today.py [--date 2026-09-01]
"""
import sys, os, re, asyncio, argparse, json, time as _time_mod
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

parser = argparse.ArgumentParser()
parser.add_argument("--date", default=None, help="Date YYYY-MM-DD (default: today)")
parser.add_argument("--lots", type=int, default=3)
args = parser.parse_args()

date_str = args.date or datetime.now(IST).strftime("%Y-%m-%d")
target_date = date(*[int(x) for x in date_str.split("-")])

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
    print(f"ERROR: {e}"); sys.exit(1)

api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
api_hash = os.getenv("TELEGRAM_API_HASH", "")
ch2_id = int(os.getenv("SIGNAL_CHANNEL2_ID", "0"))
import shutil
_data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
session_path = os.path.join(_data_dir, "telegram_reader.session")
_main_session = os.path.join(_data_dir, "telegram_user.session")
if not os.path.exists(session_path) and os.path.exists(_main_session):
    shutil.copy2(_main_session, session_path)

token = load_cached_token()
uclient = UpstoxData()
master = uclient._load_master()

INDEX_SYMS = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}
LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 40,
    "MIDCPNIFTY": 50, "CRUDEOIL": 100,
}
CH2_MAX_LOSS = 6000
PROFIT_FLOOR = 2000

_RE_REENTRY = re.compile(
    r'(?:'
    r'(?:ABOVE|NEAR)\.?\s+(?:LAST\s+SWING\s+HIGH|HIGH|SAME\s+(?:RANGE|LEVEL)|THIS\s+LEVEL|(\d+))\s*'
    r'(?:AGAIN|NEW\s+(?:BUY|TRADE)|FOCUS|(?:U\s+(?:CAN\s+)?)?PLAN|ENTER|WITH\s+TIGHT|OPEN|ALSO\s+OPEN)'
    r'|SAME\s+(?:RANGE|LEVEL)\s+(?:AGAIN|OPEN)'
    r'|NEAR\s+SAME\s+(?:RANGE|LEVEL)'
    r'|ABOVE\.?\s+(\d+)\s+(?:NEW\s+(?:BUY|TRADE)|AGAIN|FOCUS|(?:U\s+(?:CAN\s+)?)?PLAN|WITH\s+TIGHT|THIS\s+LEVEL)'
    r'|ABOVE\s+(?:HIGH|LAST\s+SWING\s+HIGH)\s+(?:AGAIN|FOCUS)'
    r'|ABOVE\s+(\d+)\s+(?:PE|CE)\s+SIDE'
    r'|(?:BELOW|BELWO)\s+(?:DAY\s+LOW|(\d+))\s+NEW\s+BUY'
    r')',
    re.IGNORECASE,
)

# Global fake clock for replay — parse_signal_ch2 uses time.time() internally
_fake_now = 0.0
_orig_time = _time_mod.time

def _replay_time():
    return _fake_now if _fake_now > 0 else _orig_time()


def _norm_channel_id(raw_id):
    if raw_id > 0:
        return int(f"-100{raw_id}")
    elif not str(raw_id).startswith("-100"):
        return int(f"-100{abs(raw_id)}")
    return raw_id


def resolve_instrument(symbol_str, ref_date):
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
        if exp is None or exp < ref_date:
            continue
        candidates.append((exp, inst))
    if not candidates:
        return None, None, None
    candidates.sort(key=lambda x: x[0])
    inst = candidates[0][1]
    return inst.get("instrument_key"), int(inst.get("lot_size", 1)) or 1, candidates[0][0]


def fetch_option_candles(inst_key, ref_date):
    y, m, d = ref_date.year, ref_date.month, ref_date.day
    from_dt = datetime(y, m, d, 9, 15, 0, tzinfo=IST)
    to_dt = datetime(y, m, d, 15, 30, 0, tzinfo=IST)
    for interval in ("5minute", "15minute"):
        try:
            candles = uclient.historical_data(inst_key, from_dt, to_dt, interval)
            _time_mod.sleep(0.3)
            if candles:
                return candles
        except Exception:
            _time_mod.sleep(0.5)
    return None


def walk_candles_detailed(candles, entry, sl, targets, qty):
    peak_pnl = 0
    floor_armed = False
    cur_sl = sl if sl and sl < entry else None
    remaining = [t for t in targets if t > entry] if targets else []

    trail = []
    for c in candles:
        low_pnl = (c["low"] - entry) * qty
        high_pnl = (c["high"] - entry) * qty
        trail.append({"time": c["date"][11:16], "o": c["open"], "h": c["high"],
                       "l": c["low"], "c": c["close"],
                       "pnl_range": f"₹{low_pnl:+,.0f} to ₹{high_pnl:+,.0f}"})

        # Check TGT BEFORE SL within same candle (optimistic ordering)
        if remaining and c["high"] >= remaining[0]:
            hit = remaining.pop(0)
            if not remaining:
                pnl = (hit - entry) * qty
                return hit, "TGT_ALL", pnl, peak_pnl, trail
            cur_sl = hit

        if CH2_MAX_LOSS > 0 and low_pnl <= -CH2_MAX_LOSS:
            exit_price = entry - (CH2_MAX_LOSS / qty)
            return exit_price, "MAX_SL", -CH2_MAX_LOSS, peak_pnl, trail

        if cur_sl and c["low"] <= cur_sl:
            pnl = (cur_sl - entry) * qty
            return cur_sl, "SL", pnl, peak_pnl, trail

        candle_peak = (c["high"] - entry) * qty
        peak_pnl = max(peak_pnl, candle_peak)
        if peak_pnl >= PROFIT_FLOOR:
            floor_armed = True
        if floor_armed and low_pnl <= PROFIT_FLOOR:
            floor_price = entry + (PROFIT_FLOOR / qty)
            return floor_price, "FLOOR", PROFIT_FLOOR, peak_pnl, trail

    exit_price = candles[-1]["close"]
    pnl = (exit_price - entry) * qty
    return exit_price, "EOD", pnl, peak_pnl, trail


def run_state_machine_with_debug(messages):
    """Full CH2 state machine (same as backtest) with per-message debug output."""
    global _fake_now

    msg_by_id = {m.id: m for m in messages}
    queued_signal = None
    queued_ts = 0.0
    queued_msg_id = 0
    trigger_held = None
    trigger_held_msg_id = 0
    last_executed_sig = None
    executed = []
    msg_signals = {}
    last_reentry_ts = 0.0
    debug_log = []

    DELAY_SECS = 5
    MARKET_CLOSE_HR = 15
    MARKET_CLOSE_MIN = 30

    _cl._ch2_pending = None
    _cl._ch2_pending_ts = 0.0

    for msg in messages:
        if not msg.text:
            continue
        text = msg.text.strip()
        ts = msg.date.astimezone(IST)
        ts_epoch = ts.timestamp()
        upper = text.upper()

        # Set fake clock so parse_signal_ch2 buffer timeout works
        _fake_now = ts_epoch

        if ts.hour > MARKET_CLOSE_HR or (ts.hour == MARKET_CLOSE_HR and ts.minute >= MARKET_CLOSE_MIN):
            continue

        # Flush delayed queue
        if queued_signal and (ts_epoch - queued_ts) > DELAY_SECS:
            executed.append({"signal": queued_signal, "ts": queued_ts, "reason": "near_exec",
                             "entry_time": datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M"),
                             "msg_id": queued_msg_id})
            last_executed_sig = queued_signal
            debug_log.append({"msg_id": msg.id, "action": f"FLUSHED queued: {queued_signal.symbol} {int(queued_signal.strike)} {queued_signal.option_type}"})
            queued_signal = None

        # WAIT FOR TRIGGER
        if re.search(r'WAIT\s+FOR\s+TRIGGER', upper):
            if queued_signal:
                trigger_held = queued_signal
                trigger_held_msg_id = queued_msg_id
                queued_signal = None
                debug_log.append({"msg_id": msg.id, "action": "WAIT_TRIGGER: moved queued → trigger_held"})
            else:
                debug_log.append({"msg_id": msg.id, "action": "WAIT_TRIGGER: nothing queued"})
            continue

        # ACTIVE
        clean_text = re.sub(r'[\U0001F600-\U0001FAFF☀-➿❤️‍\s]+', '', text).strip()
        if (re.search(r'\bACTIVE\b|\bACTT\b', upper) and len(clean_text) < 15):
            act_sig = None
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                act_sig = msg_signals.get(msg.reply_to.reply_to_msg_id)
            if not act_sig and trigger_held:
                act_sig = trigger_held
            if act_sig:
                executed.append({"signal": act_sig, "ts": ts_epoch, "reason": "active_trigger",
                                 "entry_time": ts.strftime("%H:%M"), "msg_id": msg.id})
                last_executed_sig = act_sig
                msg_signals[msg.id] = act_sig
                trigger_held = None
                debug_log.append({"msg_id": msg.id, "action": f"ACTIVE → EXECUTE: {act_sig.symbol} {int(act_sig.strike)} {act_sig.option_type}"})
            else:
                debug_log.append({"msg_id": msg.id, "action": "ACTIVE but nothing held/replied"})
            continue

        # FOCUS (reply)
        if (re.search(r'\bFOCUS\b', upper) and len(clean_text) < 15
                and msg.reply_to and msg.reply_to.reply_to_msg_id):
            ref_sig = msg_signals.get(msg.reply_to.reply_to_msg_id)
            if ref_sig:
                trigger_held = ref_sig
                msg_signals[msg.id] = ref_sig
                debug_log.append({"msg_id": msg.id, "action": f"FOCUS → trigger_held: {ref_sig.symbol} {int(ref_sig.strike)}"})
            continue

        # AVOID
        if (re.search(r'\bAVOID\b', upper) and len(clean_text) < 15
                and msg.reply_to and msg.reply_to.reply_to_msg_id):
            ref_sig = msg_signals.get(msg.reply_to.reply_to_msg_id)
            if ref_sig and trigger_held and trigger_held is ref_sig:
                trigger_held = None
                debug_log.append({"msg_id": msg.id, "action": "AVOID → cleared trigger_held"})
            continue

        # NOT ACTIVE
        if re.search(r'NOT\s+ACTIVE', upper):
            if queued_signal:
                queued_signal = None
                debug_log.append({"msg_id": msg.id, "action": "NOT_ACTIVE → cleared queue"})
            elif trigger_held:
                trigger_held = None
                debug_log.append({"msg_id": msg.id, "action": "NOT_ACTIVE → cleared trigger_held"})
            continue

        # RE-ENTRY patterns
        reentry_m = _RE_REENTRY.search(upper)
        if reentry_m:
            last = None
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                last = msg_signals.get(msg.reply_to.reply_to_msg_id)
            if not last:
                last = last_executed_sig
            if not last:
                debug_log.append({"msg_id": msg.id, "action": "RE-ENTRY pattern but no reference signal"})
                continue
            if ts_epoch - last_reentry_ts < 60:
                debug_log.append({"msg_id": msg.id, "action": "RE-ENTRY dedup (< 60s)"})
                continue
            re_sym = last.symbol.replace(" ", "").upper()
            if re_sym not in INDEX_SYMS:
                debug_log.append({"msg_id": msg.id, "action": f"RE-ENTRY skip (not index: {re_sym})"})
                continue
            new_entry = last.trigger_price
            for g in reentry_m.groups():
                if g:
                    val = float(g)
                    if val < 1000:
                        new_entry = val
                    break
            side_m = re.search(r'(CE|PE)\s+SIDE', upper)
            opt_type = side_m.group(1) if side_m else last.option_type
            sl_ratio = last.stop_loss / last.trigger_price if last.trigger_price > 0 else 0.90
            re_sig = ParsedSignal(
                action="BUY", symbol=last.symbol, strike=last.strike,
                option_type=opt_type, trigger_price=new_entry,
                stop_loss=round(new_entry * sl_ratio), targets=last.targets,
            )
            last_reentry_ts = ts_epoch
            msg_signals[msg.id] = re_sig
            has_above = bool(re.search(r'\bABOVE\b', upper))
            if has_above:
                trigger_held = re_sig
                trigger_held_msg_id = msg.id
                debug_log.append({"msg_id": msg.id, "action": f"RE-ENTRY ABOVE → trigger_held: {re_sig.symbol} {int(re_sig.strike)} {opt_type} entry={new_entry}"})
            else:
                executed.append({"signal": re_sig, "ts": ts_epoch, "reason": "re-entry",
                                 "entry_time": ts.strftime("%H:%M"), "msg_id": msg.id})
                last_executed_sig = re_sig
                debug_log.append({"msg_id": msg.id, "action": f"RE-ENTRY EXECUTE: {re_sig.symbol} {int(re_sig.strike)} {opt_type} entry={new_entry}"})
            continue

        # AGAIN (reply-based re-entry)
        if msg.reply_to and msg.reply_to.reply_to_msg_id and re.search(r'\bAGAIN\b', upper):
            reply_id = msg.reply_to.reply_to_msg_id
            orig = msg_by_id.get(reply_id)
            if orig and orig.text:
                # Set fake clock for parsing the original message
                _fake_now = ts_epoch
                orig_sig = parse_signal_ch2(orig.text)
                if orig_sig:
                    re_sym = orig_sig.symbol.replace(" ", "").upper()
                    if re_sym not in INDEX_SYMS:
                        continue
                    reply_sig = parse_signal_ch2(text)
                    if reply_sig and reply_sig.stop_loss and reply_sig.targets:
                        orig_sig = reply_sig
                    executed.append({"signal": orig_sig, "ts": ts_epoch, "reason": "re-entry",
                                     "entry_time": ts.strftime("%H:%M"), "msg_id": msg.id})
                    last_executed_sig = orig_sig
                    debug_log.append({"msg_id": msg.id, "action": f"AGAIN → EXECUTE: {orig_sig.symbol} {int(orig_sig.strike)} {orig_sig.option_type}"})
                    continue

        # Try parsing as new signal
        sig = parse_signal_ch2(text)
        if sig:
            ch2_sym = sig.symbol.replace(" ", "").upper()
            if ch2_sym not in INDEX_SYMS:
                msg_signals[msg.id] = sig
                debug_log.append({"msg_id": msg.id, "action": f"PARSED (non-index, skip): {sig.symbol} {int(sig.strike)} {sig.option_type}"})
                continue
            msg_signals[msg.id] = sig
            is_above = bool(re.search(r'\bABOVE\b', text, re.I)) or _cl._ch2_last_is_above

            if is_above:
                trigger_held = sig
                trigger_held_msg_id = msg.id
                debug_log.append({"msg_id": msg.id, "action": f"PARSED ABOVE → trigger_held: {sig.symbol} {int(sig.strike)} {sig.option_type} entry={sig.trigger_price} sl={sig.stop_loss} tgt={sig.targets}"})
                continue

            queued_signal = sig
            queued_ts = ts_epoch
            queued_msg_id = msg.id
            debug_log.append({"msg_id": msg.id, "action": f"PARSED NEAR → queued: {sig.symbol} {int(sig.strike)} {sig.option_type} entry={sig.trigger_price} sl={sig.stop_loss} tgt={sig.targets}"})
            continue

        # Check if buffer was updated
        if _cl._ch2_pending:
            debug_log.append({"msg_id": msg.id, "action": f"buffer: {_cl._ch2_pending}"})

    # Flush remaining
    if queued_signal:
        executed.append({"signal": queued_signal, "ts": queued_ts, "reason": "end_flush",
                         "entry_time": datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M"),
                         "msg_id": queued_msg_id})

    _fake_now = 0.0
    return executed, debug_log


async def main():
    from telethon import TelegramClient

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: Not authorized"); return

    ch2_entity = _norm_channel_id(ch2_id)
    fetch_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=IST)
    fetch_end = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59, tzinfo=IST)

    print(f"Fetching CH2 messages for {target_date} ...")
    all_msgs = []
    async for msg in client.iter_messages(ch2_entity, limit=5000, offset_date=fetch_end + timedelta(hours=1)):
        ts = msg.date.astimezone(IST)
        if ts.date() < target_date:
            break
        if ts.date() == target_date:
            all_msgs.append(msg)
    all_msgs.reverse()
    print(f"  {len(all_msgs)} messages\n")
    await client.disconnect()

    if not all_msgs:
        print("No messages found."); return

    # ================================================================
    # PART 1: Dump raw messages
    # ================================================================
    print(f"{'='*100}")
    print(f"  ALL CH2 MESSAGES — {target_date} ({len(all_msgs)} messages)")
    print(f"{'='*100}\n")

    msg_dump = []
    for i, msg in enumerate(all_msgs):
        ts = msg.date.astimezone(IST)
        text = msg.text or "(no text)"
        reply_to = ""
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            reply_to = f" [reply→{msg.reply_to.reply_to_msg_id}]"
        print(f"  [{i+1:>3}] {ts.strftime('%H:%M:%S')} ID={msg.id}{reply_to}")
        for line in text.splitlines():
            print(f"        {line}")
        print()
        msg_dump.append({"idx": i+1, "id": msg.id, "time": ts.strftime("%H:%M:%S"),
                         "text": text, "reply_to": msg.reply_to.reply_to_msg_id if msg.reply_to else None})

    # ================================================================
    # PART 2: Run full state machine with fake clock
    # ================================================================
    print(f"\n{'='*100}")
    print(f"  STATE MACHINE EXECUTION")
    print(f"{'='*100}\n")

    # Monkey-patch time.time in the channel_listener module so the
    # split-message buffer timeout works correctly in replay mode.
    # The listener uses `import time as _time; _time.time()` so we
    # need to patch the module-level _time reference.
    import time as _time_stdlib
    orig_stdlib = _time_stdlib.time
    _time_stdlib.time = _replay_time
    # _cl is already imported as src.notify.channel_listener
    orig_cl_time = _cl._time.time
    _cl._time.time = _replay_time

    try:
        executed, debug_log = run_state_machine_with_debug(all_msgs)
    finally:
        _time_stdlib.time = orig_stdlib
        _cl._time.time = orig_cl_time

    # Print debug log (only interesting actions)
    for d in debug_log:
        action = d["action"]
        if "buffer" in action.lower() and "PARSED" not in action and "EXECUTE" not in action:
            continue
        print(f"  ID={d['msg_id']:>5}: {action}")

    print(f"\n  Total signals executed: {len(executed)}")
    for ex in executed:
        sig = ex["signal"]
        print(f"    {ex['entry_time']} {sig.symbol} {int(sig.strike)} {sig.option_type} "
              f"entry={sig.trigger_price} sl={sig.stop_loss} tgt={sig.targets} "
              f"[{ex['reason']}]")

    # ================================================================
    # PART 3: Simulate with actual candles
    # ================================================================
    print(f"\n{'='*100}")
    print(f"  TRADE SIMULATION — Actual candle results")
    print(f"{'='*100}\n")

    total_pnl = 0
    trade_results = []

    for ex in executed:
        sig = ex["signal"]
        ts_dt = datetime.fromtimestamp(ex["ts"], IST)
        entry_time = ex["entry_time"]

        if ts_dt.hour > 15 or (ts_dt.hour == 15 and ts_dt.minute >= 30):
            print(f"  {entry_time} {sig.symbol} {int(sig.strike)} {sig.option_type} — SKIPPED (after market)")
            continue

        base_sym = re.match(r"([A-Z&]+)", sig.symbol.upper().replace(" ", "")).group(1)
        sym_str = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"

        inst_key, master_lot, exp_date = resolve_instrument(sym_str, target_date)
        if not inst_key:
            print(f"  {entry_time} {sym_str} — NO INSTRUMENT FOUND")
            continue

        lot_size = LOT_SIZES.get(base_sym, master_lot or 75)
        qty = lot_size * args.lots

        opt_candles = fetch_option_candles(inst_key, target_date)
        if not opt_candles:
            print(f"  {entry_time} {sym_str} — NO CANDLE DATA")
            continue

        filtered = [c for c in opt_candles if c["date"][11:16] >= entry_time]
        if not filtered:
            print(f"  {entry_time} {sym_str} — NO CANDLES AFTER ENTRY")
            continue

        entry_price = filtered[0]["open"]
        exit_price, result, pnl, peak_pnl, trail = walk_candles_detailed(
            filtered, entry_price, sig.stop_loss, list(sig.targets), qty
        )

        total_pnl += pnl
        max_high = max(c["h"] for c in trail) if trail else 0
        trade_results.append({
            "time": entry_time, "symbol": sym_str, "base_sym": base_sym,
            "entry": entry_price, "exit": exit_price, "result": result,
            "pnl": pnl, "peak_pnl": peak_pnl, "trigger": sig.trigger_price,
            "sl": sig.stop_loss, "targets": list(sig.targets),
            "reason": ex["reason"], "max_high": max_high,
        })

        icon = "WIN" if pnl >= 0 else "LOSS"
        print(f"  {entry_time} {sym_str} [{ex['reason']}]")
        print(f"    Signal:  trigger={sig.trigger_price} SL={sig.stop_loss} TGT={sig.targets}")
        print(f"    Actual:  entry={entry_price:.1f} → exit={exit_price:.1f} ({result})")
        print(f"    P&L:     ₹{pnl:+,.0f}  (peak: ₹{peak_pnl:+,.0f})  [{icon}]")

        if result in ("SL", "MAX_SL"):
            tgts_reachable = [t for t in sig.targets if t <= max_high]
            if tgts_reachable:
                print(f"    ⚠️  TGT {tgts_reachable} reachable (high={max_high:.1f}) but exited {result}")
            if peak_pnl > PROFIT_FLOOR:
                print(f"    ⚠️  Was profitable (peak ₹{peak_pnl:+,.0f} > floor ₹{PROFIT_FLOOR}) but ended {result}")
        print()

    # ================================================================
    # PART 4: Compare vs operator's recap
    # ================================================================
    print(f"\n{'='*100}")
    print(f"  SUMMARY — {target_date}")
    print(f"{'='*100}")
    wins = sum(1 for t in trade_results if t["pnl"] >= 0)
    losses = sum(1 for t in trade_results if t["pnl"] < 0)
    print(f"  State machine signals:  {len(executed)}")
    print(f"  Trades simulated:       {len(trade_results)}")
    if wins + losses > 0:
        print(f"  Win/Loss:               {wins}W / {losses}L ({wins/(wins+losses)*100:.0f}%)")
    print(f"  Total P&L:              ₹{total_pnl:+,.0f}")

    # Operator's EOD recap (extracted from messages)
    print(f"\n  --- Operator's EOD Recap ---")
    recap_trades = []
    for msg in all_msgs:
        if not msg.text:
            continue
        t = msg.text.upper()
        if "TRADE NO" in t:
            trade_num = re.search(r'TRADE NO\s*(\d+)', t)
            if trade_num:
                num = int(trade_num.group(1))
                is_hit = "TGT HIT" in t or "ALL TGT" in t
                is_sl = "SL HIT" in t or "SL HIT" in t
                is_not_active = "NOT ACTIVE" in t
                result = "TGT" if is_hit else ("SL" if is_sl else ("SKIP" if is_not_active else "?"))
                # Get the replied-to signal
                ref_id = msg.reply_to.reply_to_msg_id if msg.reply_to else None
                recap_trades.append({"num": num, "result": result, "ref_id": ref_id})
                print(f"    Trade #{num}: {result}" + (f" (ref ID={ref_id})" if ref_id else ""))

    recap_wins = sum(1 for t in recap_trades if t["result"] == "TGT")
    recap_sl = sum(1 for t in recap_trades if t["result"] == "SL")
    recap_skip = sum(1 for t in recap_trades if t["result"] == "SKIP")
    print(f"    Total: {recap_wins} TGT + {recap_sl} SL + {recap_skip} skip = {len(recap_trades)} trades")
    print(f"\n  --- Parser Coverage ---")
    print(f"    Operator reported:  {len(recap_trades)} trades")
    print(f"    We captured:        {len(trade_results)} trades")
    print(f"    Missing:            {len(recap_trades) - len(trade_results)} trades")

    # Save
    out_file = os.path.join(_data_dir, f"ch2_messages_{target_date}.json")
    with open(out_file, "w") as f:
        json.dump({"date": str(target_date), "messages": msg_dump,
                    "trades": trade_results, "total_pnl": total_pnl,
                    "recap": recap_trades}, f, indent=2, default=str)
    print(f"\n  Saved: {out_file}")

    txt_file = os.path.join(_data_dir, f"ch2_messages_{target_date}.txt")
    with open(txt_file, "w") as f:
        for m in msg_dump:
            f.write(f"[{m['idx']:>3}] {m['time']} | ID={m['id']}")
            if m['reply_to']:
                f.write(f" [reply→{m['reply_to']}]")
            f.write(f"\n{m['text']}\n\n")
    print(f"  Messages: {txt_file}")


asyncio.run(main())

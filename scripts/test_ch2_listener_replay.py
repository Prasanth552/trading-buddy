#!/usr/bin/env python3
"""Replay saved CH2 messages through the LISTENER's backported state machine.

Tests the actual channel_listener.py code (chain walking, root dedup,
instrument cooldown, re-entry validation) against saved message dumps.

Usage:
  .venv/bin/python3 scripts/test_ch2_listener_replay.py --date 2026-09-01
"""
import sys, os, re, json, time as _time_mod, argparse
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

parser = argparse.ArgumentParser()
parser.add_argument("--date", required=True, help="Date YYYY-MM-DD")
args = parser.parse_args()

data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
json_file = os.path.join(data_dir, f"ch2_messages_{args.date}.json")

if not os.path.exists(json_file):
    print(f"ERROR: {json_file} not found. Run extract_ch2_today.py first.")
    sys.exit(1)

with open(json_file) as f:
    data = json.load(f)

messages = data["messages"]
recap = data.get("recap", [])
print(f"Loaded {len(messages)} messages for {args.date}")
print(f"Operator recap: {len(recap)} trades\n")

# Import the listener module
import config
import src.notify.channel_listener as _cl
from src.notify.channel_listener import (
    ParsedSignal, parse_signal_ch2,
    _ch2_resolve_signal_via_chain, _ch2_find_root_signal_id,
    _ch2_can_execute, _ch2_inst_key,
    _RE_REENTRY, CH2_INDEX_ONLY,
)

# Fake clock for replay
_fake_now = 0.0
_orig_time = _time_mod.time
def _replay_time():
    return _fake_now if _fake_now > 0 else _orig_time()


def reset_ch2_state():
    """Clear all CH2 state in the listener module."""
    _cl._ch2_pending = None
    _cl._ch2_pending_ts = 0.0
    _cl._ch2_queued_signal = None
    _cl._ch2_queued_task = None
    _cl._ch2_trigger_held = None
    _cl._ch2_last_is_above = False
    _cl._ch2_last_executed = None
    _cl._ch2_last_reentry_ts = 0.0
    _cl._ch2_msg_signals.clear()
    _cl._ch2_msg_replies.clear()
    _cl._ch2_executed_roots.clear()
    _cl._ch2_inst_to_root.clear()
    _cl._ch2_inst_last_exec_ts.clear()
    _cl._ch2_trigger_held_msg_id = 0
    _cl._ch2_trigger_held_is_fallback = False
    _cl._ch2_buffer_start_id = None


def replay_messages(messages):
    """Replay CH2 messages through the listener's state machine logic."""
    global _fake_now

    reset_ch2_state()

    # Monkey-patch time
    _time_mod.time = _replay_time
    _cl._time.time = _replay_time

    executed = []
    debug_log = []

    DELAY_SECS = 5
    queued_signal = None
    queued_ts = 0.0
    queued_msg_id = 0

    try:
        for msg_data in messages:
            msg_id = msg_data["id"]
            text = msg_data["text"]
            reply_to_id = msg_data.get("reply_to")
            time_str = msg_data["time"]

            if not text or text == "(no text)":
                continue

            # Parse time → epoch
            h, m, s = map(int, time_str.split(":"))
            dt = datetime.strptime(args.date, "%Y-%m-%d").replace(
                hour=h, minute=m, second=s, tzinfo=IST
            )
            ts_epoch = dt.timestamp()
            _fake_now = ts_epoch

            # Skip after market close
            if h > 15 or (h == 15 and m >= 30):
                continue

            upper = text.strip().upper()
            clean_text = re.sub(r'[\U0001F600-\U0001FAFF☀-➿❤️‍\s]+', '', text).strip()

            # Track reply chain
            if reply_to_id:
                _cl._ch2_msg_replies[msg_id] = reply_to_id

            # Flush delayed queue
            if queued_signal and (ts_epoch - queued_ts) > DELAY_SECS:
                if _ch2_can_execute(queued_signal, queued_msg_id, origin_msg_id=queued_msg_id):
                    entry_time = datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M")
                    executed.append({
                        "signal": queued_signal, "time": entry_time,
                        "msg_id": queued_msg_id, "reason": "near_exec",
                    })
                    debug_log.append(f"  ID={msg_id}: FLUSHED queued: {_ch2_inst_key(queued_signal)}")
                else:
                    debug_log.append(f"  ID={msg_id}: FLUSH blocked by dedup: {_ch2_inst_key(queued_signal)}")
                queued_signal = None

            # --- WAIT FOR TRIGGER ---
            if re.search(r'WAIT\s+FOR\s+TRIGGER', upper):
                if queued_signal:
                    _cl._ch2_trigger_held = queued_signal
                    _cl._ch2_trigger_held_msg_id = queued_msg_id
                    _cl._ch2_trigger_held_is_fallback = False
                    queued_signal = None
                    debug_log.append(f"  ID={msg_id}: WAIT_TRIGGER: moved queued → trigger_held")
                else:
                    debug_log.append(f"  ID={msg_id}: WAIT_TRIGGER: nothing queued")
                continue

            # --- ACTIVE ---
            if re.search(r'\bACTIVE\b|\bACTT\b', upper) and len(clean_text) < 15:
                act_sig = None
                act_origin = msg_id
                act_is_fallback = False

                if reply_to_id:
                    act_sig = _ch2_resolve_signal_via_chain(reply_to_id)
                    if act_sig:
                        act_origin = msg_id
                        debug_log.append(f"  ID={msg_id}: ACTIVE via chain from #{reply_to_id}")

                if not act_sig and _cl._ch2_trigger_held:
                    act_sig = _cl._ch2_trigger_held
                    act_origin = _cl._ch2_trigger_held_msg_id or msg_id
                    act_is_fallback = _cl._ch2_trigger_held_is_fallback

                if act_sig:
                    _cl._ch2_trigger_held = None
                    key = _ch2_inst_key(act_sig)
                    if _ch2_can_execute(act_sig, msg_id, origin_msg_id=act_origin,
                                        is_fallback=act_is_fallback):
                        executed.append({
                            "signal": act_sig, "time": dt.strftime("%H:%M"),
                            "msg_id": msg_id, "reason": "active_trigger",
                        })
                        _cl._ch2_last_executed = act_sig
                        debug_log.append(f"  ID={msg_id}: ACTIVE → EXECUTE: {key}")
                    else:
                        debug_log.append(f"  ID={msg_id}: ACTIVE blocked: {key}")
                    _cl._ch2_msg_signals[msg_id] = act_sig
                else:
                    debug_log.append(f"  ID={msg_id}: ACTIVE but nothing held/replied")
                continue

            # --- FOCUS ---
            if (re.search(r'\bFOCUS\b', upper) and len(clean_text) < 15
                    and reply_to_id):
                ref_sig = _ch2_resolve_signal_via_chain(reply_to_id)
                if ref_sig:
                    _cl._ch2_trigger_held = ref_sig
                    _cl._ch2_trigger_held_msg_id = msg_id
                    _cl._ch2_trigger_held_is_fallback = False
                    _cl._ch2_msg_signals[msg_id] = ref_sig
                    debug_log.append(f"  ID={msg_id}: FOCUS → trigger_held: {_ch2_inst_key(ref_sig)}")
                else:
                    debug_log.append(f"  ID={msg_id}: FOCUS but chain resolution failed")
                continue

            # --- AVOID ---
            if (re.search(r'\bAVOID\b', upper) and len(clean_text) < 15
                    and reply_to_id):
                ref_sig = _ch2_resolve_signal_via_chain(reply_to_id)
                if ref_sig and _cl._ch2_trigger_held:
                    if _ch2_inst_key(_cl._ch2_trigger_held) == _ch2_inst_key(ref_sig):
                        _cl._ch2_trigger_held = None
                        debug_log.append(f"  ID={msg_id}: AVOID → cleared trigger_held")
                continue

            # --- NOT ACTIVE ---
            if re.search(r'NOT\s+ACTIVE', upper):
                if queued_signal:
                    queued_signal = None
                    debug_log.append(f"  ID={msg_id}: NOT_ACTIVE → cleared queue")
                elif _cl._ch2_trigger_held:
                    _cl._ch2_trigger_held = None
                    debug_log.append(f"  ID={msg_id}: NOT_ACTIVE → cleared trigger_held")
                continue

            # --- RE-ENTRY ---
            reentry_m = _RE_REENTRY.search(upper)
            if reentry_m:
                last = None
                is_fallback = False
                has_reply = bool(reply_to_id)

                if has_reply:
                    last = _ch2_resolve_signal_via_chain(reply_to_id)
                    if last:
                        debug_log.append(f"  ID={msg_id}: RE-ENTRY: chain resolved to {_ch2_inst_key(last)}")

                if not last and not has_reply:
                    last = _cl._ch2_last_executed
                    is_fallback = True
                    if last:
                        debug_log.append(f"  ID={msg_id}: RE-ENTRY: FALLBACK to {last.symbol} {int(last.strike)}")

                if not last:
                    debug_log.append(f"  ID={msg_id}: RE-ENTRY pattern but no reference signal")
                    continue

                re_sym = last.symbol.replace(" ", "").upper()
                if re_sym not in CH2_INDEX_ONLY:
                    debug_log.append(f"  ID={msg_id}: RE-ENTRY skip non-index: {re_sym}")
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

                max_tgt = max(last.targets) if last.targets else 0
                if new_entry > max_tgt * 1.5 and max_tgt > 0:
                    debug_log.append(f"  ID={msg_id}: RE-ENTRY SKIP: entry={new_entry} > max TGT={max_tgt} × 1.5")
                    continue

                sl_ratio = last.stop_loss / last.trigger_price if last.trigger_price > 0 else 0.90
                re_sig = ParsedSignal(
                    action="BUY", symbol=last.symbol, strike=last.strike,
                    option_type=opt_type, trigger_price=new_entry,
                    stop_loss=round(new_entry * sl_ratio), targets=last.targets,
                )

                if not is_fallback:
                    _cl._ch2_msg_signals[msg_id] = re_sig

                has_above = bool(re.search(r'\bABOVE\b', upper))
                if has_above:
                    _cl._ch2_trigger_held = re_sig
                    _cl._ch2_trigger_held_msg_id = msg_id
                    _cl._ch2_trigger_held_is_fallback = is_fallback
                    fb = " [FALLBACK]" if is_fallback else ""
                    debug_log.append(f"  ID={msg_id}: RE-ENTRY ABOVE → trigger_held: {_ch2_inst_key(re_sig)} entry={new_entry}{fb}")
                else:
                    if _ch2_can_execute(re_sig, msg_id, origin_msg_id=msg_id,
                                        is_fallback=is_fallback):
                        executed.append({
                            "signal": re_sig, "time": dt.strftime("%H:%M"),
                            "msg_id": msg_id, "reason": "re-entry",
                        })
                        _cl._ch2_last_executed = re_sig
                        debug_log.append(f"  ID={msg_id}: RE-ENTRY EXECUTE: {_ch2_inst_key(re_sig)} entry={new_entry}")
                    else:
                        debug_log.append(f"  ID={msg_id}: RE-ENTRY blocked by dedup: {_ch2_inst_key(re_sig)}")
                continue

            # --- Parse as signal ---
            had_pending = _cl._ch2_pending is not None
            sig = parse_signal_ch2(text)

            if not had_pending and _cl._ch2_pending is not None:
                _cl._ch2_buffer_start_id = msg_id

            if sig:
                ch2_sym = sig.symbol.replace(" ", "").upper()
                if ch2_sym not in CH2_INDEX_ONLY:
                    debug_log.append(f"  ID={msg_id}: PARSED (non-index, skip): {_ch2_inst_key(sig)}")
                    continue

                # Buffer complete tracking
                completed_buffer_start = None
                if had_pending and _cl._ch2_pending is None and _cl._ch2_buffer_start_id:
                    _cl._ch2_msg_signals[_cl._ch2_buffer_start_id] = sig
                    completed_buffer_start = _cl._ch2_buffer_start_id
                    _cl._ch2_buffer_start_id = None

                _cl._ch2_msg_signals[msg_id] = sig
                is_above = bool(re.search(r'\bABOVE\b', text, re.I)) or _cl._ch2_last_is_above

                if is_above:
                    _cl._ch2_trigger_held = sig
                    _cl._ch2_trigger_held_msg_id = completed_buffer_start or msg_id
                    _cl._ch2_trigger_held_is_fallback = False
                    debug_log.append(f"  ID={msg_id}: PARSED ABOVE → trigger_held: {_ch2_inst_key(sig)} entry={sig.trigger_price} sl={sig.stop_loss} tgt={sig.targets}")
                    continue

                # NEAR — queue
                queued_signal = sig
                queued_ts = ts_epoch
                queued_msg_id = msg_id
                debug_log.append(f"  ID={msg_id}: PARSED NEAR → queued: {_ch2_inst_key(sig)} entry={sig.trigger_price} sl={sig.stop_loss} tgt={sig.targets}")
                continue

        # Flush remaining
        if queued_signal:
            if _ch2_can_execute(queued_signal, queued_msg_id, origin_msg_id=queued_msg_id):
                entry_time = datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M")
                executed.append({
                    "signal": queued_signal, "time": entry_time,
                    "msg_id": queued_msg_id, "reason": "end_flush",
                })

    finally:
        _fake_now = 0.0
        _time_mod.time = _orig_time
        _cl._time.time = _orig_time

    return executed, debug_log


# Run replay
print("=" * 80)
print("  LISTENER REPLAY — using backported state machine")
print("=" * 80)
print()

executed, debug_log = replay_messages(messages)

for line in debug_log:
    print(line)

print(f"\n  Total signals executed: {len(executed)}")
for ex in executed:
    sig = ex["signal"]
    key = _ch2_inst_key(sig)
    print(f"    {ex['time']} {key} entry={sig.trigger_price} sl={sig.stop_loss} "
          f"tgt={sig.targets} [{ex['reason']}]")

# Compare with operator recap
print(f"\n{'=' * 80}")
print(f"  COMPARISON")
print(f"{'=' * 80}")
print(f"\n  Listener executed:    {len(executed)} trades")
print(f"  Operator reported:    {len(recap)} trades")
print(f"  Difference:           {len(executed) - len(recap):+d}")

if recap:
    recap_ids = {t.get("ref_id") for t in recap if t.get("ref_id")}
    our_roots = set()
    for ex in executed:
        root = _ch2_find_root_signal_id(ex["msg_id"])
        our_roots.add(root)

    matched = 0
    for rid in recap_ids:
        if rid in our_roots or rid - 1 in our_roots or rid + 1 in our_roots:
            matched += 1

    print(f"\n  Operator refs matched: {matched}/{len(recap_ids)}")
    unmatched_ops = []
    for t in recap:
        rid = t.get("ref_id")
        if rid and rid not in our_roots and rid - 1 not in our_roots and rid + 1 not in our_roots:
            unmatched_ops.append(f"    Trade #{t['num']}: {t['result']} (ref {rid})")
    if unmatched_ops:
        print(f"\n  Operator trades we MISSED:")
        for line in unmatched_ops:
            print(line)

    our_extra = []
    for ex in executed:
        root = _ch2_find_root_signal_id(ex["msg_id"])
        found = False
        for rid in recap_ids:
            if abs(root - rid) <= 1:
                found = True
                break
        if not found:
            key = _ch2_inst_key(ex["signal"])
            our_extra.append(f"    {ex['time']} {key} (root={root})")
    if our_extra:
        print(f"\n  EXTRA trades (not in operator's recap):")
        for line in our_extra:
            print(line)

print()

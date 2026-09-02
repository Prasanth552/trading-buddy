#!/usr/bin/env python3
"""Test CH2 listener state machine with synthetic reply-chain scenarios.

Covers:
  1. Signal → ACTIVE (direct reply)
  2. Signal → price update → ACTIVE (2-hop chain walk)
  3. Signal → re-entry (reply) → ACTIVE (chain through re-entry)
  4. Signal → FOCUS (reply) → ACTIVE (trigger_held from focus)
  5. Fallback re-entry (no reply) — should NOT poison chain
  6. Same-instrument cooldown (45 min)
  7. Root dedup (same root via different paths)
  8. Entry > max_tgt validation
  9. Buffer split (msg1: symbol, msg2: TGT/SL) + chain from buffer start
 10. AVOID cancels trigger_held
 11. NOT ACTIVE clears queue/held
 12. Two instruments interleaved — no cross-contamination

Usage:
  python3 scripts/test_ch2_chains.py
"""
import sys, os, re, time as _time_mod
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("UPSTOX_API_KEY", "test")
os.environ.setdefault("UPSTOX_API_SECRET", "test")
os.environ.setdefault("UPSTOX_REDIRECT_URI", "http://localhost")
os.environ.setdefault("TELEGRAM_API_ID", "0")
os.environ.setdefault("TELEGRAM_API_HASH", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

IST = ZoneInfo("Asia/Kolkata")

import src.notify.channel_listener as _cl
from src.notify.channel_listener import (
    ParsedSignal, parse_signal_ch2,
    _ch2_resolve_signal_via_chain, _ch2_find_root_signal_id,
    _ch2_can_execute, _ch2_inst_key,
    _RE_REENTRY, CH2_INDEX_ONLY,
)

# Fake clock
_fake_now = 0.0
_orig_time = _time_mod.time
def _replay_time():
    return _fake_now if _fake_now > 0 else _orig_time()


def reset_state():
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


def make_epoch(h, m, s=0):
    return datetime(2026, 9, 2, h, m, s, tzinfo=IST).timestamp()


def make_msg(msg_id, text, time_epoch, reply_to=None):
    return {"id": msg_id, "text": text, "time_epoch": time_epoch, "reply_to": reply_to}


def run_scenario(name, messages):
    """Run a list of messages through the state machine. Returns list of executed trades."""
    global _fake_now
    reset_state()
    _time_mod.time = _replay_time
    _cl._time.time = _replay_time

    executed = []
    queued = None  # (signal, ts, msg_id)
    trigger_held_is_reentry = False
    reentry_origins = set()
    DELAY = 5

    try:
        for msg in messages:
            msg_id = msg["id"]
            text = msg["text"]
            reply_to = msg.get("reply_to")
            ts_epoch = msg["time_epoch"]
            _fake_now = ts_epoch
            dt = datetime.fromtimestamp(ts_epoch, IST)

            upper = text.strip().upper()
            clean = re.sub(r'[\U0001F600-\U0001FAFF☀-➿❤️‍\s]+', '', text).strip()

            if reply_to:
                _cl._ch2_msg_replies[msg_id] = reply_to

            # Flush queue
            if queued and (ts_epoch - queued[1]) > DELAY:
                sig, qts, qid = queued
                if _ch2_can_execute(sig, qid, origin_msg_id=qid):
                    t = datetime.fromtimestamp(qts, IST).strftime("%H:%M")
                    executed.append({"inst": _ch2_inst_key(sig), "time": t, "reason": "near_exec", "msg_id": qid})
                    _cl._ch2_last_executed = sig
                queued = None

            # WAIT FOR TRIGGER
            if re.search(r'WAIT\s+FOR\s+TRIGGER', upper):
                if queued:
                    _cl._ch2_trigger_held = queued[0]
                    _cl._ch2_trigger_held_msg_id = queued[2]
                    _cl._ch2_trigger_held_is_fallback = False
                    trigger_held_is_reentry = False
                    queued = None
                continue

            # ACTIVE
            if (re.search(r'\bACTIVE\b|\bACTT\b', upper)
                    and not re.search(r'NOT\s+ACTIVE', upper)
                    and len(clean) < 15):
                act_sig = None
                act_origin = msg_id
                act_fb = False
                if reply_to:
                    act_sig = _ch2_resolve_signal_via_chain(reply_to)
                    if act_sig:
                        act_origin = msg_id
                act_is_reentry = bool(act_sig and reply_to and reply_to in reentry_origins)
                if not act_sig and _cl._ch2_trigger_held:
                    act_sig = _cl._ch2_trigger_held
                    act_origin = _cl._ch2_trigger_held_msg_id or msg_id
                    act_fb = _cl._ch2_trigger_held_is_fallback
                    act_is_reentry = trigger_held_is_reentry
                if act_sig:
                    _cl._ch2_trigger_held = None
                    trigger_held_is_reentry = False
                    if _ch2_can_execute(act_sig, msg_id, origin_msg_id=act_origin, is_fallback=act_fb,
                                        own_root=act_is_reentry):
                        executed.append({"inst": _ch2_inst_key(act_sig), "time": dt.strftime("%H:%M"),
                                         "reason": "active", "msg_id": msg_id})
                        _cl._ch2_last_executed = act_sig
                    _cl._ch2_msg_signals[msg_id] = act_sig
                continue

            # FOCUS
            if re.search(r'\bFOCUS\b', upper) and len(clean) < 15 and reply_to:
                ref = _ch2_resolve_signal_via_chain(reply_to)
                if ref:
                    _cl._ch2_trigger_held = ref
                    _cl._ch2_trigger_held_msg_id = msg_id
                    _cl._ch2_trigger_held_is_fallback = False
                    trigger_held_is_reentry = False
                    _cl._ch2_msg_signals[msg_id] = ref
                continue

            # AVOID
            if re.search(r'\bAVOID\b', upper) and len(clean) < 15 and reply_to:
                ref = _ch2_resolve_signal_via_chain(reply_to)
                if ref and _cl._ch2_trigger_held:
                    if _ch2_inst_key(_cl._ch2_trigger_held) == _ch2_inst_key(ref):
                        _cl._ch2_trigger_held = None
                continue

            # NOT ACTIVE
            if re.search(r'NOT\s+ACTIVE', upper):
                if queued:
                    queued = None
                elif _cl._ch2_trigger_held:
                    _cl._ch2_trigger_held = None
                continue

            # RE-ENTRY
            reentry_m = _RE_REENTRY.search(upper)
            if reentry_m:
                last = None
                is_fb = False
                has_reply = bool(reply_to)
                if has_reply:
                    last = _ch2_resolve_signal_via_chain(reply_to)
                if not last and not has_reply:
                    last = _cl._ch2_last_executed
                    is_fb = True
                if not last:
                    continue
                new_entry = last.trigger_price
                for g in reentry_m.groups():
                    if g:
                        v = float(g)
                        if v < 1000:
                            new_entry = v
                        break
                side_m = re.search(r'(CE|PE)\s+SIDE', upper)
                opt_type = side_m.group(1) if side_m else last.option_type
                max_tgt = max(last.targets) if last.targets else 0
                if new_entry > max_tgt * 1.5 and max_tgt > 0:
                    continue
                sl_ratio = last.stop_loss / last.trigger_price if last.trigger_price > 0 else 0.90
                re_sig = ParsedSignal(action="BUY", symbol=last.symbol, strike=last.strike,
                                      option_type=opt_type, trigger_price=new_entry,
                                      stop_loss=round(new_entry * sl_ratio), targets=last.targets)
                _cl._ch2_msg_signals[msg_id] = re_sig
                reentry_origins.add(msg_id)
                has_above = bool(re.search(r'\bABOVE\b', upper))
                has_new_price = (new_entry != last.trigger_price)
                if has_new_price and not has_above:
                    if _ch2_can_execute(re_sig, msg_id, origin_msg_id=msg_id, is_fallback=is_fb,
                                       own_root=True):
                        executed.append({"inst": _ch2_inst_key(re_sig), "time": dt.strftime("%H:%M"),
                                         "reason": "re-entry", "msg_id": msg_id})
                        _cl._ch2_last_executed = re_sig
                else:
                    _cl._ch2_trigger_held = re_sig
                    _cl._ch2_trigger_held_msg_id = msg_id
                    _cl._ch2_trigger_held_is_fallback = is_fb
                    trigger_held_is_reentry = True
                continue

            # Parse signal
            had_pending = _cl._ch2_pending is not None
            sig = parse_signal_ch2(text)
            if not had_pending and _cl._ch2_pending is not None:
                _cl._ch2_buffer_start_id = msg_id
            if sig:
                ch2_sym = sig.symbol.replace(" ", "").upper()
                if ch2_sym not in CH2_INDEX_ONLY:
                    continue
                completed_start = None
                if had_pending and _cl._ch2_pending is None and _cl._ch2_buffer_start_id:
                    _cl._ch2_msg_signals[_cl._ch2_buffer_start_id] = sig
                    completed_start = _cl._ch2_buffer_start_id
                    _cl._ch2_buffer_start_id = None
                _cl._ch2_msg_signals[msg_id] = sig
                is_above = bool(re.search(r'\bABOVE\b', text, re.I)) or _cl._ch2_last_is_above
                if is_above:
                    _cl._ch2_trigger_held = sig
                    _cl._ch2_trigger_held_msg_id = completed_start or msg_id
                    _cl._ch2_trigger_held_is_fallback = False
                    trigger_held_is_reentry = False
                    continue
                queued = (sig, ts_epoch, msg_id)
                continue

        # Flush remaining
        if queued:
            sig, qts, qid = queued
            if _ch2_can_execute(sig, qid, origin_msg_id=qid):
                t = datetime.fromtimestamp(qts, IST).strftime("%H:%M")
                executed.append({"inst": _ch2_inst_key(sig), "time": t, "reason": "near_exec", "msg_id": qid})

    finally:
        _fake_now = 0.0
        _time_mod.time = _orig_time
        _cl._time.time = _orig_time

    return executed


# ============================================================================
#  TEST SCENARIOS
# ============================================================================

passed = 0
failed = 0

def check(name, executed, expected_insts, expected_count=None):
    global passed, failed
    count = expected_count if expected_count is not None else len(expected_insts)
    actual_insts = [e["inst"] for e in executed]
    ok = len(executed) == count
    if expected_insts:
        ok = ok and actual_insts == expected_insts
    status = "PASS" if ok else "FAIL"
    if not ok:
        failed += 1
    else:
        passed += 1
    print(f"  [{status}] {name}")
    if not ok:
        print(f"         Expected {count} trades: {expected_insts}")
        print(f"         Got      {len(executed)} trades: {actual_insts}")
        for e in executed:
            print(f"           {e['time']} {e['inst']} [{e['reason']}]")
    return ok


print("=" * 80)
print("  CH2 LISTENER — Chain Resolution Test Suite")
print("=" * 80)
print()


# --- Test 1: Signal → ACTIVE (direct reply) ---
print("  1. Basic signal → ACTIVE flow")
msgs = [
    make_msg(100, "NIFTY 24200 PE\nABOVE 160", make_epoch(9, 15)),
    make_msg(101, "TGT 170/185/200\nSL 150", make_epoch(9, 15, 10)),
    make_msg(102, "Active ✅", make_epoch(9, 16), reply_to=100),
]
check("Signal → ACTIVE (direct reply to buffer start)",
      run_scenario("t1", msgs), ["NIFTY 24200 PE"])


# --- Test 2: 2-hop chain walk ---
print("\n  2. Multi-hop chain: signal → update → ACTIVE")
msgs = [
    make_msg(200, "NIFTY 24200 PE\nABOVE 160", make_epoch(9, 15)),
    make_msg(201, "TGT 170/185/200\nSL 150", make_epoch(9, 15, 10)),
    make_msg(202, "price near 158 now", make_epoch(9, 18), reply_to=200),  # update (not a signal)
    make_msg(203, "Active", make_epoch(9, 20), reply_to=202),  # replies to update, should walk to 200
]
check("ACTIVE walks 2 hops: 203→202→200",
      run_scenario("t2", msgs), ["NIFTY 24200 PE"])


# --- Test 3: Signal → re-entry (reply) → ACTIVE ---
print("\n  3. Signal → re-entry → ACTIVE (chain through re-entry)")
msgs = [
    make_msg(300, "NIFTY 24200 PE\nABOVE 160", make_epoch(9, 15)),
    make_msg(301, "TGT 170/185/200\nSL 150", make_epoch(9, 15, 10)),
    make_msg(302, "Active", make_epoch(9, 16), reply_to=300),
    make_msg(303, "Above 170 again", make_epoch(9, 40), reply_to=300),  # re-entry via chain
    make_msg(304, "Active", make_epoch(9, 41), reply_to=303),
]
# Re-entry is a legitimate new trade when confirmed by ACTIVE
check("Re-entry ACTIVE → 2 trades (own_root bypasses root dedup)",
      run_scenario("t3", msgs), ["NIFTY 24200 PE", "NIFTY 24200 PE"])


# --- Test 4: FOCUS → ACTIVE ---
print("\n  4. Signal → FOCUS → ACTIVE")
msgs = [
    make_msg(400, "NIFTY 24150 PE\nABOVE 125", make_epoch(9, 30)),
    make_msg(401, "TGT 135/150/180\nSL 115", make_epoch(9, 30, 10)),
    make_msg(402, "some other message", make_epoch(9, 35)),
    make_msg(403, "Focus", make_epoch(9, 40), reply_to=400),  # puts 24150 PE into trigger_held
    make_msg(404, "Active", make_epoch(9, 42)),  # no reply → uses trigger_held from FOCUS
]
check("FOCUS loads trigger_held, ACTIVE executes it",
      run_scenario("t4", msgs), ["NIFTY 24150 PE"])


# --- Test 5: Fallback re-entry doesn't poison chain ---
print("\n  5. Fallback re-entry (no reply) must NOT become chain target")
msgs = [
    make_msg(500, "NIFTY 24200 PE\nABOVE 160", make_epoch(9, 15)),
    make_msg(501, "TGT 170/185/200\nSL 150", make_epoch(9, 15, 10)),
    make_msg(502, "Active", make_epoch(9, 16), reply_to=500),
    # Fallback re-entry (no reply_to) — should NOT register in msg_signals
    make_msg(503, "Above 170 again", make_epoch(9, 20)),  # fallback to last_executed
    # This ACTIVE replies to 503 — if 503 was in msg_signals, it'd resolve to 24200 PE
    # and try to execute (root dedup should catch it). But 503 shouldn't be in msg_signals at all.
    make_msg(504, "Active", make_epoch(9, 22), reply_to=503),
]
result = run_scenario("t5", msgs)
# 503 is ABOVE → trigger_held (but as fallback, so trigger_held_is_fallback=True)
# 504: ACTIVE with reply to 503. 503 is NOT in msg_signals (fallback protection).
# So chain walk from 503 finds nothing. Falls back to trigger_held (which is the fallback re-entry).
# Root dedup should catch it since it's same root.
check("Fallback re-entry: only 1 trade (root dedup catches ACTIVE)",
      result, ["NIFTY 24200 PE"])


# --- Test 6: Instrument cooldown ---
print("\n  6. Same instrument within 10 min → blocked")
msgs = [
    make_msg(600, "NIFTY 24050 CE\nNEAR 85", make_epoch(11, 13)),
    make_msg(601, "TGT 95/110/130\nSL 77", make_epoch(11, 13, 10)),
    # 8 min later: different root, same instrument — within 10m cooldown
    make_msg(610, "NIFTY 24050 CE\nABOVE 104", make_epoch(11, 21)),
    make_msg(611, "TGT 95/110/130\nSL 94", make_epoch(11, 21, 10)),
    make_msg(612, "Active", make_epoch(11, 22), reply_to=610),
]
check("Second NIFTY 24050 CE blocked (8m < 10m cooldown)",
      run_scenario("t6", msgs), ["NIFTY 24050 CE"])


# --- Test 7: Instrument cooldown expires → allowed ---
print("\n  7. Same instrument after 20 min → allowed")
msgs = [
    make_msg(700, "NIFTY 24000 PE\nNEAR 24", make_epoch(13, 0)),
    make_msg(701, "TGT 38/50/70\nSL 19", make_epoch(13, 0, 10)),
    make_msg(702, "filler msg", make_epoch(13, 0, 20)),  # triggers flush at 13:00:20 → cooldown starts here
    # 50 min later: new signal, same instrument — should be allowed (49m40s > 45m)
    make_msg(710, "NIFTY 24000 PE\nNEAR 35", make_epoch(13, 50)),
    make_msg(711, "TGT 44/60/80\nSL 30", make_epoch(13, 50, 10)),
]
check("Second NIFTY 24000 PE allowed (50m > 10m)",
      run_scenario("t7", msgs), ["NIFTY 24000 PE", "NIFTY 24000 PE"])


# --- Test 8: Entry > max_tgt validation ---
print("\n  8. Re-entry with entry > max_tgt × 1.5 → skipped")
msgs = [
    make_msg(800, "NIFTY 24150 PE\nABOVE 125", make_epoch(9, 30)),
    make_msg(801, "TGT 135/150/180\nSL 115", make_epoch(9, 30, 10)),
    make_msg(802, "Active", make_epoch(9, 31), reply_to=800),
    # Fallback re-entry with SENSEX-level price (451) applied to NIFTY PE (max_tgt=180)
    make_msg(803, "Above 451 focus", make_epoch(10, 0)),  # 451 > 180 * 1.5 = 270
]
check("Re-entry skipped: entry 451 > max_tgt 180 × 1.5",
      run_scenario("t8", msgs), ["NIFTY 24150 PE"])


# --- Test 9: Split buffer + chain from buffer start ---
print("\n  9. Split buffer: reply to msg1 resolves correctly")
msgs = [
    make_msg(900, "NIFTY 24200 PE\nABOVE 160", make_epoch(9, 15)),  # buffer start
    make_msg(901, "TGT 170/185/200\nSL 150", make_epoch(9, 15, 10)),  # buffer complete → sig registered under both 900 and 901
    make_msg(902, "price update 158", make_epoch(9, 18), reply_to=900),  # replies to buffer start
    make_msg(903, "Active", make_epoch(9, 20), reply_to=902),  # walks: 903→902→900 → finds signal
]
check("ACTIVE via chain through buffer start ID",
      run_scenario("t9", msgs), ["NIFTY 24200 PE"])


# --- Test 10: AVOID cancels trigger_held ---
print("\n  10. AVOID cancels held signal")
msgs = [
    make_msg(1000, "NIFTY 24200 PE\nABOVE 160", make_epoch(9, 15)),
    make_msg(1001, "TGT 170/185/200\nSL 150", make_epoch(9, 15, 10)),
    make_msg(1002, "Avoid", make_epoch(9, 18), reply_to=1000),
    make_msg(1003, "Active", make_epoch(9, 20)),  # trigger_held was cleared by AVOID
]
check("AVOID clears trigger_held → ACTIVE has nothing",
      run_scenario("t10", msgs), [], expected_count=0)


# --- Test 11: NOT ACTIVE clears queue ---
print("\n  11. NOT ACTIVE cancels queued signal")
msgs = [
    make_msg(1100, "NIFTY 24050 CE\nNEAR 85", make_epoch(11, 13)),
    make_msg(1101, "TGT 95/110/130\nSL 77", make_epoch(11, 13, 10)),
    make_msg(1102, "Not active avoid", make_epoch(11, 13, 12)),  # within 5s delay
]
check("NOT ACTIVE within 5s delay → no execution",
      run_scenario("t11", msgs), [], expected_count=0)


# --- Test 12: Two instruments interleaved ---
print("\n  12. Two instruments interleaved — no cross-contamination")
msgs = [
    make_msg(1200, "NIFTY 24200 PE\nABOVE 160", make_epoch(9, 15)),
    make_msg(1201, "TGT 170/185/200\nSL 150", make_epoch(9, 15, 10)),
    make_msg(1210, "SENSEX 76800 CE\nABOVE 410", make_epoch(9, 22)),
    make_msg(1211, "TGT 430/470/520\nSL 380", make_epoch(9, 22, 10)),
    make_msg(1202, "Active", make_epoch(9, 25), reply_to=1200),  # NIFTY 24200 PE
    make_msg(1212, "Active", make_epoch(9, 29), reply_to=1210),  # SENSEX 76800 CE
]
check("Two instruments each get their own ACTIVE",
      run_scenario("t12", msgs), ["NIFTY 24200 PE", "SENSEX 76800 CE"])


# --- Test 13: Root dedup across different paths ---
print("\n  13. Same root via FOCUS and re-entry → only one execution")
msgs = [
    make_msg(1300, "NIFTY 24150 PE\nABOVE 125", make_epoch(9, 30)),
    make_msg(1301, "TGT 135/150/180\nSL 115", make_epoch(9, 30, 10)),
    make_msg(1302, "Focus", make_epoch(9, 35), reply_to=1300),
    make_msg(1303, "Active", make_epoch(9, 36)),  # executes via FOCUS → trigger_held
    # Re-entry on same signal
    make_msg(1304, "Above 130 again", make_epoch(9, 40), reply_to=1300),
    make_msg(1305, "Active", make_epoch(9, 41), reply_to=1304),  # should be deduped
]
check("FOCUS exec + re-entry ACTIVE on same root → 1 trade",
      run_scenario("t13", msgs), ["NIFTY 24150 PE"])


# --- Test 14: 3-hop chain walk ---
print("\n  14. Deep chain: signal → reply → reply → ACTIVE (3 hops)")
msgs = [
    make_msg(1400, "NIFTY 24250 PE\nABOVE 138", make_epoch(12, 0)),
    make_msg(1401, "TGT 148/160/180\nSL 128", make_epoch(12, 0, 10)),
    make_msg(1402, "watching 136", make_epoch(12, 5), reply_to=1400),
    make_msg(1403, "now at 139", make_epoch(12, 10), reply_to=1402),
    make_msg(1404, "Active", make_epoch(12, 12), reply_to=1403),  # walks 1404→1403→1402→1400
]
check("3-hop chain walk resolves correctly",
      run_scenario("t14", msgs), ["NIFTY 24250 PE"])


# --- Test 15: Re-entry always held (needs ACTIVE to execute) ---
print("\n  15. Re-entry (NEAR or ABOVE) always held for ACTIVE")
msgs = [
    make_msg(1500, "NIFTY 24050 PE\nNEAR 15", make_epoch(13, 5)),
    make_msg(1501, "TGT 26/38/50\nSL 15", make_epoch(13, 5, 10)),
    make_msg(1502, "some filler", make_epoch(13, 5, 20)),
    # Re-entry 46 min later — goes to trigger_held, not executed
    make_msg(1503, "Near same range again", make_epoch(13, 51), reply_to=1500),
    # Without ACTIVE, only the original trade executes
]
check("NEAR re-entry held (no ACTIVE → only 1 trade)",
      run_scenario("t15", msgs), ["NIFTY 24050 PE"])


# --- Test 15b: Re-entry + ACTIVE → executes ---
print("\n  15b. Re-entry + ACTIVE → second trade executes")
msgs = [
    make_msg(1510, "NIFTY 24050 PE\nNEAR 15", make_epoch(13, 5)),
    make_msg(1511, "TGT 26/38/50\nSL 15", make_epoch(13, 5, 10)),
    make_msg(1512, "some filler", make_epoch(13, 5, 20)),
    # Re-entry 46 min later — held
    make_msg(1513, "Near same range again", make_epoch(13, 51), reply_to=1510),
    # ACTIVE confirms re-entry
    make_msg(1514, "Active", make_epoch(13, 52)),
]
check("NEAR re-entry + ACTIVE → 2 trades",
      run_scenario("t15b", msgs), ["NIFTY 24050 PE", "NIFTY 24050 PE"])


# --- Test 15c: Re-entry with specific new price → executes immediately ---
print("\n  15c. Re-entry with new price (Near 320 try with tight sl) → executes")
msgs = [
    make_msg(1520, "SENSEX 76400 PE\nABOVE 345", make_epoch(9, 27)),
    make_msg(1521, "TGT 365/400/460\nSL 315", make_epoch(9, 27, 10)),
    make_msg(1522, "Active", make_epoch(9, 28), reply_to=1520),
    # Operator gives new entry at 320 (different from original 345) — 20 min later
    make_msg(1523, "Near 320 try with tight sl", make_epoch(9, 48), reply_to=1520),
]
check("Near 320 (new price) → executes without ACTIVE",
      run_scenario("t15c", msgs), ["SENSEX 76400 PE", "SENSEX 76400 PE"])


# --- Test 15d: Re-entry guidance (same price) + ACTIVE → new trade via own_root ---
print("\n  15d. Re-entry guidance (\"Above day high again focus\") + ACTIVE → new trade")
msgs = [
    make_msg(1530, "SENSEX 76400 PE\nABOVE 345", make_epoch(9, 27)),
    make_msg(1531, "TGT 365/400/460\nSL 315", make_epoch(9, 27, 10)),
    make_msg(1532, "Active", make_epoch(9, 28), reply_to=1530),
    # Re-entry guidance (same price, has ABOVE) — held as trigger_held with is_reentry=True
    make_msg(1533, "Above day high again focus with tight sl", make_epoch(9, 48), reply_to=1530),
    # ACTIVE confirms re-entry — should execute as NEW trade (own_root=True, not blocked by root dedup)
    make_msg(1534, "Active", make_epoch(9, 49), reply_to=1533),
]
check("Re-entry guidance + ACTIVE → 2 trades (own_root bypasses root dedup)",
      run_scenario("t15d", msgs), ["SENSEX 76400 PE", "SENSEX 76400 PE"])


# --- Test 16: Fallback re-entry respects inst_to_root dedup ---
print("\n  16. Fallback re-entry dedup via inst_to_root")
msgs = [
    make_msg(1600, "NIFTY 24100 CE\nNEAR 60", make_epoch(11, 30)),
    make_msg(1601, "TGT 70/85/100\nSL 53", make_epoch(11, 30, 10)),
    make_msg(1602, "some filler", make_epoch(11, 30, 20)),
    # Fallback re-entry (no reply) — same instrument, within cooldown
    make_msg(1603, "Above 65 again", make_epoch(11, 35)),
    make_msg(1604, "Active", make_epoch(11, 36)),
]
# 1603 is fallback (no reply), ABOVE → trigger_held. 1604 ACTIVE uses trigger_held.
# Instrument cooldown: 11:35 is 5m from 11:30 → blocked
check("Fallback re-entry blocked by instrument cooldown",
      run_scenario("t16", msgs), ["NIFTY 24100 CE"])


# ============================================================================
print(f"\n{'=' * 80}")
print(f"  RESULTS: {passed} passed, {failed} failed out of {passed + failed}")
print(f"{'=' * 80}")

sys.exit(1 if failed else 0)

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
        ParsedSignal, parse_signal_ch2, CH2_INDEX_ONLY,
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
CH2_MAX_LOSS = 4000
PROFIT_FLOOR = 2000

_RE_REENTRY = re.compile(
    r'(?:'
    r'(?:ABOVE|NEAR)\.?\s+(?:LAST\s+SWING\s+HIGH|HIGH|SAME\s+(?:RANGE|LEVEL)|THIS\s+LEVEL|(\d+))\s*'
    r'(?:AGAIN|NEW\s+(?:BUY|TRADE)|FOCUS|(?:U\s+(?:CAN\s+)?)?PLAN|ENTER|WITH\s+TIGHT|OPEN|ALSO\s+OPEN|TRY\s+WITH\s+TIGHT)'
    r'|SAME\s+(?:RANGE|LEVEL)\s+(?:AGAIN|OPEN|FOCUS)'
    r'|NEAR\s+SAME\s+(?:RANGE|LEVEL)'
    r'|ABOVE\.?\s+(\d+)\s+(?:NEW\s+(?:BUY|TRADE)|AGAIN|FOCUS|(?:U\s+(?:CAN\s+)?)?PLAN|WITH\s+TIGHT|THIS\s+LEVEL|KEEP\s+YOUR\s+EYES)'
    r'|ABOVE\s+(?:HIGH|LAST\s+SWING\s+HIGH|DAY\s+HIGH)\s+(?:AGAIN|FOCUS)'
    r'|ABOVE\s+(\d+)\s+(?:PE|CE)\s+SIDE'
    r'|(?:BELOW|BELWO)\s+(?:DAY\s+LOW|(\d+))\s+NEW\s+BUY'
    r'|SL\s+HIT\s*[,.]?\s*(?:REBUY|RE[\s-]*BUY|RE[\s-]*ENTER)\s+(?:FROM\s+)?(?:LOW\s+)?(?:NEAR\s+)?(\d+(?:\.\d+)?)?'
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


_SYMBOL_ALIASES = {
    "BAJAJAUTO": "BAJAJ-AUTO",
    "BAJAJ AUTO": "BAJAJ-AUTO",
    "KALYANJIL": "KALYANKJIL",
    "LIC": "LICI",
    "M&M": "M_M",
    "M&MFIN": "M_MFIN",
}


def resolve_instrument(symbol_str, ref_date):
    parts = symbol_str.strip().split()
    if len(parts) < 3:
        return None, None, None
    opt_type = parts[-1]
    strike = float(parts[-2])
    sym = "".join(parts[:-2]).upper()
    sym = _SYMBOL_ALIASES.get(sym, sym)
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
    if not candidates and strike >= 10000:
        alt_strike = strike / 10
        for inst in master:
            seg = inst.get("segment", "")
            if seg not in ("NSE_FO", "BSE_FO", "MCX_FO"):
                continue
            if inst.get("asset_symbol", "").upper() != sym:
                continue
            if inst.get("instrument_type") != opt_type:
                continue
            if abs(float(inst.get("strike_price", -1)) - alt_strike) > 0.01:
                continue
            exp = _expiry_to_date(inst.get("expiry"))
            if exp is None or exp < ref_date:
                continue
            candidates.append((exp, inst))
        if candidates:
            print(f"    Strike fix: {sym} {int(strike)} → {int(alt_strike)} (operator extra zero)")
    # Nearest-strike fallback: snap to closest available strike
    if not candidates:
        search_strike = strike / 10 if strike >= 10000 else strike
        nearest = None
        nearest_dist = float("inf")
        for inst in master:
            seg = inst.get("segment", "")
            if seg not in ("NSE_FO", "BSE_FO", "MCX_FO"):
                continue
            if inst.get("asset_symbol", "").upper() != sym:
                continue
            if inst.get("instrument_type") != opt_type:
                continue
            exp = _expiry_to_date(inst.get("expiry"))
            if exp is None or exp < ref_date:
                continue
            inst_strike = float(inst.get("strike_price", -1))
            dist = abs(inst_strike - search_strike)
            if dist < nearest_dist:
                nearest_dist = dist
                nearest = (exp, inst, inst_strike)
        if nearest and nearest_dist <= search_strike * 0.02:
            candidates.append((nearest[0], nearest[1]))
            print(f"    Strike snap: {sym} {int(strike)} → {int(nearest[2])} (nearest, delta={nearest_dist:.0f})")
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

        if cur_sl and c["low"] <= cur_sl:
            sl_pnl = (cur_sl - entry) * qty
            if sl_pnl >= -CH2_MAX_LOSS:
                return cur_sl, "SL", sl_pnl, peak_pnl, trail

        if CH2_MAX_LOSS > 0 and low_pnl <= -CH2_MAX_LOSS:
            exit_price = entry - (CH2_MAX_LOSS / qty)
            return exit_price, "MAX_SL", -CH2_MAX_LOSS, peak_pnl, trail

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


def _inst_key(sig):
    """Unique instrument identifier: 'NIFTY 24200 PE'."""
    return f"{sig.symbol.replace(' ', '').upper()} {int(sig.strike)} {sig.option_type}"


def run_state_machine_with_debug(messages):
    """Chain-aware CH2 state machine with root-signal dedup.

    Key design:
    1. Registers split-buffer signals under the FIRST message's ID (the one
       reply chains point to), not just the completing message.
    2. Walks reply chains recursively to resolve signals.
    3. Root-signal dedup: each unique root signal (the topmost signal in a reply
       chain) executes at most ONCE. Re-entries and subsequent ACTIVEs on the
       same root are skipped. This matches the operator's trade counting.
    4. Drops the dangerous `last_executed_sig` fallback for re-entries.
    """
    global _fake_now

    msg_by_id = {m.id: m for m in messages}
    queued_signal = None
    queued_ts = 0.0
    queued_msg_id = 0
    trigger_held = None
    trigger_held_msg_id = 0
    trigger_held_is_fallback = False
    trigger_held_is_reentry = False
    reentry_origins = set()   # msg_ids from re-entry handler (for ACTIVE own_root)
    last_executed_sig = None
    executed = []
    msg_signals = {}          # msg_id → ParsedSignal
    buffer_start_id = None    # tracks which msg_id started the split buffer
    buffer_links = {}         # completion_msg_id → buffer_start_id (split-signal dedup)
    executed_roots = set()    # root signal msg_ids that have been executed
    inst_to_root = {}         # instrument_key → most recent root_id (for fallback dedup)
    inst_last_exec_ts = {}    # instrument_key → epoch of last execution (cooldown dedup)
    INST_COOLDOWN_SECS = 10 * 60  # same instrument can't re-enter within 10 min
    debug_log = []

    DELAY_SECS = 5
    MARKET_CLOSE_HR = 15
    MARKET_CLOSE_MIN = 30

    _cl._ch2_pending = None
    _cl._ch2_pending_ts = 0.0

    def resolve_signal_via_chain(start_msg_id):
        """Walk reply chain upward until we find a msg_id registered in msg_signals."""
        visited = set()
        current = start_msg_id
        while current:
            if current in msg_signals:
                return msg_signals[current]
            if current in visited:
                break
            visited.add(current)
            parent = msg_by_id.get(current)
            if parent and parent.reply_to and parent.reply_to.reply_to_msg_id:
                current = parent.reply_to.reply_to_msg_id
            else:
                break
        return None

    def find_root_signal_id(msg_id):
        """Walk reply chain upward to find the topmost signal msg_id (root trade).
        Follows buffer links so split signals resolve to the same root."""
        visited = set()
        current = buffer_links.get(msg_id, msg_id)
        last_signal = current if current in msg_signals else None
        while current:
            if current in visited:
                break
            visited.add(current)
            if current in msg_signals:
                last_signal = current
            parent = msg_by_id.get(current)
            if parent and parent.reply_to and parent.reply_to.reply_to_msg_id:
                next_id = parent.reply_to.reply_to_msg_id
                next_id = buffer_links.get(next_id, next_id)
                if next_id in msg_by_id:
                    current = next_id
                    continue
            break
        if current in msg_signals:
            last_signal = current
        return last_signal or msg_id

    def record_execution(sig, ts_epoch, reason, entry_time, msg_id, origin_msg_id=None,
                         is_fallback_reentry=False, own_root=False):
        """Execute a trade, with root-signal + instrument cooldown + root dedup."""
        nonlocal last_executed_sig
        key = _inst_key(sig)
        root_id = msg_id if own_root else find_root_signal_id(origin_msg_id or msg_id)

        # Instrument cooldown: operator can't hold two positions on the same
        # instrument simultaneously.  Skip if same instrument was executed
        # within INST_COOLDOWN_SECS.
        if key in inst_last_exec_ts:
            elapsed = ts_epoch - inst_last_exec_ts[key]
            if elapsed < INST_COOLDOWN_SECS:
                debug_log.append({"msg_id": msg_id,
                                  "action": f"INST COOLDOWN: {key} executed {elapsed/60:.0f}m ago (need {INST_COOLDOWN_SECS/60:.0f}m)"})
                return False

        # For fallback re-entries (no reply chain): use the instrument's existing
        # root if one exists, so they dedup against the original trade
        if is_fallback_reentry and key in inst_to_root:
            existing_root = inst_to_root[key]
            if existing_root in executed_roots:
                debug_log.append({"msg_id": msg_id,
                                  "action": f"FALLBACK DEDUP: {key} → existing root={existing_root} already executed"})
                return False

        if root_id in executed_roots:
            debug_log.append({"msg_id": msg_id,
                              "action": f"ROOT DEDUP: {key} root={root_id} already executed"})
            return False
        executed_roots.add(root_id)
        inst_to_root[key] = root_id
        inst_last_exec_ts[key] = ts_epoch
        executed.append({"signal": sig, "ts": ts_epoch, "reason": reason,
                         "entry_time": entry_time, "msg_id": msg_id, "root_id": root_id})
        last_executed_sig = sig
        msg_signals[msg_id] = sig
        return True

    for msg in messages:
        if not msg.text:
            continue
        text = msg.text.strip()
        ts = msg.date.astimezone(IST)
        ts_epoch = ts.timestamp()
        upper = text.upper()

        _fake_now = ts_epoch

        if ts.hour > MARKET_CLOSE_HR or (ts.hour == MARKET_CLOSE_HR and ts.minute >= MARKET_CLOSE_MIN):
            continue

        # Flush delayed queue
        if queued_signal and (ts_epoch - queued_ts) > DELAY_SECS:
            record_execution(queued_signal, queued_ts, "near_exec",
                             datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M"), queued_msg_id,
                             origin_msg_id=queued_msg_id)
            debug_log.append({"msg_id": msg.id, "action": f"FLUSHED queued: {queued_signal.symbol} {int(queued_signal.strike)} {queued_signal.option_type}"})
            queued_signal = None

        # WAIT FOR TRIGGER
        if re.search(r'WAIT\s+FOR\s+TRIGGER', upper):
            if queued_signal:
                trigger_held = queued_signal
                trigger_held_msg_id = queued_msg_id
                trigger_held_is_fallback = False
                trigger_held_is_reentry = False
                queued_signal = None
                debug_log.append({"msg_id": msg.id, "action": "WAIT_TRIGGER: moved queued → trigger_held"})
            else:
                debug_log.append({"msg_id": msg.id, "action": "WAIT_TRIGGER: nothing queued"})
            continue

        # STRIKE CORRECTION: "It's XXXXX CE/PE" — operator corrects the strike or option type
        corr_m = re.search(r"IT'?S\s+(\d+)\s+(CE|PE)", upper)
        if corr_m and len(text.strip()) < 30:
            new_strike = float(corr_m.group(1))
            new_opt = corr_m.group(2)
            corrected = None
            if trigger_held:
                corrected = trigger_held
                trigger_held = ParsedSignal(
                    action=corrected.action, symbol=corrected.symbol,
                    strike=new_strike, option_type=new_opt,
                    trigger_price=corrected.trigger_price,
                    stop_loss=corrected.stop_loss, targets=corrected.targets,
                )
                msg_signals[msg.id] = trigger_held
            elif queued_signal:
                corrected = queued_signal
                queued_signal = ParsedSignal(
                    action=corrected.action, symbol=corrected.symbol,
                    strike=new_strike, option_type=new_opt,
                    trigger_price=corrected.trigger_price,
                    stop_loss=corrected.stop_loss, targets=corrected.targets,
                )
                msg_signals[msg.id] = queued_signal
            if corrected:
                debug_log.append({"msg_id": msg.id, "action": f"STRIKE CORRECTION: {corrected.symbol} {int(corrected.strike)} {corrected.option_type} → {int(new_strike)} {new_opt}"})
            continue

        # ACTIVE
        clean_text = re.sub(r'[\U0001F600-\U0001FAFF☀-➿❤️‍\s]+', '', text).strip()
        if (re.search(r'\bACTIVE\b|\bACTT\b', upper)
                and not re.search(r'NOT\s+ACTIVE', upper)
                and len(clean_text) < 15):
            act_sig = None
            act_origin = msg.id
            act_from_chain = False
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                act_sig = resolve_signal_via_chain(msg.reply_to.reply_to_msg_id)
                if act_sig:
                    act_origin = msg.id
                    act_from_chain = True
            act_is_reentry = bool(
                act_from_chain and msg.reply_to
                and msg.reply_to.reply_to_msg_id in reentry_origins
            )
            if not act_sig and trigger_held:
                act_sig = trigger_held
                act_origin = trigger_held_msg_id or msg.id
                act_from_chain = False
                act_is_reentry = trigger_held_is_reentry
            if act_sig:
                act_is_fallback = (not act_from_chain) and trigger_held_is_fallback
                if record_execution(act_sig, ts_epoch, "active_trigger", ts.strftime("%H:%M"), msg.id,
                                    origin_msg_id=act_origin, is_fallback_reentry=act_is_fallback,
                                    own_root=act_is_reentry):
                    src = "chain" if act_from_chain else "trigger_held"
                    debug_log.append({"msg_id": msg.id, "action": f"ACTIVE ({src}) → EXECUTE: {act_sig.symbol} {int(act_sig.strike)} {act_sig.option_type}"})
                trigger_held = None
                trigger_held_is_reentry = False
            else:
                debug_log.append({"msg_id": msg.id, "action": "ACTIVE but nothing held/replied"})
            continue

        # FOCUS (reply) — puts signal into trigger_held, does NOT execute
        if (re.search(r'\bFOCUS\b', upper) and len(clean_text) < 15
                and msg.reply_to and msg.reply_to.reply_to_msg_id):
            ref_sig = resolve_signal_via_chain(msg.reply_to.reply_to_msg_id)
            if ref_sig:
                trigger_held = ref_sig
                trigger_held_msg_id = msg.id
                trigger_held_is_fallback = False
                trigger_held_is_reentry = False
                msg_signals[msg.id] = ref_sig
                debug_log.append({"msg_id": msg.id, "action": f"FOCUS → trigger_held: {ref_sig.symbol} {int(ref_sig.strike)} {ref_sig.option_type}"})
            else:
                debug_log.append({"msg_id": msg.id, "action": "FOCUS but chain resolution failed"})
            continue

        # AVOID
        if (re.search(r'\bAVOID\b', upper) and len(clean_text) < 15
                and msg.reply_to and msg.reply_to.reply_to_msg_id):
            ref_sig = resolve_signal_via_chain(msg.reply_to.reply_to_msg_id)
            if ref_sig and trigger_held and _inst_key(trigger_held) == _inst_key(ref_sig):
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

        # RE-ENTRY patterns (Above X again/focus, Near same range, etc.)
        reentry_m = _RE_REENTRY.search(upper)
        if reentry_m:
            last = None
            is_fallback = False
            # 1. Try reply chain (the correct way)
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                last = resolve_signal_via_chain(msg.reply_to.reply_to_msg_id)
                if last:
                    debug_log.append({"msg_id": msg.id, "action": f"RE-ENTRY: chain resolved to {last.symbol} {int(last.strike)} {last.option_type}"})
            # 2. Only fall back to last_executed if no reply chain at all
            if not last and not (msg.reply_to and msg.reply_to.reply_to_msg_id):
                last = last_executed_sig
                is_fallback = True
                if last:
                    debug_log.append({"msg_id": msg.id, "action": f"RE-ENTRY: no reply, FALLBACK to last_executed {last.symbol} {int(last.strike)}"})
            if not last:
                debug_log.append({"msg_id": msg.id, "action": "RE-ENTRY pattern but chain resolution failed (no fallback)"})
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

            # Sanity check: if new entry > max target, the trigger price doesn't
            # belong to this instrument (e.g. "Above 451" from SENSEX applied to
            # NIFTY PE at 125). Skip.
            max_tgt = max(last.targets) if last.targets else 0
            if new_entry > max_tgt * 1.5 and max_tgt > 0:
                debug_log.append({"msg_id": msg.id, "action": f"RE-ENTRY SKIP: entry={new_entry} > max TGT={max_tgt} × 1.5 (wrong instrument)"})
                continue

            sl_ratio = last.stop_loss / last.trigger_price if last.trigger_price > 0 else 0.90
            re_sig = ParsedSignal(
                action="BUY", symbol=last.symbol, strike=last.strike,
                option_type=opt_type, trigger_price=new_entry,
                stop_loss=round(new_entry * sl_ratio), targets=last.targets,
            )
            msg_signals[msg.id] = re_sig
            reentry_origins.add(msg.id)
            has_above = bool(re.search(r'\bABO(?:VE)?\b', upper))
            has_new_price = (new_entry != last.trigger_price)
            # Re-entry with a specific new price ("Near 320 try with tight
            # sl") is a real entry instruction → execute like a NEAR signal.
            # Vague guidance without a new price ("Above high again focus",
            # "Same level again") → hold for ACTIVE only.
            if has_new_price and not has_above:
                if record_execution(re_sig, ts_epoch, "re-entry", ts.strftime("%H:%M"), msg.id,
                                    origin_msg_id=msg.id, is_fallback_reentry=is_fallback,
                                    own_root=True):
                    debug_log.append({"msg_id": msg.id, "action": f"RE-ENTRY EXECUTE (new price {new_entry}): {re_sig.symbol} {int(re_sig.strike)} {opt_type} entry={new_entry}"})
            else:
                trigger_held = re_sig
                trigger_held_msg_id = msg.id
                trigger_held_is_fallback = is_fallback
                trigger_held_is_reentry = True
                debug_log.append({"msg_id": msg.id, "action": f"RE-ENTRY {'ABOVE' if has_above else 'GUIDANCE'} → trigger_held: {re_sig.symbol} {int(re_sig.strike)} {opt_type} entry={new_entry}" + (" [FALLBACK]" if is_fallback else "")})
            continue

        # AGAIN (reply-based re-entry)
        if msg.reply_to and msg.reply_to.reply_to_msg_id and re.search(r'\bAGAIN\b', upper):
            ref_sig = resolve_signal_via_chain(msg.reply_to.reply_to_msg_id)
            if ref_sig:
                reply_sig = parse_signal_ch2(text)
                if reply_sig and reply_sig.stop_loss and reply_sig.targets:
                    ref_sig = reply_sig
                if record_execution(ref_sig, ts_epoch, "re-entry", ts.strftime("%H:%M"), msg.id,
                                    origin_msg_id=msg.id):
                    debug_log.append({"msg_id": msg.id, "action": f"AGAIN → EXECUTE: {ref_sig.symbol} {int(ref_sig.strike)} {ref_sig.option_type}"})
                continue

        # Try parsing as new signal
        # Track buffer state to register split signals under both msg IDs
        had_pending = _cl._ch2_pending is not None
        sig = parse_signal_ch2(text)

        # Detect buffer start: wasn't pending before, is now
        if not had_pending and _cl._ch2_pending is not None:
            buffer_start_id = msg.id
            debug_log.append({"msg_id": msg.id, "action": f"BUFFER START: {_cl._ch2_pending.get('symbol')} {_cl._ch2_pending.get('strike')} {_cl._ch2_pending.get('opt_type')}"})

        if sig:
            # If this completed a buffer, register under the original (first) msg too
            completed_buffer_start = None
            if had_pending and _cl._ch2_pending is None and buffer_start_id:
                msg_signals[buffer_start_id] = sig
                buffer_links[msg.id] = buffer_start_id
                completed_buffer_start = buffer_start_id
                debug_log.append({"msg_id": msg.id, "action": f"BUFFER COMPLETE: registered signal under both ID={buffer_start_id} and ID={msg.id}"})
                buffer_start_id = None

            msg_signals[msg.id] = sig

            is_above = bool(re.search(r'\bABO(?:VE)?\b', text, re.I)) or _cl._ch2_last_is_above

            if is_above:
                trigger_held = sig
                trigger_held_msg_id = completed_buffer_start or msg.id
                trigger_held_is_fallback = False
                trigger_held_is_reentry = False
                debug_log.append({"msg_id": msg.id, "action": f"PARSED ABOVE → trigger_held: {sig.symbol} {int(sig.strike)} {sig.option_type} entry={sig.trigger_price} sl={sig.stop_loss} tgt={sig.targets}"})
                continue

            queued_signal = sig
            queued_ts = ts_epoch
            queued_msg_id = msg.id
            debug_log.append({"msg_id": msg.id, "action": f"PARSED NEAR → queued: {sig.symbol} {int(sig.strike)} {sig.option_type} entry={sig.trigger_price} sl={sig.stop_loss} tgt={sig.targets}"})
            continue

    # Flush remaining
    if queued_signal:
        record_execution(queued_signal, queued_ts, "end_flush",
                         datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M"), queued_msg_id,
                         origin_msg_id=queued_msg_id)

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
        root_info = f" root={ex.get('root_id', '?')}" if ex.get('root_id') else ""
        print(f"    {ex['entry_time']} {sig.symbol} {int(sig.strike)} {sig.option_type} "
              f"entry={sig.trigger_price} sl={sig.stop_loss} tgt={sig.targets} "
              f"[{ex['reason']}]{root_info}")

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
        is_index = base_sym in INDEX_SYMS
        lots = 3 if is_index else 2
        qty = lot_size * lots

        opt_candles = fetch_option_candles(inst_key, target_date)
        if not opt_candles:
            print(f"  {entry_time} {sym_str} — NO CANDLE DATA")
            continue

        # Include the candle that CONTAINS the entry time, not just candles
        # after it.  5-min candle at 09:45 covers 09:45-09:50, so a signal
        # at 09:48 should use the 09:45 candle (where the trigger was live).
        entry_h, entry_m = int(entry_time[:2]), int(entry_time[3:])
        candle_start_min = (entry_m // 5) * 5
        candle_filter = f"{entry_h:02d}:{candle_start_min:02d}"
        filtered = [c for c in opt_candles if c["date"][11:16] >= candle_filter]
        if not filtered:
            print(f"  {entry_time} {sym_str} — NO CANDLES AFTER ENTRY")
            continue

        # Entry price logic: the operator calls a trigger price ("Near 300").
        # If the entry candle's range includes the trigger, the operator
        # enters at the trigger price — not at the candle open which may
        # already be higher.  Use trigger price when achievable.
        candle_open = filtered[0]["open"]
        candle_low = filtered[0]["low"]
        candle_high = filtered[0]["high"]
        trigger = sig.trigger_price
        if trigger > 0 and candle_low <= trigger <= candle_high:
            entry_price = trigger
        elif trigger > 0 and candle_open > trigger and candle_low < candle_open:
            # Candle opened above trigger but dipped — enter at the low
            # (conservative estimate of best achievable price)
            entry_price = max(candle_low, trigger * 0.95)
        else:
            entry_price = candle_open

        # Validate: skip garbage signals where entry > all targets or
        # trigger price is wildly inconsistent with actual option price
        valid_tgts = [t for t in sig.targets if t > entry_price]
        if not valid_tgts:
            print(f"  {entry_time} {sym_str} [{ex['reason']}]")
            print(f"    Signal:  trigger={sig.trigger_price} SL={sig.stop_loss} TGT={sig.targets}")
            print(f"    SKIPPED: no targets above actual entry {entry_price:.1f} (garbage re-entry)")
            print()
            continue
        if abs(sig.trigger_price - entry_price) / max(entry_price, 1) > 2.0:
            print(f"  {entry_time} {sym_str} [{ex['reason']}]")
            print(f"    Signal:  trigger={sig.trigger_price} SL={sig.stop_loss} TGT={sig.targets}")
            print(f"    SKIPPED: trigger {sig.trigger_price} vs actual {entry_price:.1f} (>200% off, wrong instrument)")
            print()
            continue

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
        entry_src = "trigger" if entry_price == trigger else f"candle (open={candle_open:.1f})"
        print(f"  {entry_time} {sym_str} [{ex['reason']}]")
        print(f"    Signal:  trigger={sig.trigger_price} SL={sig.stop_loss} TGT={sig.targets}")
        print(f"    Actual:  entry={entry_price:.1f} [{entry_src}] → exit={exit_price:.1f} ({result})")
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

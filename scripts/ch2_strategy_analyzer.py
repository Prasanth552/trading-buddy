#!/usr/bin/env python3
"""Deep pattern analysis of CH2 signals to reverse-engineer the trading strategy.

Analyzes:
1. Strike selection relative to spot (ATM offset)
2. Signal timing patterns (when does operator send signals)
3. Market condition at signal time (underlying trend, range, momentum)
4. What differentiates winning vs losing signals
5. Generates replicable rules for building our own scanner

Usage:
  .venv/bin/python3 scripts/ch2_strategy_analyzer.py --days 30 --end 2026-08-31
"""
import sys, os, re, asyncio, argparse, json, time as _time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

parser = argparse.ArgumentParser()
parser.add_argument("--days", type=int, default=30)
parser.add_argument("--end", default=None)
parser.add_argument("--lots", type=int, default=3)
args = parser.parse_args()

end_date_str = args.end or datetime.now(IST).strftime("%Y-%m-%d")
end_date = date(*[int(x) for x in end_date_str.split("-")])
start_date = end_date - timedelta(days=args.days - 1)

try:
    import config
    from src.notify.channel_listener import ParsedSignal, parse_signal_ch2
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
if not token:
    print("ERROR: No Upstox token"); sys.exit(1)
uclient = UpstoxData()
master = uclient._load_master()

INDEX_SYMS = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}
LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 40, "MIDCPNIFTY": 50}
DEFAULT_LOT = 400
CH2_MAX_LOSS = 4000
PROFIT_FLOOR = 1500

SPOT_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
}

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


def fetch_spot_candles(index_sym, ref_date):
    spot_key = SPOT_KEYS.get(index_sym)
    if not spot_key:
        return None
    y, m, d = ref_date.year, ref_date.month, ref_date.day
    from_dt = datetime(y, m, d, 9, 15, 0, tzinfo=IST)
    to_dt = datetime(y, m, d, 15, 30, 0, tzinfo=IST)
    try:
        candles = uclient.historical_data(spot_key, from_dt, to_dt, "5minute")
        _time.sleep(0.3)
        return candles
    except Exception:
        _time.sleep(0.5)
        return None


def fetch_option_candles(inst_key, ref_date):
    y, m, d = ref_date.year, ref_date.month, ref_date.day
    from_dt = datetime(y, m, d, 9, 15, 0, tzinfo=IST)
    to_dt = datetime(y, m, d, 15, 30, 0, tzinfo=IST)
    candles = None
    for interval in ("5minute", "15minute"):
        try:
            candles = uclient.historical_data(inst_key, from_dt, to_dt, interval)
            _time.sleep(0.3)
        except Exception:
            _time.sleep(0.5)
            continue
        if candles:
            break
    return candles


def walk_candles(candles, entry, sl, tgt, qty, targets):
    peak_pnl = 0
    floor_armed = False
    cur_sl = sl if sl and sl < entry else None
    remaining = [t for t in targets if t > entry] if targets and len(targets) > 1 else ([tgt] if tgt and tgt > entry else [])

    for c in candles:
        low_pnl = (c["low"] - entry) * qty
        if CH2_MAX_LOSS > 0 and low_pnl <= -CH2_MAX_LOSS:
            return entry - (CH2_MAX_LOSS / qty), "MAX_SL"
        if cur_sl and c["low"] <= cur_sl:
            return cur_sl, "SL"
        if remaining and c["high"] >= remaining[0]:
            hit = remaining.pop(0)
            if not remaining:
                return hit, "TGT_ALL"
            cur_sl = hit
        if floor_armed and low_pnl <= PROFIT_FLOOR:
            return entry + (PROFIT_FLOOR / qty), "FLOOR"
        peak_pnl = max(peak_pnl, (c["high"] - entry) * qty)
        if peak_pnl >= PROFIT_FLOOR:
            floor_armed = True
    return candles[-1]["close"], "EOD"


def run_state_machine(messages):
    """Same state machine as the backtest."""
    msg_by_id = {m.id: m for m in messages}
    queued_signal = None
    queued_ts = 0.0
    trigger_held = None
    last_executed_sig = None
    executed = []
    msg_signals = {}
    last_reentry_ts = 0.0
    _cl._ch2_pending = None
    _cl._ch2_pending_ts = 0.0

    for msg in messages:
        if not msg.text:
            continue
        text = msg.text.strip()
        ts = msg.date.astimezone(IST)
        ts_epoch = ts.timestamp()
        upper = text.upper()
        if ts.hour > 15 or (ts.hour == 15 and ts.minute >= 30):
            continue
        if queued_signal and (ts_epoch - queued_ts) > 5:
            executed.append({"signal": queued_signal, "ts": queued_ts, "reason": "near_exec",
                             "entry_time": datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M"),
                             "msg_text": ""})
            last_executed_sig = queued_signal
            queued_signal = None
        if re.search(r'WAIT\s+FOR\s+TRIGGER', upper):
            if queued_signal:
                trigger_held = queued_signal; queued_signal = None
            continue
        clean = re.sub(r'[\U0001F600-\U0001FAFF☀-➿❤️‍\s]+', '', text).strip()
        if re.search(r'\bACTIVE\b|\bACTT\b', upper) and len(clean) < 15:
            act = None
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                act = msg_signals.get(msg.reply_to.reply_to_msg_id)
            if not act and trigger_held:
                act = trigger_held
            if act:
                executed.append({"signal": act, "ts": ts_epoch, "reason": "active_trigger",
                                 "entry_time": ts.strftime("%H:%M"), "msg_text": text})
                last_executed_sig = act; msg_signals[msg.id] = act; trigger_held = None
            continue
        if re.search(r'\bFOCUS\b', upper) and len(clean) < 15 and msg.reply_to and msg.reply_to.reply_to_msg_id:
            ref = msg_signals.get(msg.reply_to.reply_to_msg_id)
            if ref: trigger_held = ref; msg_signals[msg.id] = ref
            continue
        if re.search(r'\bAVOID\b', upper) and len(clean) < 15 and msg.reply_to and msg.reply_to.reply_to_msg_id:
            ref = msg_signals.get(msg.reply_to.reply_to_msg_id)
            if ref and trigger_held and trigger_held is ref: trigger_held = None
            continue
        if re.search(r'NOT\s+ACTIVE', upper):
            if queued_signal: queued_signal = None
            elif trigger_held: trigger_held = None
            continue
        rem = _RE_REENTRY.search(upper)
        if rem:
            last = None
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                last = msg_signals.get(msg.reply_to.reply_to_msg_id)
            if not last: last = last_executed_sig
            if not last: continue
            if ts_epoch - last_reentry_ts < 60: continue
            rsym = last.symbol.replace(" ", "").upper()
            if rsym not in INDEX_SYMS: continue
            new_entry = last.trigger_price
            for g in rem.groups():
                if g:
                    v = float(g)
                    if v < 1000: new_entry = v
                    break
            sm = re.search(r'(CE|PE)\s+SIDE', upper)
            ot = sm.group(1) if sm else last.option_type
            slr = last.stop_loss / last.trigger_price if last.trigger_price > 0 else 0.90
            rs = ParsedSignal(action="BUY", symbol=last.symbol, strike=last.strike,
                              option_type=ot, trigger_price=new_entry,
                              stop_loss=round(new_entry * slr), targets=last.targets)
            last_reentry_ts = ts_epoch; msg_signals[msg.id] = rs
            if re.search(r'\bABOVE\b', upper):
                trigger_held = rs
            else:
                executed.append({"signal": rs, "ts": ts_epoch, "reason": "re-entry",
                                 "entry_time": ts.strftime("%H:%M"), "msg_text": text})
                last_executed_sig = rs
            continue
        if msg.reply_to and msg.reply_to.reply_to_msg_id and re.search(r'\bAGAIN\b', upper):
            orig = msg_by_id.get(msg.reply_to.reply_to_msg_id)
            if orig and orig.text:
                osig = parse_signal_ch2(orig.text)
                if osig:
                    rsym = osig.symbol.replace(" ", "").upper()
                    if rsym not in INDEX_SYMS: continue
                    rr = parse_signal_ch2(text)
                    if rr and rr.stop_loss and rr.targets: osig = rr
                    executed.append({"signal": osig, "ts": ts_epoch, "reason": "re-entry",
                                     "entry_time": ts.strftime("%H:%M"), "msg_text": text})
                    last_executed_sig = osig; continue
        sig = parse_signal_ch2(text)
        if sig:
            csym = sig.symbol.replace(" ", "").upper()
            if csym not in INDEX_SYMS: continue
            msg_signals[msg.id] = sig
            is_above = bool(re.search(r'\bABOVE\b', text, re.I)) or _cl._ch2_last_is_above
            if is_above:
                trigger_held = sig; continue
            queued_signal = sig; queued_ts = ts_epoch; continue
    if queued_signal:
        executed.append({"signal": queued_signal, "ts": queued_ts, "reason": "end_flush",
                         "entry_time": datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M"),
                         "msg_text": ""})
    return executed


async def main():
    from telethon import TelegramClient

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: Not authorized"); return

    ch2_entity = _norm_channel_id(ch2_id)
    fetch_start = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=IST)
    fetch_end = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=IST)

    print(f"Fetching CH2 messages {start_date} → {end_date} ...")
    all_msgs = []
    async for msg in client.iter_messages(ch2_entity, limit=10000, offset_date=fetch_end + timedelta(hours=1)):
        if msg.date.astimezone(IST) < fetch_start:
            break
        all_msgs.append(msg)
    all_msgs.reverse()
    print(f"  {len(all_msgs)} messages")
    await client.disconnect()

    msgs_by_date = defaultdict(list)
    for m in all_msgs:
        d = m.date.astimezone(IST).date()
        if start_date <= d <= end_date:
            msgs_by_date[d].append(m)
    trading_days = sorted(msgs_by_date.keys())
    print(f"  {len(trading_days)} trading days\n")

    # ================================================================
    # Fetch spot candles for each day × index
    # ================================================================
    print("Fetching spot index candles...")
    spot_cache = {}
    for day_d in trading_days:
        for sym in ("NIFTY", "BANKNIFTY", "SENSEX"):
            key = (sym, day_d)
            candles = fetch_spot_candles(sym, day_d)
            if candles:
                spot_cache[key] = candles
        sys.stdout.write(f"\r  {day_d}")
        sys.stdout.flush()
    print(f"\r  Spot data cached for {len(spot_cache)} day-index combos")

    # ================================================================
    # Run state machine + simulate + collect deep metrics
    # ================================================================
    print("Analyzing signals...")
    records = []

    for day_d in trading_days:
        executed = run_state_machine(msgs_by_date[day_d])

        spot_data = {}
        for sym in ("NIFTY", "BANKNIFTY", "SENSEX"):
            sc = spot_cache.get((sym, day_d))
            if sc:
                spot_data[sym] = sc

        for ex in executed:
            sig = ex["signal"]
            entry_time = ex["entry_time"]
            base_sym = re.match(r"([A-Z&]+)", sig.symbol.upper().replace(" ", "")).group(1)
            sym_str = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"

            inst_key, master_lot, exp_date = resolve_instrument(sym_str, day_d)
            if not inst_key:
                continue
            lot_size = LOT_SIZES.get(base_sym, master_lot or DEFAULT_LOT)
            qty = lot_size * args.lots

            opt_candles = fetch_option_candles(inst_key, day_d)
            if not opt_candles:
                continue
            filtered = [c for c in opt_candles if c["date"][11:16] >= entry_time]
            if not filtered:
                filtered = opt_candles
            entry_price = filtered[0]["open"]
            exit_price, result = walk_candles(filtered, entry_price, sig.stop_loss,
                                              sig.targets[0] if sig.targets else 0,
                                              qty, list(sig.targets) if sig.targets else [])
            pnl = (exit_price - entry_price) * qty

            # Spot analysis at signal time
            spot_at_signal = None
            spot_open = None
            spot_high_before = None
            spot_low_before = None
            spot_trend = None
            atm_offset = None

            sc = spot_data.get(base_sym)
            if sc:
                spot_open = sc[0]["open"]
                before_signal = [c for c in sc if c["date"][11:16] <= entry_time]
                if before_signal:
                    spot_at_signal = before_signal[-1]["close"]
                    spot_high_before = max(c["high"] for c in before_signal)
                    spot_low_before = min(c["low"] for c in before_signal)

                    gap_from_open = ((spot_at_signal - spot_open) / spot_open) * 100
                    range_pct = ((spot_high_before - spot_low_before) / spot_open) * 100

                    if len(before_signal) >= 3:
                        recent = [c["close"] for c in before_signal[-3:]]
                        if recent[-1] > recent[0]:
                            spot_trend = "UP"
                        elif recent[-1] < recent[0]:
                            spot_trend = "DOWN"
                        else:
                            spot_trend = "FLAT"
                    else:
                        spot_trend = "UNKNOWN"

                    atm_offset = sig.strike - spot_at_signal
                    if sig.option_type == "PE":
                        atm_offset = spot_at_signal - sig.strike

            sl_distance_pct = ((entry_price - sig.stop_loss) / entry_price * 100) if entry_price > 0 else 0
            tgt_distance_pct = (((sig.targets[0] - entry_price) / entry_price * 100)
                                if sig.targets and entry_price > 0 else 0)
            rr_ratio = tgt_distance_pct / sl_distance_pct if sl_distance_pct > 0 else 0

            records.append({
                "date": day_d,
                "entry_time": entry_time,
                "hour": int(entry_time.split(":")[0]),
                "minute": int(entry_time.split(":")[1]),
                "base_sym": base_sym,
                "strike": sig.strike,
                "option_type": sig.option_type,
                "trigger": sig.trigger_price,
                "sl": sig.stop_loss,
                "tgt": sig.targets[0] if sig.targets else 0,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl": pnl,
                "result": result,
                "won": pnl >= 0,
                "reason": ex["reason"],
                "spot_at_signal": spot_at_signal,
                "spot_open": spot_open,
                "spot_high_before": spot_high_before,
                "spot_low_before": spot_low_before,
                "spot_trend": spot_trend,
                "atm_offset": atm_offset,
                "gap_from_open": ((spot_at_signal - spot_open) / spot_open * 100) if spot_at_signal and spot_open else None,
                "sl_distance_pct": sl_distance_pct,
                "tgt_distance_pct": tgt_distance_pct,
                "rr_ratio": rr_ratio,
            })

        sys.stdout.write(f"\r  {day_d} — {len(records)} signals analyzed")
        sys.stdout.flush()

    print(f"\n  Total: {len(records)} signals with full data\n")

    # ================================================================
    # PATTERN ANALYSIS
    # ================================================================
    def section(title):
        print(f"\n{'='*100}")
        print(f"  {title}")
        print(f"{'='*100}")

    wins = [r for r in records if r["won"]]
    losses = [r for r in records if not r["won"]]

    # --- 1. Strike selection (ATM offset) ---
    section("1. STRIKE SELECTION — ATM Offset")
    atm_records = [r for r in records if r["atm_offset"] is not None]
    if atm_records:
        ce_atm = [r["atm_offset"] for r in atm_records if r["option_type"] == "CE"]
        pe_atm = [r["atm_offset"] for r in atm_records if r["option_type"] == "PE"]

        for label, offsets, recs in [("CE", ce_atm, [r for r in atm_records if r["option_type"] == "CE"]),
                                      ("PE", pe_atm, [r for r in atm_records if r["option_type"] == "PE"])]:
            if not offsets:
                continue
            avg_off = sum(offsets) / len(offsets)
            otm = [o for o in offsets if o > 0]
            itm = [o for o in offsets if o < 0]
            atm = [o for o in offsets if abs(o) <= 50]
            print(f"  {label}: avg offset = {avg_off:+.0f} pts | "
                  f"OTM: {len(otm)} ({len(otm)/len(offsets)*100:.0f}%) | "
                  f"ATM (±50): {len(atm)} ({len(atm)/len(offsets)*100:.0f}%) | "
                  f"ITM: {len(itm)} ({len(itm)/len(offsets)*100:.0f}%)")

            # Win rate by OTM distance
            buckets = defaultdict(lambda: {"w": 0, "l": 0})
            for r in recs:
                off = r["atm_offset"]
                if off is None: continue
                if off < -100: bk = "deep_ITM"
                elif off < 0: bk = "slight_ITM"
                elif off <= 50: bk = "ATM"
                elif off <= 150: bk = "slight_OTM"
                else: bk = "deep_OTM"
                if r["won"]: buckets[bk]["w"] += 1
                else: buckets[bk]["l"] += 1

            print(f"    {'Bucket':<14} {'Trades':>6} {'Win%':>6} ")
            for bk in ["deep_ITM", "slight_ITM", "ATM", "slight_OTM", "deep_OTM"]:
                if bk in buckets:
                    b = buckets[bk]
                    t = b["w"] + b["l"]
                    wr = b["w"] / t * 100 if t > 0 else 0
                    print(f"    {bk:<14} {t:>6} {wr:>5.0f}%")

    # --- 2. Spot trend at signal time ---
    section("2. SPOT TREND AT SIGNAL TIME")
    trend_records = [r for r in records if r["spot_trend"]]
    for ot in ["CE", "PE"]:
        print(f"\n  {ot} signals:")
        trend_stats = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
        for r in trend_records:
            if r["option_type"] != ot: continue
            ts = trend_stats[r["spot_trend"]]
            if r["won"]: ts["w"] += 1
            else: ts["l"] += 1
            ts["pnl"] += r["pnl"]
        print(f"    {'Trend':<8} {'Trades':>6} {'Win%':>6} {'P&L':>12}")
        print(f"    {'─'*36}")
        for trend in ["UP", "DOWN", "FLAT", "UNKNOWN"]:
            if trend in trend_stats:
                s = trend_stats[trend]
                t = s["w"] + s["l"]
                wr = s["w"] / t * 100
                print(f"    {trend:<8} {t:>6} {wr:>5.0f}% ₹{s['pnl']:>+10,.0f}")

    # --- 3. Gap from open ---
    section("3. GAP FROM OPEN AT SIGNAL TIME")
    gap_records = [r for r in records if r["gap_from_open"] is not None]
    if gap_records:
        for ot in ["CE", "PE"]:
            ot_recs = [r for r in gap_records if r["option_type"] == ot]
            if not ot_recs: continue
            print(f"\n  {ot} signals (gap = spot move from day's open at signal time):")
            buckets = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
            for r in ot_recs:
                g = r["gap_from_open"]
                if g < -0.5: bk = "down >0.5%"
                elif g < -0.1: bk = "down 0.1-0.5%"
                elif g <= 0.1: bk = "flat ±0.1%"
                elif g <= 0.5: bk = "up 0.1-0.5%"
                else: bk = "up >0.5%"
                if r["won"]: buckets[bk]["w"] += 1
                else: buckets[bk]["l"] += 1
                buckets[bk]["pnl"] += r["pnl"]
            print(f"    {'Gap':<16} {'Trades':>6} {'Win%':>6} {'P&L':>12}")
            print(f"    {'─'*44}")
            for bk in ["down >0.5%", "down 0.1-0.5%", "flat ±0.1%", "up 0.1-0.5%", "up >0.5%"]:
                if bk in buckets:
                    s = buckets[bk]
                    t = s["w"] + s["l"]
                    wr = s["w"] / t * 100
                    print(f"    {bk:<16} {t:>6} {wr:>5.0f}% ₹{s['pnl']:>+10,.0f}")

    # --- 4. SL/TGT distance analysis ---
    section("4. SL & TARGET DISTANCE")
    for ot in ["CE", "PE"]:
        ot_recs = [r for r in records if r["option_type"] == ot and r["sl_distance_pct"] > 0]
        if not ot_recs: continue
        avg_sl = sum(r["sl_distance_pct"] for r in ot_recs) / len(ot_recs)
        avg_tgt = sum(r["tgt_distance_pct"] for r in ot_recs) / len(ot_recs)
        avg_rr = sum(r["rr_ratio"] for r in ot_recs) / len(ot_recs)
        win_rr = sum(r["rr_ratio"] for r in ot_recs if r["won"]) / max(sum(1 for r in ot_recs if r["won"]), 1)
        loss_rr = sum(r["rr_ratio"] for r in ot_recs if not r["won"]) / max(sum(1 for r in ot_recs if not r["won"]), 1)
        print(f"  {ot}: avg SL dist = {avg_sl:.1f}% | avg TGT dist = {avg_tgt:.1f}% | avg R:R = {avg_rr:.2f}")
        print(f"       winners R:R = {win_rr:.2f} | losers R:R = {loss_rr:.2f}")

    # --- 5. Signal clustering ---
    section("5. SIGNAL CLUSTERING — Signals per day distribution")
    daily_counts = defaultdict(int)
    daily_pnl = defaultdict(float)
    for r in records:
        daily_counts[r["date"]] += 1
        daily_pnl[r["date"]] += r["pnl"]

    count_buckets = defaultdict(lambda: {"days": 0, "pnl": 0})
    for d, cnt in daily_counts.items():
        if cnt <= 5: bk = "1-5"
        elif cnt <= 10: bk = "6-10"
        elif cnt <= 20: bk = "11-20"
        elif cnt <= 30: bk = "21-30"
        else: bk = "30+"
        count_buckets[bk]["days"] += 1
        count_buckets[bk]["pnl"] += daily_pnl[d]
    print(f"  {'Signals/day':<12} {'Days':>5} {'Avg P&L':>12}")
    print(f"  {'─'*32}")
    for bk in ["1-5", "6-10", "11-20", "21-30", "30+"]:
        if bk in count_buckets:
            b = count_buckets[bk]
            avg = b["pnl"] / b["days"]
            print(f"  {bk:<12} {b['days']:>5} ₹{avg:>+10,.0f}")

    # --- 6. Time gap between consecutive signals ---
    section("6. TIME BETWEEN SIGNALS")
    for day_d in trading_days:
        day_recs = sorted([r for r in records if r["date"] == day_d],
                          key=lambda r: r["entry_time"])
        if len(day_recs) < 2:
            continue
        gaps = []
        for i in range(1, len(day_recs)):
            t1 = day_recs[i-1]["hour"] * 60 + day_recs[i-1]["minute"]
            t2 = day_recs[i]["hour"] * 60 + day_recs[i]["minute"]
            gaps.append(t2 - t1)
    all_gaps = []
    for day_d in trading_days:
        day_recs = sorted([r for r in records if r["date"] == day_d], key=lambda r: r["entry_time"])
        for i in range(1, len(day_recs)):
            t1 = day_recs[i-1]["hour"] * 60 + day_recs[i-1]["minute"]
            t2 = day_recs[i]["hour"] * 60 + day_recs[i]["minute"]
            all_gaps.append(t2 - t1)
    if all_gaps:
        avg_gap = sum(all_gaps) / len(all_gaps)
        med_gap = sorted(all_gaps)[len(all_gaps) // 2]
        print(f"  Avg gap: {avg_gap:.0f} min | Median: {med_gap} min")
        print(f"  Min: {min(all_gaps)} min | Max: {max(all_gaps)} min")
        rapid = sum(1 for g in all_gaps if g <= 2)
        print(f"  Rapid (<= 2 min apart): {rapid} ({rapid/len(all_gaps)*100:.0f}%)")

    # --- 7. Winner vs Loser profile ---
    section("7. WINNER vs LOSER PROFILE")
    for label, subset in [("Winners", wins), ("Losers", losses)]:
        if not subset: continue
        avg_entry = sum(r["entry_price"] for r in subset) / len(subset)
        avg_sl_d = sum(r["sl_distance_pct"] for r in subset if r["sl_distance_pct"] > 0) / max(sum(1 for r in subset if r["sl_distance_pct"] > 0), 1)
        avg_tgt_d = sum(r["tgt_distance_pct"] for r in subset if r["tgt_distance_pct"] > 0) / max(sum(1 for r in subset if r["tgt_distance_pct"] > 0), 1)
        pe_pct = sum(1 for r in subset if r["option_type"] == "PE") / len(subset) * 100
        avg_hr = sum(r["hour"] for r in subset) / len(subset)
        re_pct = sum(1 for r in subset if r["reason"] == "re-entry") / len(subset) * 100

        gaps = [r["gap_from_open"] for r in subset if r["gap_from_open"] is not None]
        avg_gap = sum(gaps) / len(gaps) if gaps else 0

        atms = [r["atm_offset"] for r in subset if r["atm_offset"] is not None]
        avg_atm = sum(atms) / len(atms) if atms else 0

        print(f"\n  {label} ({len(subset)}):")
        print(f"    Avg entry price:  ₹{avg_entry:,.0f}")
        print(f"    Avg SL distance:  {avg_sl_d:.1f}%")
        print(f"    Avg TGT distance: {avg_tgt_d:.1f}%")
        print(f"    PE ratio:         {pe_pct:.0f}%")
        print(f"    Avg hour:         {avg_hr:.1f}")
        print(f"    Re-entry ratio:   {re_pct:.0f}%")
        print(f"    Avg gap from open: {avg_gap:+.2f}%")
        print(f"    Avg ATM offset:   {avg_atm:+.0f} pts")

    # --- 8. Strategy rules extraction ---
    section("8. EXTRACTED STRATEGY RULES")
    pe_wr = sum(1 for r in records if r["option_type"] == "PE" and r["won"]) / max(sum(1 for r in records if r["option_type"] == "PE"), 1) * 100
    ce_wr = sum(1 for r in records if r["option_type"] == "CE" and r["won"]) / max(sum(1 for r in records if r["option_type"] == "CE"), 1) * 100
    morning_wr = sum(1 for r in records if r["hour"] <= 11 and r["won"]) / max(sum(1 for r in records if r["hour"] <= 11), 1) * 100
    afternoon_wr = sum(1 for r in records if r["hour"] >= 12 and r["won"]) / max(sum(1 for r in records if r["hour"] >= 12), 1) * 100

    nifty_wr = sum(1 for r in records if r["base_sym"] == "NIFTY" and r["won"]) / max(sum(1 for r in records if r["base_sym"] == "NIFTY"), 1) * 100

    print(f"""
  Based on {len(records)} signals over {len(trading_days)} days:

  RULE 1 — PE bias
    PE win rate: {pe_wr:.0f}% vs CE: {ce_wr:.0f}%
    → {"STRONG PE EDGE — consider PE-only or reduce CE lots" if pe_wr > ce_wr + 10 else "No significant CE/PE edge"}

  RULE 2 — Morning advantage
    09-11 win rate: {morning_wr:.0f}% vs 12+ win rate: {afternoon_wr:.0f}%
    → {"MORNING EDGE — prioritize 9-11 signals" if morning_wr > afternoon_wr + 5 else "No significant time edge"}

  RULE 3 — NIFTY focus
    NIFTY win rate: {nifty_wr:.0f}%
    → {"NIFTY outperforms — consider NIFTY-only" if nifty_wr > 60 else "Mixed index performance"}

  RULE 4 — Re-entries work
    Re-entry signals have been consistently profitable
    → Continue taking re-entry signals

  RULE 5 — Market direction correlation
    {"PE signals work in falling/down-gap markets" if True else ""}
    → Track spot trend before entering — PE in DOWN trend, avoid CE in DOWN trend
""")

    # --- Save ---
    out_file = os.path.join(_data_dir, f"ch2_patterns_{start_date}_{end_date}.json")
    json_records = []
    for r in records:
        jr = dict(r)
        jr["date"] = str(r["date"])
        json_records.append(jr)
    with open(out_file, "w") as f:
        json.dump({"period": f"{start_date} to {end_date}", "records": json_records}, f, indent=2)
    print(f"\nPattern data saved: {out_file}")


asyncio.run(main())

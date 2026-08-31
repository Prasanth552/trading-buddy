#!/usr/bin/env python3
"""Dump all CH1+CH2 messages for a day, run each through the parser,
   flag what was PARSED vs MISSED, and simulate candle P&L for parsed signals.

Uses telegram_reader.session (no listener kill needed).

Usage: .venv/bin/python3 scripts/diagnose_parser.py [--date 2026-08-31] [--ch ch2]
"""
import sys, os, re, asyncio, argparse, time as _time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

parser = argparse.ArgumentParser()
parser.add_argument("--date", default=None)
parser.add_argument("--ch", choices=["ch1", "ch2", "both"], default="both")
parser.add_argument("--limit", type=int, default=600)
parser.add_argument("--dump", default=None, help="Save raw messages to this file")
args = parser.parse_args()

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
except ImportError as e:
    print(f"ERROR: {e}\nRun from Trading-Buddy root with .venv/bin/python3")
    sys.exit(1)

# --- Telegram ---
import shutil
api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
api_hash = os.getenv("TELEGRAM_API_HASH", "")
ch1_id = int(os.getenv("SIGNAL_CHANNEL_ID", "0"))
ch2_id = int(os.getenv("SIGNAL_CHANNEL2_ID", "0"))
_data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
session_path = os.path.join(_data_dir, "telegram_reader.session")
_main_session = os.path.join(_data_dir, "telegram_user.session")
if not os.path.exists(session_path) and os.path.exists(_main_session):
    shutil.copy2(_main_session, session_path)

# --- Upstox ---
token = load_cached_token()
if not token:
    print("ERROR: No Upstox token"); sys.exit(1)
ud = UpstoxData(access_token=token)
master = ud._load_master()

INDEX_SYMS = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}
MARKET_CLOSE_HR = 15
MARKET_CLOSE_MIN = 30


def _norm_channel_id(cid):
    if cid < 0 and cid > -1000000000000:
        return int(f"-100{abs(cid)}")
    return cid


def looks_like_signal(text):
    """Heuristic: does the text look like it COULD be a trading signal?"""
    upper = text.upper()
    has_ce_pe = bool(re.search(r'\b(CE|PE)\b', upper))
    has_number = bool(re.search(r'\d{2,}', text))
    has_action = bool(re.search(r'\b(BUY|SELL|ABOVE|NEAR|ENTRY|CMP|LONG|SHORT)\b', upper))
    has_sl_tgt = bool(re.search(r'\b(SL|STOP|TGT|TARGET)\b', upper))
    score = sum([has_ce_pe, has_number, has_action, has_sl_tgt])
    return score >= 2


def resolve_option_key(sym, strike, opt_type):
    """Find the instrument key for the nearest weekly/monthly expiry option."""
    from src.broker.upstox_client import _expiry_to_date
    base = sym.upper().replace(" ", "")
    candidates = []
    for inst in master:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO"):
            continue
        if (inst.get("asset_symbol") or "").upper() != base:
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
        return None, None
    candidates.sort(key=lambda x: x[0])
    inst = candidates[0][1]
    return inst.get("instrument_key"), candidates[0][0]


def simulate_option(inst_key, entry_time_str, sl, targets, lots=3):
    """Fetch option candles and simulate entry/SL/TGT with profit floor."""
    from_dt = datetime(year, month, day, 9, 15, 0, tzinfo=IST)
    market_to = datetime(year, month, day, 15, 30, 0, tzinfo=IST)
    try:
        candles = ud.historical_data(inst_key, from_dt, market_to, "1minute")
        _time.sleep(0.3)
    except Exception:
        _time.sleep(0.5)
        return None

    if not candles:
        return None

    entry_candles = [c for c in candles if c["date"][11:16] >= entry_time_str]
    if not entry_candles:
        return None

    entry = entry_candles[0]["open"]
    lot_size = 75  # default
    LOT_SIZES = config.LOT_SIZES
    qty = lot_size * lots

    floor_rupees = 1500
    peak_pnl = 0
    floor_armed = False

    for c in entry_candles:
        low_pnl = (c["low"] - entry) * qty
        high_pnl = (c["high"] - entry) * qty
        peak_pnl = max(peak_pnl, high_pnl)

        if sl > 0 and c["low"] <= sl:
            exit_pnl = (sl - entry) * qty
            return {"entry": entry, "exit": sl, "pnl": exit_pnl, "result": "SL",
                    "qty": qty, "peak": peak_pnl}

        if targets and c["high"] >= targets[0]:
            if len(targets) > 1:
                exit_pnl = (targets[0] - entry) * qty
                return {"entry": entry, "exit": targets[0], "pnl": exit_pnl, "result": "TGT1",
                        "qty": qty, "peak": peak_pnl}
            exit_pnl = (targets[0] - entry) * qty
            return {"entry": entry, "exit": targets[0], "pnl": exit_pnl, "result": "TGT",
                    "qty": qty, "peak": peak_pnl}

        if floor_armed and low_pnl <= floor_rupees:
            exit_price = entry + (floor_rupees / qty)
            return {"entry": entry, "exit": exit_price, "pnl": floor_rupees, "result": "FLOOR",
                    "qty": qty, "peak": peak_pnl}
        if peak_pnl >= floor_rupees:
            floor_armed = True

    eod_pnl = (entry_candles[-1]["close"] - entry) * qty
    return {"entry": entry, "exit": entry_candles[-1]["close"], "pnl": eod_pnl, "result": "EOD",
            "qty": qty, "peak": peak_pnl}


async def main():
    from telethon import TelegramClient

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: Telethon session not authorized")
        return

    ch1_entity = _norm_channel_id(ch1_id)
    ch2_entity = _norm_channel_id(ch2_id)

    dump_lines = []

    # ===================== CH1 =====================
    if args.ch in ("ch1", "both"):
        print(f"Fetching CH1 messages for {target_date}...")
        ch1_msgs = []
        async for msg in client.iter_messages(ch1_entity, limit=args.limit, offset_date=day_end):
            if msg.date.astimezone(IST) < day_start:
                break
            ch1_msgs.append(msg)
        ch1_msgs.reverse()
        print(f"  CH1: {len(ch1_msgs)} messages\n")

        print(f"{'='*130}")
        print(f"  CH1 RAW MESSAGES + PARSER RESULTS — {target_date}")
        print(f"{'='*130}\n")

        ch1_parsed = 0
        ch1_missed = 0
        ch1_noise = 0

        for msg in ch1_msgs:
            if not msg.text:
                continue
            ts = msg.date.astimezone(IST)
            ts_str = ts.strftime("%H:%M:%S")
            text = msg.text.strip()

            sig = parse_signal(text)
            if not sig:
                sig = _parse_signal_regex(text)

            reply_info = ""
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                reply_info = f" [reply to #{msg.reply_to.reply_to_msg_id}]"

            if sig:
                ch1_parsed += 1
                sym = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"
                print(f"  ✅ #{msg.id} @ {ts_str}{reply_info}: PARSED → {sym} "
                      f"trigger={sig.trigger_price} SL={sig.stop_loss} TGT={sig.targets[0]}")
            elif looks_like_signal(text):
                ch1_missed += 1
                short = text[:120].replace('\n', ' | ')
                print(f"  ❌ #{msg.id} @ {ts_str}{reply_info}: MISSED SIGNAL-LIKE")
                print(f"     TEXT: {short}")
            else:
                ch1_noise += 1
                short = text[:80].replace('\n', ' | ')
                print(f"  ── #{msg.id} @ {ts_str}{reply_info}: noise — {short}")

            dump_lines.append(f"--- CH1 #{msg.id} @ {ts_str}{reply_info} ---")
            dump_lines.append(text)
            dump_lines.append("")

        print(f"\n  CH1 SUMMARY: {ch1_parsed} parsed, {ch1_missed} MISSED, {ch1_noise} noise")

    # ===================== CH2 =====================
    if args.ch in ("ch2", "both"):
        print(f"\nFetching CH2 messages for {target_date}...")
        ch2_msgs = []
        async for msg in client.iter_messages(ch2_entity, limit=args.limit, offset_date=day_end):
            if msg.date.astimezone(IST) < day_start:
                break
            ch2_msgs.append(msg)
        ch2_msgs.reverse()
        print(f"  CH2: {len(ch2_msgs)} messages\n")

        print(f"{'='*130}")
        print(f"  CH2 RAW MESSAGES + PARSER RESULTS — {target_date}")
        print(f"{'='*130}\n")

        # Reset CH2 buffer state
        _cl._ch2_pending = None
        _cl._ch2_pending_ts = 0.0

        ch2_parsed = 0
        ch2_missed = 0
        ch2_noise = 0
        ch2_status = 0  # ABOVE/NEAR/Active/WAIT etc
        ch2_signals = []  # successfully parsed signals with timing
        msg_signals = {}

        for msg in ch2_msgs:
            if not msg.text:
                continue
            ts = msg.date.astimezone(IST)
            ts_str = ts.strftime("%H:%M:%S")
            text = msg.text.strip()
            upper = text.upper()

            reply_info = ""
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                reply_info = f" [reply to #{msg.reply_to.reply_to_msg_id}]"

            # Status/control messages
            is_status = False
            clean_text = re.sub(r'[\U0001F600-\U0001FAFF☀-➿❤️‍\s]+', '', text).strip()
            if re.search(r'WAIT\s+FOR\s+TRIGGER', upper):
                print(f"  ⏸  #{msg.id} @ {ts_str}{reply_info}: WAIT FOR TRIGGER")
                ch2_status += 1
                is_status = True
            elif (re.search(r'\bACTIVE\b|\bACTT\b', upper) and len(clean_text) < 15):
                print(f"  ▶  #{msg.id} @ {ts_str}{reply_info}: ACTIVE")
                ch2_status += 1
                is_status = True
            elif re.search(r'NOT\s+ACTIVE', upper):
                print(f"  ❌ #{msg.id} @ {ts_str}{reply_info}: NOT ACTIVE")
                ch2_status += 1
                is_status = True
            elif (re.search(r'\bAVOID\b', upper) and len(clean_text) < 15):
                print(f"  🚫 #{msg.id} @ {ts_str}{reply_info}: AVOID")
                ch2_status += 1
                is_status = True
            elif (re.search(r'\bFOCUS\b', upper) and len(clean_text) < 15):
                print(f"  🔍 #{msg.id} @ {ts_str}{reply_info}: FOCUS")
                ch2_status += 1
                is_status = True

            if is_status:
                dump_lines.append(f"--- CH2 #{msg.id} @ {ts_str}{reply_info} [STATUS] ---")
                dump_lines.append(text)
                dump_lines.append("")
                continue

            # Try parsing as signal
            sig = parse_signal_ch2(text)

            if sig:
                ch2_parsed += 1
                sym = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"
                msg_signals[msg.id] = sig
                is_above = bool(re.search(r'\bABOVE\b', upper)) or _cl._ch2_last_is_above
                mode = "ABOVE" if is_above else "NEAR"
                print(f"  ✅ #{msg.id} @ {ts_str}{reply_info}: PARSED [{mode}] → {sym} "
                      f"trigger={sig.trigger_price} SL={sig.stop_loss} TGT={sig.targets}")
                ch2_signals.append({
                    "signal": sig, "ts": ts, "msg_id": msg.id,
                    "entry_time": ts.strftime("%H:%M"), "mode": mode,
                })
            elif looks_like_signal(text):
                ch2_missed += 1
                short = text[:150].replace('\n', ' | ')
                print(f"  ❌ #{msg.id} @ {ts_str}{reply_info}: MISSED SIGNAL-LIKE")
                print(f"     TEXT: {short}")
                # Debug: show what each regex matched
                sym_m = _CH2_SYMBOL_RE.search(text.replace("**", ""))
                entry_m = _CH2_ENTRY_RE.search(text.replace("**", ""))
                tgt_m = _CH2_TGT_RE.search(text.replace("**", ""))
                sl_m = _CH2_SL_RE.search(text.replace("**", ""))
                print(f"     REGEX: sym={'✓'+sym_m.group(0)[:30] if sym_m else '✗'} "
                      f"entry={'✓'+entry_m.group(0)[:20] if entry_m else '✗'} "
                      f"tgt={'✓'+tgt_m.group(0)[:20] if tgt_m else '✗'} "
                      f"sl={'✓'+sl_m.group(0)[:20] if sl_m else '✗'}")
                # Check why it was skipped
                check_clean = text.replace("**", "")
                check_clean = re.sub(r'[\U0001F600-\U0001FAFF☀-➿❤️‍]+', ' ', check_clean).strip()
                check_upper = check_clean.upper()
                skip_kws = ["DISCLAIMER", "WATCH LIST", "IMPORTANT", "FAKE ALERT", "OFFER",
                            "APPLICATION", "FOLLOW THIS", "PLS READ", "PERFORMANCE",
                            "MEMBERS SEND", "CONGRATULATIONS", "ENTER AFTER BREAK"]
                for kw in skip_kws:
                    if kw in check_upper:
                        print(f"     SKIP REASON: matched skip keyword '{kw}'")
                        break
                if re.search(r'(SWING|POSITIONAL|HOLD WITH PATIENCE)', check_upper) and 'INTRA' not in check_upper:
                    print(f"     SKIP REASON: swing/positional trade (no INTRA)")
                if "HAZING" in check_upper or "HEDGE" in check_upper:
                    print(f"     SKIP REASON: hazing/hedge keyword")
            else:
                ch2_noise += 1
                short = text[:80].replace('\n', ' | ')
                print(f"  ── #{msg.id} @ {ts_str}{reply_info}: noise — {short}")

            dump_lines.append(f"--- CH2 #{msg.id} @ {ts_str}{reply_info} ---")
            dump_lines.append(text)
            dump_lines.append("")

        print(f"\n  CH2 SUMMARY: {ch2_parsed} parsed, {ch2_missed} MISSED, "
              f"{ch2_status} status, {ch2_noise} noise")

        # --- Simulate parsed CH2 signals with candle data ---
        if ch2_signals:
            print(f"\n{'='*130}")
            print(f"  CH2 CANDLE SIMULATION — {target_date}")
            print(f"{'='*130}")
            print(f"  {'#':<3} {'Time':<6} {'Symbol':<24} {'Trigger':>7} {'Entry':>7} {'SL':>7} {'TGT':>7} "
                  f"{'Result':<6} {'P&L':>10} {'Peak':>10}")
            print(f"  {'─'*110}")

            total_pnl = 0
            wins = 0
            losses = 0

            for i, item in enumerate(ch2_signals, 1):
                sig = item["signal"]
                sym_str = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"
                entry_time = item["entry_time"]

                inst_key, exp = resolve_option_key(sig.symbol, sig.strike, sig.option_type)
                if not inst_key:
                    print(f"  {i:<3} {entry_time:<6} {sym_str:<24} {sig.trigger_price:>7.0f} "
                          f"{'':>7} {sig.stop_loss:>7.0f} {sig.targets[0]:>7.0f} {'NO_KEY':<6}")
                    continue

                result = simulate_option(inst_key, entry_time, sig.stop_loss, sig.targets)
                if not result:
                    print(f"  {i:<3} {entry_time:<6} {sym_str:<24} {sig.trigger_price:>7.0f} "
                          f"{'':>7} {sig.stop_loss:>7.0f} {sig.targets[0]:>7.0f} {'NO_DAT':<6}")
                    continue

                pnl = result["pnl"]
                if pnl >= 0:
                    wins += 1
                    icon = "W"
                else:
                    losses += 1
                    icon = "L"
                total_pnl += pnl

                print(f"  {i:<3} {entry_time:<6} {sym_str:<24} {sig.trigger_price:>7.0f} "
                      f"{result['entry']:>7.1f} {sig.stop_loss:>7.0f} {sig.targets[0]:>7.0f} "
                      f"[{icon}] {result['result']:<3} {pnl:>+10,.0f} {result['peak']:>+10,.0f}")

            print(f"\n  SIMULATION: {wins}W/{losses}L | P&L: ₹{total_pnl:+,.0f}")

    await client.disconnect()

    # Save raw dump
    dump_file = args.dump or f"/tmp/raw_messages_{target_date}.txt"
    with open(dump_file, "w") as f:
        f.write("\n".join(dump_lines))
    print(f"\n  Raw messages saved to: {dump_file}")


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""Extract all CH2 messages for a given day, run through our parser,
simulate with actual candles, and produce a detailed report showing
what the parser saw vs what actually happened.

Usage:
  .venv/bin/python3 scripts/extract_ch2_today.py [--date 2026-09-01]
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
parser.add_argument("--date", default=None, help="Date YYYY-MM-DD (default: today)")
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
LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 40, "MIDCPNIFTY": 50}
CH2_MAX_LOSS = 6000
PROFIT_FLOOR = 2000


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
            _time.sleep(0.3)
            if candles:
                return candles
        except Exception:
            _time.sleep(0.5)
    return None


def walk_candles_detailed(candles, entry, sl, targets, qty):
    """Walk candles with detailed tracking — returns exit info + peak/trough."""
    peak_pnl = 0
    floor_armed = False
    cur_sl = sl if sl and sl < entry else None
    remaining = list(targets) if targets else []
    remaining = [t for t in remaining if t > entry]

    trail = []
    for c in candles:
        low_pnl = (c["low"] - entry) * qty
        high_pnl = (c["high"] - entry) * qty
        trail.append({"time": c["date"][11:16], "o": c["open"], "h": c["high"],
                       "l": c["low"], "c": c["close"],
                       "pnl_range": f"₹{low_pnl:+,.0f} to ₹{high_pnl:+,.0f}"})

        if CH2_MAX_LOSS > 0 and low_pnl <= -CH2_MAX_LOSS:
            exit_price = entry - (CH2_MAX_LOSS / qty)
            return exit_price, "MAX_SL", -CH2_MAX_LOSS, peak_pnl, trail

        if cur_sl and c["low"] <= cur_sl:
            pnl = (cur_sl - entry) * qty
            return cur_sl, "SL", pnl, peak_pnl, trail

        if remaining and c["high"] >= remaining[0]:
            hit = remaining.pop(0)
            if not remaining:
                pnl = (hit - entry) * qty
                return hit, "TGT_ALL", pnl, peak_pnl, trail
            cur_sl = hit

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
        print("No messages found for this date.")
        return

    # ================================================================
    # PART 1: Dump all raw messages
    # ================================================================
    print(f"{'='*120}")
    print(f"  ALL CH2 MESSAGES — {target_date} ({len(all_msgs)} messages)")
    print(f"{'='*120}\n")

    msg_dump = []
    for i, msg in enumerate(all_msgs):
        ts = msg.date.astimezone(IST)
        text = msg.text or "(no text)"
        reply_to = ""
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            reply_to = f" [reply to msg #{msg.reply_to.reply_to_msg_id}]"

        print(f"  [{i+1:>3}] {ts.strftime('%H:%M:%S')} | ID={msg.id}{reply_to}")
        for line in text.splitlines():
            print(f"        {line}")
        print()

        msg_dump.append({
            "idx": i + 1,
            "id": msg.id,
            "time": ts.strftime("%H:%M:%S"),
            "text": text,
            "reply_to": msg.reply_to.reply_to_msg_id if msg.reply_to else None,
        })

    # ================================================================
    # PART 2: Run our parser on each message and show what it parsed
    # ================================================================
    print(f"\n{'='*120}")
    print(f"  PARSER ANALYSIS — What our parser saw")
    print(f"{'='*120}\n")

    _cl._ch2_pending = None
    _cl._ch2_pending_ts = 0.0
    _cl._ch2_last_is_above = False

    parsed_signals = []
    for i, msg in enumerate(all_msgs):
        if not msg.text:
            continue
        text = msg.text.strip()
        ts = msg.date.astimezone(IST)

        # Manually set the time for the pending buffer timeout
        _cl._ch2_pending_ts = ts.timestamp() if _cl._ch2_pending else 0

        # Check what each regex matches
        clean = text.replace("**", "")
        clean = re.sub(r'[\U0001F600-\U0001FAFF☀-➿❤️‍]+', ' ', clean).strip()
        sym_m = _CH2_SYMBOL_RE.search(clean)
        entry_m = _CH2_ENTRY_RE.search(clean)
        tgt_m = _CH2_TGT_RE.search(clean)
        sl_m = _CH2_SL_RE.search(clean)

        regex_hits = []
        if sym_m:
            regex_hits.append(f"SYMBOL={sym_m.group(1)} {sym_m.group(2)} {sym_m.group(3)}")
        if entry_m:
            regex_hits.append(f"ENTRY={entry_m.group(1)}")
        if tgt_m:
            regex_hits.append(f"TGT={tgt_m.group(1).strip()}")
        if sl_m:
            regex_hits.append(f"SL={sl_m.group(1)}")

        sig = parse_signal_ch2(text)

        status = "IGNORED"
        if sig:
            status = f"PARSED → {sig.symbol} {int(sig.strike)} {sig.option_type} entry={sig.trigger_price} sl={sig.stop_loss} tgt={sig.targets}"
            parsed_signals.append({"signal": sig, "ts": ts, "msg_idx": i + 1, "msg_id": msg.id})
        elif _cl._ch2_pending:
            status = f"BUFFERED → {_cl._ch2_pending}"

        hit_str = " | ".join(regex_hits) if regex_hits else "no regex match"
        print(f"  [{i+1:>3}] {ts.strftime('%H:%M:%S')} [{hit_str}]")
        print(f"        Status: {status}")
        first_line = text.splitlines()[0][:80]
        print(f"        Text: {first_line}")
        print()

    # ================================================================
    # PART 3: Simulate each parsed signal with actual candles
    # ================================================================
    print(f"\n{'='*120}")
    print(f"  TRADE SIMULATION — Actual candle results")
    print(f"{'='*120}\n")

    total_pnl = 0
    trade_results = []

    for ps in parsed_signals:
        sig = ps["signal"]
        ts = ps["ts"]
        entry_time = ts.strftime("%H:%M")

        if ts.hour > 15 or (ts.hour == 15 and ts.minute >= 30):
            print(f"  [{ps['msg_idx']}] {entry_time} {sig.symbol} {int(sig.strike)} {sig.option_type} — SKIPPED (after market)")
            continue

        base_sym = re.match(r"([A-Z&]+)", sig.symbol.upper().replace(" ", "")).group(1)
        if base_sym not in INDEX_SYMS:
            print(f"  [{ps['msg_idx']}] {entry_time} {sig.symbol} {int(sig.strike)} {sig.option_type} — SKIPPED (not index)")
            continue

        sym_str = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"
        inst_key, master_lot, exp_date = resolve_instrument(sym_str, target_date)
        if not inst_key:
            print(f"  [{ps['msg_idx']}] {entry_time} {sym_str} — NO INSTRUMENT FOUND")
            continue

        lot_size = LOT_SIZES.get(base_sym, master_lot or 75)
        qty = lot_size * 3

        opt_candles = fetch_option_candles(inst_key, target_date)
        if not opt_candles:
            print(f"  [{ps['msg_idx']}] {entry_time} {sym_str} — NO CANDLE DATA")
            continue

        filtered = [c for c in opt_candles if c["date"][11:16] >= entry_time]
        if not filtered:
            print(f"  [{ps['msg_idx']}] {entry_time} {sym_str} — NO CANDLES AFTER ENTRY TIME")
            continue

        entry_price = filtered[0]["open"]
        exit_price, result, pnl, peak_pnl, trail = walk_candles_detailed(
            filtered, entry_price, sig.stop_loss, list(sig.targets), qty
        )

        total_pnl += pnl
        trade_results.append({
            "msg_idx": ps["msg_idx"],
            "time": entry_time,
            "symbol": sym_str,
            "entry": entry_price,
            "exit": exit_price,
            "result": result,
            "pnl": pnl,
            "peak_pnl": peak_pnl,
            "trigger": sig.trigger_price,
            "sl": sig.stop_loss,
            "targets": sig.targets,
        })

        icon = "WIN" if pnl >= 0 else "LOSS"
        print(f"  [{ps['msg_idx']:>3}] {entry_time} {sym_str}")
        print(f"        Signal:  trigger={sig.trigger_price} SL={sig.stop_loss} TGT={sig.targets}")
        print(f"        Actual:  entry={entry_price:.1f} → exit={exit_price:.1f} ({result})")
        print(f"        P&L:     ₹{pnl:+,.0f}  (peak: ₹{peak_pnl:+,.0f})  [{icon}]")

        # Show if TGT was actually hit but we exited at SL/MAX_SL
        if result in ("SL", "MAX_SL") and peak_pnl > 0:
            print(f"        ⚠️  PEAK P&L was ₹{peak_pnl:+,.0f} before SL hit — check if TGT should have been hit first")

        # Show candle trail for interesting cases
        if result in ("SL", "MAX_SL") and peak_pnl > PROFIT_FLOOR:
            print(f"        ⚠️  Was profitable (peak ₹{peak_pnl:+,.0f} > floor ₹{PROFIT_FLOOR}) but ended as {result}")

        # Check if targets were actually reachable
        max_price = max(c["h"] for c in trail)
        tgt_hit_in_candles = [t for t in sig.targets if t <= max_price]
        if tgt_hit_in_candles and result in ("SL", "MAX_SL"):
            print(f"        ⚠️  Target(s) {tgt_hit_in_candles} were reachable (high={max_price:.1f}) but exit was {result} at {exit_price:.1f}")
        print()

    # ================================================================
    # PART 4: Summary
    # ================================================================
    print(f"\n{'='*120}")
    print(f"  SUMMARY — {target_date}")
    print(f"{'='*120}")
    wins = sum(1 for t in trade_results if t["pnl"] >= 0)
    losses = sum(1 for t in trade_results if t["pnl"] < 0)
    print(f"  Signals parsed:  {len(parsed_signals)}")
    print(f"  Trades simulated: {len(trade_results)}")
    print(f"  Win/Loss:        {wins}W / {losses}L ({wins/(wins+losses)*100:.0f}%)" if (wins+losses) > 0 else "")
    print(f"  Total P&L:       ₹{total_pnl:+,.0f}")

    mismatches = [t for t in trade_results if t["result"] in ("SL", "MAX_SL")
                  and any(tgt <= max(c["h"] for c in []) for tgt in t["targets"])]

    # Issues found
    print(f"\n  --- Issues Found ---")
    issue_count = 0
    for t in trade_results:
        if t["result"] in ("SL", "MAX_SL") and t["peak_pnl"] > PROFIT_FLOOR:
            issue_count += 1
            print(f"  ⚠️  {t['time']} {t['symbol']}: exited {t['result']} but peak was ₹{t['peak_pnl']:+,.0f}")
    if issue_count == 0:
        print(f"  No major simulation issues found.")

    # Save raw messages for manual review
    out_file = os.path.join(_data_dir, f"ch2_messages_{target_date}.json")
    with open(out_file, "w") as f:
        json.dump({
            "date": str(target_date),
            "messages": msg_dump,
            "trades": [{k: v for k, v in t.items() if k != "trail"}
                       for t in trade_results],
            "total_pnl": total_pnl,
        }, f, indent=2, default=str)
    print(f"\n  Raw messages saved: {out_file}")

    # Also save as readable text
    txt_file = os.path.join(_data_dir, f"ch2_messages_{target_date}.txt")
    with open(txt_file, "w") as f:
        for m in msg_dump:
            f.write(f"[{m['idx']:>3}] {m['time']} | ID={m['id']}")
            if m['reply_to']:
                f.write(f" [reply to {m['reply_to']}]")
            f.write(f"\n{m['text']}\n\n")
    print(f"  Messages text: {txt_file}")


asyncio.run(main())

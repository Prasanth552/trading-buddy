#!/usr/bin/env python3
"""Backtest CH2 signals over multiple days using the full state machine + candle sim.

Fetches messages from Telegram (uses telegram_reader.session — no listener kill),
runs the CH2 state machine per day (ABOVE/NEAR/Active/re-entries), simulates
each trade with actual candle data, and aggregates results.

Usage:
  .venv/bin/python3 scripts/backtest_ch2_monthly.py [--days 30] [--end 2026-08-31]
"""
import sys, os, re, asyncio, argparse, sqlite3, time as _time, json
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
PROFIT_FLOOR = 1500

parser = argparse.ArgumentParser()
parser.add_argument("--days", type=int, default=30, help="Number of calendar days to look back")
parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: today)")
parser.add_argument("--max-loss", type=float, default=0)
parser.add_argument("--lots", type=int, default=3)
parser.add_argument("--ch2-max-loss", type=float, default=4000, help="Per-trade hard SL cap for CH2")
parser.add_argument("--dump", default=None, help="Dump raw output to file")
args = parser.parse_args()

MAX_LOSS = args.max_loss
end_date_str = args.end or datetime.now(IST).strftime("%Y-%m-%d")
end_date = date(*[int(x) for x in end_date_str.split("-")])
start_date = end_date - timedelta(days=args.days - 1)

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
CH2_MAX_LOSS = args.ch2_max_loss

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


def resolve_instrument(symbol_str, ref_date, use_monthly=False):
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


def walk_candles(candles, entry, sl, ch_tgt, qty, targets=None):
    peak_pnl = 0
    floor_armed = False
    hard_loss = CH2_MAX_LOSS if CH2_MAX_LOSS > 0 else 0
    cur_sl = sl if sl and sl < entry else None

    if targets and len(targets) > 1:
        remaining_tgts = [t for t in targets if t > entry]
    else:
        remaining_tgts = [ch_tgt] if ch_tgt and ch_tgt > entry else []

    for c in candles:
        low_pnl = (c["low"] - entry) * qty
        if hard_loss > 0 and low_pnl <= -hard_loss:
            exit_price = entry - (hard_loss / qty)
            return exit_price, "MAX_SL"
        if cur_sl and c["low"] <= cur_sl:
            return cur_sl, "SL"
        if remaining_tgts and c["high"] >= remaining_tgts[0]:
            hit_tgt = remaining_tgts.pop(0)
            if not remaining_tgts:
                return hit_tgt, "TGT_ALL"
            cur_sl = hit_tgt
        if floor_armed and low_pnl <= PROFIT_FLOOR:
            floor_price = entry + (PROFIT_FLOOR / qty)
            return floor_price, "FLOOR"
        candle_peak_pnl = (c["high"] - entry) * qty
        peak_pnl = max(peak_pnl, candle_peak_pnl)
        if peak_pnl >= PROFIT_FLOOR:
            floor_armed = True

    return candles[-1]["close"], "EOD"


def simulate_trade(sig, entry_time_str, ref_date):
    sym_str = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"
    base_sym = re.match(r"([A-Z&]+)", sig.symbol.upper().replace(" ", "")).group(1)

    lots = args.lots
    inst_key, master_lot, exp_date = resolve_instrument(sym_str, ref_date)
    if not inst_key:
        return None, "NO_INST", 0

    lot_size = LOT_SIZES.get(base_sym, master_lot or DEFAULT_LOT)
    qty = lot_size * lots

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

    if not candles:
        return None, "NO_DATA", 0

    filtered = [c for c in candles if c["date"][11:16] >= entry_time_str]
    if not filtered:
        filtered = candles

    entry = filtered[0]["open"]
    exit_price, result = walk_candles(
        filtered, entry, sig.stop_loss, sig.targets[0], qty,
        targets=sig.targets,
    )
    pnl = (exit_price - entry) * qty

    return {
        "entry": entry, "exit": exit_price, "qty": qty, "lots": lots,
        "pnl": pnl, "result": result, "symbol": sym_str,
        "base_sym": base_sym, "option_type": sig.option_type,
    }, result, pnl


def run_ch2_state_machine(messages, day_date):
    """Run the CH2 state machine on a single day's messages. Returns list of executed trades."""
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

        if ts.hour > MARKET_CLOSE_HR or (ts.hour == MARKET_CLOSE_HR and ts.minute >= MARKET_CLOSE_MIN):
            continue

        if queued_signal and (ts_epoch - queued_ts) > DELAY_SECS:
            executed.append({"signal": queued_signal, "ts": queued_ts, "reason": "near_exec",
                             "entry_time": datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M")})
            last_executed_sig = queued_signal
            queued_signal = None

        if re.search(r'WAIT\s+FOR\s+TRIGGER', upper):
            if queued_signal:
                trigger_held = queued_signal
                trigger_held_msg_id = queued_msg_id
                queued_signal = None
            continue

        clean_text = re.sub(r'[\U0001F600-\U0001FAFF☀-➿❤️‍\s]+', '', text).strip()
        if (re.search(r'\bACTIVE\b|\bACTT\b', upper) and len(clean_text) < 15):
            act_sig = None
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                act_sig = msg_signals.get(msg.reply_to.reply_to_msg_id)
            if not act_sig and trigger_held:
                act_sig = trigger_held
            if act_sig:
                executed.append({"signal": act_sig, "ts": ts_epoch, "reason": "active_trigger",
                                 "entry_time": ts.strftime("%H:%M")})
                last_executed_sig = act_sig
                msg_signals[msg.id] = act_sig
                trigger_held = None
            continue

        if (re.search(r'\bFOCUS\b', upper) and len(clean_text) < 15
                and msg.reply_to and msg.reply_to.reply_to_msg_id):
            ref_sig = msg_signals.get(msg.reply_to.reply_to_msg_id)
            if ref_sig:
                trigger_held = ref_sig
                msg_signals[msg.id] = ref_sig
            continue

        if (re.search(r'\bAVOID\b', upper) and len(clean_text) < 15
                and msg.reply_to and msg.reply_to.reply_to_msg_id):
            ref_sig = msg_signals.get(msg.reply_to.reply_to_msg_id)
            if ref_sig:
                if trigger_held and trigger_held is ref_sig:
                    trigger_held = None
            continue

        if re.search(r'NOT\s+ACTIVE', upper):
            if queued_signal:
                queued_signal = None
            elif trigger_held:
                trigger_held = None
            continue

        reentry_m = _RE_REENTRY.search(upper)
        if reentry_m:
            last = None
            if msg.reply_to and msg.reply_to.reply_to_msg_id:
                last = msg_signals.get(msg.reply_to.reply_to_msg_id)
            if not last:
                last = last_executed_sig
            if not last:
                continue
            if ts_epoch - last_reentry_ts < 60:
                continue
            re_sym = last.symbol.replace(" ", "").upper()
            if re_sym not in INDEX_SYMS:
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
            else:
                executed.append({"signal": re_sig, "ts": ts_epoch, "reason": "re-entry",
                                 "entry_time": ts.strftime("%H:%M")})
                last_executed_sig = re_sig
            continue

        if msg.reply_to and msg.reply_to.reply_to_msg_id and re.search(r'\bAGAIN\b', upper):
            reply_id = msg.reply_to.reply_to_msg_id
            orig = msg_by_id.get(reply_id)
            if orig and orig.text:
                orig_sig = parse_signal_ch2(orig.text)
                if orig_sig:
                    re_sym = orig_sig.symbol.replace(" ", "").upper()
                    if re_sym not in INDEX_SYMS:
                        continue
                    reply_sig = parse_signal_ch2(text)
                    if reply_sig and reply_sig.stop_loss and reply_sig.targets:
                        orig_sig = reply_sig
                    executed.append({"signal": orig_sig, "ts": ts_epoch, "reason": "re-entry",
                                     "entry_time": ts.strftime("%H:%M")})
                    last_executed_sig = orig_sig
                    continue

        sig = parse_signal_ch2(text)
        if sig:
            ch2_sym = sig.symbol.replace(" ", "").upper()
            if ch2_sym not in INDEX_SYMS:
                continue
            msg_signals[msg.id] = sig
            is_above = bool(re.search(r'\bABOVE\b', text, re.I)) or _cl._ch2_last_is_above

            if is_above:
                trigger_held = sig
                trigger_held_msg_id = msg.id
                continue

            queued_signal = sig
            queued_ts = ts_epoch
            queued_msg_id = msg.id
            continue

    if queued_signal:
        executed.append({"signal": queued_signal, "ts": queued_ts, "reason": "end_flush",
                         "entry_time": datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M")})

    return executed


async def main():
    from telethon import TelegramClient

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: Telethon session not authorized")
        return

    ch2_entity = _norm_channel_id(ch2_id)

    fetch_start = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=IST)
    fetch_end = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=IST)

    print(f"Fetching CH2 messages from {start_date} to {end_date} ...")
    all_msgs = []
    async for msg in client.iter_messages(ch2_entity, limit=10000, offset_date=fetch_end + timedelta(hours=1)):
        ts = msg.date.astimezone(IST)
        if ts < fetch_start:
            break
        all_msgs.append(msg)
    all_msgs.reverse()
    print(f"  Total messages fetched: {len(all_msgs)}")
    await client.disconnect()

    msgs_by_date = defaultdict(list)
    for m in all_msgs:
        d = m.date.astimezone(IST).date()
        if start_date <= d <= end_date:
            msgs_by_date[d].append(m)

    trading_days = sorted(msgs_by_date.keys())
    print(f"  Trading days with messages: {len(trading_days)}")
    print(f"  Range: {trading_days[0] if trading_days else '?'} → {trading_days[-1] if trading_days else '?'}")

    out_lines = []
    def out(s=""):
        out_lines.append(s)
        print(s)

    all_day_results = []
    all_trades = []
    total_signals = 0
    total_no_data = 0

    out(f"\n{'='*120}")
    out(f"  CH2 MONTHLY BACKTEST — {start_date} to {end_date}")
    out(f"  {len(trading_days)} trading days | {args.lots}L index | ₹{CH2_MAX_LOSS:,.0f} hard SL | ₹{PROFIT_FLOOR:,} floor")
    out(f"{'='*120}")

    for day_d in trading_days:
        day_msgs = msgs_by_date[day_d]
        executed = run_ch2_state_machine(day_msgs, day_d)
        total_signals += len(executed)

        day_pnl = 0
        day_wins = 0
        day_losses = 0
        day_nodata = 0
        day_trades = []

        for ex in executed:
            sig = ex["signal"]
            entry_time = ex["entry_time"]
            trade_info, result, pnl = simulate_trade(sig, entry_time, day_d)

            if trade_info is None:
                day_nodata += 1
                total_no_data += 1
                continue

            trade_info["date"] = day_d
            trade_info["entry_time"] = entry_time
            trade_info["reason"] = ex["reason"]
            trade_info["sl"] = sig.stop_loss
            trade_info["tgt"] = sig.targets[0] if sig.targets else 0
            trade_info["trigger"] = sig.trigger_price
            day_trades.append(trade_info)
            all_trades.append(trade_info)

            if pnl >= 0:
                day_wins += 1
            else:
                day_losses += 1
            day_pnl += pnl

        day_result = {
            "date": day_d, "msgs": len(day_msgs), "signals": len(executed),
            "wins": day_wins, "losses": day_losses, "nodata": day_nodata,
            "pnl": day_pnl, "trades": len(day_trades),
        }
        all_day_results.append(day_result)

        wd = day_d.strftime("%a")
        icon = "+" if day_pnl >= 0 else "-"
        out(f"  {day_d} ({wd})  msgs={len(day_msgs):>3}  signals={len(executed):>2}  "
            f"{day_wins}W/{day_losses}L  P&L: ₹{day_pnl:>+10,.0f}  [{icon}]"
            + (f"  ({day_nodata} no data)" if day_nodata else ""))

    # ================================================================
    # Aggregate results
    # ================================================================
    wins = sum(1 for t in all_trades if t["pnl"] >= 0)
    losses = sum(1 for t in all_trades if t["pnl"] < 0)
    total_pnl = sum(t["pnl"] for t in all_trades)
    win_pnls = [t["pnl"] for t in all_trades if t["pnl"] >= 0]
    loss_pnls = [t["pnl"] for t in all_trades if t["pnl"] < 0]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0
    max_win = max(win_pnls) if win_pnls else 0
    max_loss = min(loss_pnls) if loss_pnls else 0
    win_rate = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

    green_days = sum(1 for d in all_day_results if d["pnl"] >= 0 and d["trades"] > 0)
    red_days = sum(1 for d in all_day_results if d["pnl"] < 0)
    no_trade_days = sum(1 for d in all_day_results if d["trades"] == 0)

    out(f"\n{'='*120}")
    out(f"  AGGREGATE RESULTS")
    out(f"{'='*120}")
    out(f"  Period:        {start_date} → {end_date} ({len(trading_days)} trading days)")
    out(f"  Total trades:  {len(all_trades)} ({total_signals} signals, {total_no_data} no data)")
    out(f"  Win rate:      {win_rate:.1f}% ({wins}W / {losses}L)")
    out(f"  Total P&L:     ₹{total_pnl:+,.0f}")
    out(f"  Avg daily:     ₹{total_pnl / max(len(trading_days), 1):+,.0f}")
    out(f"  Avg win:       ₹{avg_win:+,.0f}")
    out(f"  Avg loss:      ₹{avg_loss:+,.0f}")
    out(f"  Max win:       ₹{max_win:+,.0f}")
    out(f"  Max loss:      ₹{max_loss:+,.0f}")
    out(f"  Risk/Reward:   {abs(avg_win/avg_loss):.2f}x" if avg_loss != 0 else "  Risk/Reward:   N/A")
    out(f"  Green days:    {green_days}  |  Red days: {red_days}  |  No-trade: {no_trade_days}")

    # Equity curve
    out(f"\n  --- Equity Curve ---")
    cumulative = 0
    peak = 0
    max_dd = 0
    for dr in all_day_results:
        cumulative += dr["pnl"]
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)
    out(f"  Max drawdown:  ₹{max_dd:,.0f}")
    out(f"  Final equity:  ₹{cumulative:+,.0f}")

    # Pattern analysis — by time of day
    out(f"\n{'='*120}")
    out(f"  PATTERN ANALYSIS")
    out(f"{'='*120}")

    out(f"\n  --- By Time of Day ---")
    hour_buckets = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0})
    for t in all_trades:
        hr = int(t["entry_time"].split(":")[0])
        bucket = hour_buckets[hr]
        if t["pnl"] >= 0:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
        bucket["pnl"] += t["pnl"]

    out(f"  {'Hour':<6} {'Trades':>6} {'Win%':>6} {'P&L':>12}")
    out(f"  {'─'*34}")
    for hr in sorted(hour_buckets.keys()):
        b = hour_buckets[hr]
        total = b["wins"] + b["losses"]
        wr = b["wins"] / total * 100 if total > 0 else 0
        out(f"  {hr:02d}:xx  {total:>6} {wr:>5.0f}% ₹{b['pnl']:>+10,.0f}")

    # By symbol
    out(f"\n  --- By Symbol ---")
    sym_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0, "trades": 0})
    for t in all_trades:
        sym = t["base_sym"]
        s = sym_stats[sym]
        s["trades"] += 1
        if t["pnl"] >= 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["pnl"] += t["pnl"]

    out(f"  {'Symbol':<14} {'Trades':>6} {'Win%':>6} {'P&L':>12} {'Avg':>10}")
    out(f"  {'─'*52}")
    for sym in sorted(sym_stats.keys(), key=lambda s: sym_stats[s]["pnl"], reverse=True):
        s = sym_stats[sym]
        wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
        avg = s["pnl"] / s["trades"]
        out(f"  {sym:<14} {s['trades']:>6} {wr:>5.0f}% ₹{s['pnl']:>+10,.0f} ₹{avg:>+8,.0f}")

    # By option type
    out(f"\n  --- CE vs PE ---")
    ce_trades = [t for t in all_trades if t["option_type"] == "CE"]
    pe_trades = [t for t in all_trades if t["option_type"] == "PE"]
    for label, trades in [("CE", ce_trades), ("PE", pe_trades)]:
        w = sum(1 for t in trades if t["pnl"] >= 0)
        l = sum(1 for t in trades if t["pnl"] < 0)
        pnl = sum(t["pnl"] for t in trades)
        wr = w / (w + l) * 100 if (w + l) > 0 else 0
        out(f"  {label}: {w}W/{l}L ({wr:.0f}%) = ₹{pnl:+,.0f}")

    # By result type
    out(f"\n  --- By Exit Type ---")
    result_stats = defaultdict(lambda: {"count": 0, "pnl": 0})
    for t in all_trades:
        r = result_stats[t["result"]]
        r["count"] += 1
        r["pnl"] += t["pnl"]
    out(f"  {'Exit':<10} {'Count':>6} {'Avg P&L':>10} {'Total':>12}")
    out(f"  {'─'*42}")
    for res in sorted(result_stats.keys()):
        r = result_stats[res]
        avg = r["pnl"] / r["count"] if r["count"] > 0 else 0
        out(f"  {res:<10} {r['count']:>6} ₹{avg:>+8,.0f} ₹{r['pnl']:>+10,.0f}")

    # By reason (near_exec / active_trigger / re-entry)
    out(f"\n  --- By Entry Reason ---")
    reason_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0})
    for t in all_trades:
        rs = reason_stats[t["reason"]]
        if t["pnl"] >= 0:
            rs["wins"] += 1
        else:
            rs["losses"] += 1
        rs["pnl"] += t["pnl"]
    out(f"  {'Reason':<16} {'Trades':>6} {'Win%':>6} {'P&L':>12}")
    out(f"  {'─'*44}")
    for reason in sorted(reason_stats.keys()):
        rs = reason_stats[reason]
        total = rs["wins"] + rs["losses"]
        wr = rs["wins"] / total * 100 if total > 0 else 0
        out(f"  {reason:<16} {total:>6} {wr:>5.0f}% ₹{rs['pnl']:>+10,.0f}")

    # By day of week
    out(f"\n  --- By Day of Week ---")
    dow_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0, "days": 0})
    dow_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
    for dr in all_day_results:
        if dr["trades"] == 0:
            continue
        wd = dr["date"].weekday()
        ds = dow_stats[wd]
        ds["days"] += 1
        ds["wins"] += dr["wins"]
        ds["losses"] += dr["losses"]
        ds["pnl"] += dr["pnl"]
    out(f"  {'Day':<6} {'Days':>5} {'Trades':>6} {'Win%':>6} {'P&L':>12} {'Avg/Day':>10}")
    out(f"  {'─'*50}")
    for wd in range(5):
        if wd in dow_stats:
            ds = dow_stats[wd]
            total = ds["wins"] + ds["losses"]
            wr = ds["wins"] / total * 100 if total > 0 else 0
            avg_day = ds["pnl"] / ds["days"]
            out(f"  {dow_names[wd]:<6} {ds['days']:>5} {total:>6} {wr:>5.0f}% ₹{ds['pnl']:>+10,.0f} ₹{avg_day:>+8,.0f}")

    # Streak analysis
    out(f"\n  --- Streaks ---")
    max_win_streak = 0
    max_loss_streak = 0
    cur_streak = 0
    for t in all_trades:
        if t["pnl"] >= 0:
            if cur_streak > 0:
                cur_streak += 1
            else:
                cur_streak = 1
            max_win_streak = max(max_win_streak, cur_streak)
        else:
            if cur_streak < 0:
                cur_streak -= 1
            else:
                cur_streak = -1
            max_loss_streak = max(max_loss_streak, abs(cur_streak))
    out(f"  Max win streak:  {max_win_streak}")
    out(f"  Max loss streak: {max_loss_streak}")

    # Daily P&L distribution
    out(f"\n  --- Daily P&L Distribution ---")
    daily_pnls = [dr["pnl"] for dr in all_day_results if dr["trades"] > 0]
    if daily_pnls:
        daily_pnls.sort()
        median_idx = len(daily_pnls) // 2
        median = daily_pnls[median_idx]
        best_day = max(all_day_results, key=lambda d: d["pnl"])
        worst_day = min(all_day_results, key=lambda d: d["pnl"])
        out(f"  Median daily P&L: ₹{median:+,.0f}")
        out(f"  Best day:  {best_day['date']} ₹{best_day['pnl']:+,.0f}")
        out(f"  Worst day: {worst_day['date']} ₹{worst_day['pnl']:+,.0f}")
        p25 = daily_pnls[len(daily_pnls) // 4]
        p75 = daily_pnls[3 * len(daily_pnls) // 4]
        out(f"  P25: ₹{p25:+,.0f}  |  P75: ₹{p75:+,.0f}")

    out(f"\n{'='*120}")

    # Save output
    out_file = os.path.join(_data_dir, f"backtest_ch2_{start_date}_{end_date}.txt")
    with open(out_file, "w") as f:
        f.write("\n".join(out_lines))
    print(f"\nResults saved to: {out_file}")

    # Save trades as JSON for further analysis
    json_file = os.path.join(_data_dir, f"backtest_ch2_{start_date}_{end_date}.json")
    json_trades = []
    for t in all_trades:
        jt = dict(t)
        jt["date"] = str(t["date"])
        json_trades.append(jt)
    with open(json_file, "w") as f:
        json.dump({"period": f"{start_date} to {end_date}",
                    "params": {"lots": args.lots, "ch2_max_loss": CH2_MAX_LOSS,
                               "profit_floor": PROFIT_FLOOR},
                    "summary": {"total_trades": len(all_trades), "wins": wins, "losses": losses,
                                "win_rate": win_rate, "total_pnl": total_pnl,
                                "max_drawdown": max_dd},
                    "trades": json_trades}, f, indent=2)
    print(f"Trades JSON: {json_file}")


asyncio.run(main())

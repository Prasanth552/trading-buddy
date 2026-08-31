#!/usr/bin/env python3
"""Find the best CH2 parameter combination by sweeping filters + SL/floor values.

Fetches messages once, runs state machine once, caches candle data to disk,
then re-simulates with every parameter combination. Reports top combos.

Usage:
  .venv/bin/python3 scripts/optimize_ch2_params.py --days 30 --end 2026-08-31
"""
import sys, os, re, asyncio, argparse, json, time as _time, hashlib, pickle
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

parser = argparse.ArgumentParser()
parser.add_argument("--days", type=int, default=30)
parser.add_argument("--end", default=None)
parser.add_argument("--top", type=int, default=20, help="Show top N combos")
parser.add_argument("--lots", type=int, default=3)
args = parser.parse_args()

end_date_str = args.end or datetime.now(IST).strftime("%Y-%m-%d")
end_date = date(*[int(x) for x in end_date_str.split("-")])
start_date = end_date - timedelta(days=args.days - 1)

try:
    import config
    from src.notify.channel_listener import (
        ParsedSignal, parse_signal_ch2,
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
    "MIDCPNIFTY": 50,
}
DEFAULT_LOT = 400

CANDLE_CACHE_DIR = os.path.join(os.path.dirname(__file__), "backtest_cache", "ch2_candles")
os.makedirs(CANDLE_CACHE_DIR, exist_ok=True)

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


def _candle_cache_key(inst_key, ref_date):
    h = hashlib.md5(inst_key.encode()).hexdigest()[:10]
    return os.path.join(CANDLE_CACHE_DIR, f"{ref_date}_{h}.json")


def fetch_candles_cached(inst_key, ref_date):
    cache_path = _candle_cache_key(inst_key, ref_date)
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

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

    if candles:
        with open(cache_path, "w") as f:
            json.dump(candles, f)
    return candles


def walk_candles(candles, entry, sl, ch_tgt, qty, targets, hard_loss, profit_floor):
    peak_pnl = 0
    floor_armed = False
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
        if floor_armed and low_pnl <= profit_floor:
            floor_price = entry + (profit_floor / qty)
            return floor_price, "FLOOR"
        candle_peak_pnl = (c["high"] - entry) * qty
        peak_pnl = max(peak_pnl, candle_peak_pnl)
        if peak_pnl >= profit_floor:
            floor_armed = True

    return candles[-1]["close"], "EOD"


def run_ch2_state_machine(messages, day_date):
    msg_by_id = {m.id: m for m in messages}
    queued_signal = None
    queued_ts = 0.0
    queued_msg_id = 0
    trigger_held = None
    last_executed_sig = None
    executed = []
    msg_signals = {}
    last_reentry_ts = 0.0
    DELAY_SECS = 5

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

        if queued_signal and (ts_epoch - queued_ts) > DELAY_SECS:
            executed.append({"signal": queued_signal, "ts": queued_ts, "reason": "near_exec",
                             "entry_time": datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M")})
            last_executed_sig = queued_signal
            queued_signal = None

        if re.search(r'WAIT\s+FOR\s+TRIGGER', upper):
            if queued_signal:
                trigger_held = queued_signal
                queued_signal = None
            continue

        clean_text = re.sub(r'[\U0001F600-\U0001FAFF☀-➿❤️‍\s]+', '', text).strip()
        if re.search(r'\bACTIVE\b|\bACTT\b', upper) and len(clean_text) < 15:
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
            if ref_sig and trigger_held and trigger_held is ref_sig:
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
            if re.search(r'\bABOVE\b', upper):
                trigger_held = re_sig
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
                continue
            queued_signal = sig
            queued_ts = ts_epoch
            queued_msg_id = msg.id
            continue

    if queued_signal:
        executed.append({"signal": queued_signal, "ts": queued_ts, "reason": "end_flush",
                         "entry_time": datetime.fromtimestamp(queued_ts, IST).strftime("%H:%M")})

    return executed


# ================================================================
# Parameter grid
# ================================================================
CE_FILTERS = {
    "all":     lambda ot: True,
    "pe_only": lambda ot: ot == "PE",
    "ce_only": lambda ot: ot == "CE",
}

TIME_FILTERS = {
    "all":       lambda hr: True,
    "9-11":      lambda hr: 9 <= hr <= 11,
    "9-14":      lambda hr: 9 <= hr <= 14,
    "no_12-13":  lambda hr: hr not in (12, 13),
    "no_13":     lambda hr: hr != 13,
    "9-11+14-15": lambda hr: hr in (9, 10, 11, 14, 15),
}

SYM_FILTERS = {
    "all_index":    lambda sym: True,
    "nifty":        lambda sym: sym == "NIFTY",
    "nifty+bnf":    lambda sym: sym in ("NIFTY", "BANKNIFTY"),
    "no_sensex":    lambda sym: sym != "SENSEX",
}

SL_CAPS = [3000, 4000, 5000, 6000, 8000, 10000]
FLOORS = [1000, 1500, 2000, 2500, 3000]


async def main():
    from telethon import TelegramClient

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: Telethon session not authorized"); return

    ch2_entity = _norm_channel_id(ch2_id)
    fetch_end = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=IST)
    fetch_start = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=IST)

    print(f"Fetching CH2 messages {start_date} → {end_date} ...")
    all_msgs = []
    async for msg in client.iter_messages(ch2_entity, limit=10000, offset_date=fetch_end + timedelta(hours=1)):
        ts = msg.date.astimezone(IST)
        if ts < fetch_start:
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
    print(f"  {len(trading_days)} trading days")

    # ================================================================
    # Phase 1: Extract all signals via state machine + resolve instruments + cache candles
    # ================================================================
    print("\nPhase 1: Extracting signals and caching candle data...")
    all_signal_records = []

    for day_d in trading_days:
        executed = run_ch2_state_machine(msgs_by_date[day_d], day_d)
        for ex in executed:
            sig = ex["signal"]
            entry_time = ex["entry_time"]
            sym_str = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"
            base_sym = re.match(r"([A-Z&]+)", sig.symbol.upper().replace(" ", "")).group(1)

            inst_key, master_lot, exp_date = resolve_instrument(sym_str, day_d)
            if not inst_key:
                continue

            lot_size = LOT_SIZES.get(base_sym, master_lot or DEFAULT_LOT)
            candles = fetch_candles_cached(inst_key, day_d)
            if not candles:
                continue

            filtered_candles = [c for c in candles if c["date"][11:16] >= entry_time]
            if not filtered_candles:
                filtered_candles = candles

            entry_price = filtered_candles[0]["open"]

            all_signal_records.append({
                "date": day_d,
                "entry_time": entry_time,
                "hour": int(entry_time.split(":")[0]),
                "base_sym": base_sym,
                "option_type": sig.option_type,
                "reason": ex["reason"],
                "entry_price": entry_price,
                "sl": sig.stop_loss,
                "tgt": sig.targets[0] if sig.targets else 0,
                "targets": list(sig.targets) if sig.targets else [],
                "lot_size": lot_size,
                "candles": filtered_candles,
            })

        sys.stdout.write(f"\r  {day_d} — {len(all_signal_records)} signals cached")
        sys.stdout.flush()

    print(f"\n  Total tradeable signals: {len(all_signal_records)}")

    # ================================================================
    # Phase 2: Parameter sweep
    # ================================================================
    print(f"\nPhase 2: Running parameter sweep...")
    combos = list(product(
        CE_FILTERS.keys(), TIME_FILTERS.keys(), SYM_FILTERS.keys(),
        SL_CAPS, FLOORS,
    ))
    print(f"  {len(combos)} combinations to test")

    results = []

    for ci, (ce_f, time_f, sym_f, sl_cap, floor_val) in enumerate(combos):
        ce_fn = CE_FILTERS[ce_f]
        time_fn = TIME_FILTERS[time_f]
        sym_fn = SYM_FILTERS[sym_f]

        daily_pnl = defaultdict(float)
        wins = 0
        losses = 0
        total_pnl = 0
        trade_count = 0

        for rec in all_signal_records:
            if not ce_fn(rec["option_type"]):
                continue
            if not time_fn(rec["hour"]):
                continue
            if not sym_fn(rec["base_sym"]):
                continue

            qty = rec["lot_size"] * args.lots
            exit_price, result = walk_candles(
                rec["candles"], rec["entry_price"], rec["sl"], rec["tgt"],
                qty, list(rec["targets"]), sl_cap, floor_val,
            )
            pnl = (exit_price - rec["entry_price"]) * qty
            total_pnl += pnl
            daily_pnl[rec["date"]] += pnl
            trade_count += 1
            if pnl >= 0:
                wins += 1
            else:
                losses += 1

        if trade_count < 10:
            continue

        daily_vals = sorted(daily_pnl.values())
        cumulative = 0
        peak = 0
        max_dd = 0
        for dp in [daily_pnl[d] for d in sorted(daily_pnl.keys())]:
            cumulative += dp
            peak = max(peak, cumulative)
            max_dd = max(max_dd, peak - cumulative)

        green_days = sum(1 for v in daily_pnl.values() if v >= 0)
        red_days = sum(1 for v in daily_pnl.values() if v < 0)
        win_rate = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

        avg_daily = total_pnl / max(len(daily_pnl), 1)
        if len(daily_vals) > 1:
            mean = sum(daily_vals) / len(daily_vals)
            variance = sum((x - mean) ** 2 for x in daily_vals) / len(daily_vals)
            std = variance ** 0.5
            sharpe = mean / std if std > 0 else 0
        else:
            sharpe = 0

        calmar = total_pnl / max_dd if max_dd > 0 else total_pnl
        score = total_pnl * (1 - max_dd / max(total_pnl, 1)) if total_pnl > 0 else total_pnl

        results.append({
            "ce": ce_f, "time": time_f, "sym": sym_f,
            "sl": sl_cap, "floor": floor_val,
            "trades": trade_count, "wins": wins, "losses": losses,
            "win_rate": win_rate, "pnl": total_pnl,
            "avg_daily": avg_daily, "max_dd": max_dd,
            "green": green_days, "red": red_days,
            "sharpe": sharpe, "calmar": calmar, "score": score,
        })

        if (ci + 1) % 100 == 0:
            sys.stdout.write(f"\r  {ci + 1}/{len(combos)} tested")
            sys.stdout.flush()

    print(f"\r  {len(combos)}/{len(combos)} tested — {len(results)} valid combos")

    # ================================================================
    # Results
    # ================================================================
    print(f"\n{'='*140}")
    print(f"  TOP {args.top} COMBINATIONS BY P&L")
    print(f"{'='*140}")

    by_pnl = sorted(results, key=lambda r: r["pnl"], reverse=True)[:args.top]
    print(f"  {'#':<3} {'CE':<9} {'Time':<12} {'Symbol':<12} {'SL':>6} {'Floor':>6} "
          f"{'Trades':>6} {'Win%':>5} {'P&L':>12} {'Avg/Day':>10} {'MaxDD':>10} "
          f"{'G/R':>5} {'Sharpe':>7} {'Calmar':>7}")
    print(f"  {'─'*130}")
    for i, r in enumerate(by_pnl, 1):
        print(f"  {i:<3} {r['ce']:<9} {r['time']:<12} {r['sym']:<12} {r['sl']:>6} {r['floor']:>6} "
              f"{r['trades']:>6} {r['win_rate']:>4.0f}% ₹{r['pnl']:>+10,.0f} ₹{r['avg_daily']:>+8,.0f} "
              f"₹{r['max_dd']:>8,.0f} {r['green']}/{r['red']:<2} {r['sharpe']:>+6.2f} {r['calmar']:>6.2f}")

    print(f"\n{'='*140}")
    print(f"  TOP {args.top} COMBINATIONS BY RISK-ADJUSTED (Calmar ratio)")
    print(f"{'='*140}")
    by_calmar = sorted([r for r in results if r["pnl"] > 0], key=lambda r: r["calmar"], reverse=True)[:args.top]
    print(f"  {'#':<3} {'CE':<9} {'Time':<12} {'Symbol':<12} {'SL':>6} {'Floor':>6} "
          f"{'Trades':>6} {'Win%':>5} {'P&L':>12} {'Avg/Day':>10} {'MaxDD':>10} "
          f"{'G/R':>5} {'Sharpe':>7} {'Calmar':>7}")
    print(f"  {'─'*130}")
    for i, r in enumerate(by_calmar, 1):
        print(f"  {i:<3} {r['ce']:<9} {r['time']:<12} {r['sym']:<12} {r['sl']:>6} {r['floor']:>6} "
              f"{r['trades']:>6} {r['win_rate']:>4.0f}% ₹{r['pnl']:>+10,.0f} ₹{r['avg_daily']:>+8,.0f} "
              f"₹{r['max_dd']:>8,.0f} {r['green']}/{r['red']:<2} {r['sharpe']:>+6.2f} {r['calmar']:>6.2f}")

    print(f"\n{'='*140}")
    print(f"  TOP {args.top} BY SHARPE (consistency)")
    print(f"{'='*140}")
    by_sharpe = sorted([r for r in results if r["pnl"] > 0], key=lambda r: r["sharpe"], reverse=True)[:args.top]
    print(f"  {'#':<3} {'CE':<9} {'Time':<12} {'Symbol':<12} {'SL':>6} {'Floor':>6} "
          f"{'Trades':>6} {'Win%':>5} {'P&L':>12} {'Avg/Day':>10} {'MaxDD':>10} "
          f"{'G/R':>5} {'Sharpe':>7} {'Calmar':>7}")
    print(f"  {'─'*130}")
    for i, r in enumerate(by_sharpe, 1):
        print(f"  {i:<3} {r['ce']:<9} {r['time']:<12} {r['sym']:<12} {r['sl']:>6} {r['floor']:>6} "
              f"{r['trades']:>6} {r['win_rate']:>4.0f}% ₹{r['pnl']:>+10,.0f} ₹{r['avg_daily']:>+8,.0f} "
              f"₹{r['max_dd']:>8,.0f} {r['green']}/{r['red']:<2} {r['sharpe']:>+6.2f} {r['calmar']:>6.2f}")

    # ================================================================
    # Best overall recommendation
    # ================================================================
    best = by_calmar[0] if by_calmar else by_pnl[0]
    print(f"\n{'='*140}")
    print(f"  RECOMMENDED CONFIGURATION")
    print(f"{'='*140}")
    print(f"  CE filter:     {best['ce']}")
    print(f"  Time filter:   {best['time']}")
    print(f"  Symbol filter: {best['sym']}")
    print(f"  SL cap:        ₹{best['sl']:,}")
    print(f"  Profit floor:  ₹{best['floor']:,}")
    print(f"  ---")
    print(f"  Expected trades/month: {best['trades']}")
    print(f"  Win rate:       {best['win_rate']:.0f}%")
    print(f"  Monthly P&L:    ₹{best['pnl']:+,.0f}")
    print(f"  Daily avg:      ₹{best['avg_daily']:+,.0f}")
    print(f"  Max drawdown:   ₹{best['max_dd']:,.0f}")
    print(f"  Calmar ratio:   {best['calmar']:.2f}")
    print(f"{'='*140}")

    out_file = os.path.join(_data_dir, f"optimize_ch2_{start_date}_{end_date}.json")
    with open(out_file, "w") as f:
        json.dump({
            "period": f"{start_date} to {end_date}",
            "top_by_pnl": by_pnl[:10],
            "top_by_calmar": [r for r in by_calmar[:10]],
            "top_by_sharpe": [r for r in by_sharpe[:10]],
            "recommended": best,
        }, f, indent=2, default=str)
    print(f"\nResults saved: {out_file}")


asyncio.run(main())

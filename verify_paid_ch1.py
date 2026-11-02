#!/usr/bin/env python3
"""Verify DT paid channel signals against actual candle data via Upstox API.
For each signal: resolve the option instrument, fetch 1-min candles, walk price
to see if SL or Target hit first."""
import json, re, os, sys, time, calendar
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import config
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo("Asia/Kolkata")

with open("data/ch1_oct_messages.json") as f:
    messages = json.load(f)

# ── Parse signals (same as analyze script) ────────────────────────
SIGNAL_RE = re.compile(
    r'BUY\s+([A-Z&\s]+?)\s*\n+\s*(\d+(?:\.\d+)?)\s+(CE|PE)\s+(?:ABOVE|BELOW|NEAR|@|AT)\s+(\d+(?:\.\d+)?)\s*\n+\s*SL\s+(\d+(?:\.\d+)?)\s+TARGET\s+([\d.\s]+)',
    re.IGNORECASE | re.MULTILINE
)
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}

signals = []
for m in messages:
    match = SIGNAL_RE.search(m["text"].strip())
    if not match:
        continue
    symbol = match.group(1).upper().strip().replace(" ", "")
    if symbol in INDEX_NAMES:
        continue
    strike = float(match.group(2))
    opt_type = match.group(3).upper()
    entry_price = float(match.group(4))
    sl = float(match.group(5))
    targets = [float(x) for x in re.findall(r'[\d.]+', match.group(6).strip())]
    has_wait = "WAIT TO ACTIVATE" in m["text"].upper()

    signals.append({
        "msg_id": m["id"],
        "ts": m["ts"],
        "symbol": symbol,
        "strike": strike,
        "option_type": opt_type,
        "entry_price": entry_price,
        "sl": sl,
        "targets": targets,
        "has_wait": has_wait,
    })

print(f"Parsed {len(signals)} stock option signals")

# ── Load Upstox master for instrument resolution ──────────────────
token = load_cached_token()
if not token:
    print("ERROR: No Upstox token")
    sys.exit(1)

ud = UpstoxData()
master = ud._load_master()
print(f"Master instruments loaded: {len(master)}")

def find_nearest_expiry(symbol, strike, opt_type, signal_date):
    """Find the nearest monthly expiry option matching the signal."""
    candidates = []
    for inst in master:
        seg = inst.get("segment", "")
        if seg != "NSE_FO":
            continue
        if inst.get("instrument_type") != opt_type:
            continue
        asset = (inst.get("asset_symbol") or inst.get("name", "")).upper().replace(" ", "")
        if asset != symbol:
            continue
        try:
            inst_strike = float(inst.get("strike_price", -1))
        except (ValueError, TypeError):
            continue
        if abs(inst_strike - strike) > 0.01:
            continue
        exp = inst.get("expiry")
        if not exp:
            continue
        try:
            exp_date = datetime.strptime(exp[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if exp_date < signal_date:
            continue
        candidates.append((exp_date, inst))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]

# ── Walk candles for each signal ──────────────────────────────────
results = []
api_calls = 0

for i, sig in enumerate(signals):
    sig_dt = datetime.fromisoformat(sig["ts"])
    sig_date = sig_dt.date()

    inst = find_nearest_expiry(sig["symbol"], sig["strike"], sig["option_type"], sig_date)
    if inst is None:
        results.append({**sig, "result": "NO_INSTRUMENT", "actual_pnl": 0, "exit_at": None})
        continue

    ikey = inst.get("instrument_key") or inst.get("instrument_token", "")
    exp_str = inst.get("expiry", "")[:10]

    # Fetch 1-min candles for signal day
    from_str = sig_date.isoformat()
    to_str = sig_date.isoformat()

    try:
        candles = ud.historical_candle(ikey, "1minute", from_str, to_str)
        api_calls += 1
    except Exception as e:
        results.append({**sig, "result": f"API_ERROR: {e}", "actual_pnl": 0, "exit_at": None})
        if api_calls % 5 == 0:
            time.sleep(1)
        continue

    if api_calls % 5 == 0:
        time.sleep(0.5)

    if not candles:
        results.append({**sig, "result": "NO_CANDLES", "actual_pnl": 0, "exit_at": None})
        continue

    entry = sig["entry_price"]
    sl = sig["sl"]
    tgt1 = sig["targets"][0] if sig["targets"] else entry * 1.1

    # Walk candles from signal time
    sig_minute = sig_dt.strftime("%H:%M")
    entered = False
    hit_sl = False
    hit_tgt = False
    exit_price = None
    exit_time = None
    high_after_entry = entry

    for c in candles:
        # candle format: [timestamp, open, high, low, close, volume, oi]
        c_ts = c[0] if isinstance(c, list) else c.get("timestamp", "")
        if isinstance(c, list):
            c_open, c_high, c_low, c_close = c[1], c[2], c[3], c[4]
        else:
            c_open = c.get("open", 0)
            c_high = c.get("high", 0)
            c_low = c.get("low", 0)
            c_close = c.get("close", 0)

        # Extract time from candle
        if isinstance(c_ts, str):
            try:
                c_time = datetime.fromisoformat(c_ts.replace("Z", "+00:00")).astimezone(IST).strftime("%H:%M")
                c_time_full = datetime.fromisoformat(c_ts.replace("Z", "+00:00")).astimezone(IST)
            except:
                continue
        else:
            continue

        if c_time < sig_minute:
            continue

        if not entered:
            if c_high >= entry:
                entered = True
            else:
                continue

        # Track peak
        if c_high > high_after_entry:
            high_after_entry = c_high

        # Check SL hit (low touches SL)
        if c_low <= sl:
            hit_sl = True
            exit_price = sl
            exit_time = c_time
            break

        # Check T1 hit
        if c_high >= tgt1:
            hit_tgt = True
            exit_price = tgt1
            exit_time = c_time
            break

    # If neither hit by EOD, use last candle close
    if entered and not hit_sl and not hit_tgt:
        last_c = candles[-1]
        if isinstance(last_c, list):
            exit_price = last_c[4]  # close
        else:
            exit_price = last_c.get("close", entry)
        exit_time = "15:30"

    if not entered:
        results.append({**sig, "result": "NOT_TRIGGERED", "actual_pnl": 0,
                       "exit_at": None, "peak": 0})
        continue

    actual_pnl = (exit_price - entry) if exit_price else 0
    result_tag = "SL_HIT" if hit_sl else "TGT1_HIT" if hit_tgt else "EOD_EXIT"

    results.append({
        **sig, "result": result_tag, "actual_pnl": actual_pnl,
        "exit_at": exit_time, "exit_price_actual": exit_price,
        "peak": high_after_entry,
        "instrument": ikey, "expiry": exp_str,
    })

    if (i + 1) % 10 == 0:
        print(f"  Processed {i+1}/{len(signals)}...")

# ── Print results ─────────────────────────────────────────────────
print(f"\n{'='*100}")
print("  ACTUAL CANDLE VERIFICATION — DT PAID CHANNEL OCT 2026")
print(f"{'='*100}")

by_date = defaultdict(list)
for r in results:
    by_date[r["ts"][:10]].append(r)

total_pnl = 0
wins = losses = no_trade = errors = 0

for day in sorted(by_date):
    day_pnl = sum(r["actual_pnl"] for r in by_date[day])
    total_pnl += day_pnl
    print(f"\n  ── {day} ({len(by_date[day])} signals, day P&L: {day_pnl:+.1f} pts) ──")
    for r in by_date[day]:
        ts = r["ts"][11:16]
        tgts = "/".join(str(t) for t in r["targets"])
        wait = " [W]" if r["has_wait"] else ""
        res = r["result"]
        pnl = r["actual_pnl"]
        exit_t = r.get("exit_at", "")
        peak = r.get("peak", 0)

        if res in ("SL_HIT",):
            losses += 1
        elif res in ("TGT1_HIT", "EOD_EXIT"):
            if pnl > 0:
                wins += 1
            else:
                losses += 1
        elif res == "NOT_TRIGGERED":
            no_trade += 1
        else:
            errors += 1

        print(f"    {ts} | {r['symbol']} {r['strike']} {r['option_type']} | "
              f"E:{r['entry_price']} SL:{r['sl']} T1:{r['targets'][0] if r['targets'] else '?'} | "
              f"{res} @{r.get('exit_price_actual', '?')} ({exit_t}) | "
              f"PnL:{pnl:+.1f}{wait}")

# ── Summary ───────────────────────────────────────────────────────
print(f"\n{'='*100}")
print("  SUMMARY")
print(f"{'='*100}")
print(f"  Total signals: {len(signals)}")
print(f"  Traded (triggered): {wins + losses}")
print(f"  Not triggered: {no_trade}")
print(f"  Errors/No instrument: {errors}")
print(f"  Wins: {wins}, Losses: {losses}")
if (wins + losses) > 0:
    print(f"  Win rate: {wins*100/(wins+losses):.1f}%")
print(f"  Total P&L (points, 1 lot): {total_pnl:+,.1f}")

# Daily summary
print(f"\n  Daily breakdown:")
running = 0
win_days = loss_days = 0
for day in sorted(by_date):
    day_results = [r for r in by_date[day] if r["result"] not in ("NO_INSTRUMENT", "API_ERROR", "NO_CANDLES", "NOT_TRIGGERED")]
    day_pnl = sum(r["actual_pnl"] for r in day_results)
    day_wins = sum(1 for r in day_results if r["actual_pnl"] > 0)
    day_losses = sum(1 for r in day_results if r["actual_pnl"] <= 0)
    running += day_pnl
    if day_pnl > 0:
        win_days += 1
    elif day_pnl < 0:
        loss_days += 1
    print(f"    {day}  {len(day_results):>2} traded  W:{day_wins} L:{day_losses}  "
          f"PnL:{day_pnl:>+8.1f}  Running:{running:>+10.1f}")

print(f"\n  Win days: {win_days}, Loss days: {loss_days}")
if len(by_date) > 0:
    print(f"  Avg daily P&L: {total_pnl/len(by_date):+.1f} pts")

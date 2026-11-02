#!/usr/bin/env python3
"""Analyze DT paid channel stock option signals for October 2026.
Parse signals, compare with actual candle data via Upstox API."""
import json, re, os, sys, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

with open("data/ch1_oct_messages.json") as f:
    messages = json.load(f)

# ── Step 1: Parse entry signals ──────────────────────────────────
SIGNAL_RE = re.compile(
    r'BUY\s+([A-Z&\s]+?)\s*\n+\s*(\d+(?:\.\d+)?)\s+(CE|PE)\s+(?:ABOVE|BELOW|NEAR|@|AT)\s+(\d+(?:\.\d+)?)\s*\n+\s*SL\s+(\d+(?:\.\d+)?)\s+TARGET\s+([\d.\s]+)',
    re.IGNORECASE | re.MULTILINE
)

INDEX_NAMES = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}

signals = []
msg_by_id = {m["id"]: m for m in messages}

for m in messages:
    text = m["text"].strip().upper()
    match = SIGNAL_RE.search(m["text"].strip())
    if not match:
        continue

    symbol = match.group(1).upper().replace(" ", "")
    if symbol in INDEX_NAMES:
        continue  # skip index signals, we only want stock

    strike = float(match.group(2))
    opt_type = match.group(3).upper()
    entry_price = float(match.group(4))
    sl = float(match.group(5))
    targets_raw = match.group(6).strip()
    targets = [float(x) for x in re.findall(r'[\d.]+', targets_raw)]

    # Check for "WAIT TO ACTIVATE" in same message or immediate reply
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

# Also check replies for WAIT TO ACTIVATE
for m in messages:
    if m["reply_to"] and "WAIT TO ACTIVATE" in m["text"].upper():
        for s in signals:
            if s["msg_id"] == m["reply_to"]:
                s["has_wait"] = True

print(f"Total messages: {len(messages)}")
print(f"Parsed signals: {len(signals)} (stock options only, excluding index)")
print(f"  With WAIT TO ACTIVATE: {sum(1 for s in signals if s['has_wait'])}")
print(f"  Direct entry: {sum(1 for s in signals if not s['has_wait'])}")

# ── Step 2: Collect follow-up messages for each signal ────────────
for s in signals:
    s["followups"] = []
for m in messages:
    if m["reply_to"]:
        for s in signals:
            if s["msg_id"] == m["reply_to"]:
                s["followups"].append({"ts": m["ts"], "text": m["text"]})

# ── Step 3: Determine outcome from follow-up messages ─────────────
for s in signals:
    s["channel_outcome"] = "UNKNOWN"
    s["channel_exit_price"] = None
    for fu in s["followups"]:
        txt = fu["text"].upper()
        if "SL HIT" in txt or "SL TRIGGERED" in txt or "STOPLOSS" in txt:
            s["channel_outcome"] = "SL_HIT"
        elif "BOOK" in txt or "TRAIL" in txt:
            s["channel_outcome"] = "PROFIT_BOOK"
            nums = re.findall(r'[\d.]+', fu["text"])
            if nums:
                s["channel_exit_price"] = float(nums[0])
        elif "TARGET" in txt and ("DONE" in txt or "ACHIEVED" in txt or "HIT" in txt):
            s["channel_outcome"] = "TARGET_HIT"
        elif "COST" in txt and "SL" in txt:
            s["channel_outcome"] = "SL_COST"  # SL moved to cost
        elif re.match(r'^\s*[\d.]+\s*$', fu["text"].strip()):
            price = float(fu["text"].strip())
            if price > s["entry_price"]:
                s["channel_outcome"] = "PROFIT_BOOK"
                s["channel_exit_price"] = price

# ── Step 4: Print signal summary ──────────────────────────────────
print(f"\n{'='*90}")
print("  OCTOBER 2026 — DT PAID CHANNEL STOCK OPTION SIGNALS")
print(f"{'='*90}")

by_date = defaultdict(list)
for s in signals:
    day = s["ts"][:10]
    by_date[day].append(s)

for day in sorted(by_date):
    print(f"\n  ── {day} ({len(by_date[day])} signals) ──")
    for s in by_date[day]:
        ts = s["ts"][11:16]
        tgts = "/".join(str(t) for t in s["targets"])
        wait = " [WAIT]" if s["has_wait"] else ""
        outcome = s["channel_outcome"]
        exit_p = f" @{s['channel_exit_price']}" if s["channel_exit_price"] else ""
        print(f"    {ts} | {s['symbol']} {s['strike']} {s['option_type']} | "
              f"Entry:{s['entry_price']} SL:{s['sl']} TGT:{tgts} | "
              f"{outcome}{exit_p}{wait}")

# ── Step 5: Summary statistics ────────────────────────────────────
outcomes = defaultdict(int)
for s in signals:
    outcomes[s["channel_outcome"]] += 1

print(f"\n{'='*90}")
print("  OUTCOME SUMMARY (from channel follow-up messages)")
print(f"{'='*90}")
for k, v in sorted(outcomes.items(), key=lambda x: -x[1]):
    print(f"  {k:<20} {v:>4} signals")
print(f"  {'TOTAL':<20} {len(signals):>4} signals")

# ── Step 6: Estimate P&L per signal ──────────────────────────────
total_pnl = 0
win = loss = unknown = 0
daily_pnl = defaultdict(float)

print(f"\n{'='*90}")
print("  ESTIMATED P&L (1 lot per signal, entry→exit or entry→SL)")
print(f"{'='*90}")

for s in signals:
    day = s["ts"][:10]
    entry = s["entry_price"]
    sl = s["sl"]
    tgt1 = s["targets"][0] if s["targets"] else entry * 1.1

    if s["channel_outcome"] == "SL_HIT":
        pnl = sl - entry  # loss
        loss += 1
    elif s["channel_outcome"] == "SL_COST":
        pnl = 0  # breakeven
        unknown += 1
    elif s["channel_outcome"] in ("PROFIT_BOOK", "TARGET_HIT"):
        if s["channel_exit_price"]:
            pnl = s["channel_exit_price"] - entry
        else:
            pnl = tgt1 - entry  # assume T1 hit
        win += 1
    else:
        pnl = 0
        unknown += 1

    total_pnl += pnl
    daily_pnl[day] += pnl

print(f"  Wins: {win}, Losses: {loss}, Unknown/Breakeven: {unknown}")
print(f"  Win rate: {win*100/(win+loss):.1f}%" if (win+loss) > 0 else "  Win rate: N/A")
print(f"  Estimated total points: {total_pnl:+,.1f}")

print(f"\n  Daily P&L (points):")
running = 0
for day in sorted(daily_pnl):
    running += daily_pnl[day]
    cnt = len(by_date[day])
    print(f"    {day}  {cnt:>2} signals  {daily_pnl[day]:>+8.1f} pts  running: {running:>+10.1f}")

# ── Step 7: Unique symbols ────────────────────────────────────────
symbols = defaultdict(int)
for s in signals:
    symbols[s["symbol"]] += 1
print(f"\n  Unique stocks: {len(symbols)}")
print(f"  Most traded: {sorted(symbols.items(), key=lambda x: -x[1])[:10]}")

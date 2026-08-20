"""Analyze a Telegram channel's signal quality from dumped messages.

Parses signal messages (SYMBOL CE/PE, ABOVE/NEAR price, TGT, SL)
and tracks whether targets or SL were hit based on follow-up messages.

Usage: python3 scripts/analyze_channel.py /tmp/channel_msgs.txt
"""
import sys
import re
from collections import defaultdict
from datetime import datetime

if len(sys.argv) < 2:
    print("Usage: python3 scripts/analyze_channel.py <messages_file>")
    sys.exit(1)

with open(sys.argv[1], "r") as f:
    raw = f.read()

blocks = raw.split("\n---\n")

# Parse timestamp + text from each block
messages = []
for block in blocks:
    block = block.strip()
    if not block:
        continue
    m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] (.*)", block, re.DOTALL)
    if m:
        ts_str, text = m.group(1), m.group(2).strip()
        messages.append({"ts": ts_str, "text": text})

print(f"Total messages parsed: {len(messages)}")
print()

# Pattern to detect a fresh signal call
# e.g. "NIFTY 23850 CE" or "BANK NIFTY 57400 PE" or "INDIGO 5100 CE"
# Followed within next few messages by ABOVE/NEAR + TGT + SL
SIGNAL_RE = re.compile(
    r"^(?:BANK\s*NIFTY|NIFTY|SENSEX|FINNIFTY|MIDCPNIFTY|"
    r"[A-Z]{2,20})\s+\d+\s+(?:CE|PE)\s*$",
    re.MULTILINE,
)

ENTRY_RE = re.compile(r"(?:ABOVE|NEAR|Above|Near|above|near)\s+(\d+[\.\d]*)", re.IGNORECASE)
TGT_RE = re.compile(r"(?:TGT|TARGET|Tgt|tgt)\s+(\d+[\s/\d\+\.\-]+)", re.IGNORECASE)
SL_RE = re.compile(r"(?:SL|Sl|sl|STOPLOSS|Stop\s*loss)\s+(?:below\s+)?(\d+[\.\d]*)", re.IGNORECASE)

TGT_HIT_RE = re.compile(r"(?:1st|2nd|3rd|first|second|third)\s+(?:TGT|target|hit|done)", re.IGNORECASE)
ALL_TGT_RE = re.compile(r"(?:all\s+TGT|all\s+target|Jackpot|JACKPOT|boom|BOOM)", re.IGNORECASE)
SL_HIT_RE = re.compile(r"(?:SL\s+hit|sl\s+hit|stop\s*loss\s+hit|loss\s*book)", re.IGNORECASE)

signals = []
i = 0
while i < len(messages):
    text = messages[i]["text"]
    # Remove emoji/bold markers
    clean = re.sub(r"[*\U0001F600-\U0001FAFF☀-➿❤️]+", "", text).strip()

    # Check if this is a signal header
    if SIGNAL_RE.search(clean):
        sig_match = SIGNAL_RE.search(clean)
        symbol = sig_match.group(0).strip()
        ts = messages[i]["ts"]
        date = ts[:10]

        entry = None
        tgts = []
        sl = None

        # Look at this message and next 5 for entry/tgt/sl
        window = [messages[i]["text"]]
        for j in range(1, min(8, len(messages) - i)):
            window.append(messages[i + j]["text"])

        combined = "\n".join(window)

        em = ENTRY_RE.search(combined)
        if em:
            entry = float(em.group(1))

        tm = TGT_RE.search(combined)
        if tm:
            tgt_str = tm.group(1)
            tgts = [float(x) for x in re.findall(r"(\d+[\.\d]*)", tgt_str)]

        sm = SL_RE.search(combined)
        if sm:
            sl_val = sm.group(1)
            # "SL 7-8 point" means SL is entry - 7/8 points
            if entry and float(sl_val) < 30:
                sl = entry - float(sl_val)
            else:
                sl = float(sl_val)

        # Now track outcome from subsequent messages (same day)
        outcome = "UNKNOWN"
        tgt_count = 0
        sl_triggered = False

        for j in range(1, min(80, len(messages) - i)):
            future_msg = messages[i + j]
            future_date = future_msg["ts"][:10]
            if future_date != date:
                break
            ft = future_msg["text"]

            if TGT_HIT_RE.search(ft):
                tgt_count += 1
            if ALL_TGT_RE.search(ft):
                tgt_count = max(tgt_count, 3)
            if SL_HIT_RE.search(ft):
                sl_triggered = True
            # Check for "Hit" with angry emoji (SL hit)
            if re.search(r"Hit\s*😡", ft):
                sl_triggered = True
            # "point loss" or "point Done" patterns
            if re.search(r"\d+\s+point\s+(?:loss|Loss)", ft):
                sl_triggered = True
            if re.search(r"\d+\s+point\s+(?:Done|done|profit)", ft):
                if tgt_count == 0:
                    tgt_count = 1

        if sl_triggered:
            outcome = "SL_HIT"
        elif tgt_count >= 3:
            outcome = "ALL_TGT"
        elif tgt_count >= 2:
            outcome = "TGT2"
        elif tgt_count >= 1:
            outcome = "TGT1"
        else:
            # Check if there were price updates going up from entry
            if entry:
                max_price = 0
                for j in range(1, min(60, len(messages) - i)):
                    future_msg = messages[i + j]
                    future_date = future_msg["ts"][:10]
                    if future_date != date:
                        break
                    ft = future_msg["text"].strip()
                    if re.match(r"^\d+[\.\d]*$", ft):
                        p = float(ft)
                        if p > max_price:
                            max_price = p
                if max_price > 0 and tgts:
                    if max_price >= tgts[0]:
                        outcome = "TGT1"
                    elif sl and max_price <= sl:
                        outcome = "SL_HIT"
                    else:
                        outcome = "PARTIAL"

        if entry:
            signals.append({
                "ts": ts,
                "date": date,
                "symbol": symbol,
                "entry": entry,
                "tgts": tgts,
                "sl": sl,
                "outcome": outcome,
                "tgt_count": tgt_count,
            })

    i += 1

# Filter out duplicate signals (same symbol same minute)
seen = set()
unique_signals = []
for s in signals:
    key = f"{s['ts']}_{s['symbol']}"
    if key not in seen:
        seen.add(key)
        unique_signals.append(s)

signals = unique_signals

print("=" * 80)
print(f"CHANNEL SIGNAL ANALYSIS — Shrivastav G Prime")
print(f"Period: {signals[0]['date'] if signals else 'N/A'} to {signals[-1]['date'] if signals else 'N/A'}")
print(f"Total signals found: {len(signals)}")
print("=" * 80)
print()

# Categorize
index_signals = [s for s in signals if any(x in s["symbol"] for x in ["NIFTY", "SENSEX", "BANK"])]
stock_signals = [s for s in signals if s not in index_signals]
commodity_signals = [s for s in signals if any(x in s["symbol"] for x in ["CRUDE", "GOLD", "SILVER", "NATURAL"])]

# Remove commodities from stock
stock_signals = [s for s in stock_signals if s not in commodity_signals]

# Outcome counts
def analyze_group(name, group):
    if not group:
        print(f"  {name}: No signals")
        return

    tgt1 = sum(1 for s in group if s["outcome"] in ("TGT1", "TGT2", "ALL_TGT"))
    tgt2 = sum(1 for s in group if s["outcome"] in ("TGT2", "ALL_TGT"))
    tgt3 = sum(1 for s in group if s["outcome"] == "ALL_TGT")
    sl_hit = sum(1 for s in group if s["outcome"] == "SL_HIT")
    partial = sum(1 for s in group if s["outcome"] == "PARTIAL")
    unknown = sum(1 for s in group if s["outcome"] == "UNKNOWN")

    wr = tgt1 / len(group) * 100 if group else 0

    print(f"  {name}")
    print(f"  {'─' * 50}")
    print(f"    Total signals:     {len(group)}")
    print(f"    TGT1+ hit:         {tgt1} ({tgt1/len(group)*100:.0f}%)")
    print(f"    TGT2+ hit:         {tgt2} ({tgt2/len(group)*100:.0f}%)")
    print(f"    All TGT hit:       {tgt3} ({tgt3/len(group)*100:.0f}%)")
    print(f"    SL hit:            {sl_hit} ({sl_hit/len(group)*100:.0f}%)")
    print(f"    Partial/Small:     {partial}")
    print(f"    Unknown:           {unknown}")
    print(f"    Win Rate (TGT1+):  {wr:.0f}%")
    print()

print("─" * 80)
print("OVERALL BREAKDOWN")
print("─" * 80)
print()
analyze_group("ALL SIGNALS", signals)
analyze_group("INDEX OPTIONS (NIFTY/BANKNIFTY/SENSEX)", index_signals)
analyze_group("STOCK OPTIONS", stock_signals)
if commodity_signals:
    analyze_group("COMMODITIES (CRUDE/GOLD)", commodity_signals)

# Daily breakdown
print("─" * 80)
print("DAILY SIGNAL COUNT & WIN RATE")
print("─" * 80)
print()
print(f"  {'Date':<12} {'Signals':>8} {'TGT1+':>8} {'SL':>8} {'WR':>8}")
print(f"  {'─'*12} {'─'*8} {'─'*8} {'─'*8} {'─'*8}")

daily = defaultdict(list)
for s in signals:
    daily[s["date"]].append(s)

total_days = 0
green_days = 0
for date in sorted(daily.keys()):
    day_sigs = daily[date]
    wins = sum(1 for s in day_sigs if s["outcome"] in ("TGT1", "TGT2", "ALL_TGT"))
    losses = sum(1 for s in day_sigs if s["outcome"] == "SL_HIT")
    wr = wins / len(day_sigs) * 100 if day_sigs else 0
    total_days += 1
    if wins > losses:
        green_days += 1
    marker = "+" if wins > losses else ("-" if losses > wins else "~")
    print(f"  {date:<12} {len(day_sigs):>8} {wins:>8} {losses:>8} {wr:>7.0f}% {marker}")

print()
print(f"  Green days: {green_days}/{total_days} ({green_days/total_days*100:.0f}%)")

# Print each signal detail
print()
print("─" * 80)
print("SIGNAL DETAILS")
print("─" * 80)
print()
print(f"  {'Time':<17} {'Symbol':<25} {'Entry':>7} {'SL':>7} {'TGTs':<20} {'Outcome':<10}")
print(f"  {'─'*17} {'─'*25} {'─'*7} {'─'*7} {'─'*20} {'─'*10}")

for s in signals:
    tgt_str = "/".join(str(int(t)) for t in s["tgts"][:3]) if s["tgts"] else "?"
    sl_str = f"{s['sl']:.0f}" if s["sl"] else "?"
    icon = {"TGT1": "T1", "TGT2": "T2", "ALL_TGT": "T3", "SL_HIT": "SL",
            "PARTIAL": "PT", "UNKNOWN": "??"}
    marker = icon.get(s["outcome"], "??")
    color = "W" if s["outcome"] in ("TGT1", "TGT2", "ALL_TGT") else ("L" if s["outcome"] == "SL_HIT" else "?")
    print(f"  {s['ts']:<17} {s['symbol']:<25} {s['entry']:>7.0f} {sl_str:>7} {tgt_str:<20} [{color}] {marker}")

print()
print("=" * 80)
print("VERDICT")
print("=" * 80)
print()
wins = sum(1 for s in signals if s["outcome"] in ("TGT1", "TGT2", "ALL_TGT"))
losses = sum(1 for s in signals if s["outcome"] == "SL_HIT")
total = len(signals)
wr = wins / total * 100 if total else 0
print(f"  Signals: {total} | Wins: {wins} | Losses: {losses} | Win Rate: {wr:.0f}%")
print(f"  Green Days: {green_days}/{total_days}")
print(f"  Avg signals/day: {total/total_days:.1f}" if total_days else "")
print()

# Compare with CH1
print("  COMPARISON WITH YOUR CURRENT CHANNEL (CH1):")
print(f"    CH1 Win Rate: ~62%")
print(f"    This channel:  {wr:.0f}%")
print()
if wr > 65:
    print("  This channel looks BETTER than CH1 on win rate.")
elif wr > 55:
    print("  Similar quality to CH1. Worth testing in parallel.")
else:
    print("  Lower win rate than CH1. May not be worth switching.")
print()
print("=" * 80)

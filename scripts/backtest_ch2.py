#!/usr/bin/env python3
"""Backtest Shrivastav G Prime channel signals with the FIXED parser.

Uses the same parser logic as the live ch2 listener (split-message buffering,
case-insensitive, SL-in-symbol fix, etc.) to parse all signals from the
channel dump, then tracks outcomes from follow-up price messages.

Usage: .venv/bin/python3 scripts/backtest_ch2.py /tmp/channel_msgs.txt
"""
import sys, re, time as _time
from collections import defaultdict
from datetime import datetime
from dataclasses import dataclass

LOTS = 3
CAPITAL_PER_TRADE = 200_000
PROFIT_CAP = 2000
MAX_LOSS_CAP = 8000

LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 40,
    "MIDCPNIFTY": 50,
    "RELIANCE": 250, "TCS": 175, "INFY": 600, "INDIGO": 300,
    "TRENT": 625, "PAYTM": 1600, "KEI": 200, "BSE": 250,
    "TITAN": 375, "BRITANNIA": 200, "ABB": 250, "HAL": 300,
    "EICHERMOT": 150, "SIEMENS": 275, "MCX": 900, "POLYCAB": 200,
    "LTM": 200, "LTIM": 200, "PERSISTENT": 200, "DIXON": 200,
    "APOLLOHOSP": 250, "BAJAJAUTO": 250, "CUMMINSIND": 400,
    "MFSL": 1600, "PIIND": 300, "LT": 300, "MARUTI": 100,
    "RADICO": 1200, "BHARTIARTL": 475, "AMBER": 200,
    "MUTHOOTFIN": 1000, "HEROMOTOCO": 150, "HINDALCO": 1500,
    "MOTHERSON": 6000, "HAVELLS": 500, "WOCKPHARMA": 200,
}
DEFAULT_LOT = 400

# --- Parser (same logic as channel_listener.py parse_signal_ch2) ---
SKIP_COMMODITIES = {"CRUDEOIL", "CRUDE", "GOLD", "SILVER", "NATURALGAS", "GOLF"}
SKIP_NOISE = {"DISCLAIMER", "WATCH LIST", "IMPORTANT", "FAKE ALERT",
              "OFFER", "APPLICATION", "FOLLOW THIS", "PLS READ",
              "PERFORMANCE", "MEMBERS SEND", "CONGRATULATIONS"}

SYMBOL_RE = re.compile(
    r'(?:(?:Intra|positional|Hazing|Note)[/\s]*)*'
    r'((?:BANK\s*NIFTY|NIFTY|SENSEX|FINNIFTY|MIDCPNIFTY|[A-Za-z&]{2,20}))'
    r'\s+(\d+)\s+(CE|PE)',
    re.IGNORECASE | re.MULTILINE,
)
ENTRY_RE = re.compile(
    r'(?:ABOVE|NEAR|Entry\s+near|BUY\s*@|CMP)\s*[:\-]?\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)
TGT_RE = re.compile(
    r'(?:TGT|TARGET)\s*[:\-]?\s*([\d\s,/.+\-]+)',
    re.IGNORECASE,
)
SL_RE = re.compile(
    r'(?:^|[^A-Z])(?:SL|Stop\s*loss)\s*(?:bel\w*\s*|use\s*)?(\d+(?:\.\d+)?(?:\s*[-–]\s*\d+)?)\s*(point)?',
    re.IGNORECASE,
)

TGT_HIT_RE = re.compile(r'(?:1st|2nd|3rd|first|second|third|TGT)\s*(?:TGT\s*)?hit', re.I)
ALL_TGT_RE = re.compile(r'(?:all\s+TGT|all\s+target|Jackpot|capital\s+double|BOOM)', re.I)
SL_HIT_RE = re.compile(r'(?:SL\s+hit|stop\s*loss\s+hit|loss\s*book|Gone\s*😔)', re.I)
NOT_ACTIVE_RE = re.compile(r'not\s+active\s+avoid', re.I)


@dataclass
class Signal:
    ts: str
    date: str
    symbol: str
    strike: float
    opt_type: str
    entry: float
    targets: list
    sl: float
    outcome: str = "UNKNOWN"
    tgt_count: int = 0
    max_price: float = 0
    exit_price: float = 0


def extract_sl(sl_match, trigger):
    raw = sl_match.group(1).strip()
    is_pts = sl_match.group(2) is not None
    if '-' in raw or '–' in raw:
        parts = re.split(r'[-–]', raw)
        val = float(parts[0].strip())
    else:
        val = float(raw)
    if is_pts and trigger > 0:
        return trigger - val
    if val <= 25 and trigger > 50:
        return trigger - val
    return val


def extract_targets(tgt_match):
    raw = tgt_match.group(1)
    nums = re.findall(r'\d+(?:\.\d+)?', raw)
    return [float(n) for n in nums if float(n) > 0]


# --- Read messages ---
msg_file = sys.argv[1] if len(sys.argv) > 1 else "/tmp/channel_msgs.txt"
with open(msg_file) as f:
    raw = f.read()

blocks = raw.split("\n---\n")
messages = []
for block in blocks:
    block = block.strip()
    if not block:
        continue
    m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] (.*)", block, re.DOTALL)
    if m:
        messages.append({"ts": m.group(1), "text": m.group(2).strip()})

print(f"Total messages: {len(messages)}")

# --- Parse signals using buffered parser ---
signals = []
pending = None
pending_ts = ""

for i, msg in enumerate(messages):
    text = msg["text"]
    ts = msg["ts"]
    clean = text.replace("**", "")
    clean = re.sub(r'[\U0001F600-\U0001FAFF☀-➿❤️‍]+', ' ', clean).strip()

    if len(clean) < 5:
        continue

    upper = clean.upper()
    if any(skip in upper for skip in SKIP_NOISE):
        continue

    if NOT_ACTIVE_RE.search(upper):
        pending = None
        continue

    sym_match = SYMBOL_RE.search(clean)
    entry_match = ENTRY_RE.search(clean)
    tgt_match = TGT_RE.search(clean)
    sl_match = SL_RE.search(clean)

    has_sym = sym_match is not None
    has_tgt = tgt_match is not None
    has_sl = sl_match is not None

    if has_sym:
        raw_sym = sym_match.group(1).upper().strip()
        raw_sym = re.sub(r'\s+', ' ', raw_sym)
        if raw_sym == "BANK NIFTY":
            raw_sym = "BANKNIFTY"
        if raw_sym in SKIP_COMMODITIES:
            continue

        strike = float(sym_match.group(2))
        opt_type = sym_match.group(3).upper()
        trigger = float(entry_match.group(1)) if entry_match else 0.0

        if has_tgt and has_sl:
            targets = extract_targets(tgt_match)
            sl = extract_sl(sl_match, trigger)
            if sl <= 0 or not targets or trigger <= 0:
                continue
            # Skip swing/positional unless also intra
            if any(kw in upper for kw in ("SWING", "POSITIONAL", "HOLD WITH PATIENCE")):
                if "INTRA" not in upper:
                    continue

            signals.append(Signal(
                ts=ts, date=ts[:10], symbol=raw_sym, strike=strike,
                opt_type=opt_type, entry=trigger, targets=targets, sl=sl,
            ))
            pending = None
        else:
            # Buffer partial — wait for TGT/SL in next message
            pending = {
                "ts": ts, "date": ts[:10], "symbol": raw_sym, "strike": strike,
                "opt_type": opt_type, "trigger": trigger,
            }
            pending_ts = ts
        continue

    # No symbol found — check if we can complete a pending signal
    if not has_sym and pending and (has_tgt or has_sl):
        # Check messages are close in time (same minute or next)
        if has_tgt and has_sl:
            targets = extract_targets(tgt_match)
            trigger = pending["trigger"]
            if not trigger and entry_match:
                trigger = float(entry_match.group(1))
            sl = extract_sl(sl_match, trigger)
            if sl > 0 and targets and trigger > 0:
                signals.append(Signal(
                    ts=pending["ts"], date=pending["date"],
                    symbol=pending["symbol"], strike=pending["strike"],
                    opt_type=pending["opt_type"], entry=trigger,
                    targets=targets, sl=sl,
                ))
            pending = None

# --- Deduplicate (same symbol + same minute) ---
seen = set()
unique = []
for s in signals:
    key = f"{s.ts}_{s.symbol}_{s.strike}_{s.opt_type}"
    if key not in seen:
        seen.add(key)
        unique.append(s)
signals = unique

print(f"Parsed {len(signals)} equity signals")

# --- Track outcomes from follow-up messages ---
# For each signal, look at subsequent messages on the same day to determine outcome
msg_by_date = defaultdict(list)
for msg in messages:
    msg_by_date[msg["ts"][:10]].append(msg)

for sig in signals:
    day_msgs = msg_by_date[sig.date]
    sig_time = sig.ts

    # Find messages AFTER this signal on the same day
    after = False
    prices_seen = []
    for msg in day_msgs:
        if msg["ts"] == sig_time and SYMBOL_RE.search(msg["text"].replace("**", "")):
            after = True
            continue
        if not after:
            if msg["ts"] > sig_time:
                after = True
            else:
                continue

        ft = msg["text"].strip()

        # Check for TGT hit
        if TGT_HIT_RE.search(ft):
            sig.tgt_count = max(sig.tgt_count, 1)
            if re.search(r'2nd|second', ft, re.I):
                sig.tgt_count = max(sig.tgt_count, 2)
            if re.search(r'3rd|third', ft, re.I):
                sig.tgt_count = max(sig.tgt_count, 3)
        if ALL_TGT_RE.search(ft):
            sig.tgt_count = max(sig.tgt_count, len(sig.targets))

        # Check for SL hit
        if SL_HIT_RE.search(ft):
            sig.outcome = "SL_HIT"

        # Track bare price numbers (could be for this or another signal)
        price_m = re.match(r'^(\d+(?:\.\d+)?)\s*$', ft)
        if price_m:
            p = float(price_m.group(1))
            # Only consider prices in a reasonable range of entry
            if sig.entry * 0.3 < p < sig.entry * 5:
                prices_seen.append(p)
                sig.max_price = max(sig.max_price, p)

    # Determine outcome
    if sig.outcome == "SL_HIT":
        sig.exit_price = sig.sl
    elif sig.tgt_count >= len(sig.targets):
        sig.outcome = "ALL_TGT"
        sig.exit_price = sig.targets[-1] if sig.targets else sig.entry
    elif sig.tgt_count >= 2:
        sig.outcome = "TGT2"
        sig.exit_price = sig.targets[1] if len(sig.targets) > 1 else sig.targets[0]
    elif sig.tgt_count >= 1:
        sig.outcome = "TGT1"
        sig.exit_price = sig.targets[0]
    else:
        # Check if max price from price feed hit TGT1
        if sig.max_price >= sig.targets[0]:
            sig.outcome = "TGT1"
            sig.exit_price = sig.targets[0]
        elif sig.max_price > 0 and sig.max_price <= sig.sl:
            sig.outcome = "SL_HIT"
            sig.exit_price = sig.sl
        elif sig.max_price > sig.entry:
            sig.outcome = "PARTIAL"
            sig.exit_price = sig.max_price
        else:
            sig.outcome = "UNKNOWN"
            sig.exit_price = sig.entry  # assume flat

# --- Simulate P&L ---
print()
print("=" * 115)
print("SHRIVASTAV G PRIME — BACKTEST (FIXED PARSER)")
print("=" * 115)
print(f"  Period: {signals[0].date} to {signals[-1].date}")
print(f"  Signals: {len(signals)} equity (commodities excluded)")
print(f"  Capital: ₹{CAPITAL_PER_TRADE:,} | Lots: {LOTS} | Profit cap: ₹{PROFIT_CAP:,} | Max loss: ₹{MAX_LOSS_CAP:,}")
print()

header = (f"  {'#':<4} {'Time':<17} {'Symbol':<20} {'Type':>3} {'Entry':>7} {'TGT1':>7} "
          f"{'SL':>7} {'Result':<9} {'Full P&L':>10} {'Capped':>10}")
print(header)
print("  " + "─" * 113)

daily = defaultdict(lambda: {"full_pnl": 0, "cap_pnl": 0, "trades": 0,
                              "wins": 0, "losses": 0, "unknown": 0})
total_full = 0
total_cap = 0
wins = 0
losses = 0
unknowns = 0

for idx, s in enumerate(signals):
    lot_size = LOT_SIZES.get(s.symbol, DEFAULT_LOT)
    qty = lot_size * LOTS

    if s.outcome in ("TGT1", "TGT2", "ALL_TGT"):
        pts = s.exit_price - s.entry
        icon = "W"
    elif s.outcome == "SL_HIT":
        pts = s.sl - s.entry  # negative
        icon = "L"
    elif s.outcome == "PARTIAL":
        pts = s.max_price - s.entry
        icon = "~"
    else:
        pts = 0
        icon = "?"

    full_pnl = pts * qty
    # Apply caps
    if full_pnl > 0:
        cap_pnl = min(full_pnl, PROFIT_CAP)
    else:
        cap_pnl = max(full_pnl, -MAX_LOSS_CAP)

    total_full += full_pnl
    total_cap += cap_pnl

    if s.outcome in ("TGT1", "TGT2", "ALL_TGT", "PARTIAL"):
        wins += 1
        daily[s.date]["wins"] += 1
    elif s.outcome == "SL_HIT":
        losses += 1
        daily[s.date]["losses"] += 1
    else:
        unknowns += 1
        daily[s.date]["unknown"] += 1

    daily[s.date]["full_pnl"] += full_pnl
    daily[s.date]["cap_pnl"] += cap_pnl
    daily[s.date]["trades"] += 1

    print(f"  {idx+1:<4} {s.ts:<17} {s.symbol:<20} {s.opt_type:>3} {s.entry:>7.0f} "
          f"{s.targets[0]:>7.0f} {s.sl:>7.0f} [{icon}] {s.outcome:<7} "
          f"₹{full_pnl:>+9,.0f} ₹{cap_pnl:>+9,.0f}")

# --- Summary ---
total = len(signals)
known = wins + losses
print()
print("=" * 115)
print("SUMMARY")
print("=" * 115)
print()
print(f"  Total signals:     {total}")
print(f"  Known outcome:     {known} ({known/total*100:.0f}%)")
print(f"  Unknown/flat:      {unknowns}")
print()
if known > 0:
    wr = wins / known * 100
    print(f"  Winners:           {wins} ({wr:.0f}% of known)")
    print(f"  Losers:            {losses} ({100-wr:.0f}% of known)")
print()
n_days = len(daily)
print(f"  {'Metric':<25} {'Full Ride':>15} {'Capped':>15}")
print(f"  {'─'*25} {'─'*15} {'─'*15}")
print(f"  {'Total P&L':<25} {'₹{:+,}'.format(int(total_full)):>15} {'₹{:+,}'.format(int(total_cap)):>15}")
if n_days > 0:
    print(f"  {'Avg P&L/day':<25} {'₹{:+,}'.format(int(total_full/n_days)):>15} {'₹{:+,}'.format(int(total_cap/n_days)):>15}")
    print(f"  {'Avg P&L/trade':<25} {'₹{:+,}'.format(int(total_full/total)):>15} {'₹{:+,}'.format(int(total_cap/total)):>15}")

print()
print(f"  DAILY BREAKDOWN")
print(f"  {'Date':<12} {'Trades':>7} {'Wins':>6} {'Loss':>6} {'??':>4} {'Full P&L':>12} {'Capped':>12}")
print(f"  {'─'*12} {'─'*7} {'─'*6} {'─'*6} {'─'*4} {'─'*12} {'─'*12}")

green_full = 0
green_cap = 0
for date in sorted(daily.keys()):
    d = daily[date]
    mark = "+" if d["full_pnl"] > 0 else ("-" if d["full_pnl"] < 0 else "~")
    if d["full_pnl"] > 0:
        green_full += 1
    if d["cap_pnl"] > 0:
        green_cap += 1
    print(f"  {date:<12} {d['trades']:>7} {d['wins']:>6} {d['losses']:>6} {d['unknown']:>4} "
          f"{'₹{:+,}'.format(int(d['full_pnl'])):>12} "
          f"{'₹{:+,}'.format(int(d['cap_pnl'])):>12} {mark}")

print()
print(f"  Green days (Full):   {green_full}/{n_days} ({green_full/n_days*100:.0f}%)")
print(f"  Green days (Capped): {green_cap}/{n_days} ({green_cap/n_days*100:.0f}%)")
print()

# Compare with CH1
print(f"  COMPARISON:")
print(f"    CH1 (current):  ~62% WR, ~₹3-5K/day")
if known > 0:
    print(f"    CH2 (this):     {wr:.0f}% WR, ₹{int(total_cap/n_days):+,}/day (capped)")
print()
print("  NOTE: Outcomes based on channel's own follow-up messages (price feed + TGT/SL hit posts).")
print("  This is channel-reported data, not verified against actual candle data.")
print("  Live paper trading (now active as ch2) will give the definitive answer.")
print("=" * 115)

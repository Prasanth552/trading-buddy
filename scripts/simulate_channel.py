"""Simulate P&L from channel signals with ₹2L capital.

Two modes:
  1. FULL — ride to TGT1 (or SL if hit)
  2. CAPPED — exit at ₹2K profit per trade (or SL if hit first)

Usage: python3 scripts/simulate_channel.py /tmp/channel_msgs.txt
"""
import sys, re
from collections import defaultdict

if len(sys.argv) < 2:
    print("Usage: python3 scripts/simulate_channel.py <messages_file>")
    sys.exit(1)

CAPITAL = 200_000
PROFIT_TARGET = 2000
MAX_TRADES_PER_DAY = 5

# NSE lot sizes (approximate, as of 2026)
LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 40,
    "MIDCPNIFTY": 50, "BANK NIFTY": 30,
    # Stocks — common ones
    "RELIANCE": 250, "TCS": 175, "INFY": 600, "HDFCBANK": 550,
    "ICICIBANK": 1400, "SBIN": 1500, "TATAMOTORS": 1400,
    "BAJFINANCE": 250, "LT": 300, "MARUTI": 100, "INDIGO": 300,
    "TRENT": 625, "PAYTM": 1600, "KEI": 200, "BSE": 250,
    "TITAN": 375, "BRITANNIA": 200, "ABB": 250, "HAL": 300,
    "EICHERMOT": 150, "SIEMENS": 275, "MCX": 900, "POLYCAB": 200,
    "LTM": 200, "LTIM": 200, "PERSISTENT": 200, "DIXON": 200,
    "TRENT": 625, "APOLLOHOSP": 250, "BAJAJAUTO": 250,
    "CUMMINSIND": 400, "MFSL": 1600, "PIIND": 300,
    "HERMOTOCO": 300, "CGPOWER": 1800, "DIVISLAB": 150,
    "MOTHERSON": 6700, "COFORGE": 200, "AMBER": 200,
    "PFC": 3200, "REC": 2250, "OIL": 1600, "HINDALCO": 1400,
    "NATIONALUM": 4600, "PNB": 8000, "FORTIS": 1400,
    "BOSCH": 50, "ASHOKLEY": 4500, "ASTRAL": 550,
    "ADANIENT": 500, "BIOCON": 2300, "KALYANJIL": 1600,
    "CROMPTON": 2800, "CHOLAFIN": 500, "POWERGRID": 4500,
    "HCLTECH": 700, "TIINDIA": 375, "SOLARINDS": 100,
    "SHREECEM": 25, "NAUKRI": 525, "BHARTIARTL": 475,
    "PNBHOUSING": 900, "JUBLFOOD": 1250, "NYKAA": 2525,
    "MANAPPURAM": 4000, "OFSS": 100, "HINDZINC": 2900,
    "RADICO": 1200, "SUPRIMEIND": 300, "GVT&D": 400,
    "HYUNDAI": 500, "INDIANB": 1000, "NMDC": 6750,
    # Commodities
    "CRUDEOIL": 100, "GOLD": 100, "SILVER": 30,
    "NATURALGAS": 1250,
}
DEFAULT_LOT = 400

with open(sys.argv[1], "r") as f:
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

SIGNAL_RE = re.compile(
    r"^(?:BANK\s*NIFTY|NIFTY|SENSEX|FINNIFTY|MIDCPNIFTY|"
    r"[A-Z]{2,20})\s+\d+\s+(?:CE|PE)\s*$",
    re.MULTILINE,
)
ENTRY_RE = re.compile(r"(?:ABOVE|NEAR|Above|Near)\s+(\d+[\.\d]*)", re.IGNORECASE)
TGT_RE = re.compile(r"(?:TGT|TARGET|Tgt)\s+(\d+[\s/\d\+\.\-]+)", re.IGNORECASE)
SL_RE = re.compile(r"(?:SL|Sl|sl|STOPLOSS|Stop\s*loss)\s+(?:below\s+)?(\d+[\.\d]*)", re.IGNORECASE)
TGT_HIT_RE = re.compile(r"(?:1st|2nd|3rd|first|second|third)\s+(?:TGT|target|hit|done)", re.IGNORECASE)
ALL_TGT_RE = re.compile(r"(?:all\s+TGT|all\s+target|Jackpot|JACKPOT|boom|BOOM)", re.IGNORECASE)
SL_HIT_RE = re.compile(r"(?:SL\s+hit|sl\s+hit|stop\s*loss\s+hit|loss\s*book)", re.IGNORECASE)


def get_underlying(symbol):
    parts = symbol.strip().split()
    name = parts[0].upper()
    if len(parts) > 1 and parts[0].upper() == "BANK":
        name = "BANK NIFTY"
    return name


def get_lot_size(underlying):
    return LOT_SIZES.get(underlying, DEFAULT_LOT)


signals = []
i = 0
while i < len(messages):
    text = messages[i]["text"]
    clean = re.sub(r"[*\U0001F600-\U0001FAFF☀-➿❤️]+", "", text).strip()

    if SIGNAL_RE.search(clean):
        sig_match = SIGNAL_RE.search(clean)
        symbol = sig_match.group(0).strip()
        ts = messages[i]["ts"]
        date = ts[:10]

        entry = None
        tgts = []
        sl = None

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
            sl_val = float(sm.group(1))
            if entry and sl_val < 30:
                sl = entry - sl_val
            else:
                sl = sl_val

        # Track outcome
        outcome = "UNKNOWN"
        tgt_count = 0
        max_price_seen = entry or 0

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
            if SL_HIT_RE.search(ft) or re.search(r"Hit\s*😡", ft):
                outcome = "SL_HIT"
            if re.search(r"\d+\s+point\s+(?:loss|Loss)", ft):
                outcome = "SL_HIT"

            # Track price updates
            stripped = ft.strip()
            if re.match(r"^\d+[\.\d]*$", stripped):
                p = float(stripped)
                if p > max_price_seen:
                    max_price_seen = p

        if outcome != "SL_HIT":
            if tgt_count >= 3:
                outcome = "ALL_TGT"
            elif tgt_count >= 2:
                outcome = "TGT2"
            elif tgt_count >= 1:
                outcome = "TGT1"
            elif entry and tgts and max_price_seen >= tgts[0]:
                outcome = "TGT1"
            elif entry and sl and max_price_seen <= sl:
                outcome = "SL_HIT"

        if entry and (tgts or sl):
            signals.append({
                "ts": ts, "date": date, "symbol": symbol,
                "entry": entry, "tgts": tgts, "sl": sl,
                "outcome": outcome, "tgt_count": tgt_count,
                "max_price": max_price_seen,
            })
    i += 1

# Deduplicate
seen = set()
unique = []
for s in signals:
    key = f"{s['ts']}_{s['symbol']}"
    if key not in seen:
        seen.add(key)
        unique.append(s)
signals = unique

# Filter: only equity + index options (skip commodities for now since different exchange)
equity_signals = [s for s in signals if not any(
    x in get_underlying(s["symbol"]) for x in ["CRUDEOIL", "CRUDE", "GOLD", "SILVER", "NATURAL"]
)]
commodity_signals = [s for s in signals if s not in equity_signals]

print("=" * 90)
print(f"CHANNEL P&L SIMULATION — Shrivastav G Prime")
print(f"Capital: ₹{CAPITAL:,} | Profit Target: ₹{PROFIT_TARGET:,}/trade")
print(f"Period: {signals[0]['date']} to {signals[-1]['date']}")
print(f"Signals: {len(equity_signals)} equity + {len(commodity_signals)} commodity")
print("=" * 90)
print()
print("NOTE: Taking FIRST signal per instrument per day. Max 1 lot per trade.")
print("      Commodity signals shown separately (MCX, different capital needs).")
print()

def simulate(sigs, label, max_per_day=99):
    daily = defaultdict(lambda: {
        "full_pnl": 0, "capped_pnl": 0, "trades": 0,
        "full_wins": 0, "capped_wins": 0, "details": []
    })

    total_full = 0
    total_capped = 0
    total_trades = 0
    full_wins = 0
    capped_wins = 0
    total_full_win_pnl = 0
    total_full_loss_pnl = 0
    total_capped_win_pnl = 0
    total_capped_loss_pnl = 0

    # Limit: 1 signal per underlying per day
    day_taken = defaultdict(set)

    for s in sigs:
        underlying = get_underlying(s["symbol"])
        date = s["date"]

        if underlying in day_taken[date]:
            continue
        if len(day_taken[date]) >= max_per_day:
            continue

        day_taken[date].add(underlying)

        lot_size = get_lot_size(underlying)
        entry = s["entry"]
        tgts = s["tgts"]
        sl = s["sl"]
        outcome = s["outcome"]

        lots = 3  # 3 lots with ₹2L capital (weekly expiry options)
        qty = lot_size * lots

        # --- FULL mode: exit at TGT1 or SL ---
        if outcome in ("TGT1", "TGT2", "ALL_TGT") and tgts:
            exit_price = tgts[0]
            full_pnl = (exit_price - entry) * qty
        elif outcome == "SL_HIT" and sl:
            exit_price = sl
            full_pnl = (sl - entry) * qty
        else:
            # Unknown — assume small loss (exit at entry, no profit)
            full_pnl = 0
            exit_price = entry

        # --- CAPPED mode: exit at ₹2K profit or SL ---
        if outcome == "SL_HIT" and sl:
            capped_pnl = (sl - entry) * qty
        elif outcome in ("TGT1", "TGT2", "ALL_TGT") and tgts:
            # How many points needed for ₹2K?
            points_for_target = PROFIT_TARGET / qty
            cap_exit = entry + points_for_target
            if cap_exit <= tgts[0]:
                capped_pnl = PROFIT_TARGET
            else:
                capped_pnl = (tgts[0] - entry) * qty
        else:
            capped_pnl = 0

        total_full += full_pnl
        total_capped += capped_pnl
        total_trades += 1

        if full_pnl > 0:
            full_wins += 1
            total_full_win_pnl += full_pnl
        elif full_pnl < 0:
            total_full_loss_pnl += full_pnl

        if capped_pnl > 0:
            capped_wins += 1
            total_capped_win_pnl += capped_pnl
        elif capped_pnl < 0:
            total_capped_loss_pnl += capped_pnl

        d = daily[date]
        d["full_pnl"] += full_pnl
        d["capped_pnl"] += capped_pnl
        d["trades"] += 1
        if full_pnl > 0:
            d["full_wins"] += 1
        if capped_pnl > 0:
            d["capped_wins"] += 1

        icon = "W" if outcome in ("TGT1", "TGT2", "ALL_TGT") else ("L" if outcome == "SL_HIT" else "?")
        d["details"].append({
            "ts": s["ts"], "symbol": s["symbol"], "entry": entry,
            "exit": exit_price, "qty": qty, "lot_size": lot_size,
            "lots": lots, "full_pnl": full_pnl, "capped_pnl": capped_pnl,
            "outcome": outcome, "icon": icon,
        })

    # Print trade details
    print(f"{'─' * 90}")
    print(f"  {label} — TRADE DETAILS")
    print(f"{'─' * 90}")
    print()
    print(f"  {'Time':<17} {'Symbol':<22} {'Lots':>5} {'Qty':>6} {'Entry':>7} {'Exit':>7}"
          f" {'Full P&L':>10} {'₹2K Cap':>10} {'Result'}")
    print(f"  {'─'*17} {'─'*22} {'─'*5} {'─'*6} {'─'*7} {'─'*7} {'─'*10} {'─'*10} {'─'*6}")

    for date in sorted(daily.keys()):
        for t in daily[date]["details"]:
            print(f"  {t['ts']:<17} {t['symbol']:<22} {t['lots']:>5} {t['qty']:>6} "
                  f"{t['entry']:>7.0f} {t['exit']:>7.0f} "
                  f"₹{t['full_pnl']:>+9,.0f} ₹{t['capped_pnl']:>+9,.0f} "
                  f"[{t['icon']}]")
        # Day separator
        d = daily[date]
        print(f"  {'':>17} {'── ' + date + ' ──':<22} {'':>4} {'':>7} {'':>7} "
              f"₹{d['full_pnl']:>+9,.0f} ₹{d['capped_pnl']:>+9,.0f} "
              f"({d['full_wins']}/{d['trades']}W)")
        print()

    # Summary
    print(f"{'=' * 90}")
    print(f"  {label} — SUMMARY")
    print(f"{'=' * 90}")
    print()

    n_days = len(daily)
    full_wr = full_wins / total_trades * 100 if total_trades else 0
    capped_wr = capped_wins / total_trades * 100 if total_trades else 0
    avg_full_win = total_full_win_pnl / full_wins if full_wins else 0
    avg_full_loss = total_full_loss_pnl / (total_trades - full_wins) if (total_trades - full_wins) else 0
    avg_capped_win = total_capped_win_pnl / capped_wins if capped_wins else 0
    avg_capped_loss = total_capped_loss_pnl / (total_trades - capped_wins) if (total_trades - capped_wins) else 0

    print(f"  {'Metric':<35} {'Full Ride':>15} {'₹2K Target':>15}")
    print(f"  {'─'*35} {'─'*15} {'─'*15}")
    print(f"  {'Total Trades':<35} {total_trades:>15}")
    print(f"  {'Win Rate':<35} {full_wr:>14.0f}% {capped_wr:>14.0f}%")
    print(f"  {'Total P&L':<35} {'₹{:+,}'.format(int(total_full)):>15} {'₹{:+,}'.format(int(total_capped)):>15}")
    print(f"  {'Avg P&L/day':<35} {'₹{:+,}'.format(int(total_full/n_days)):>15} {'₹{:+,}'.format(int(total_capped/n_days)):>15}")
    print(f"  {'Avg Win':<35} {'₹{:+,}'.format(int(avg_full_win)):>15} {'₹{:+,}'.format(int(avg_capped_win)):>15}")
    print(f"  {'Avg Loss':<35} {'₹{:+,}'.format(int(avg_full_loss)):>15} {'₹{:+,}'.format(int(avg_capped_loss)):>15}")
    print(f"  {'Loss:Win Ratio':<35} {abs(avg_full_loss/avg_full_win) if avg_full_win else 0:>14.1f}x {abs(avg_capped_loss/avg_capped_win) if avg_capped_win else 0:>14.1f}x")
    print()

    # Daily P&L
    print(f"  {'Date':<12} {'Trades':>7} {'Full P&L':>12} {'₹2K Cap':>12} {'Full':>6} {'Cap':>6}")
    print(f"  {'─'*12} {'─'*7} {'─'*12} {'─'*12} {'─'*6} {'─'*6}")

    full_green = 0
    cap_green = 0
    for date in sorted(daily.keys()):
        d = daily[date]
        f_mark = "+" if d["full_pnl"] > 0 else "-"
        c_mark = "+" if d["capped_pnl"] > 0 else "-"
        if d["full_pnl"] > 0:
            full_green += 1
        if d["capped_pnl"] > 0:
            cap_green += 1
        print(f"  {date:<12} {d['trades']:>7} "
              f"{'₹{:+,}'.format(int(d['full_pnl'])):>12} "
              f"{'₹{:+,}'.format(int(d['capped_pnl'])):>12} "
              f"{f_mark:>6} {c_mark:>6}")

    print()
    print(f"  Green days (Full):  {full_green}/{n_days} ({full_green/n_days*100:.0f}%)")
    print(f"  Green days (₹2K):   {cap_green}/{n_days} ({cap_green/n_days*100:.0f}%)")
    print()

    return total_full, total_capped, total_trades, n_days


print()
print("SECTION 1: EQUITY & INDEX OPTIONS (NSE)")
print()
eq_full, eq_cap, eq_trades, eq_days = simulate(equity_signals, "EQUITY + INDEX")

if commodity_signals:
    print()
    print()
    print("SECTION 2: COMMODITIES (MCX)")
    print()
    com_full, com_cap, com_trades, com_days = simulate(commodity_signals, "COMMODITIES")

print()
print("=" * 90)
print("FINAL VERDICT")
print("=" * 90)
print()
print(f"  With ₹{CAPITAL:,} capital, 1 lot per trade:")
print()
if commodity_signals:
    print(f"  {'':30} {'Full Ride':>15} {'₹2K Target':>15}")
    print(f"  {'─'*30} {'─'*15} {'─'*15}")
    print(f"  {'Equity P&L':<30} {'₹{:+,}'.format(int(eq_full)):>15} {'₹{:+,}'.format(int(eq_cap)):>15}")
    print(f"  {'Commodity P&L':<30} {'₹{:+,}'.format(int(com_full)):>15} {'₹{:+,}'.format(int(com_cap)):>15}")
    print(f"  {'TOTAL':<30} {'₹{:+,}'.format(int(eq_full+com_full)):>15} {'₹{:+,}'.format(int(eq_cap+com_cap)):>15}")
    all_days = max(eq_days, com_days)
    print(f"  {'Avg/day':<30} {'₹{:+,}'.format(int((eq_full+com_full)/all_days)):>15} {'₹{:+,}'.format(int((eq_cap+com_cap)/all_days)):>15}")
else:
    print(f"  {'':30} {'Full Ride':>15} {'₹2K Target':>15}")
    print(f"  {'─'*30} {'─'*15} {'─'*15}")
    print(f"  {'Total P&L':<30} {'₹{:+,}'.format(int(eq_full)):>15} {'₹{:+,}'.format(int(eq_cap)):>15}")
    print(f"  {'Avg/day':<30} {'₹{:+,}'.format(int(eq_full/eq_days)):>15} {'₹{:+,}'.format(int(eq_cap/eq_days)):>15}")

print()
print(f"  Your ₹5K/day target needs: ~2-3 winning trades with ₹2K target each")
print()
print("=" * 90)

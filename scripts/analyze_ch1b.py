#!/usr/bin/env python3
"""Analyze Channel 1B (free signals channel) from a dump file.
Parses BUY signals, backtests against actual candle data with Floor2K strategy."""
import os
import re
import sys
from datetime import datetime
from collections import defaultdict
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from src.broker.upstox_data import UpstoxData
from src.notify.channel_listener import calc_charges
from scripts.analyze_channel2 import resolve_instrument

IST = ZoneInfo("Asia/Kolkata")

# Patterns to skip (not entry signals)
_SKIP_PATTERNS = [
    re.compile(r'PROFIT PER', re.I),
    re.compile(r'TODAY CALL STOCK', re.I),
    re.compile(r'GOOD MORNING', re.I),
    re.compile(r'HAVE A PROFITABLE', re.I),
    re.compile(r'TOTAL\s*=\s*PROFIT', re.I),
    re.compile(r'Payment Link', re.I),
    re.compile(r'ONE CALL IS ENOUGH', re.I),
    re.compile(r'FREE PRACTICE CALL', re.I),
    re.compile(r'Profit Calculated', re.I),
    re.compile(r'Query', re.I),
    re.compile(r'SAME CALL GIVEN', re.I),
    re.compile(r'PREMIUM', re.I),
]

_RE_BUY_SIGNAL = re.compile(
    r'BUY\s+(.+?)\s+(\d+)\s+(CE|PE)\s+ABOVE\s+(\d+(?:\.\d+)?)',
    re.I
)

_RE_BUY_MULTILINE = re.compile(
    r'BUY\s+(\S+)\s*\n\s*(\d+)\s+(CE|PE)\s+ABOVE\s+(\d+(?:\.\d+)?)',
    re.I
)

_RE_SL_TGT = re.compile(
    r'SL\s+(\d+(?:\.\d+)?)\s+TARGET\s+([\d.\s]+)',
    re.I
)


def parse_dump_signals(dump_path: str) -> list[dict]:
    """Parse entry signals from the dump file."""
    with open(dump_path) as f:
        content = f.read()

    blocks = re.split(r'--- MSG \d+ \| ', content)
    signals = []

    for block in blocks:
        if not block.strip():
            continue

        # Extract timestamp
        ts_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2})', block)
        if not ts_match:
            continue
        ts_str = ts_match.group(1)
        ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M').replace(tzinfo=IST)

        # Get text after the timestamp line
        text_start = block.find('---\n')
        if text_start < 0:
            continue
        text = block[text_start + 4:].strip()

        # Skip non-signal messages
        if any(p.search(text) for p in _SKIP_PATTERNS):
            continue

        # Try single-line: BUY SYMBOL STRIKE CE/PE ABOVE price
        m = _RE_BUY_SIGNAL.search(text)
        if not m:
            # Try multi-line: BUY SYMBOL\nSTRIKE CE/PE ABOVE price
            m = _RE_BUY_MULTILINE.search(text)

        if not m:
            continue

        symbol = m.group(1).strip().upper()
        strike = float(m.group(2))
        option_type = m.group(3).upper()
        trigger = float(m.group(4))

        # Check for SL/TGT
        sl = 0.0
        target = 0.0
        sl_match = _RE_SL_TGT.search(text)
        if sl_match:
            sl = float(sl_match.group(1))
            targets = sl_match.group(2).strip().split()
            if targets:
                target = float(targets[-1])

        # Clean symbol
        symbol = re.sub(r'#\w+', '', symbol).strip()
        symbol = symbol.replace('&', '&')

        signals.append({
            'ts': ts,
            'symbol': symbol,
            'strike': strike,
            'option_type': option_type,
            'trigger': trigger,
            'sl': sl,
            'target': target,
            'raw': text[:100],
        })

    return signals


def simulate_floor2k(candles, entry, qty, entry_time, sl=0, target=0):
    """Simulate trade with Floor2K strategy + optional SL/TGT from channel."""
    peak_net = 0
    exit_price = entry
    exit_time = "15:29"
    result = "MKT CLOSE"
    high_seen = entry
    low_seen = entry

    for c in candles:
        time_part = c[0][11:16]
        if time_part < entry_time:
            continue
        o, h, l, cl = c[1], c[2], c[3], c[4]
        high_seen = max(high_seen, h)
        low_seen = min(low_seen, l)

        # Check channel SL
        if sl > 0 and l <= sl:
            exit_price = sl
            exit_time = time_part
            result = "SL HIT"
            break

        # Check channel target
        if target > 0 and h >= target:
            exit_price = target
            exit_time = time_part
            result = "TARGET HIT"
            break

        # Check Floor2K
        gross = (cl - entry) * qty
        ch_est = calc_charges(entry, cl, qty)["total"]
        net = gross - ch_est
        peak_net = max(peak_net, net)
        if peak_net >= 2000 and net <= 2000:
            exit_price = cl
            exit_time = time_part
            result = "FLOOR EXIT"
            break

        if time_part >= "15:25":
            exit_price = cl
            exit_time = time_part
            result = "MKT CLOSE"
            break

    gross = (exit_price - entry) * qty
    charges = calc_charges(entry, exit_price, qty)["total"]
    net = gross - charges

    return {
        "result": result,
        "exit_price": exit_price,
        "exit_time": exit_time,
        "gross": gross,
        "charges": charges,
        "net": net,
        "high": high_seen,
        "low": low_seen,
    }


def main():
    dump_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ch1b_dump.txt"
    print(f"Parsing signals from {dump_path}...")
    signals = parse_dump_signals(dump_path)
    print(f"Found {len(signals)} entry signals\n")

    for i, s in enumerate(signals, 1):
        sl_info = f" SL={s['sl']}" if s['sl'] else ""
        tgt_info = f" TGT={s['target']}" if s['target'] else ""
        print(f"  {i:2d}. {s['ts'].strftime('%Y-%m-%d %H:%M')} | "
              f"{s['symbol']} {int(s['strike'])} {s['option_type']} "
              f"ABOVE {s['trigger']}{sl_info}{tgt_info}")

    print(f"\n{'='*120}")
    print("BACKTESTING with Floor2K strategy (1 lot per trade)...")
    print(f"{'='*120}\n")

    ud = UpstoxData()
    today = datetime.now(IST).date()
    all_trades = []

    for sig in signals:
        trade_date = sig['ts'].date()
        entry_time = sig['ts'].strftime('%H:%M')

        key, lot_size = resolve_instrument(
            ud, sig['symbol'], sig['strike'], sig['option_type'], trade_date
        )
        if not key:
            print(f"  SKIP: Could not resolve {sig['symbol']} {int(sig['strike'])} {sig['option_type']} on {trade_date}")
            continue

        entry = sig['trigger']
        if entry <= 0:
            continue

        qty = lot_size  # 1 lot

        # Fetch candles
        try:
            date_str = trade_date.isoformat()
            if date_str == today.isoformat():
                d = ud._get(f"/v3/historical-candle/intraday/{key}/minutes/1")
            else:
                d = ud._get(f"/v3/historical-candle/{key}/minutes/1/{date_str}/{date_str}")
            candles = d.get("data", {}).get("candles", [])
            candles.sort(key=lambda c: c[0])
        except Exception as e:
            print(f"  SKIP: Candle fetch failed for {sig['symbol']}: {e}")
            continue

        if not candles:
            print(f"  SKIP: No candles for {sig['symbol']} on {date_str}")
            continue

        res = simulate_floor2k(candles, entry, qty, entry_time, sig['sl'], sig['target'])

        trade = {
            'date': trade_date,
            'symbol': f"{sig['symbol']} {int(sig['strike'])} {sig['option_type']}",
            'entry': entry,
            'entry_time': entry_time,
            'qty': qty,
            'lot_size': lot_size,
            'sl': sig['sl'],
            'target': sig['target'],
            **res,
        }
        all_trades.append(trade)

        sign = "+" if res['net'] >= 0 else ""
        print(f"  {trade_date} {entry_time} | {trade['symbol']:30s} | "
              f"entry={entry:>7.1f} | {res['result']:>12s} @ {res['exit_price']:>7.1f} {res['exit_time']} | "
              f"net={sign}{res['net']:,.0f}")

    # Summary
    if not all_trades:
        print("\nNo trades to analyze!")
        return

    total_net = sum(t['net'] for t in all_trades)
    total_charges = sum(t['charges'] for t in all_trades)
    wins = sum(1 for t in all_trades if t['net'] > 0)
    losses = len(all_trades) - wins

    print(f"\n{'='*120}")
    print(f"SUMMARY — {len(all_trades)} trades | W/L: {wins}/{losses} ({wins/len(all_trades)*100:.0f}%) | "
          f"Net: {total_net:+,.0f} | Charges: {total_charges:,.0f}")
    print(f"{'='*120}")

    # Daily breakdown
    daily = defaultdict(lambda: {'net': 0, 'trades': 0, 'wins': 0})
    for t in all_trades:
        d = daily[t['date']]
        d['net'] += t['net']
        d['trades'] += 1
        if t['net'] > 0:
            d['wins'] += 1

    print(f"\nDAILY P&L:")
    cum = 0
    green = red = 0
    for date in sorted(daily):
        d = daily[date]
        cum += d['net']
        sign = "+" if d['net'] >= 0 else ""
        tag = "GREEN" if d['net'] >= 0 else "RED"
        if d['net'] >= 0:
            green += 1
        else:
            red += 1
        print(f"  {date} | {d['trades']} trades ({d['wins']}W) | "
              f"{sign}{d['net']:>8,.0f} | cum: {cum:>+10,.0f} | {tag}")

    print(f"\n  {green} green days, {red} red days")
    print(f"  Avg per trade: {total_net/len(all_trades):+,.0f}")
    print(f"  Avg per day: {total_net/len(daily):+,.0f}")


if __name__ == "__main__":
    main()

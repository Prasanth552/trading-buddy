#!/usr/bin/env python3
"""Analyze Channel 2 signals over last 15 days against actual candle data.
1 lot per trade, ₹1L capital, ₹2K floor strategy."""
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from src.broker.upstox_data import UpstoxData
from src.notify.channel_listener import calc_charges

IST = ZoneInfo("Asia/Kolkata")
CHANNEL_ID = -1001547107686
PROFIT_TARGET = 2000

# --- Step 1: Fetch messages from channel ---
async def fetch_signals():
    from telethon import TelegramClient
    client = TelegramClient(
        'data/telegram_user.session',
        int(os.getenv("TELEGRAM_API_ID", "0")),
        os.getenv("TELEGRAM_API_HASH", ""),
    )
    await client.start(phone=os.getenv("TELEGRAM_PHONE", ""))
    entity = await client.get_entity(CHANNEL_ID)
    msgs = await client.get_messages(entity, limit=500)
    signals = []
    for m in reversed(msgs):
        text = m.text or ""
        parsed = parse_ch2_signal(text)
        if parsed:
            parsed["ts"] = m.date.astimezone(IST)
            signals.append(parsed)
    await client.disconnect()
    return signals


def parse_ch2_signal(text: str) -> dict | None:
    """Parse Channel 2 signal format."""
    text = text.strip()
    # Remove markdown bold markers
    text = text.replace("**", "")
    # Remove emojis and special unicode
    clean = re.sub(r'[^\x00-\x7F]+', ' ', text).strip()
    clean = re.sub(r'\s+', ' ', clean)

    if len(clean) < 10:
        return None

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return None

    upper = clean.upper()

    # Must have BUY/SELL and CE/PE
    if "BUY" not in upper and "SELL" not in upper:
        return None
    if " CE" not in upper and " PE" not in upper:
        return None

    # Try to extract: BUY SYMBOL STRIKE CE/PE
    # Handle formats like:
    #   "BUY NIFTY 24550 CE"
    #   "BUY SENSEX 79500 CE"
    #   "BUY MANKIND PHARMA 2500 CE"
    #   "#NIFTY 24,500 PE"  (with BUY implied)
    #   "NIFTY 24650 PE" (with arrows/emojis)

    # Clean up for parsing
    parse_text = re.sub(r'[^\w\s.&/-]', ' ', clean).strip()
    parse_text = re.sub(r'\s+', ' ', parse_text).upper()

    # Remove leading # or similar
    parse_text = re.sub(r'^[#\s]+', '', parse_text)

    # Extract action
    action = "BUY"
    if "SELL" in parse_text:
        action = "SELL"

    # Find CE/PE and strike
    m_opt = re.search(r'(\d[\d,]*(?:\.\d+)?)\s+(CE|PE)', parse_text)
    if not m_opt:
        return None

    strike = float(m_opt.group(1).replace(",", ""))
    option_type = m_opt.group(2)

    # Extract symbol: everything between BUY/action and strike
    before_strike = parse_text[:m_opt.start()].strip()
    before_strike = re.sub(r'^(BUY|SELL)\s+', '', before_strike).strip()
    symbol = before_strike.strip()
    if not symbol:
        return None

    # Extract ABOVE/entry price
    trigger = 0.0
    for line in lines:
        m_above = re.search(r'ABOVE\s*[:\-]?\s*(\d+(?:\.\d+)?)', line, re.I)
        if m_above:
            trigger = float(m_above.group(1))
            break

    # Extract SL
    sl = 0.0
    for line in lines:
        m_sl = re.search(r'SL\s*[:\-]?\s*(\d+(?:\.\d+)?)', line, re.I)
        if m_sl:
            sl = float(m_sl.group(1))
            break

    # Extract targets
    targets = []
    for line in lines:
        m_tgt = re.search(r'(?:TARGET|TGT)\s*[:\-]?\s*([\d\s,/.+]+)', line, re.I)
        if m_tgt:
            raw = m_tgt.group(1)
            nums = re.findall(r'\d+(?:\.\d+)?', raw)
            targets = [float(n) for n in nums]
            break

    if sl <= 0 or not targets:
        return None

    # Detect swing trade
    is_swing = False
    text_upper = text.upper()
    if "SWING" in text_upper or "HOLD WITH PATIENCE" in text_upper or "HOLDING TRADE" in text_upper:
        is_swing = True

    # Extract expiry if present
    expiry = None
    m_exp = re.search(r'EXPIRY\s+(\d+\s+\w+)', text, re.I)
    if m_exp:
        expiry = m_exp.group(1).strip()

    return {
        "action": action,
        "symbol": symbol,
        "strike": strike,
        "option_type": option_type,
        "trigger": trigger,
        "sl": sl,
        "targets": targets,
        "is_swing": is_swing,
        "expiry": expiry,
        "raw": text[:120],
    }


def resolve_instrument(ud, symbol, strike, option_type, trade_date):
    """Resolve instrument key for NIFTY/SENSEX/stock options."""
    from src.broker.upstox_data import _expiry_to_date
    import gzip, json, requests, config

    instruments = ud._load_master()
    sym = symbol.replace(" ", "").upper()

    # Map common names
    ALIASES = {
        "NIFTY": "NIFTY",
        "BANKNIFTY": "BANKNIFTY",
        "SENSEX": "SENSEX",
        "BANKEX": "BANKEX",
    }

    candidates = []
    for inst in instruments:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO"):
            continue
        asym = (inst.get("asset_symbol") or inst.get("name") or "").upper().replace(" ", "")

        if asym != sym and not (sym in ALIASES and asym == ALIASES[sym]):
            # Try fuzzy
            if not (asym.startswith(sym) or sym.startswith(asym)):
                continue

        if inst.get("instrument_type") != option_type:
            continue
        if abs(float(inst.get("strike_price", -1)) - strike) > 0.01:
            continue

        exp = _expiry_to_date(inst.get("expiry"))
        if exp is None or exp < trade_date:
            continue
        candidates.append((exp, inst))

    if not candidates:
        return None, 1

    candidates.sort(key=lambda x: x[0])
    chosen = candidates[0][1]
    lot_size = int(chosen.get("lot_size", 1)) or 1
    return chosen.get("instrument_key"), lot_size


def simulate_trade(candles, entry, sl, target, qty, entry_time):
    """Simulate with channel target + SL. Also track ₹2K floor."""
    peak_net = 0
    high_seen = entry
    low_seen = entry

    for c in candles:
        time_part = c[0][11:16]
        if time_part < entry_time:
            continue
        o, h, l, cl = c[1], c[2], c[3], c[4]
        high_seen = max(high_seen, h)
        low_seen = min(low_seen, l)

        # Check SL
        if l <= sl:
            gross = (sl - entry) * qty
            ch = calc_charges(entry, sl, qty)["total"]
            return "SL HIT", sl, time_part, gross, ch, gross - ch, high_seen, low_seen

        # Check target
        if h >= target:
            gross = (target - entry) * qty
            ch = calc_charges(entry, target, qty)["total"]
            return "TARGET HIT", target, time_part, gross, ch, gross - ch, high_seen, low_seen

        # Check ₹2K floor
        gross = (cl - entry) * qty
        ch_est = calc_charges(entry, cl, qty)["total"]
        net = gross - ch_est
        peak_net = max(peak_net, net)
        if peak_net >= PROFIT_TARGET and net <= PROFIT_TARGET:
            ch = calc_charges(entry, cl, qty)["total"]
            return "FLOOR EXIT", cl, time_part, gross, ch, gross - ch, high_seen, low_seen

        if time_part >= "15:29":
            gross = (cl - entry) * qty
            ch = calc_charges(entry, cl, qty)["total"]
            return "MKT CLOSE", cl, time_part, gross, ch, gross - ch, high_seen, low_seen

    # If no exit found, use last candle
    if candles:
        last = candles[-1]
        cl = last[4]
        gross = (cl - entry) * qty
        ch = calc_charges(entry, cl, qty)["total"]
        return "LAST CANDLE", cl, last[0][11:16], gross, ch, gross - ch, high_seen, low_seen

    return "NO DATA", entry, "??:??", 0, 0, 0, entry, entry


async def main():
    print("Fetching signals from Channel 2...")
    signals = await fetch_signals()

    intraday = [s for s in signals if not s["is_swing"]]
    swing = [s for s in signals if s["is_swing"]]

    print(f"\nFound {len(signals)} total signals: {len(intraday)} intraday, {len(swing)} swing")

    ud = UpstoxData()

    print("\n" + "=" * 90)
    print("CHANNEL 2 — INTRADAY SIGNALS (1 lot, ₹2K floor, channel SL+target)")
    print("=" * 90)

    total_net = 0
    total_charges = 0
    total_gross = 0
    wins = 0
    losses = 0
    trade_count = 0

    for sig in intraday:
        trade_date = sig["ts"].date()
        entry_time = sig["ts"].strftime("%H:%M")

        # Resolve instrument
        key, lot_size = resolve_instrument(ud, sig["symbol"], sig["strike"], sig["option_type"], trade_date)
        if not key:
            print(f"\n{'─' * 90}")
            print(f"  SKIP: Could not resolve {sig['symbol']} {int(sig['strike'])} {sig['option_type']}")
            continue

        # Use trigger price as entry (or signal price)
        entry = sig["trigger"] if sig["trigger"] > 0 else 0
        if entry <= 0:
            continue

        qty = lot_size  # 1 lot
        sl = sig["sl"]
        target = sig["targets"][0]

        # Fetch candles (historical for past dates, intraday for today)
        try:
            date_str = trade_date.isoformat()
            today_str = datetime.now(IST).date().isoformat()

            if date_str == today_str:
                d = ud._get(f"/v3/historical-candle/intraday/{key}/minutes/1")
            else:
                d = ud._get(f"/v3/historical-candle/{key}/minutes/1/{date_str}/{date_str}")

            candles = d.get("data", {}).get("candles", [])
            candles.sort(key=lambda c: c[0])
        except Exception as e:
            print(f"\n  SKIP: Candle fetch failed for {key}: {e}")
            continue

        if not candles:
            print(f"\n  SKIP: No candles for {sig['symbol']} on {date_str}")
            continue

        result, exit_price, exit_time, gross, charges, net, high, low = simulate_trade(
            candles, entry, sl, target, qty, entry_time
        )

        trade_count += 1
        total_gross += gross
        total_charges += charges
        total_net += net
        if net > 0:
            wins += 1
        else:
            losses += 1

        sign = "+" if net >= 0 else ""
        print(f"\n{'─' * 90}")
        print(f"  {sig['symbol']} {int(sig['strike'])} {sig['option_type']}  |  {trade_date}  |  Qty: {qty}")
        print(f"  Entry: {entry} @ {entry_time} | SL: {sl} | Target: {target}")
        print(f"  Day range: Low {low} → High {high}")
        print(f"  → {result:>14} @ {exit_price} at {exit_time}  |  Gross: {gross:+,.0f}  Charges: {charges:.0f}  Net: {sign}{net:,.0f}")

    print(f"\n{'=' * 90}")
    print(f"  INTRADAY SUMMARY")
    print(f"  Trades: {trade_count} | Wins: {wins} | Losses: {losses} | "
          f"Win rate: {wins/trade_count*100:.0f}%" if trade_count > 0 else "  No trades")
    print(f"  Total gross: {total_gross:+,.0f}")
    print(f"  Total charges: {total_charges:,.0f}")
    print(f"  Total net: {total_net:+,.0f}")
    print(f"{'=' * 90}")

    if swing:
        print(f"\n{'=' * 90}")
        print(f"  SWING TRADES (not simulated — need multi-day tracking)")
        print(f"{'=' * 90}")
        for s in swing:
            print(f"  {s['ts'].strftime('%Y-%m-%d')} | {s['symbol']} {int(s['strike'])} {s['option_type']} "
                  f"| Entry: {s['trigger']} SL: {s['sl']} Targets: {s['targets']}")


if __name__ == "__main__":
    asyncio.run(main())

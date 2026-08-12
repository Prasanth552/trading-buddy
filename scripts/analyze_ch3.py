#!/usr/bin/env python3
"""Analyze Channel 3 signals over last 30 days against actual candle data.
Tests multiple exit strategies since CH3 gives no SL/TGT."""
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from src.broker.upstox_data import UpstoxData
from src.notify.channel_listener import calc_charges
from scripts.analyze_channel2 import resolve_instrument

IST = ZoneInfo("Asia/Kolkata")
CHANNEL_ID = -1002228650108


def parse_ch3_signal(text: str) -> dict | None:
    """Parse Channel 3 signal format: SYMBOL STRIKE CE/PE, BUY ABOVE/NEAR price."""
    text = text.strip()
    clean = text.replace("**", "")
    clean = re.sub(r'[^\x00-\x7F]+', ' ', clean).strip()
    clean = re.sub(r'\s+', ' ', clean)

    if len(clean) < 10:
        return None

    upper = clean.upper()
    if ("CE" not in upper and "PE" not in upper):
        return None
    if "BUY" not in upper:
        return None
    if "ABOVE" not in upper and "NEAR" not in upper:
        return None
    # Skip "TGT paid" check — all CH3 signals have it
    if "PAID" not in upper:
        return None

    # Clean for parsing
    parse_text = re.sub(r'[^\w\s.&/-]', ' ', clean).strip()
    parse_text = re.sub(r'(\d)\s+(\d{3})(?=\s)', r'\1\2', parse_text)
    parse_text = re.sub(r'\s+', ' ', parse_text).upper()
    parse_text = re.sub(r'^[#\s]+', '', parse_text)

    # Find CE/PE and strike
    m_opt = re.search(r'(\d[\d,]*(?:\.\d+)?)\s+(CE|PE)', parse_text)
    if not m_opt:
        return None

    strike = float(m_opt.group(1).replace(",", ""))
    option_type = m_opt.group(2)

    # Extract symbol
    before_strike = parse_text[:m_opt.start()].strip()
    before_strike = re.sub(r'^(BUY|SELL)\s+', '', before_strike).strip()
    symbol = before_strike.strip()
    if not symbol:
        return None

    # Extract entry price (ABOVE/NEAR)
    trigger = 0.0
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines:
        m_entry = re.search(r'(?:ABOVE|NEAR)\s*[:\-]?\s*0?(\d+(?:\.\d+)?)', line, re.I)
        if m_entry:
            trigger = float(m_entry.group(1))
            break

    if trigger <= 0:
        return None

    # Detect CRUDEOIL
    is_commodity = "CRUDEOIL" in symbol.upper() or "CRUDE" in symbol.upper()

    return {
        "symbol": symbol,
        "strike": strike,
        "option_type": option_type,
        "trigger": trigger,
        "is_commodity": is_commodity,
        "raw": text[:120],
    }


def simulate_with_strategies(candles, entry, qty, entry_time):
    """Simulate trade with multiple exit strategies. Returns dict of results."""
    results = {}

    # Strategy configs: (name, sl_pct, target_pct)
    strategies = [
        ("SL20_TGT30", 0.20, 0.30),
        ("SL20_TGT50", 0.20, 0.50),
        ("SL30_TGT50", 0.30, 0.50),
        ("SL30_TGT100", 0.30, 1.00),
        ("FLOOR2K", None, None),  # Only ₹2K floor + 3:25 exit
    ]

    for name, sl_pct, tgt_pct in strategies:
        sl = entry * (1 - sl_pct) if sl_pct else 0
        target = entry * (1 + tgt_pct) if tgt_pct else 0
        peak_net = 0
        high_seen = entry
        low_seen = entry
        result = "MKT CLOSE"
        exit_price = entry
        exit_time = "15:29"

        for c in candles:
            time_part = c[0][11:16]
            if time_part < entry_time:
                continue
            o, h, l, cl = c[1], c[2], c[3], c[4]
            high_seen = max(high_seen, h)
            low_seen = min(low_seen, l)

            # Check SL
            if sl > 0 and l <= sl:
                exit_price = sl
                exit_time = time_part
                result = "SL HIT"
                break

            # Check target
            if target > 0 and h >= target:
                exit_price = target
                exit_time = time_part
                result = "TARGET HIT"
                break

            # Check ₹2K floor
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

        results[name] = {
            "result": result,
            "exit_price": exit_price,
            "exit_time": exit_time,
            "gross": gross,
            "charges": charges,
            "net": net,
            "high": high_seen,
            "low": low_seen,
            "max_profit_pct": (high_seen - entry) / entry * 100 if entry > 0 else 0,
            "max_drawdown_pct": (entry - low_seen) / entry * 100 if entry > 0 else 0,
        }

    return results


async def fetch_signals():
    from telethon import TelegramClient
    client = TelegramClient(
        'data/telegram_user.session',
        int(os.getenv("TELEGRAM_API_ID", "0")),
        os.getenv("TELEGRAM_API_HASH", ""),
    )
    await client.start(phone=os.getenv("TELEGRAM_PHONE", ""))
    cutoff = (datetime.now(IST) - timedelta(days=30)).date()
    msgs = await client.get_messages(CHANNEL_ID, limit=5000)
    signals = []
    for m in reversed(msgs):
        ts = m.date.astimezone(IST)
        if ts.date() < cutoff:
            continue
        text = m.text or ""
        parsed = parse_ch3_signal(text)
        if parsed:
            parsed["ts"] = ts
            signals.append(parsed)
    await client.disconnect()
    return signals


async def main():
    print("Fetching CH3 signals...")
    signals = await fetch_signals()
    # Skip CRUDEOIL for now
    equity_signals = [s for s in signals if not s["is_commodity"]]
    commodity_signals = [s for s in signals if s["is_commodity"]]
    print(f"Found {len(signals)} signals: {len(equity_signals)} equity, {len(commodity_signals)} commodity (skipped)")

    ud = UpstoxData()
    today = datetime.now(IST).date()

    strategy_totals = {}
    all_trades = []

    for sig in equity_signals:
        trade_date = sig["ts"].date()
        entry_time = sig["ts"].strftime("%H:%M")

        key, lot_size = resolve_instrument(ud, sig["symbol"], sig["strike"], sig["option_type"], trade_date)
        if not key:
            print(f"  SKIP: Could not resolve {sig['symbol']} {int(sig['strike'])} {sig['option_type']} on {trade_date}")
            continue

        entry = sig["trigger"]
        if entry <= 0:
            continue

        qty = lot_size  # 1 lot for analysis

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
            print(f"  SKIP: Candle fetch failed: {e}")
            continue

        if not candles:
            print(f"  SKIP: No candles for {sig['symbol']} on {date_str}")
            continue

        results = simulate_with_strategies(candles, entry, qty, entry_time)

        trade_info = {
            "date": trade_date,
            "symbol": f"{sig['symbol']} {int(sig['strike'])} {sig['option_type']}",
            "entry": entry,
            "entry_time": entry_time,
            "qty": qty,
            "lot_size": lot_size,
            "results": results,
        }
        all_trades.append(trade_info)

        for strat_name, res in results.items():
            if strat_name not in strategy_totals:
                strategy_totals[strat_name] = {"net": 0, "wins": 0, "losses": 0, "trades": 0,
                                               "charges": 0, "gross": 0}
            st = strategy_totals[strat_name]
            st["trades"] += 1
            st["net"] += res["net"]
            st["gross"] += res["gross"]
            st["charges"] += res["charges"]
            if res["net"] > 0:
                st["wins"] += 1
            else:
                st["losses"] += 1

    # --- Print detailed trade results for best strategy ---
    print(f"\n{'=' * 120}")
    print(f"CH3 BACKTEST — {len(all_trades)} trades over last 30 days (1 lot each)")
    print(f"{'=' * 120}")

    # Find best strategy
    best_strat = max(strategy_totals, key=lambda k: strategy_totals[k]["net"])

    for trade in all_trades:
        res = trade["results"][best_strat]
        sign = "+" if res["net"] >= 0 else ""
        print(f"  {trade['date']} {trade['entry_time']} | {trade['symbol']:25s} | "
              f"entry={trade['entry']:>7.1f} | {res['result']:>12s} @ {res['exit_price']:>7.1f} {res['exit_time']} | "
              f"high={res['high']:>7.1f} ({res['max_profit_pct']:+.0f}%) low={res['low']:>7.1f} ({res['max_drawdown_pct']:.0f}%↓) | "
              f"net={sign}{res['net']:,.0f}")

    # --- Strategy comparison ---
    print(f"\n{'=' * 120}")
    print(f"STRATEGY COMPARISON (1 lot per trade)")
    print(f"{'=' * 120}")
    print(f"{'Strategy':<20s} | {'Trades':>6s} | {'Wins':>4s} | {'Loss':>4s} | {'Win%':>5s} | "
          f"{'Gross':>10s} | {'Charges':>8s} | {'Net':>10s} | {'Avg/trade':>10s}")
    print(f"{'-' * 120}")

    for name in ["SL20_TGT30", "SL20_TGT50", "SL30_TGT50", "SL30_TGT100", "FLOOR2K"]:
        st = strategy_totals.get(name, {})
        if not st or st["trades"] == 0:
            continue
        wr = st["wins"] / st["trades"] * 100
        avg = st["net"] / st["trades"]
        print(f"{name:<20s} | {st['trades']:>6d} | {st['wins']:>4d} | {st['losses']:>4d} | {wr:>4.0f}% | "
              f"{st['gross']:>+10,.0f} | {st['charges']:>8,.0f} | {st['net']:>+10,.0f} | {avg:>+10,.0f}")

    print(f"\n  BEST STRATEGY: {best_strat} → Net {strategy_totals[best_strat]['net']:+,.0f}")

    # --- Daily breakdown for best strategy ---
    print(f"\n{'=' * 120}")
    print(f"DAILY P&L — {best_strat}")
    print(f"{'=' * 120}")
    from collections import defaultdict
    daily = defaultdict(lambda: {"net": 0, "trades": 0, "wins": 0})
    for trade in all_trades:
        res = trade["results"][best_strat]
        d = daily[trade["date"]]
        d["net"] += res["net"]
        d["trades"] += 1
        if res["net"] > 0:
            d["wins"] += 1

    cum = 0
    green_days = 0
    red_days = 0
    for date in sorted(daily):
        d = daily[date]
        cum += d["net"]
        sign = "+" if d["net"] >= 0 else ""
        color = "GREEN" if d["net"] >= 0 else "RED"
        if d["net"] >= 0:
            green_days += 1
        else:
            red_days += 1
        print(f"  {date} | {d['trades']} trades ({d['wins']}W) | {sign}{d['net']:>8,.0f} | cum: {cum:>+10,.0f} | {color}")

    print(f"\n  {green_days} green days, {red_days} red days")


if __name__ == "__main__":
    asyncio.run(main())

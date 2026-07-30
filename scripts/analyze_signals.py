"""Fetch actual option price data for channel signals and compute P&L.

Usage (on the server):
    cd ~/Trading-Buddy && .venv/bin/python3 scripts/analyze_signals.py
"""
from __future__ import annotations

import json
import sys
import requests
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from src.broker.upstox_client import UpstoxClient

IST = ZoneInfo("Asia/Kolkata")

SIGNALS = [
    {
        "date": "2026-07-29",
        "symbol": "JSWSTEEL",
        "strike": 1240,
        "opt": "CE",
        "trigger": 48,
        "sl": 44,
        "targets": [53, 60],
        "signal_time": "10:13",
        "exit_msg": "50, BOOK SMALL",
        "exit_time": "10:20",
    },
    {
        "date": "2026-07-30",
        "symbol": "WAAREEENER",
        "strike": 2700,
        "opt": "PE",
        "trigger": 180,
        "sl": 162,
        "targets": [200, 240],
        "signal_time": "09:18",
        "exit_msg": "202, BOOK OR TRAIL",
        "exit_time": "09:32",
    },
    {
        "date": "2026-07-30",
        "symbol": "TVSMOTOR",
        "strike": 4100,
        "opt": "CE",
        "trigger": 165,
        "sl": 148,
        "targets": [185, 210],
        "signal_time": "09:34",
        "exit_msg": "CLOSE 166, NEAR COST",
        "exit_time": "10:06",
    },
    {
        "date": "2026-07-30",
        "symbol": "KPITTECH",
        "strike": 610,
        "opt": "PE",
        "trigger": 36,
        "sl": 32.5,
        "targets": [40, 45],
        "signal_time": "10:21",
        "exit_msg": "38.7, BOOK OR TRAIL",
        "exit_time": "11:23",
    },
    {
        "date": "2026-07-30",
        "symbol": "ADANIPORTS",
        "strike": 1700,
        "opt": "PE",
        "trigger": 60,
        "sl": 53,
        "targets": [70, 80],
        "signal_time": "10:24",
        "exit_msg": "65.15, SL COST",
        "exit_time": "11:44",
    },
    {
        "date": "2026-07-30",
        "symbol": "OIL",
        "strike": 465,
        "opt": "PE",
        "trigger": 18.5,
        "sl": 17,
        "targets": [20.75, 22],
        "signal_time": "10:46",
        "exit_msg": "19.45, SL COST",
        "exit_time": "10:51",
    },
]

LOTS = 2


def resolve_instrument(uc: UpstoxClient, symbol: str, strike: float, opt_type: str, trade_date: date):
    """Find the nearest-expiry instrument for the given option."""
    instruments = uc.load_instruments()
    candidates = []
    for inst in instruments:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO"):
            continue
        if inst.get("asset_symbol", "").upper() != symbol.upper():
            continue
        if inst.get("instrument_type") != opt_type:
            continue
        if abs(float(inst.get("strike_price", -1)) - strike) > 0.01:
            continue
        exp_val = inst.get("expiry")
        if exp_val is None:
            continue
        if isinstance(exp_val, (int, float)) and exp_val > 1e10:
            exp = datetime.fromtimestamp(exp_val / 1000, tz=IST).date()
        elif isinstance(exp_val, str):
            exp = datetime.fromisoformat(exp_val).date()
        else:
            continue
        if exp < trade_date:
            continue
        candidates.append((exp, inst))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def fetch_candles(instrument_key: str, trade_date: str, interval: str = "1minute"):
    """Fetch intraday candle data from Upstox historical API."""
    url = f"https://api.upstox.com/v2/historical-candle/{instrument_key}/{interval}/{trade_date}/{trade_date}"
    resp = requests.get(url, timeout=15)
    data = resp.json()
    if data.get("status") != "success":
        return None
    candles = data.get("data", {}).get("candles", [])
    candles.sort(key=lambda c: c[0])
    return candles


def analyze_signal(candles, sig):
    """Analyze candles against signal parameters. Returns dict with results."""
    if not candles:
        return {"status": "NO_DATA"}

    trigger = sig["trigger"]
    sl = sig["sl"]
    targets = sig["targets"]

    triggered = False
    entry_price = None
    entry_time = None
    high_after_entry = 0
    low_after_entry = float("inf")
    sl_hit = False
    sl_time = None
    targets_hit = []
    last_price = None
    last_time = None

    for candle in candles:
        ts_str, o, h, l, c, vol, oi = candle
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(IST)
        time_str = ts.strftime("%H:%M")

        if not triggered:
            if h >= trigger:
                triggered = True
                entry_price = trigger
                entry_time = time_str
                high_after_entry = h
                low_after_entry = l
        else:
            high_after_entry = max(high_after_entry, h)
            low_after_entry = min(low_after_entry, l)

            if l <= sl and not sl_hit:
                sl_hit = True
                sl_time = time_str

            for t in targets:
                if h >= t and t not in targets_hit:
                    targets_hit.append(t)

        last_price = c
        last_time = time_str

    if not triggered:
        return {"status": "NOT_TRIGGERED", "high_of_day": high_after_entry if high_after_entry else None}

    return {
        "status": "TRIGGERED",
        "entry_price": entry_price,
        "entry_time": entry_time,
        "high_after_entry": high_after_entry,
        "low_after_entry": low_after_entry,
        "sl_hit": sl_hit,
        "sl_time": sl_time,
        "targets_hit": sorted(targets_hit),
        "last_price": last_price,
        "last_time": last_time,
    }


def main():
    uc = UpstoxClient()
    print("Loading instruments...")
    uc.load_instruments()

    results = []
    for sig in SIGNALS:
        trade_date = date.fromisoformat(sig["date"])
        print(f"\n{'='*60}")
        print(f"  {sig['symbol']} {sig['strike']} {sig['opt']} — {sig['date']}")
        print(f"  Signal: ABOVE {sig['trigger']} | SL {sig['sl']} | TGT {sig['targets']}")
        print(f"{'='*60}")

        inst = resolve_instrument(uc, sig["symbol"], sig["strike"], sig["opt"], trade_date)
        if not inst:
            print(f"  ❌ Could not resolve instrument")
            results.append({"signal": sig, "result": {"status": "UNRESOLVED"}})
            continue

        ikey = inst["instrument_key"]
        lot_size = int(inst.get("lot_size", 1)) or 1
        exp_val = inst.get("expiry")
        if isinstance(exp_val, (int, float)) and exp_val > 1e10:
            exp_date = datetime.fromtimestamp(exp_val / 1000, tz=IST).date()
        else:
            exp_date = "?"

        print(f"  Instrument: {inst.get('tradingsymbol', ikey)}")
        print(f"  Key: {ikey} | Lot: {lot_size} | Expiry: {exp_date}")

        candles = fetch_candles(ikey, sig["date"])
        if candles is None:
            print(f"  ❌ Could not fetch candle data")
            results.append({"signal": sig, "result": {"status": "NO_CANDLES"}})
            continue

        print(f"  Candles fetched: {len(candles)}")

        analysis = analyze_signal(candles, sig)
        results.append({"signal": sig, "result": analysis, "lot_size": lot_size})

        if analysis["status"] == "NOT_TRIGGERED":
            print(f"  ⚪ Trigger {sig['trigger']} NOT hit")
        elif analysis["status"] == "TRIGGERED":
            print(f"  ✅ Triggered at {analysis['entry_price']} ({analysis['entry_time']})")
            print(f"  📈 High after entry: {analysis['high_after_entry']}")
            print(f"  📉 Low after entry: {analysis['low_after_entry']}")
            if analysis["sl_hit"]:
                print(f"  🔴 SL {sig['sl']} hit at {analysis['sl_time']}")
            if analysis["targets_hit"]:
                print(f"  🎯 Targets hit: {analysis['targets_hit']}")

            # P&L calc based on channel exit message
            exit_price = None
            for word in sig["exit_msg"].replace(",", " ").split():
                try:
                    exit_price = float(word)
                    break
                except ValueError:
                    pass

            if exit_price:
                pnl_per_unit = exit_price - sig["trigger"]
                pnl_per_lot = pnl_per_unit * lot_size
                total_pnl = pnl_per_lot * LOTS
                print(f"  💰 Channel exit: {exit_price} ({sig['exit_msg']})")
                print(f"  💰 P&L per unit: {pnl_per_unit:+.2f}")
                print(f"  💰 P&L per lot ({lot_size} qty): ₹{pnl_per_lot:+,.0f}")
                print(f"  💰 Total P&L ({LOTS} lots): ₹{total_pnl:+,.0f}")

    # Summary
    print(f"\n{'='*60}")
    print(f"  SUMMARY — {LOTS} lots per trade")
    print(f"{'='*60}")
    grand_total = 0
    for r in results:
        sig = r["signal"]
        res = r["result"]
        lot_size = r.get("lot_size", 1)
        label = f"{sig['symbol']} {sig['strike']} {sig['opt']}"

        exit_price = None
        for word in sig["exit_msg"].replace(",", " ").split():
            try:
                exit_price = float(word)
                break
            except ValueError:
                pass

        if res["status"] == "TRIGGERED" and exit_price:
            pnl = (exit_price - sig["trigger"]) * lot_size * LOTS
            grand_total += pnl
            print(f"  {label:30s} → ₹{pnl:+,.0f}")
        else:
            print(f"  {label:30s} → {res['status']}")

    print(f"\n  GRAND TOTAL: ₹{grand_total:+,.0f}")
    print()


if __name__ == "__main__":
    main()

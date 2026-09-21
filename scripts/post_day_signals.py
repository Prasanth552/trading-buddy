"""Post all 3 signal types to Telegram channel for a given date.

1. OEH — Open=High stock PE buys (bearish reversal)
2. Index Straddles — SELL ATM CE+PE on NIFTY/BANKNIFTY/SENSEX
3. Stock Credit Spreads — bull put / bear call on liquid stocks

No strategy names exposed. SL/TGT in ₹ amounts. 1 lot trades.
Posts entry signal → reply with exit result → day summary.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/post_day_signals.py 2026-09-21
    PYTHONPATH=. .venv/bin/python3 scripts/post_day_signals.py  # today
"""
from __future__ import annotations

import os
import re
import sys
import time as _time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

IST = ZoneInfo("Asia/Kolkata")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_SIGNAL_CHANNEL", "-1004385130897")


def _tg_post(data: dict) -> dict:
    for attempt in range(4):
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=data, timeout=10)
        if resp.ok:
            return resp.json().get("result", {})
        if resp.status_code == 429:
            retry_after = resp.json().get("parameters", {}).get("retry_after", 10)
            print(f"  Rate limited, waiting {retry_after}s...")
            _time.sleep(retry_after + 1)
            continue
        print(f"  FAILED: {resp.text}")
        return {}
    return {}


def send(text: str) -> int:
    result = _tg_post({"chat_id": TELEGRAM_CHANNEL, "text": text, "parse_mode": "HTML"})
    msg_id = result.get("message_id", 0)
    if msg_id:
        print(f"  Posted (msg_id={msg_id})")
    return msg_id


def reply(text: str, reply_to: int):
    data = {"chat_id": TELEGRAM_CHANNEL, "text": text, "parse_mode": "HTML"}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    result = _tg_post(data)
    if result.get("message_id"):
        print(f"  Reply (msg_id={result['message_id']})")
    else:
        print(f"  Reply FAILED")


# ── OEH Scanner (Open=High stock PE buys) ────────────────────────

OEH_UNIVERSE = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "BHARTIARTL",
    "SBIN", "ITC", "BAJFINANCE", "LT", "KOTAKBANK", "AXISBANK",
    "TITAN", "MARUTI", "SUNPHARMA", "HCLTECH", "WIPRO", "TATASTEEL",
    "ADANIENT", "CIPLA", "DRREDDY", "M&M", "ASIANPAINT", "HINDUNILVR",
    "NESTLEIND", "ONGC", "ULTRACEMCO", "JSWSTEEL", "TRENT",
    "BAJAJFINSV", "VEDL", "HINDALCO", "BPCL", "HEROMOTOCO", "EICHERMOT",
    "TATAPOWER", "BEL", "NTPC", "POWERGRID", "COALINDIA", "PIDILITIND",
    "SHREECEM", "DABUR", "COLPAL", "AMBUJACEM", "BHEL",
    "DIVISLAB", "BRITANNIA",
]
OEH_BLOCKLIST = {"GODREJCP", "GRASIM"}
OEH_TOLERANCE = 0.05
OEH_MIN_DROP_PCT = 0.3
OEH_SL_PCT = 0.30
PROFIT_FLOOR = 1500
MAX_LOSS_PER_TRADE = 5000

LOT_SIZES = {
    "RELIANCE": 250, "TCS": 175, "HDFCBANK": 550, "INFY": 400,
    "ICICIBANK": 700, "BHARTIARTL": 475, "SBIN": 1500, "ITC": 1600,
    "BAJFINANCE": 125, "LT": 300, "KOTAKBANK": 400, "AXISBANK": 625,
    "TITAN": 375, "MARUTI": 100, "SUNPHARMA": 700, "HCLTECH": 500,
    "WIPRO": 1600, "TATASTEEL": 1100, "ADANIENT": 250, "CIPLA": 650,
    "DRREDDY": 125, "M&M": 350, "ASIANPAINT": 300, "HINDUNILVR": 300,
    "NESTLEIND": 50, "ONGC": 3250, "ULTRACEMCO": 100, "JSWSTEEL": 900,
    "TRENT": 625, "BAJAJFINSV": 500, "VEDL": 1550, "HINDALCO": 1500,
    "BPCL": 1800, "HEROMOTOCO": 150, "EICHERMOT": 150, "TATAPOWER": 1350,
    "BEL": 1600, "NTPC": 2400, "POWERGRID": 2400, "COALINDIA": 2400,
    "PIDILITIND": 250, "SHREECEM": 25, "DABUR": 1250, "COLPAL": 200,
    "AMBUJACEM": 1000, "BHEL": 3300, "DIVISLAB": 100, "BRITANNIA": 200,
    "NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20,
}


def walk_candles_floor(candles, entry, sl, tgt, qty):
    peak_pnl = 0
    floor_armed = False
    for c in candles:
        tgt_hit = tgt and tgt > entry and c["high"] >= tgt
        sl_hit = sl and sl < entry and c["low"] <= sl
        low_pnl = (c["low"] - entry) * qty

        if tgt_hit and sl_hit:
            return tgt, "TGT"
        elif tgt_hit:
            return tgt, "TGT"
        elif sl_hit:
            return sl, "SL"
        elif floor_armed and low_pnl <= PROFIT_FLOOR:
            floor_price = entry + (PROFIT_FLOOR / qty)
            return floor_price, "FLOOR"

        candle_peak = (c["high"] - entry) * qty
        peak_pnl = max(peak_pnl, candle_peak)
        if peak_pnl >= PROFIT_FLOOR:
            floor_armed = True

    return candles[-1]["close"], "EOD"


def run_oeh_scan(ref_date: date):
    """Scan OEH stocks and backtest PE buys. Returns list of trade dicts."""
    from src.broker.upstox_data import UpstoxData, load_cached_token
    from src.broker.upstox_client import _expiry_to_date

    token = load_cached_token()
    if not token:
        print("  OEH: No Upstox token, skipping")
        return []

    ud = UpstoxData(access_token=token)
    master = ud._load_master()

    eq_keys = {}
    for inst in master:
        if inst.get("segment") == "NSE_EQ":
            tsym = (inst.get("trading_symbol") or "").upper()
            if tsym:
                eq_keys[tsym] = inst.get("instrument_key")

    # Build strike steps
    sym_strikes = {}
    for inst in master:
        if inst.get("segment") not in ("NSE_FO", "BSE_FO"):
            continue
        asym = (inst.get("asset_symbol") or "").upper()
        if inst.get("instrument_type") not in ("CE", "PE"):
            continue
        sp = float(inst.get("strike_price", 0))
        if sp > 0:
            sym_strikes.setdefault(asym, set()).add(sp)
    strike_steps = {}
    for sym, strikes in sym_strikes.items():
        ss = sorted(strikes)
        if len(ss) >= 2:
            gaps = [ss[i+1] - ss[i] for i in range(min(10, len(ss)-1))]
            strike_steps[sym] = min(gaps)
        else:
            strike_steps[sym] = 50

    year, month, day = ref_date.year, ref_date.month, ref_date.day
    from_dt = datetime(year, month, day, 9, 15, tzinfo=IST)
    to_dt = datetime(year, month, day, 9, 16, tzinfo=IST)

    # Step 1: Find OEH (Open=High → PE) and OEL (Open=Low → CE) candidates
    print("  OEH/OEL: Scanning stocks...")
    candidates = []
    scanned = 0
    for sym in OEH_UNIVERSE:
        if sym in OEH_BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if not inst_key:
            continue
        try:
            candles = ud.historical_data(inst_key, from_dt, to_dt, "1minute")
            _time.sleep(0.15)
        except Exception:
            _time.sleep(1)
            try:
                candles = ud.historical_data(inst_key, from_dt, to_dt, "1minute")
            except Exception:
                continue
        scanned += 1
        if not candles:
            continue
        open_p = candles[0]["open"]
        if open_p <= 0:
            continue
        high_p = candles[0]["high"]
        low_p = candles[0]["low"]
        close_p = candles[0]["close"]

        # OEH: High ≤ Open (bearish) → buy PE
        if high_p <= open_p + OEH_TOLERANCE:
            drop_pct = (open_p - close_p) / open_p * 100
            if drop_pct >= OEH_MIN_DROP_PCT:
                candidates.append({"symbol": sym, "open": open_p, "close": close_p,
                                   "move_pct": drop_pct, "direction": "bearish", "opt_type": "PE"})

        # OEL: Low ≥ Open (bullish) → buy CE
        if low_p >= open_p - OEH_TOLERANCE:
            rise_pct = (close_p - open_p) / open_p * 100
            if rise_pct >= OEH_MIN_DROP_PCT:
                candidates.append({"symbol": sym, "open": open_p, "close": close_p,
                                   "move_pct": rise_pct, "direction": "bullish", "opt_type": "CE"})

    candidates.sort(key=lambda x: x["move_pct"], reverse=True)
    oeh_count = sum(1 for c in candidates if c["direction"] == "bearish")
    oel_count = sum(1 for c in candidates if c["direction"] == "bullish")
    print(f"  OEH: {oeh_count} bearish + {oel_count} bullish = {len(candidates)} candidates from {scanned} stocks")

    if not candidates:
        return []

    # Step 2: Backtest PE trades
    from_dt_opt = datetime(year, month, day, 9, 15, tzinfo=IST)
    to_dt_opt = datetime(year, month, day, 15, 30, tzinfo=IST)
    trades = []

    for c in candidates:
        sym = c["symbol"]
        opt_type = c["opt_type"]  # PE for OEH, CE for OEL
        step = strike_steps.get(sym, 50)
        atm = round(c["open"] / step) * step

        # Find nearest option of the right type
        all_opts = []
        for inst in master:
            if inst.get("segment") not in ("NSE_FO", "BSE_FO"):
                continue
            asym = (inst.get("asset_symbol") or "").upper()
            if asym != sym or inst.get("instrument_type") != opt_type:
                continue
            sp = float(inst.get("strike_price", -1))
            exp = _expiry_to_date(inst.get("expiry"))
            if exp and exp >= ref_date and sp > 0:
                all_opts.append((sp, exp, inst))
        if not all_opts:
            continue
        all_opts.sort(key=lambda x: (abs(x[0] - atm), x[1]))
        best = all_opts[0]
        strike = best[0]
        expiry = best[1]
        opt_key = best[2].get("instrument_key")
        inst_lot = int(best[2].get("lot_size", 1)) or 1

        try:
            candles = ud.historical_data(opt_key, from_dt_opt, to_dt_opt, "5minute")
            _time.sleep(0.15)
        except Exception:
            continue
        if not candles:
            continue

        filtered = [x for x in candles if x["date"][11:16] >= "09:20"]
        if not filtered:
            filtered = candles

        entry = filtered[0]["open"]
        if entry <= 0:
            continue

        lot_size = LOT_SIZES.get(sym, inst_lot)
        sl = round(entry * (1 - OEH_SL_PCT), 2)
        tgt = round(entry * 2.0, 2)

        sl_per_unit = entry - sl
        if sl_per_unit > 0:
            min_1lot_loss = sl_per_unit * lot_size
            if min_1lot_loss > MAX_LOSS_PER_TRADE:
                continue

        qty = lot_size
        exit_price, exit_reason = walk_candles_floor(filtered, entry, sl, tgt, qty)
        pnl = round((exit_price - entry) * qty, 2)

        direction_label = "BUY PE" if opt_type == "PE" else "BUY CE"
        trades.append({
            "type": "oeh",
            "symbol": sym,
            "strike": strike,
            "opt_type": opt_type,
            "direction_label": direction_label,
            "expiry": expiry.strftime("%d%b"),
            "entry": entry,
            "sl": sl,
            "tgt": tgt,
            "sl_amount": round(sl_per_unit * qty),
            "tgt_amount": round((tgt - entry) * qty),
            "lot": qty,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "pnl": pnl,
            "won": pnl > 0,
            "move_pct": c["move_pct"],
        })

    print(f"  OEH: {len(trades)} trades backtested")
    return trades


def run_index_straddles(ref_date: date):
    """Run index straddle strategies. Returns list of trade dicts."""
    from src.strategy.live_runner import run_day

    print("  Straddles: Running index strategies...")
    idx_res = run_day(ref_date, lots=1, force=True)

    trades = []
    for sname, data in idx_res.items():
        for idx_name, r in data.get("indexes", {}).items():
            if r.get("skipped"):
                continue

            strike = r.get("atm_strike", 0)
            ce_entry = r.get("ce_entry", 0) or 0
            pe_entry = r.get("pe_entry", 0) or 0
            combined = ce_entry + pe_entry
            ce_exit = r.get("ce_exit", 0) or 0
            pe_exit = r.get("pe_exit", 0) or 0
            combined_exit = ce_exit + pe_exit
            net_pnl = r.get("net_pnl", 0) or 0
            entry_time = r.get("entry_time", "")
            exit_time = r.get("exit_time", "")
            exit_reason = r.get("exit_reason", "")
            lot = LOT_SIZES.get(idx_name, 1)
            legs = r.get("legs", 1)

            sl_amount = round(combined * 0.35)
            tgt_amount = round(combined * 0.20)

            trades.append({
                "type": "straddle",
                "symbol": idx_name,
                "strike": strike,
                "ce_entry": ce_entry,
                "pe_entry": pe_entry,
                "combined": combined,
                "combined_exit": combined_exit,
                "sl_amount": sl_amount,
                "tgt_amount": tgt_amount,
                "lot": lot,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "exit_reason": exit_reason,
                "pnl": net_pnl,
                "won": net_pnl > 0,
                "legs": legs,
            })

    print(f"  Straddles: {len(trades)} trades")
    return trades


def run_stock_spreads(ref_date: date):
    """Run stock credit spread strategies. Returns list of trade dicts."""
    from src.strategy.stock_runner import run_day

    print("  Spreads: Running stock strategies...")
    stk_res = run_day(ref_date, lots=1, force=True)

    trades = []
    for sname, data in stk_res.items():
        for stock, sr in data.get("stocks", {}).items():
            if sr.get("skipped"):
                continue
            net_pnl = sr.get("net_pnl", 0) or 0
            sell_strike = sr.get("sell_strike", 0) or 0
            buy_strike = sr.get("buy_strike", 0) or 0
            net_credit = sr.get("net_credit", 0) or 0
            direction = sr.get("direction", "")
            lot = LOT_SIZES.get(stock, 1)
            exit_reason = sr.get("exit_reason", "")
            opt_type = "PE" if direction == "bull" else "CE"

            if sell_strike <= 0:
                continue

            sl_amount = round(net_credit * 2 * lot)
            tgt_amount = round(net_credit * 0.70 * lot)

            trades.append({
                "type": "spread",
                "symbol": stock,
                "direction": direction,
                "sell_strike": sell_strike,
                "buy_strike": buy_strike,
                "opt_type": opt_type,
                "net_credit": net_credit,
                "sl_amount": sl_amount,
                "tgt_amount": tgt_amount,
                "lot": lot,
                "exit_reason": exit_reason,
                "pnl": net_pnl,
                "won": net_pnl > 0,
            })

    print(f"  Spreads: {len(trades)} trades")
    return trades


# ── Post to Telegram ──────────────────────────────────────────────

def post_all(ref_date: date, oeh_trades: list, straddle_trades: list, spread_trades: list):
    all_trades = oeh_trades + straddle_trades + spread_trades
    if not all_trades:
        print("No signals to post.")
        return

    oeh_count = len(oeh_trades)
    str_count = len(straddle_trades)
    spr_count = len(spread_trades)

    print(f"\nPosting {len(all_trades)} signals ({oeh_count} OEH + {str_count} straddle + {spr_count} spread)...\n")

    # Opening
    opening = (
        f"📊 <b>Signals — {ref_date.strftime('%d %b %Y')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
    )
    if oeh_count:
        opening += f"🟢 Stock Picks: {oeh_count}\n"
    if str_count:
        opening += f"🔷 Index Straddles: {str_count}\n"
    if spr_count:
        opening += f"🔶 Stock Spreads: {spr_count}\n"
    opening += f"━━━━━━━━━━━━━━━━━━━━━"
    send(opening)
    _time.sleep(1)

    total_pnl = 0
    wins = 0
    total = 0
    sig_num = 0

    # ── OEH signals ──
    for t in oeh_trades:
        sig_num += 1
        total += 1

        move_emoji = "📉" if t["opt_type"] == "PE" else "📈"
        entry_msg = (
            f"🟢 <b>Signal #{sig_num} — {t['direction_label']}</b>\n\n"
            f"📌 {t['symbol']} {t['strike']:.0f} {t['opt_type']} ({t['expiry']})\n"
            f"💰 Entry: ₹{t['entry']:.1f}\n"
            f"🛑 SL: ₹{t['sl']:.1f} (₹{t['sl_amount']:,})\n"
            f"🎯 TGT: ₹{t['tgt']:.1f} (₹{t['tgt_amount']:,})\n"
            f"📦 Qty: {t['lot']} | ⏰ 09:20\n"
            f"{move_emoji} Move: {t['move_pct']:.1f}%"
        )
        msg_id = send(entry_msg)
        _time.sleep(1.5)

        pnl = t["pnl"]
        total_pnl += pnl
        if t["won"]:
            wins += 1
        emoji = "✅" if t["won"] else "❌"

        exit_msg = (
            f"{emoji} <b>EXIT — {t['symbol']} {t['strike']:.0f} {t['opt_type']}</b>\n\n"
            f"📊 Exit: ₹{t['exit_price']:.1f} ({t['exit_reason']})\n"
            f"💰 <b>P&L: ₹{pnl:+,.0f}</b>"
        )
        reply(exit_msg, msg_id)
        _time.sleep(1.5)

    # ── Straddle signals ──
    for t in straddle_trades:
        sig_num += 1
        total += 1

        entry_msg = (
            f"🔷 <b>Signal #{sig_num} — SELL STRADDLE</b>\n\n"
            f"📌 {t['symbol']} {t['strike']:.0f} CE + PE\n"
            f"💰 SELL CE @ ₹{t['ce_entry']:.1f}\n"
            f"💰 SELL PE @ ₹{t['pe_entry']:.1f}\n"
            f"📊 Combined: ₹{t['combined']:.0f}\n\n"
            f"🛑 SL: ₹{t['sl_amount']:,}\n"
            f"🎯 TGT: ₹{t['tgt_amount']:,} decay\n"
            f"📦 Lot: {t['lot']} | ⏰ {t['entry_time'] or '—'}"
        )
        msg_id = send(entry_msg)
        _time.sleep(1.5)

        pnl = t["pnl"]
        total_pnl += pnl
        if t["won"]:
            wins += 1
        emoji = "✅" if t["won"] else "❌"

        exit_tag = t["exit_reason"].replace("_", " ").upper()
        if "FLOOR" in exit_tag:
            exit_tag = "FLOOR EXIT"

        exit_msg = (
            f"{emoji} <b>EXIT — {t['symbol']} {t['strike']:.0f} Straddle</b>\n\n"
            f"📊 Exit: ₹{t['combined_exit']:.0f}\n"
            f"⏰ {t['exit_time'] or '—'} | {exit_tag}\n"
            f"💰 <b>P&L: ₹{pnl:+,.0f}</b>"
        )
        if t.get("legs", 1) > 1:
            exit_msg += f"\n🔄 ({t['legs']} legs)"
        reply(exit_msg, msg_id)
        _time.sleep(1.5)

    # ── Spread signals ──
    for t in spread_trades:
        sig_num += 1
        total += 1

        dir_label = "BULL PUT" if t["direction"] == "bull" else "BEAR CALL"
        entry_msg = (
            f"🔶 <b>Signal #{sig_num} — {dir_label} SPREAD</b>\n\n"
            f"📌 {t['symbol']}\n"
            f"💰 SELL {t['sell_strike']:.0f} {t['opt_type']} @ ₹{t['net_credit']:.1f}\n"
            f"🛡 BUY {t['buy_strike']:.0f} {t['opt_type']} (hedge)\n\n"
            f"🛑 SL: ₹{t['sl_amount']:,}\n"
            f"🎯 TGT: ₹{t['tgt_amount']:,}\n"
            f"📦 Lot: {t['lot']}"
        )
        msg_id = send(entry_msg)
        _time.sleep(1.5)

        pnl = t["pnl"]
        total_pnl += pnl
        if t["won"]:
            wins += 1
        emoji = "✅" if t["won"] else "❌"
        exit_tag = (t["exit_reason"] or "closed").replace("_", " ").upper()

        exit_msg = (
            f"{emoji} <b>EXIT — {t['symbol']} {dir_label}</b>\n\n"
            f"⏰ {exit_tag}\n"
            f"💰 <b>P&L: ₹{pnl:+,.0f}</b>"
        )
        reply(exit_msg, msg_id)
        _time.sleep(1.5)

    # ── Summary ──
    wr = wins / total * 100 if total > 0 else 0
    emoji = "🟢" if total_pnl > 0 else "🔴"

    summary = (
        f"📋 <b>Day Summary — {ref_date.strftime('%d %b %Y')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    if oeh_count:
        oeh_pnl = sum(t["pnl"] for t in oeh_trades)
        oeh_wins = sum(1 for t in oeh_trades if t["won"])
        summary += f"🟢 OEH: {oeh_wins}/{oeh_count} wins | ₹{oeh_pnl:+,.0f}\n"
    if str_count:
        str_pnl = sum(t["pnl"] for t in straddle_trades)
        str_wins = sum(1 for t in straddle_trades if t["won"])
        summary += f"🔷 Straddles: {str_wins}/{str_count} wins | ₹{str_pnl:+,.0f}\n"
    if spr_count:
        spr_pnl = sum(t["pnl"] for t in spread_trades)
        spr_wins = sum(1 for t in spread_trades if t["won"])
        summary += f"🔶 Spreads: {spr_wins}/{spr_count} wins | ₹{spr_pnl:+,.0f}\n"

    summary += (
        f"\n📊 Total: {wins}/{total} wins ({wr:.0f}%)\n"
        f"{emoji} <b>Net P&L: ₹{total_pnl:+,.0f}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>All trades 1 lot | Real option prices</i>"
    )
    send(summary)

    print(f"\nDone! {total} signals. {wins}/{total} wins ({wr:.0f}%) ₹{total_pnl:+,.0f}")


def main():
    ref_date = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(IST).date()

    print(f"{'='*60}")
    print(f"  Signal Poster — {ref_date}")
    print(f"{'='*60}\n")

    oeh = run_oeh_scan(ref_date)
    straddles = run_index_straddles(ref_date)
    spreads = run_stock_spreads(ref_date)

    post_all(ref_date, oeh, straddles, spreads)


if __name__ == "__main__":
    main()

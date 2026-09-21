"""Post OEH straddle + stock credit spread results as clean signals to Telegram.

No strategy names exposed. SL/TGT in ₹ amounts. 1 lot trades.
Posts entry signal → then reply with exit result.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/post_day_signals.py 2026-09-21
    PYTHONPATH=. .venv/bin/python3 scripts/post_day_signals.py  # today
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_SIGNAL_CHANNEL", "-1004385130897")

LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20,
    "RELIANCE": 250, "HDFCBANK": 550, "ICICIBANK": 700,
    "TCS": 175, "INFY": 400, "SBIN": 750,
    "BAJFINANCE": 125, "LT": 150, "TATASTEEL": 5000,
}


def send(text: str) -> int:
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data={"chat_id": TELEGRAM_CHANNEL, "text": text, "parse_mode": "HTML"},
        timeout=10)
    if resp.ok:
        msg_id = resp.json().get("result", {}).get("message_id", 0)
        print(f"  Posted (msg_id={msg_id})")
        return msg_id
    print(f"  FAILED: {resp.text}")
    return 0


def reply(text: str, reply_to: int):
    data = {"chat_id": TELEGRAM_CHANNEL, "text": text, "parse_mode": "HTML"}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=data, timeout=10)
    if resp.ok:
        msg_id = resp.json().get("result", {}).get("message_id", 0)
        print(f"  Reply posted (msg_id={msg_id})")
    else:
        print(f"  Reply FAILED: {resp.text}")


def main():
    ref_date = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(IST).date()

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from src.strategy.live_runner import run_day as run_idx, INDEXES as IDX_CFG
    from src.strategy.stock_runner import run_day as run_stk

    print(f"Running strategies for {ref_date}...")

    print("\n--- Index Straddles ---")
    idx_res = run_idx(ref_date, lots=1, force=True)

    print("\n--- Stock Credit Spreads ---")
    stk_res = run_stk(ref_date, lots=1, force=True)

    # ── Collect all tradable signals ──────────────────────────────

    signals = []
    signal_num = 0

    # Index straddles — each index per strategy = one signal
    for sname, data in idx_res.items():
        for idx_name, r in data.get("indexes", {}).items():
            if r.get("skipped"):
                continue

            strike = r.get("atm_strike", 0)
            ce_entry = r.get("ce_entry", 0) or 0
            pe_entry = r.get("pe_entry", 0) or 0
            ce_exit = r.get("ce_exit", 0) or 0
            pe_exit = r.get("pe_exit", 0) or 0
            combined_entry = ce_entry + pe_entry
            combined_exit = ce_exit + pe_exit
            net_pnl = r.get("net_pnl", 0) or 0
            entry_time = r.get("entry_time", "")
            exit_time = r.get("exit_time", "")
            exit_reason = r.get("exit_reason", "")
            lot = LOT_SIZES.get(idx_name, 1)
            legs = r.get("legs", 1)

            # SL from strategy params (we don't expose %)
            # Calculate SL as ₹ amount from combined premium
            sl_amount = round(combined_entry * 0.35)  # ~35% SL on combined
            tgt_amount = round(combined_entry * 0.20)  # ~20% target (theta decay)

            signal_num += 1
            signals.append({
                "num": signal_num,
                "type": "straddle",
                "symbol": idx_name,
                "strike": strike,
                "ce_entry": ce_entry,
                "pe_entry": pe_entry,
                "combined_entry": combined_entry,
                "combined_exit": combined_exit,
                "sl_amount": sl_amount,
                "tgt_amount": tgt_amount,
                "lot": lot,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "exit_reason": exit_reason,
                "net_pnl": net_pnl,
                "won": net_pnl > 0,
                "legs": legs,
            })

    # Stock credit spreads
    for sname, data in stk_res.items():
        stocks = data.get("stocks", {})
        for stock, sr in stocks.items():
            if sr.get("skipped"):
                continue
            net_pnl = sr.get("net_pnl", 0) or 0
            sell_strike = sr.get("sell_strike", 0) or 0
            buy_strike = sr.get("buy_strike", 0) or 0
            net_credit = sr.get("net_credit", 0) or 0
            direction = sr.get("direction", "")
            lot = LOT_SIZES.get(stock, 1)
            entry_date = sr.get("entry_date", str(ref_date))
            exit_date = sr.get("exit_date", "")
            exit_reason = sr.get("exit_reason", "")
            opt_type = "PE" if direction == "bull" else "CE"
            exit_spread = sr.get("exit_spread_val", 0) or 0

            # SL = 2x credit, TGT = keep 30% credit
            sl_amount = round(net_credit * 2)
            tgt_amount = round(net_credit * 0.70)

            if sell_strike <= 0:
                continue

            signal_num += 1
            signals.append({
                "num": signal_num,
                "type": "spread",
                "symbol": stock,
                "direction": direction,
                "sell_strike": sell_strike,
                "buy_strike": buy_strike,
                "opt_type": opt_type,
                "net_credit": net_credit,
                "exit_spread": exit_spread,
                "sl_amount": sl_amount,
                "tgt_amount": tgt_amount,
                "lot": lot,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "exit_reason": exit_reason,
                "net_pnl": net_pnl,
                "won": net_pnl > 0,
            })

    if not signals:
        print("No signals to post.")
        return

    print(f"\n{'='*60}")
    print(f"Posting {len(signals)} signals for {ref_date}...")
    print(f"{'='*60}\n")

    # ── Post opening message ──────────────────────────────────────

    straddle_count = sum(1 for s in signals if s["type"] == "straddle")
    spread_count = sum(1 for s in signals if s["type"] == "spread")

    opening = (
        f"📊 <b>Signals — {ref_date.strftime('%d %b %Y')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
    )
    if straddle_count:
        opening += f"🔷 Index Straddles: {straddle_count}\n"
    if spread_count:
        opening += f"🔶 Stock Spreads: {spread_count}\n"
    opening += f"━━━━━━━━━━━━━━━━━━━━━"
    send(opening)
    time.sleep(1)

    # ── Post each signal ──────────────────────────────────────────

    total_pnl = 0
    wins = 0
    total = 0

    for s in signals:
        total += 1

        if s["type"] == "straddle":
            # Entry signal
            entry_msg = (
                f"🔷 <b>Signal #{s['num']} — SELL STRADDLE</b>\n\n"
                f"📌 {s['symbol']} {s['strike']:.0f} CE + PE\n"
                f"💰 SELL CE @ ₹{s['ce_entry']:.1f}\n"
                f"💰 SELL PE @ ₹{s['pe_entry']:.1f}\n"
                f"📊 Combined: ₹{s['combined_entry']:.0f}\n\n"
                f"🛑 SL: ₹{s['sl_amount']}\n"
                f"🎯 TGT: ₹{s['tgt_amount']} decay\n"
                f"📦 Lot: {s['lot']} | ⏰ {s['entry_time'] or '—'}"
            )
            msg_id = send(entry_msg)
            time.sleep(1.5)

            # Exit reply
            pnl = s["net_pnl"]
            total_pnl += pnl
            pnl_rs = pnl
            if pnl > 0:
                wins += 1
            result_emoji = "✅" if pnl > 0 else "❌"

            exit_tag = s["exit_reason"].replace("_", " ").upper()
            if "FLOOR" in exit_tag:
                exit_tag = "RE-ENTRY (floor hit)"

            exit_msg = (
                f"{result_emoji} <b>EXIT — {s['symbol']} {s['strike']:.0f} Straddle</b>\n\n"
                f"📊 Combined Exit: ₹{s['combined_exit']:.0f}\n"
                f"⏰ {s['exit_time'] or '—'} | {exit_tag}\n"
                f"💰 <b>P&L: ₹{pnl_rs:+,.0f}</b>"
            )
            if s["legs"] and s["legs"] > 1:
                exit_msg += f"\n🔄 ({s['legs']} legs with re-entries)"
            reply(exit_msg, msg_id)
            time.sleep(1.5)

        elif s["type"] == "spread":
            dir_label = "BULL PUT" if s["direction"] == "bull" else "BEAR CALL"
            entry_msg = (
                f"🔶 <b>Signal #{s['num']} — {dir_label} SPREAD</b>\n\n"
                f"📌 {s['symbol']}\n"
                f"💰 SELL {s['sell_strike']:.0f} {s['opt_type']} @ ₹{s['net_credit']:.1f} credit\n"
                f"🛡 BUY {s['buy_strike']:.0f} {s['opt_type']} (hedge)\n\n"
                f"🛑 SL: ₹{s['sl_amount']}\n"
                f"🎯 TGT: ₹{s['tgt_amount']} (keep credit)\n"
                f"📦 Lot: {s['lot']}"
            )
            msg_id = send(entry_msg)
            time.sleep(1.5)

            # Exit reply
            pnl = s["net_pnl"]
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            result_emoji = "✅" if pnl > 0 else "❌"

            exit_tag = (s["exit_reason"] or "closed").replace("_", " ").upper()

            exit_msg = (
                f"{result_emoji} <b>EXIT — {s['symbol']} {dir_label}</b>\n\n"
                f"⏰ {s['exit_date'] or '—'} | {exit_tag}\n"
                f"💰 <b>P&L: ₹{pnl:+,.0f}</b>"
            )
            reply(exit_msg, msg_id)
            time.sleep(1.5)

    # ── Summary ───────────────────────────────────────────────────

    wr = wins / total * 100 if total > 0 else 0
    emoji = "🟢" if total_pnl > 0 else "🔴"

    summary = (
        f"📋 <b>Day Summary — {ref_date.strftime('%d %b %Y')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Signals: {total}\n"
        f"✅ Wins: {wins} | ❌ Losses: {total - wins}\n"
        f"📈 Win Rate: {wr:.0f}%\n\n"
        f"{emoji} <b>Net P&L: ₹{total_pnl:+,.0f}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>All trades 1 lot | Real option prices</i>"
    )
    send(summary)

    print(f"\nDone! {total} signals posted. {wins}/{total} wins ({wr:.0f}%) ₹{total_pnl:+,.0f}")


if __name__ == "__main__":
    main()

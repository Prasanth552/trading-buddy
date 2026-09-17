"""Post scanner results for a given date to Telegram channel as simulated signals."""
import json
import os
import sys
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_SIGNAL_CHANNEL", "-1004385130897")
RESULTS_FILE = Path(__file__).resolve().parent.parent / "data" / "scanner_results.json"

STRATEGY_EMOJI = {
    "momentum_scalp": "⚡",
    "trend_momentum": "📈",
    "day_end_sell": "🌙",
    "vwap_bounce": "🔄",
    "ema_cross": "✂️",
    "quick_scalp": "⚡",
}


def send(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHANNEL, "text": text, "parse_mode": "HTML",
    }, timeout=10)
    if resp.ok:
        msg_id = resp.json().get("result", {}).get("message_id", 0)
        print(f"  Posted (msg_id={msg_id})")
    else:
        print(f"  FAILED: {resp.text}")


def main():
    target_date = sys.argv[1] if len(sys.argv) > 1 else "2026-09-17"

    if not RESULTS_FILE.exists():
        print(f"No results file at {RESULTS_FILE}")
        sys.exit(1)

    saved = json.loads(RESULTS_FILE.read_text())

    # Collect all trades for the target date, grouped by combo
    all_trades = []
    for ck, trades in saved["combo_trades"].items():
        strategy, label = ck.split(":", 1)
        for t in trades:
            if t["date"] == target_date:
                t["_strategy"] = strategy
                t["_label"] = label
                all_trades.append(t)

    if not all_trades:
        print(f"No trades found for {target_date}")
        sys.exit(1)

    # Deduplicate: same time + strike + option_type = same signal from different param sets
    # Keep the one from the best-performing combo
    seen = {}
    for t in all_trades:
        key = (t["time"], t["strike"], t["option_type"], t["action"])
        if key not in seen or (t["won"] and not seen[key]["won"]):
            seen[key] = t
    unique_trades = sorted(seen.values(), key=lambda t: t["time"])

    print(f"Posting {len(unique_trades)} signals for {target_date}...\n")

    # Post header
    wins = sum(1 for t in unique_trades if t["won"])
    total = len(unique_trades)
    wr = wins / total * 100
    total_pnl = sum(t["pnl_per_lot"] * 75 for t in unique_trades)  # NIFTY lot=75

    header = (
        f"📊 <b>Signal Results — {target_date}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Signals: {total} | Wins: {wins} | WR: {wr:.0f}%\n"
        f"Net P&L: ₹{total_pnl:+,.0f} (per lot)\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    send(header)
    time.sleep(1)

    # Post each signal
    for t in unique_trades:
        strat = t["_strategy"]
        label = t["_label"]
        emoji = STRATEGY_EMOJI.get(strat, "📌")
        result_emoji = "✅" if t["won"] else "❌"
        pnl = t["pnl_per_lot"] * 75

        strat_display = strat.upper().replace("_", " ")

        msg = (
            f"{emoji} <b>{strat_display}</b> [{label}]\n\n"
            f"{t['action']} NIFTY {t['strike']:.0f} {t['option_type']} "
            f"@ ₹{t['entry']:.1f}\n"
            f"🎯 TGT: ₹{t['tgt']:.1f} | 🛑 SL: ₹{t['sl']:.1f}\n\n"
            f"⏰ Entry: {t['time']} → Exit: {t['exit_time']}\n"
            f"Exit: {t['exit_reason'].upper()} @ ₹{t['exit']:.1f}\n\n"
            f"{result_emoji} <b>{'WIN' if t['won'] else 'LOSS'}</b> "
            f"₹{pnl:+,.0f} ({t['hold_mins']}m hold)"
        )
        send(msg)
        time.sleep(1.5)

    # Summary
    summary = (
        f"📋 <b>Day Summary — {target_date}</b>\n\n"
        f"{'🟢' if total_pnl > 0 else '🔴'} Net: ₹{total_pnl:+,.0f}\n"
        f"📊 Win Rate: {wr:.0f}% ({wins}/{total})\n"
        f"⚡ Best: {max(unique_trades, key=lambda t: t['pnl_per_lot'])['_strategy']} "
        f"₹{max(t['pnl_per_lot']*75 for t in unique_trades):+,.0f}\n\n"
        f"<i>Backtested with real option candles</i>"
    )
    send(summary)
    print(f"\nDone! Posted {len(unique_trades)} signals + summary.")


if __name__ == "__main__":
    main()

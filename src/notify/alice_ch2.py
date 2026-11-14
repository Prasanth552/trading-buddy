#!/usr/bin/env python3
"""Alice Blue CH2 listener — independent service.

Listens to CH2 free channel (@shrivistav), parses signals,
trades via Alice Blue with ₹3K trailing floor + 30% SL.
Completely independent from channel_listener.service.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import config
from src.broker.alice_blue import (
    AliceBlueClient, AB_SIMULATE, get_conn, init_db,
)

# ── logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %Z",
)
log = logging.getLogger("trading_buddy.alice_ch2")

IST = ZoneInfo("Asia/Kolkata")

# ── config ──────────────────────────────────────────────────────────
CHANNEL_ID = 2228650108  # CH2 free channel @shrivistav
TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", 0))
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_FILE = os.path.expanduser("~/Trading-Buddy/data/alice_ch2_session")

# Lot sizes
NIFTY_LOT = 65
NIFTY_LOTS = 3
STOCK_LOTS = 2

# Exit strategy
SL_PCT = 0.30        # 30% SL from entry
FLOOR_TRIGGER = 3000  # activate trailing floor after ₹3K profit
FLOOR_STEP = 1000     # floor ratchets every ₹1K
MAX_LOSS = 10000      # hard loss cap per trade

# Monitor interval
MONITOR_INTERVAL = 3  # seconds

# Daily loss limit
MAX_DAILY_LOSS = 25000

# ── signal parsing ──────────────────────────────────────────────────
@dataclass
class ParsedSignal:
    symbol: str
    strike: int
    option_type: str  # CE or PE
    trigger_price: float
    is_above: bool
    stop_loss: float = 0
    targets: list = None

    def __post_init__(self):
        if self.targets is None:
            self.targets = []


# Pending partial signal (split messages)
_pending: dict | None = None
_pending_ts: float = 0

# NIFTY strike pattern
NIFTY_STRIKES = re.compile(
    r"(?:NIFTY|BANKNIFTY|SENSEX|FINNIFTY|MIDCPNIFTY)\s*(\d{4,6})\s*(CE|PE)",
    re.IGNORECASE,
)
# Stock option pattern
STOCK_STRIKES = re.compile(
    r"([A-Z]{2,20})\s+(\d{2,6})\s*(CE|PE)",
    re.IGNORECASE,
)
ABOVE_RE = re.compile(r"\bABOVE\s+(\d+\.?\d*)", re.IGNORECASE)
NEAR_RE = re.compile(r"\bNEAR\s+(\d+\.?\d*)", re.IGNORECASE)
TGT_RE = re.compile(r"\bTGT\s+(\d+\.?\d*)", re.IGNORECASE)
SL_RE = re.compile(r"\bSL\s+(\d+\.?\d*)", re.IGNORECASE)

NOISE_WORDS = {"VIP", "JOIN", "PREMIUM", "PAYMENT", "FREE", "PROFIT", "LOSS",
               "BOOK", "TRAIL", "BOOKED", "UPDATE", "RUNNING"}


def parse_ch2_signal(text: str) -> ParsedSignal | None:
    global _pending, _pending_ts

    if not text or len(text) < 5:
        return None

    upper = text.upper().strip()

    # Skip noise
    if any(w in upper for w in ("JOIN VIP", "PREMIUM GROUP", "PAYMENT",
                                 "SUBSCRIBE", "WHATSAPP", "TELEGRAM GROUP",
                                 "GOOD MORNING", "DIWALI", "FESTIVAL")):
        return None

    # Try NIFTY/index pattern
    m = NIFTY_STRIKES.search(upper)
    raw_sym = strike = opt_type = None
    is_stock = False

    if m:
        for idx_name in ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"):
            if idx_name in upper:
                raw_sym = idx_name
                break
        strike = int(m.group(1))
        opt_type = m.group(2).upper()
    else:
        m2 = STOCK_STRIKES.search(upper)
        if m2 and m2.group(1) not in NOISE_WORDS:
            raw_sym = m2.group(1)
            strike = int(m2.group(2))
            opt_type = m2.group(3).upper()
            is_stock = True

    # Extract trigger
    above_m = ABOVE_RE.search(upper)
    near_m = NEAR_RE.search(upper)
    trigger = 0.0
    is_above = False
    if above_m:
        trigger = float(above_m.group(1))
        is_above = True
    elif near_m:
        trigger = float(near_m.group(1))

    # Extract TGT/SL
    tgt_m = TGT_RE.search(upper)
    sl_m = SL_RE.search(upper)
    targets = [float(tgt_m.group(1))] if tgt_m else []
    sl = float(sl_m.group(1)) if sl_m else 0

    if raw_sym and strike and opt_type and trigger > 0:
        _pending = None
        if sl <= 0:
            sl = round(trigger * (1 - SL_PCT), 2)
        if not targets:
            targets = [round(trigger * 2.0, 2)]
        return ParsedSignal(
            symbol=raw_sym, strike=strike, option_type=opt_type,
            trigger_price=trigger, is_above=is_above,
            stop_loss=sl, targets=targets,
        )

    # Buffer partial (has symbol but no trigger)
    if raw_sym and strike and opt_type and trigger <= 0:
        _pending = {
            "symbol": raw_sym, "strike": strike, "opt_type": opt_type,
            "is_above": False, "is_stock": is_stock,
        }
        _pending_ts = time.time()
        log.info("[AB-CH2] Buffered partial: %s %s %s", raw_sym, strike, opt_type)
        return None

    # Bare number as trigger for pending partial
    if _pending and time.time() - _pending_ts < 90:
        bare = re.match(r"^\s*(\d+\.?\d*)\s*$", text.strip())
        if bare:
            trigger = float(bare.group(1))
            if 5 < trigger < 5000:
                p = _pending
                _pending = None
                sl = round(trigger * (1 - SL_PCT), 2)
                return ParsedSignal(
                    symbol=p["symbol"], strike=p["strike"],
                    option_type=p["opt_type"],
                    trigger_price=trigger, is_above=p["is_above"],
                    stop_loss=sl, targets=[round(trigger * 2.0, 2)],
                )

    return None


# ── trade management ────────────────────────────────────────────────
def _now_ist() -> datetime:
    return datetime.now(IST)


def _is_market_hours() -> bool:
    now = _now_ist()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return t >= datetime.strptime("09:15", "%H:%M").time() and \
           t <= datetime.strptime("15:30", "%H:%M").time()


def _today_pnl() -> float:
    today = _now_ist().strftime("%Y-%m-%d")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM ab_trades WHERE ts LIKE ? AND pnl IS NOT NULL",
            (f"{today}%",),
        ).fetchone()
        return float(row["total"]) if row else 0


def _open_trades() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ab_trades WHERE status = 'OPEN' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def _get_lot_size(symbol: str) -> int:
    sym = symbol.upper()
    if sym == "NIFTY":
        return NIFTY_LOT
    if sym == "BANKNIFTY":
        return 30
    if sym == "SENSEX":
        return 20
    if sym == "FINNIFTY":
        return 25
    if sym == "MIDCPNIFTY":
        return 50
    # Stock options — approximate, will refine
    return 275  # common lot size


def _get_qty(symbol: str) -> int:
    lot = _get_lot_size(symbol)
    lots = NIFTY_LOTS if symbol.upper() in ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY") else STOCK_LOTS
    return lot * lots


def _find_expiry(symbol: str) -> str:
    """Find nearest weekly/monthly expiry."""
    now = _now_ist()
    # Weekly expiry is typically Thursday for NIFTY
    # For simplicity, find next Thursday
    days_ahead = 3 - now.weekday()  # Thursday = 3
    if days_ahead < 0 or (days_ahead == 0 and now.hour >= 15):
        days_ahead += 7
    expiry_date = now.date() + timedelta(days=days_ahead)
    return expiry_date.strftime("%Y-%m-%d")


# ── Telegram bot message handler ───────────────────────────────────
_alice_client: AliceBlueClient | None = None
_telegram: TelegramClient | None = None
_notify_chat_id: int | None = None


def _notify(msg: str):
    """Send notification via Telegram (reuse existing bot if available)."""
    log.info("[NOTIFY] %s", msg)
    try:
        import requests
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID", "")
        if bot_token and chat_id:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": f"[AB] {msg}", "parse_mode": "Markdown"},
                timeout=10,
            )
    except Exception:
        pass


async def _execute_signal(sig: ParsedSignal):
    """Execute a parsed signal via Alice Blue."""
    global _alice_client

    if not _is_market_hours():
        log.info("[AB-CH2] Outside market hours, skipping: %s %s %s",
                 sig.symbol, sig.strike, sig.option_type)
        return

    # Daily loss check
    daily_pnl = _today_pnl()
    if daily_pnl <= -MAX_DAILY_LOSS:
        log.warning("[AB-CH2] Daily loss limit hit: ₹%.0f", daily_pnl)
        _notify(f"⚠️ Daily loss limit hit: ₹{daily_pnl:,.0f}")
        return

    qty = _get_qty(sig.symbol)
    entry_price = sig.trigger_price  # will be replaced by LTP if available

    # Try to get LTP
    if _alice_client:
        expiry = _find_expiry(sig.symbol)
        instrument = _alice_client.get_instrument(
            sig.symbol, expiry, sig.strike,
            is_ce=(sig.option_type == "CE"),
        )
        if instrument:
            ltp = _alice_client.get_ltp(instrument)
            if ltp and ltp > 0:
                entry_price = ltp
                log.info("[AB-CH2] LTP for %s %s %s: %.2f (trigger: %.2f)",
                         sig.symbol, sig.strike, sig.option_type, ltp, sig.trigger_price)

    # Recalculate SL from entry price (not trigger)
    sl = round(entry_price * (1 - SL_PCT), 2)
    tgt = sig.targets[0] if sig.targets else round(sig.trigger_price * 2.0, 2)

    # Sanity checks
    if entry_price <= sl:
        log.warning("[AB-CH2] Entry %.2f <= SL %.2f, skipping", entry_price, sl)
        return

    if sig.trigger_price > 0:
        ratio = entry_price / sig.trigger_price
        if ratio > 3.0 or ratio < 0.25:
            log.warning("[AB-CH2] LTP %.2f too far from trigger %.2f (%.1fx), skipping",
                        entry_price, sig.trigger_price, ratio)
            return

    # Place order
    order_result = {"status": "simulated", "order_id": f"SIM_{int(time.time())}"}
    if _alice_client and not AB_SIMULATE:
        expiry = _find_expiry(sig.symbol)
        instrument = _alice_client.get_instrument(
            sig.symbol, expiry, sig.strike,
            is_ce=(sig.option_type == "CE"),
        )
        if instrument:
            order_result = _alice_client.place_order(instrument, qty)

    # Record trade
    symbol_str = f"{sig.symbol} {sig.strike} {sig.option_type}"
    ts = _now_ist().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ab_trades (ts, symbol, side, qty, price, order_id, status, "
            "stop_price, target_price, peak_pnl, channel, broker_key) "
            "VALUES (?, ?, 'BUY', ?, ?, ?, 'OPEN', ?, ?, 0, 'ch2', ?)",
            (ts, symbol_str, qty, entry_price, order_result.get("order_id", ""),
             sl, tgt, ""),
        )

    mode = "SIMULATE" if AB_SIMULATE else "LIVE"
    log.info("[AB-CH2] %s ORDER: %s x%d @ %.2f SL=%.2f TGT=%.2f",
             mode, symbol_str, qty, entry_price, sl, tgt)
    _notify(
        f"📊 *{mode} BUY* — {symbol_str}\n"
        f"Entry: {entry_price:.2f} | SL: {sl:.2f} | TGT: {tgt:.2f}\n"
        f"Qty: {qty}"
    )


# ── monitor loop ───────────────────────────────────────────────────
async def _monitor_trades():
    """Monitor open trades for SL/TGT/floor exits."""
    while True:
        try:
            trades = _open_trades()
            if not trades:
                await asyncio.sleep(MONITOR_INTERVAL)
                continue

            for trade in trades:
                tid = trade["id"]
                entry = trade["price"]
                qty = trade["qty"]
                sl = trade["stop_price"]
                tgt = trade["target_price"]
                peak_pnl = trade["peak_pnl"] or 0

                # Get current LTP
                ltp = None
                if _alice_client:
                    # Parse symbol to get instrument
                    parts = trade["symbol"].split()
                    if len(parts) >= 3:
                        sym, strike_s, ot = parts[0], parts[1], parts[2]
                        expiry = _find_expiry(sym)
                        instrument = _alice_client.get_instrument(
                            sym, expiry, int(strike_s),
                            is_ce=(ot == "CE"),
                        )
                        ltp = _alice_client.get_ltp(instrument)

                if ltp is None:
                    continue

                pnl = (ltp - entry) * qty
                peak_pnl = max(peak_pnl, pnl)

                # Update peak in DB
                with get_conn() as conn:
                    conn.execute("UPDATE ab_trades SET peak_pnl = ? WHERE id = ?",
                                 (peak_pnl, tid))

                # 1. Max loss cap
                if pnl <= -MAX_LOSS:
                    _close_trade(tid, ltp, pnl, "max_loss_cap")
                    _notify(f"🛑 *MAX LOSS* — {trade['symbol']}\nLoss: ₹{pnl:,.0f}")
                    continue

                # 2. SL hit
                if sl and ltp <= sl:
                    _close_trade(tid, ltp, pnl, "sl_hit")
                    _notify(f"🔴 *SL HIT* — {trade['symbol']}\nP&L: ₹{pnl:,.0f}")
                    continue

                # 3. TGT hit
                if tgt and ltp >= tgt:
                    _close_trade(tid, ltp, pnl, "target_hit")
                    _notify(f"🎯 *TGT HIT* — {trade['symbol']}\nP&L: ₹{pnl:,.0f}")
                    continue

                # 4. Trailing floor (₹3K trigger, ₹1K steps)
                if peak_pnl >= FLOOR_TRIGGER:
                    floor_level = (int(peak_pnl / FLOOR_STEP)) * FLOOR_STEP
                    if pnl <= floor_level:
                        _close_trade(tid, ltp, pnl, "profit_floor")
                        _notify(
                            f"🔒 *FLOOR EXIT* — {trade['symbol']}\n"
                            f"Peak: ₹{peak_pnl:+,.0f} | Floor: ₹{floor_level:,} | Exit: ₹{pnl:+,.0f}"
                        )
                        continue

        except Exception as exc:
            log.error("Monitor error: %s", exc, exc_info=True)

        await asyncio.sleep(MONITOR_INTERVAL)


def _close_trade(tid: int, exit_price: float, pnl: float, reason: str):
    """Close a trade in the DB."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE ab_trades SET status='CLOSED', exit_price=?, pnl=?, exit_reason=? WHERE id=?",
            (exit_price, round(pnl), reason, tid),
        )
    log.info("[AB-CH2] CLOSED trade %d: exit=%.2f pnl=₹%.0f reason=%s", tid, exit_price, pnl, reason)

    # Place exit order if live
    if _alice_client and not AB_SIMULATE:
        trade = None
        with get_conn() as conn:
            trade = dict(conn.execute("SELECT * FROM ab_trades WHERE id=?", (tid,)).fetchone())
        if trade:
            parts = trade["symbol"].split()
            if len(parts) >= 3:
                sym, strike_s, ot = parts[0], parts[1], parts[2]
                expiry = _find_expiry(sym)
                instrument = _alice_client.get_instrument(
                    sym, expiry, int(strike_s), is_ce=(ot == "CE"),
                )
                if instrument:
                    _alice_client.exit_order(instrument, trade["qty"])


# ── main ────────────────────────────────────────────────────────────
async def main():
    global _alice_client, _telegram

    log.info("=" * 60)
    log.info("Alice Blue CH2 Listener starting...")
    log.info("  Channel: %d", CHANNEL_ID)
    log.info("  Simulate: %s", AB_SIMULATE)
    log.info("  SL: %.0f%% | Floor: ₹%d trigger, ₹%d step",
             SL_PCT * 100, FLOOR_TRIGGER, FLOOR_STEP)
    log.info("=" * 60)

    # Init DB
    init_db()

    # Login to Alice Blue
    _alice_client = AliceBlueClient()
    if AB_SIMULATE:
        log.info("Running in SIMULATE mode — no real orders")
    else:
        ok = _alice_client.login()
        if not ok:
            log.error("Alice Blue login failed! Running in simulate mode.")

    # Connect Telegram
    _telegram = TelegramClient(SESSION_FILE, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await _telegram.connect()
    if not await _telegram.is_user_authorized():
        log.error("Telegram not authorized! Run interactively first to login.")
        return

    log.info("Telegram connected")

    @_telegram.on(events.NewMessage(chats=CHANNEL_ID))
    async def on_ch2_message(event):
        text = event.raw_text
        if not text:
            return
        log.info("[AB-CH2] MSG: %s", text[:100])
        sig = parse_ch2_signal(text)
        if sig:
            log.info("[AB-CH2] SIGNAL: %s %s %s %s %.0f",
                     sig.symbol, sig.strike, sig.option_type,
                     "ABOVE" if sig.is_above else "NEAR", sig.trigger_price)
            await _execute_signal(sig)

    # Start monitor loop
    monitor_task = asyncio.create_task(_monitor_trades())

    log.info("Listening for CH2 signals on Alice Blue...")
    print(f"Alice Blue CH2 listener running (simulate={AB_SIMULATE})")

    # Run until stopped
    try:
        await _telegram.run_until_disconnected()
    finally:
        monitor_task.cancel()
        await _telegram.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

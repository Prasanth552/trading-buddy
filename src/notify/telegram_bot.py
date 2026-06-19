"""Telegram alerts + commands for Trading Buddy.

Two surfaces:
  - TelegramNotifier: synchronous, requests-based push (alerts, EOD summary).
    Safe to call from anywhere (the trading loop, CLI). No event loop needed.
  - run_bot(): long-running command bot (python-telegram-bot) handling
    /status /pause /resume /watchlist /positions /pnl.

Message formatting helpers (format_signal_alert, chunk_message) are pure and
unit-tested offline.
"""
from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv

import config
from src.notify import state
from src.utils import market_calendar as mc
from src.utils.logging import get_logger

load_dotenv()
log = get_logger("telegram")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MSG_LEN = 4096


class TelegramError(RuntimeError):
    """Configuration / send problems in the Telegram layer."""


# --------------------------------------------------------------------------
# Pure formatting helpers (offline-testable)
# --------------------------------------------------------------------------
def chunk_message(text: str, limit: int = MAX_MSG_LEN) -> list[str]:
    """Split ``text`` into Telegram-sized chunks, preferring line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current)
            # A single line longer than the limit: hard-split it.
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


def format_signal_alert(signal: dict[str, Any]) -> str:
    """Format a signal dict as a Telegram alert (language-aware)."""
    from src.notify import messages
    return messages.signal_alert(signal)


# --------------------------------------------------------------------------
# Synchronous push notifier
# --------------------------------------------------------------------------
class TelegramNotifier:
    """Minimal synchronous Telegram sender (no event loop)."""

    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN missing — set it in .env")
        if not self.chat_id:
            raise TelegramError("TELEGRAM_CHAT_ID missing — set it in .env")

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = TELEGRAM_API.format(token=self.token, method=method)
        resp = requests.post(url, json=payload, timeout=15)
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(f"Telegram API error: {data}")
        return data

    def send_message(self, text: str, markdown: bool = True) -> None:
        """Send text to the configured chat, chunking if needed."""
        for chunk in chunk_message(text):
            payload: dict[str, Any] = {"chat_id": self.chat_id, "text": chunk,
                                       "disable_web_page_preview": True}
            if markdown:
                payload["parse_mode"] = "Markdown"
            self._post("sendMessage", payload)
        log.info("Sent Telegram message (%d chars).", len(text))

    def send_signal_alert(self, signal: dict[str, Any]) -> None:
        self.send_message(format_signal_alert(signal))


def resolve_chat_id(token: str | None = None) -> str | None:
    """Fetch the chat ID from recent updates (user must have messaged the bot)."""
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN missing — set it in .env")
    url = TELEGRAM_API.format(token=token, method="getUpdates")
    data = requests.get(url, timeout=15).json()
    for update in reversed(data.get("result", [])):
        msg = update.get("message") or update.get("channel_post")
        if msg and msg.get("chat", {}).get("id"):
            return str(msg["chat"]["id"])
    return None


# --------------------------------------------------------------------------
# Command bot (long-running; python-telegram-bot)
# --------------------------------------------------------------------------
def _today_state_text() -> str:
    from src.storage import db
    row = db.get_or_create_daily_state(mc.now_ist().date().isoformat())
    paused = state.is_paused()
    return (
        f"*Status*\n"
        f"Mode: `{config.MODE}`\n"
        f"Market: {mc.market_status()}\n"
        f"Paused: {'yes ⏸' if paused else 'no ▶'}\n"
        f"Trades today: {row['trades_count']}/{config.MAX_TRADES_PER_DAY}\n"
        f"Realised P&L: ₹{row['realised_pnl']:.2f}\n"
        f"Kill switch: {'TRIPPED' if row['kill_switch_tripped'] else 'armed'}"
    )


def run_bot() -> None:
    """Start the long-running command bot (blocks). Ctrl-C to stop."""
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN missing — set it in .env")

    async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_markdown(_today_state_text())

    async def pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        state.set_paused(True)
        await update.message.reply_text("⏸ Trading paused. New signals will not be acted on.")

    async def resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        state.set_paused(False)
        await update.message.reply_text("▶ Trading resumed.")

    async def watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        txt = ("*Watchlist*\n" + "\n".join(f"• {s}" for s in config.WATCHLIST) +
               "\n*Watch-only*\n" + "\n".join(f"• {s}" for s in config.WATCH_ONLY))
        await update.message.reply_markdown(txt)

    async def positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        from src.storage import db
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT symbol, side, qty, price, status FROM trades "
                "WHERE status NOT IN ('closed','rejected') ORDER BY ts DESC LIMIT 20"
            ).fetchall()
        if not rows:
            await update.message.reply_text("No open positions.")
            return
        txt = "*Open positions*\n" + "\n".join(
            f"• {r['symbol']} {r['side']} x{r['qty']} @ {r['price']} ({r['status']})"
            for r in rows)
        await update.message.reply_markdown(txt)

    async def pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        from src.storage import db
        row = db.get_or_create_daily_state(mc.now_ist().date().isoformat())
        await update.message.reply_markdown(
            f"*P&L today*: ₹{row['realised_pnl']:.2f}\n"
            f"Trades: {row['trades_count']}/{config.MAX_TRADES_PER_DAY}")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("pause", pause))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("watchlist", watchlist))
    app.add_handler(CommandHandler("positions", positions))
    app.add_handler(CommandHandler("pnl", pnl))
    log.info("Telegram command bot starting (polling). Ctrl-C to stop.")
    app.run_polling()

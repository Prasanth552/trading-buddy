"""Telegram channel signal listener — auto-executes option trades from signals.

Listens to a private Telegram channel using Telethon (user account API).
Supports the admin's full workflow:
  1. BUY signal posted → queued as PENDING
  2. "WAIT TO ACTIVATE" → stays pending
  3. "CAN ENTER" → executes the oldest pending signal
  4. "NOT ACTIVATED IGNORE" → discards the oldest pending signal
  5. "BOOK / TRAIL / CLOSE NEAR COST" → closes the most recent open trade
  6. "SL" (alone) → marks most recent trade as stopped out

Run:  python -m src.notify.channel_listener
Env:  TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, SIGNAL_CHANNEL_ID
"""
from __future__ import annotations

import asyncio
import os
import re
import time as _time
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import config
from src.utils.logging import get_logger

log = get_logger("channel_listener")

PENDING_EXPIRY_SEC = 300  # discard pending signals older than 5 minutes


@dataclass
class ParsedSignal:
    action: str          # BUY / SELL
    symbol: str          # e.g. "JSW ENERGY"
    strike: float        # e.g. 550
    option_type: str     # CE / PE
    trigger_price: float # premium must cross this to enter
    stop_loss: float
    targets: list[float]
    queued_at: float = field(default_factory=_time.time)


# ---------------------------------------------------------------------------
# Signal flow state (module-level, lives for the process lifetime)
# ---------------------------------------------------------------------------
_pending: list[ParsedSignal] = []
_executed: list[dict[str, Any]] = []

# ---------------------------------------------------------------------------
# Follow-up message classification
# ---------------------------------------------------------------------------
_RE_WAIT = re.compile(r'WAIT\s+TO\s+ACTIVATE', re.I)
_RE_ACTIVATE = re.compile(r'CAN\s+ENTER', re.I)
_RE_IGNORE = re.compile(r'NOT\s+ACTIVATED|(?:^|\s)IGNORE(?:\s|$)', re.I)
_RE_SL_HIT = re.compile(r'^\s*SL\s*$', re.I)
_RE_BOOK = re.compile(r'BOOK|TRAIL|CLOSE\s+NEAR\s+COST', re.I)
_RE_PRICE_ACTION = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*,\s*(.+)', re.I)


def _classify_followup(text: str) -> tuple[str, float | None]:
    """Classify a non-signal message.

    Returns (action, exit_price | None).
    Actions: "wait", "activate", "ignore", "sl_hit", "book", "unknown"
    """
    text = text.strip()

    price_m = _RE_PRICE_ACTION.match(text)
    if price_m:
        price = float(price_m.group(1))
        rest = price_m.group(2)
        if _RE_BOOK.search(rest):
            return "book", price
        return "book", price

    if _RE_WAIT.search(text):
        return "wait", None
    if _RE_IGNORE.search(text):
        return "ignore", None
    if _RE_ACTIVATE.search(text):
        return "activate", None
    if _RE_SL_HIT.match(text):
        return "sl_hit", None
    if _RE_BOOK.search(text):
        return "book", None

    return "unknown", None


def _expire_stale_pending() -> None:
    """Remove pending signals older than PENDING_EXPIRY_SEC."""
    now = _time.time()
    expired = [s for s in _pending if now - s.queued_at > PENDING_EXPIRY_SEC]
    for s in expired:
        _pending.remove(s)
        log.info("Expired stale pending signal: %s %s %s", s.symbol, s.strike, s.option_type)


# ---------------------------------------------------------------------------
# LLM + regex signal parsing (unchanged)
# ---------------------------------------------------------------------------
_PARSE_SYSTEM = """You extract trading signals from Telegram messages.
Return a JSON object with these fields (or null if the message is NOT a trading signal):
{
  "action": "BUY" or "SELL",
  "symbol": stock name (e.g. "COFORGE", "JSW ENERGY", "NIFTY", "BANKNIFTY"),
  "strike": strike price as number,
  "option_type": "CE" or "PE",
  "trigger_price": entry price / premium to enter above/below,
  "stop_loss": stop loss price,
  "targets": [target1, target2, ...]
}
If the message is not a trading signal (e.g. greetings, updates, market commentary,
exit calls, "book profit" messages), return null.
Only extract if you can identify action, symbol, strike, option_type, and at least
a stop_loss. Trigger price defaults to 0 if not specified (market order)."""

_PARSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "signal": {
            "anyOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "action": {"type": "string", "enum": ["BUY", "SELL"]},
                        "symbol": {"type": "string"},
                        "strike": {"type": "number"},
                        "option_type": {"type": "string", "enum": ["CE", "PE"]},
                        "trigger_price": {"type": "number"},
                        "stop_loss": {"type": "number"},
                        "targets": {"type": "array", "items": {"type": "number"}},
                    },
                    "required": ["action", "symbol", "strike", "option_type", "stop_loss", "targets"],
                },
                {"type": "null"},
            ]
        }
    },
    "required": ["signal"],
}


def parse_signal(text: str) -> ParsedSignal | None:
    """Parse a channel signal message using LLM to handle any format."""
    text = text.strip()
    if len(text) < 5:
        return None

    try:
        from src.llm.client import LLMClient
        llm = LLMClient()
        result = llm.complete_json(
            system=_PARSE_SYSTEM,
            user=text,
            schema=_PARSE_SCHEMA,
            model=config.LLM_FAST_MODEL,
            max_tokens=300,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("LLM parse failed: %s", exc)
        return _parse_signal_regex(text)

    sig = result.get("signal")
    if sig is None:
        return None

    targets = sig.get("targets") or []
    stop_loss = sig.get("stop_loss", 0)
    if not targets or stop_loss <= 0:
        return None

    return ParsedSignal(
        action=sig["action"],
        symbol=sig["symbol"],
        strike=float(sig["strike"]),
        option_type=sig["option_type"],
        trigger_price=float(sig.get("trigger_price", 0)),
        stop_loss=float(stop_loss),
        targets=[float(t) for t in targets],
    )


def _parse_signal_regex(text: str) -> ParsedSignal | None:
    """Fallback regex parser if LLM is unavailable."""
    text = text.strip().upper()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return None

    joined = " ".join(lines)

    m = re.match(
        r'(BUY|SELL)\s+(.+?)\s+(\d+(?:\.\d+)?)\s+(CE|PE)\s+(?:ABOVE|BELOW|@|AT)\s+(\d+(?:\.\d+)?)',
        joined,
    )
    if not m:
        return None

    rest = joined[m.end():]
    sl_m = re.search(r'SL\s+(\d+(?:\.\d+)?)', rest)
    stop_loss = float(sl_m.group(1)) if sl_m else 0.0

    targets: list[float] = []
    tgt_m = re.search(r'(?:TARGET|TGT|T)\s+([\d.\s]+)', rest)
    if tgt_m:
        targets = [float(x) for x in tgt_m.group(1).split() if re.match(r'\d+\.?\d*$', x)]

    if not targets or stop_loss <= 0:
        return None

    return ParsedSignal(
        action=m.group(1),
        symbol=m.group(2).strip(),
        strike=float(m.group(3)),
        option_type=m.group(4),
        trigger_price=float(m.group(5)),
        stop_loss=stop_loss,
        targets=targets,
    )


def _notify(msg: str) -> None:
    """Push a message to the user's Telegram bot (best-effort)."""
    try:
        from src.notify.telegram_bot import TelegramNotifier
        TelegramNotifier().send_message(msg)
    except Exception:  # noqa: BLE001
        log.warning("Could not send Telegram notification: %s", msg)


# ---------------------------------------------------------------------------
# Instrument resolution (searches Upstox master for ANY F&O stock)
# ---------------------------------------------------------------------------
def _resolve_channel_option(
    uc: Any, symbol: str, strike: float, option_type: str,
) -> tuple[dict[str, Any] | None, int]:
    """Search the Upstox instrument master for a specific option contract.

    Returns (instrument_dict, lot_size).  Searches NSE_FO and BSE_FO segments,
    picks the nearest monthly expiry >= today.
    """
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    from src.broker.upstox_client import _expiry_to_date

    IST = ZoneInfo(config.TIMEZONE)
    today = datetime.now(IST).date()

    name = symbol.replace(" ", "").upper()
    instruments = uc.load_instruments()

    candidates: list[tuple[date, dict[str, Any]]] = []
    for inst in instruments:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO"):
            continue
        if inst.get("name", "").upper() != name:
            continue
        if inst.get("instrument_type") != option_type:
            continue
        if abs(float(inst.get("strike_price", -1)) - strike) > 0.01:
            continue
        exp = _expiry_to_date(inst.get("expiry"))
        if exp is None or exp < today:
            continue
        candidates.append((exp, inst))

    if not candidates:
        return None, 1

    candidates.sort(key=lambda x: x[0])
    chosen = candidates[0][1]
    lot_size = int(chosen.get("lot_size", 1)) or 1
    return chosen, lot_size


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------
def execute_signal(sig: ParsedSignal) -> dict[str, Any]:
    """Place an option order through the Upstox broker (paper/sandbox)."""
    from src.broker.upstox_client import UpstoxClient
    from src.storage import db

    db.init_db()

    risk_per_unit = sig.trigger_price - sig.stop_loss
    if risk_per_unit <= 0:
        return {"placed": False, "reason": "SL >= trigger price"}

    try:
        uc = UpstoxClient()
        opt, master_lot_size = _resolve_channel_option(
            uc, sig.symbol, sig.strike, sig.option_type,
        )
    except Exception as exc:  # noqa: BLE001
        return {"placed": False, "reason": f"Option resolution failed: {exc}"}

    if opt is None:
        return {"placed": False, "reason": f"Could not resolve {sig.symbol} {sig.strike} {sig.option_type}"}

    lot_key = sig.symbol.replace(" ", "").upper()
    lot_size = config.LOT_SIZES.get(lot_key, master_lot_size)

    risk_per_lot = risk_per_unit * lot_size
    lots = max(1, int(config.MAX_RISK_PER_TRADE / risk_per_lot))
    max_lots = getattr(config, "MAX_LOTS_PER_TRADE", 0)
    if max_lots > 0:
        lots = min(lots, max_lots)
    qty = lots * lot_size

    instrument_token = opt.get("instrument_key") or opt.get("instrument_token", "")

    trade_row = {
        "ts": __import__("src.utils.market_calendar", fromlist=["now_ist"]).now_ist().isoformat(timespec="seconds"),
        "symbol": f"{sig.symbol} {int(sig.strike)} {sig.option_type}",
        "side": "BUY",
        "qty": qty,
        "price": sig.trigger_price,
        "stop_price": sig.stop_loss,
        "target_price": sig.targets[0],
        "mode": config.MODE,
        "status": "OPEN",
        "is_hedge": False,
        "index_entry": sig.strike,
        "pnl": None,
        "exit_price": None,
    }

    try:
        if getattr(config, "UPSTOX_SIMULATE_ORDERS", False):
            order_id = f"SIM-CHANNEL-{int(_time.time()*1000)}"
            log.info("CHANNEL SIGNAL simulated order id=%s", order_id)
        else:
            result = uc.place_order(
                instrument_token=instrument_token,
                quantity=qty,
                transaction_type="BUY" if sig.action == "BUY" else "SELL",
                price=0,
                trigger_price=sig.trigger_price,
                order_type="SL",
            )
            order_id = (result.get("order_ids") or ["?"])[0]
    except Exception as exc:  # noqa: BLE001
        return {"placed": False, "reason": f"Order failed: {exc}"}

    db.insert_trade(trade_row)

    return {
        "placed": True,
        "order_id": order_id,
        "symbol": trade_row["symbol"],
        "qty": qty,
        "entry": sig.trigger_price,
        "sl": sig.stop_loss,
        "target": sig.targets[0],
    }


# ---------------------------------------------------------------------------
# Trade exit (close an open trade in the DB)
# ---------------------------------------------------------------------------
def _close_trade(symbol: str, exit_price: float | None, reason: str) -> None:
    """Close the most recent OPEN trade matching symbol prefix."""
    try:
        from src.storage import db
        db.init_db()
        with db.get_conn() as conn:
            if symbol:
                row = conn.execute(
                    "SELECT rowid, price, qty FROM trades "
                    "WHERE symbol LIKE ? AND status = 'OPEN' ORDER BY ts DESC LIMIT 1",
                    (f"{symbol}%",)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT rowid, price, qty FROM trades "
                    "WHERE status = 'OPEN' ORDER BY ts DESC LIMIT 1",
                ).fetchone()

            if not row:
                log.warning("No open trade found to close for %s", symbol or "any")
                return

            entry_price = row["price"]
            ep = exit_price if exit_price else entry_price
            pnl = (ep - entry_price) * row["qty"]
            status = "CLOSED" if reason != "sl_hit" else "CLOSED_SL"

            conn.execute(
                "UPDATE trades SET status = ?, exit_price = ?, pnl = ? WHERE rowid = ?",
                (status, ep, pnl, row["rowid"]),
            )
            log.info("Closed trade (rowid=%d): entry=%.2f exit=%.2f pnl=%.2f reason=%s",
                     row["rowid"], entry_price, ep, pnl, reason)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to close trade: %s", exc)


# ---------------------------------------------------------------------------
# Telegram listener
# ---------------------------------------------------------------------------
async def start_listener() -> None:
    """Connect to Telegram as a user and listen for signals in the configured channel."""
    from telethon import TelegramClient, events

    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    phone = os.getenv("TELEGRAM_PHONE", "")
    channel_id = os.getenv("SIGNAL_CHANNEL_ID", "")

    if not all([api_id, api_hash, phone, channel_id]):
        log.error("Missing env vars: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, SIGNAL_CHANNEL_ID")
        return

    try:
        raw_id = int(channel_id)
        if raw_id > 0:
            channel_id_int = int(f"-100{raw_id}")
        elif not str(raw_id).startswith("-100"):
            channel_id_int = int(f"-100{abs(raw_id)}")
        else:
            channel_id_int = raw_id
    except ValueError:
        channel_id_int = None

    session_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "telegram_user.session")
    client = TelegramClient(session_path, api_id, api_hash)

    await client.start(phone=phone)
    me = await client.get_me()
    log.info("Logged in as %s (id=%s)", me.first_name, me.id)
    _notify(f"Channel listener started as {me.first_name}")

    @client.on(events.NewMessage(chats=channel_id_int or channel_id))
    async def on_signal(event):
        text = event.message.text or ""
        if not text.strip():
            return

        log.info("Channel message: %s", text[:120])
        _expire_stale_pending()

        # --- Try parsing as a new entry signal ---
        sig = parse_signal(text)

        if sig is not None:
            _pending.append(sig)
            log.info("Signal QUEUED (pending): %s %s %s %s trigger=%.2f SL=%.2f targets=%s",
                     sig.action, sig.symbol, sig.strike, sig.option_type,
                     sig.trigger_price, sig.stop_loss, sig.targets)
            _notify(
                f"*Signal received (pending)*\n"
                f"{sig.action} {sig.symbol} {int(sig.strike)} {sig.option_type}\n"
                f"Entry ABOVE {sig.trigger_price} | SL: {sig.stop_loss} | "
                f"Targets: {', '.join(str(t) for t in sig.targets)}\n"
                f"Waiting for CAN ENTER..."
            )
            return

        # --- Not a signal → check if it's a follow-up ---
        action, exit_price = _classify_followup(text)

        if action == "wait":
            n = len(_pending)
            log.info("WAIT TO ACTIVATE — %d signal(s) pending", n)
            return

        if action == "activate":
            if not _pending:
                log.warning("CAN ENTER but no pending signals")
                _notify("CAN ENTER received but no pending signals")
                return

            sig = _pending.pop(0)
            log.info("ACTIVATING: %s %s %s %s @ %.2f",
                     sig.action, sig.symbol, sig.strike, sig.option_type, sig.trigger_price)
            _notify(f"*Activating*: {sig.action} {sig.symbol} {int(sig.strike)} {sig.option_type} ABOVE {sig.trigger_price}")

            result = execute_signal(sig)
            if result["placed"]:
                _executed.append(result)
                _notify(
                    f"*Order placed*\n"
                    f"{result['symbol']} x{result['qty']}\n"
                    f"Entry: {result['entry']} | SL: {result['sl']} | Target: {result['target']}"
                )
                log.info("Order placed: %s", result)
            else:
                _notify(f"Signal not executed: {result['reason']}")
                log.warning("Signal not executed: %s", result["reason"])
            return

        if action == "ignore":
            if _pending:
                removed = _pending.pop(0)
                log.info("Signal IGNORED: %s %s %s", removed.symbol, removed.strike, removed.option_type)
                _notify(f"Signal cancelled: {removed.symbol} {int(removed.strike)} {removed.option_type}")
            else:
                log.info("IGNORE message but no pending signals")
            return

        if action in ("book", "sl_hit"):
            price_str = f" @ {exit_price}" if exit_price else ""
            action_label = "BOOK/TRAIL" if action == "book" else "SL HIT"

            if _executed:
                last = _executed[-1]
                symbol = last.get("symbol", "")
                entry = last.get("entry", 0)

                if exit_price and entry:
                    pnl_unit = exit_price - entry
                    qty = last.get("qty", 0)
                    pnl_total = pnl_unit * qty
                    _notify(
                        f"*{action_label}*{price_str}\n"
                        f"{symbol}\n"
                        f"Entry: {entry} | Exit: {exit_price}\n"
                        f"P&L: {pnl_unit:+.2f}/unit | Total: {pnl_total:+.0f}"
                    )
                else:
                    _notify(f"*{action_label}*{price_str}\nTrade: {symbol}")

                sym_prefix = symbol.split()[0] if symbol else ""
                _close_trade(sym_prefix, exit_price, action)
                _executed.pop()
            else:
                _notify(f"*{action_label}*{price_str} (no tracked trade to match)")
                log.info("Exit signal but no executed trades to match")
            return

        log.info("Unrecognized message, skipping.")

    log.info("Listening for signals in channel %s ...", channel_id)
    print(f"Listening for signals in channel {channel_id} ... (Ctrl-C to stop)")
    await client.run_until_disconnected()


def main() -> int:
    asyncio.run(start_listener())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

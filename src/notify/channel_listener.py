"""Telegram channel signal listener — auto-executes option trades from signals.

Listens to a private Telegram channel using Telethon (user account API).
Flow:
  1. BUY signal posted → execute immediately (trigger price in SL order)
  2. "BOOK / TRAIL / CLOSE NEAR COST" → closes the most recent open trade
  3. "SL" (alone) → marks most recent trade as stopped out
  4. Other messages (WAIT, greetings, etc.) → ignored

Run:  python -m src.notify.channel_listener
Env:  TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, SIGNAL_CHANNEL_ID
"""
from __future__ import annotations

import asyncio
import os
import re
import time as _time
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import config
from src.utils.logging import get_logger

log = get_logger("channel_listener")


# ---------------------------------------------------------------------------
# Brokerage & charges calculator (Upstox options, per round-trip)
# ---------------------------------------------------------------------------
def calc_charges(entry_price: float, exit_price: float, qty: int) -> dict[str, float]:
    """Calculate all trading charges for an options round-trip on Upstox.

    Returns a dict with individual components and the total.
    Rates as of 2025 for equity F&O options.
    """
    buy_turnover = entry_price * qty
    sell_turnover = exit_price * qty
    total_turnover = buy_turnover + sell_turnover

    brokerage_per_leg = 20.0
    brokerage = brokerage_per_leg * 2  # buy + sell

    stt = sell_turnover * 0.001  # 0.1% on sell side (options)
    exchange_txn = total_turnover * 0.000495  # NSE ~0.0495%
    sebi = total_turnover * 0.000001  # ₹10 per crore
    stamp_duty = buy_turnover * 0.00003  # 0.003% on buy side

    gst = (brokerage + exchange_txn) * 0.18  # 18% GST

    total = brokerage + stt + exchange_txn + sebi + stamp_duty + gst

    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange_txn": round(exchange_txn, 2),
        "gst": round(gst, 2),
        "sebi": round(sebi, 2),
        "stamp_duty": round(stamp_duty, 2),
        "total": round(total, 2),
    }

@dataclass
class ParsedSignal:
    action: str          # BUY / SELL
    symbol: str          # e.g. "JSW ENERGY"
    strike: float        # e.g. 550
    option_type: str     # CE / PE
    trigger_price: float # premium must cross this to enter
    stop_loss: float
    targets: list[float]


# ---------------------------------------------------------------------------
# Config: profit target per trade
# ---------------------------------------------------------------------------
PROFIT_TARGET = 2000  # ₹2,000 net profit per trade → auto-close

# ---------------------------------------------------------------------------
# Follow-up / exit message classification
# ---------------------------------------------------------------------------
_RE_SL_HIT = re.compile(r'^\s*SL\s*$', re.I)
_RE_BOOK = re.compile(r'BOOK|TRAIL|CLOSE\s+NEAR\s+COST', re.I)
_RE_PRICE_ACTION = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*,\s*(.+)', re.I)
_RE_CLOSE_PRICE = re.compile(r'^\s*CLOSE\s+(\d+(?:\.\d+)?)\s*$', re.I)


def _classify_followup(text: str) -> tuple[str, float | None]:
    """Classify exit/follow-up messages.

    Returns (action, exit_price | None).
    Actions: "sl_hit", "book", "unknown"
    """
    text = text.strip()

    price_m = _RE_PRICE_ACTION.match(text)
    if price_m:
        price = float(price_m.group(1))
        return "book", price

    close_m = _RE_CLOSE_PRICE.match(text)
    if close_m:
        return "book", float(close_m.group(1))

    if _RE_SL_HIT.match(text):
        return "sl_hit", None
    if _RE_BOOK.search(text):
        return "book", None

    return "unknown", None


# ---------------------------------------------------------------------------
# LLM + regex signal parsing (unchanged)
# ---------------------------------------------------------------------------
_PARSE_SYSTEM = """You extract trading signals from Telegram messages.
Return a JSON object with these fields (or null if the message is NOT a trading signal):
{
  "action": "BUY" or "SELL",
  "symbol": stock ticker EXACTLY as written in the message (e.g. "COFORGE", "JSW ENERGY", "GODFRYPHLP") — do NOT correct spelling or substitute similar names,
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

    sym = symbol.replace(" ", "").upper()
    instruments = uc.load_instruments()

    # Known channel→Upstox symbol aliases
    _SYMBOL_ALIASES: dict[str, str] = {
        "KALYANJIL": "KALYANKJIL",
        "LIC": "LICI",
    }
    sym = _SYMBOL_ALIASES.get(sym, sym)

    candidates: list[tuple[date, dict[str, Any]]] = []
    for inst in instruments:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO"):
            continue
        if inst.get("asset_symbol", "").upper() != sym:
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
        # Fuzzy fallback: find asset_symbols that start with or contain sym
        fuzzy: list[tuple[date, dict[str, Any]]] = []
        for inst in instruments:
            seg = inst.get("segment", "")
            if seg not in ("NSE_FO", "BSE_FO"):
                continue
            asym = inst.get("asset_symbol", "").upper()
            if not (asym.startswith(sym) or sym.startswith(asym)):
                continue
            if inst.get("instrument_type") != option_type:
                continue
            if abs(float(inst.get("strike_price", -1)) - strike) > 0.01:
                continue
            exp = _expiry_to_date(inst.get("expiry"))
            if exp is None or exp < today:
                continue
            fuzzy.append((exp, inst))
        if fuzzy:
            fuzzy.sort(key=lambda x: x[0])
            chosen = fuzzy[0][1]
            actual_sym = chosen.get("asset_symbol", "")
            log.info("Fuzzy match: %s → %s (asset_symbol=%s)", symbol, actual_sym, actual_sym)
            lot_size = int(chosen.get("lot_size", 1)) or 1
            return chosen, lot_size
        return None, 1

    candidates.sort(key=lambda x: x[0])
    chosen = candidates[0][1]
    lot_size = int(chosen.get("lot_size", 1)) or 1
    return chosen, lot_size


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------
def execute_signal(sig: ParsedSignal, *, channel: str = "ch1") -> dict[str, Any]:
    """Place an option order through the Upstox broker (paper/sandbox)."""
    from src.broker.upstox_client import UpstoxClient
    from src.storage import db

    db.init_db()

    # Dedup: skip if an OPEN trade with the same symbol already exists
    trade_symbol = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"
    with db.get_conn() as conn:
        dup = conn.execute(
            "SELECT id FROM trades WHERE symbol = ? AND status = 'OPEN' LIMIT 1",
            (trade_symbol,),
        ).fetchone()
    if dup:
        log.info("Duplicate signal skipped — %s already OPEN (id=%d)", trade_symbol, dup["id"])
        return {"placed": False, "reason": f"Duplicate: {trade_symbol} already open"}

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

    lots = 3 if channel == "ch2" else 1
    qty = lots * lot_size

    instrument_token = opt.get("instrument_key") or opt.get("instrument_token", "")

    # Fetch live LTP so entry reflects actual market price, not signal price
    entry_price = sig.trigger_price
    try:
        from src.broker.upstox_data import UpstoxData
        ud = UpstoxData()
        ltp_data = ud._get("/v2/market-quote/ltp",
                           params={"instrument_key": instrument_token}).get("data", {})
        for item in ltp_data.values():
            lp = item.get("last_price")
            if lp and lp > 0:
                entry_price = float(lp)
                log.info("Live LTP for %s: %.2f (signal price: %.2f)",
                         instrument_token, entry_price, sig.trigger_price)
                break
    except Exception as exc:  # noqa: BLE001
        log.warning("LTP fetch failed, using signal price: %s", exc)

    trade_row = {
        "ts": __import__("src.utils.market_calendar", fromlist=["now_ist"]).now_ist().isoformat(timespec="seconds"),
        "symbol": f"{sig.symbol} {int(sig.strike)} {sig.option_type}",
        "side": "BUY",
        "qty": qty,
        "price": entry_price,
        "stop_price": sig.stop_loss,
        "target_price": sig.targets[0],
        "broker_key": instrument_token,
        "mode": config.MODE,
        "status": "OPEN",
        "is_hedge": False,
        "index_entry": sig.strike,
        "pnl": None,
        "exit_price": None,
        "channel": channel,
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
        "entry": entry_price,
        "sl": sig.stop_loss,
        "target": sig.targets[0],
        "broker_key": instrument_token,
    }


# ---------------------------------------------------------------------------
# Trade exit (close an open trade in the DB)
# ---------------------------------------------------------------------------
def _close_trade_by_id(trade_id: int, exit_price: float, reason: str) -> None:
    """Close a specific trade by its DB id."""
    try:
        from src.storage import db
        db.init_db()
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT id, price, qty, symbol FROM trades WHERE id = ? AND status = 'OPEN'",
                (trade_id,),
            ).fetchone()
            if not row:
                return

            entry_price = row["price"]
            gross_pnl = (exit_price - entry_price) * row["qty"]
            charges = calc_charges(entry_price, exit_price, row["qty"])
            net_pnl = gross_pnl - charges["total"]
            status = "CLOSED" if reason != "sl_hit" else "CLOSED_SL"

            conn.execute(
                "UPDATE trades SET status = ?, exit_price = ?, pnl = ?, charges = ? WHERE id = ?",
                (status, exit_price, net_pnl, charges["total"], row["id"]),
            )
            log.info(
                "AUTO-CLOSE (id=%d) %s: entry=%.2f exit=%.2f gross=%.2f "
                "charges=%.2f net=%.2f reason=%s",
                row["id"], row["symbol"], entry_price, exit_price, gross_pnl,
                charges["total"], net_pnl, reason,
            )
            _notify(
                f"*Auto-close: {reason}*\n"
                f"{row['symbol']}\n"
                f"Entry: {entry_price} | Exit: {exit_price}\n"
                f"Gross: {gross_pnl:+.0f} | Charges: {charges['total']:.0f} | "
                f"Net: {net_pnl:+.0f}"
            )
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to close trade %d: %s", trade_id, exc)


# ---------------------------------------------------------------------------
# Channel 2 signal parser (NIFTY/SENSEX index options + stock options)
# ---------------------------------------------------------------------------
def parse_signal_ch2(text: str) -> ParsedSignal | None:
    """Parse Channel 2 signal format (no LLM — pure regex)."""
    text = text.strip()
    clean = text.replace("**", "")
    clean = re.sub(r'[^\x00-\x7F]+', ' ', clean).strip()
    clean = re.sub(r'\s+', ' ', clean)

    if len(clean) < 10:
        return None

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return None

    upper = clean.upper()
    if " CE" not in upper and " PE" not in upper:
        return None

    parse_text = re.sub(r'[^\w\s.&/-]', ' ', clean).strip()
    parse_text = re.sub(r'(\d)\s+(\d{3})(?=\s)', r'\1\2', parse_text)
    parse_text = re.sub(r'\s+', ' ', parse_text).upper()
    parse_text = re.sub(r'^[#\s]+', '', parse_text)

    action = "BUY"
    if "SELL" in parse_text:
        action = "SELL"

    m_opt = re.search(r'(\d[\d,]*(?:\.\d+)?)\s+(CE|PE)', parse_text)
    if not m_opt:
        return None

    strike = float(m_opt.group(1).replace(",", ""))
    option_type = m_opt.group(2)

    before_strike = parse_text[:m_opt.start()].strip()
    before_strike = re.sub(r'^(BUY|SELL)\s+', '', before_strike).strip()
    before_strike = re.sub(r'^(ZERO\s+TO\s+HERO|STOCK\s+OPTION\s+TRADE|SWING\s+TRADE)\s*', '', before_strike).strip()
    symbol = before_strike.strip()
    if not symbol:
        return None

    trigger = 0.0
    for line in lines:
        m_above = re.search(r'(?:ABOVE|BUY\s*@)\s*[:\-]?\s*(\d+(?:\.\d+)?)', line, re.I)
        if m_above:
            trigger = float(m_above.group(1))
            break

    sl = 0.0
    for line in lines:
        m_sl = re.search(r'SL\s*[:\-]?\s*(\d+(?:\.\d+)?)', line, re.I)
        if m_sl:
            sl = float(m_sl.group(1))
            break

    targets: list[float] = []
    for line in lines:
        m_tgt = re.search(r'(?:TARGET|TGT)\s*[:\-]?\s*([\d\s,/.+]+)', line, re.I)
        if m_tgt:
            raw = m_tgt.group(1)
            nums = re.findall(r'\d+(?:\.\d+)?', raw)
            targets = [float(n) for n in nums]
            break

    if sl <= 0 or not targets:
        return None

    is_swing = "SWING" in text.upper() or "HOLD WITH PATIENCE" in text.upper() or "HOLDING TRADE" in text.upper()
    if is_swing:
        return None

    return ParsedSignal(
        action=action,
        symbol=symbol,
        strike=strike,
        option_type=option_type,
        trigger_price=trigger,
        stop_loss=sl,
        targets=targets,
    )


# ---------------------------------------------------------------------------
# Telegram listener — dual-channel
# ---------------------------------------------------------------------------
def _normalize_channel_id(raw: str) -> int | None:
    """Convert a channel ID string to Telethon's -100-prefixed int format."""
    try:
        raw_id = int(raw)
        if raw_id > 0:
            return int(f"-100{raw_id}")
        elif not str(raw_id).startswith("-100"):
            return int(f"-100{abs(raw_id)}")
        return raw_id
    except ValueError:
        return None


async def start_listener() -> None:
    """Connect to Telegram and listen for signals in both channels."""
    from telethon import TelegramClient, events

    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    phone = os.getenv("TELEGRAM_PHONE", "")
    ch1_id = os.getenv("SIGNAL_CHANNEL_ID", "")
    ch2_id = os.getenv("SIGNAL_CHANNEL2_ID", "")

    if not all([api_id, api_hash, phone, ch1_id]):
        log.error("Missing env vars: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, SIGNAL_CHANNEL_ID")
        return

    ch1_int = _normalize_channel_id(ch1_id)
    ch2_int = _normalize_channel_id(ch2_id) if ch2_id else None

    listen_channels = [c for c in [ch1_int or ch1_id, ch2_int] if c]
    ch2_ids = {ch2_int} if ch2_int else set()

    session_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "telegram_user.session")
    client = TelegramClient(session_path, api_id, api_hash)

    await client.start(phone=phone)
    me = await client.get_me()
    log.info("Logged in as %s (id=%s)", me.first_name, me.id)
    _notify(f"Channel listener started as {me.first_name} (ch1 + ch2)")

    # --- Background position monitor: checks LTP every 5s, auto-closes ---
    _peak_net: dict[int, float] = {}

    async def _monitor_positions():
        """Periodically check open positions and auto-close on target/SL/floor."""
        from src.storage import db
        while True:
            await asyncio.sleep(5)
            try:
                db.init_db()
                with db.get_conn() as conn:
                    rows = conn.execute(
                        "SELECT id, symbol, price, qty, stop_price, target_price, broker_key "
                        "FROM trades WHERE status = 'OPEN' AND broker_key IS NOT NULL"
                    ).fetchall()
                if not rows:
                    continue

                keys = {r["broker_key"]: r for r in rows}
                from src.broker.upstox_data import UpstoxData
                ud = UpstoxData()
                ltp_data = ud._get("/v2/market-quote/ltp",
                                   params={"instrument_key": ",".join(keys)}).get("data", {})

                for item in ltp_data.values():
                    ikey = item.get("instrument_token", "")
                    ltp = item.get("last_price")
                    if not ltp or ikey not in keys:
                        continue
                    trade = keys[ikey]
                    tid = trade["id"]
                    entry = trade["price"]
                    qty = trade["qty"]
                    gross_pnl = (ltp - entry) * qty
                    charges_est = calc_charges(entry, ltp, qty)["total"]
                    net_pnl = gross_pnl - charges_est

                    prev_peak = _peak_net.get(tid, 0)
                    _peak_net[tid] = max(prev_peak, net_pnl)

                    if trade["target_price"] and ltp >= trade["target_price"]:
                        log.info("CHANNEL TARGET hit for %s: LTP=%.2f >= target=%.2f net=%.2f",
                                 trade["symbol"], ltp, trade["target_price"], net_pnl)
                        _close_trade_by_id(tid, ltp, "target_hit")
                        _peak_net.pop(tid, None)
                    elif trade["stop_price"] and ltp <= trade["stop_price"]:
                        log.info("SL HIT for %s: LTP=%.2f <= SL=%.2f",
                                 trade["symbol"], ltp, trade["stop_price"])
                        _close_trade_by_id(tid, ltp, "sl_hit")
                        _peak_net.pop(tid, None)
                    elif _peak_net[tid] >= PROFIT_TARGET and net_pnl <= PROFIT_TARGET:
                        log.info("FLOOR EXIT for %s: peak_net=%.2f now=%.2f (fell back to ₹%d floor)",
                                 trade["symbol"], _peak_net[tid], net_pnl, PROFIT_TARGET)
                        _close_trade_by_id(tid, ltp, "profit_floor")
                        _peak_net.pop(tid, None)
            except Exception as exc:  # noqa: BLE001
                log.debug("Monitor tick error: %s", exc)

    asyncio.get_event_loop().create_task(_monitor_positions())
    log.info("Position monitor started (target=₹%d, check every 5s)", PROFIT_TARGET)

    @client.on(events.NewMessage(chats=listen_channels))
    async def on_signal(event):
        text = event.message.text or ""
        if not text.strip():
            return

        chat_id = event.chat_id
        is_ch2 = chat_id in ch2_ids
        channel = "ch2" if is_ch2 else "ch1"
        ch_label = "CH2" if is_ch2 else "CH1"

        log.info("[%s] Channel message: %s", ch_label, text[:120])

        if is_ch2:
            sig = parse_signal_ch2(text)
        else:
            sig = parse_signal(text)

        if sig is not None:
            log.info("[%s] Parsed signal: %s %s %s %s trigger=%.2f SL=%.2f targets=%s",
                     ch_label, sig.action, sig.symbol, sig.strike, sig.option_type,
                     sig.trigger_price, sig.stop_loss, sig.targets)

            _notify(
                f"*[{ch_label}] Signal received — executing*\n"
                f"{sig.action} {sig.symbol} {int(sig.strike)} {sig.option_type}\n"
                f"Entry ABOVE {sig.trigger_price} | SL: {sig.stop_loss} | "
                f"Targets: {', '.join(str(t) for t in sig.targets)}"
            )

            result = execute_signal(sig, channel=channel)
            if result["placed"]:
                _notify(
                    f"*[{ch_label}] Order placed*\n"
                    f"{result['symbol']} x{result['qty']}\n"
                    f"Entry: {result['entry']} | SL: {result['sl']} | "
                    f"Target: {result['target']} | Floor: ₹{PROFIT_TARGET}"
                )
                log.info("[%s] Order placed: %s", ch_label, result)
            else:
                _notify(f"[{ch_label}] Signal not executed: {result['reason']}")
                log.warning("[%s] Signal not executed: %s", ch_label, result["reason"])
            return

        log.info("[%s] Not an entry signal, skipping.", ch_label)

    channels_str = f"ch1={ch1_id}"
    if ch2_id:
        channels_str += f", ch2={ch2_id}"
    log.info("Listening for signals: %s ...", channels_str)
    print(f"Listening for signals: {channels_str} ... (Ctrl-C to stop)")
    await client.run_until_disconnected()


def main() -> int:
    asyncio.run(start_listener())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

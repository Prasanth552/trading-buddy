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
PROFIT_TARGET = 1500  # ₹1,500 net profit per trade → auto-close
MAX_LOSS_PER_TRADE = 8000  # ₹8,000 hard cap — no trade can lose more than this
MAX_DAILY_LOSS = 10000  # ₹10,000 daily loss limit — stop trading after this

# ---------------------------------------------------------------------------
# Scanner (ch5) — auto-execute config
# ---------------------------------------------------------------------------
SCANNER_ENABLED = True
SCANNER_RUN_TIME = "09:20"       # IST — run once after ORB range forms
SCANNER_MIN_CONFIDENCE = 65      # only execute signals scoring >= this
SCANNER_MAX_TRADES = 3           # max trades per scanner run
SCANNER_SL_PCT = 0.30            # 30% of premium as stop-loss
SCANNER_TARGET_MULT = 2.0        # target = 2x entry premium

# ---------------------------------------------------------------------------
# OEH Scanner (Open=High) — auto-execute config
# ---------------------------------------------------------------------------
OEH_ENABLED = True
OEH_RUN_TIME = "09:20"          # IST — check after first 5-min candle
OEH_MAX_TRADES = 5              # max trades per scan
OEH_SL_PCT = 0.30               # 30% of premium as stop-loss
OEH_TARGET_MULT = 2.0           # target = 2x entry premium
OEH_TOLERANCE = 0.05            # ₹0.05 tolerance for high <= open check
OEH_MIN_DROP_PCT = 0.3          # skip candidates with <0.3% drop (weak signal)
OEH_BLOCKLIST = {"GODREJCP", "GRASIM"}  # repeat losers — skip these

# ---------------------------------------------------------------------------
# EOD Report — sent to Telegram at market close
# ---------------------------------------------------------------------------
EOD_REPORT_TIME = "15:35"  # IST — 5 min after market close
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

# ---------------------------------------------------------------------------
# Follow-up / exit message classification
# ---------------------------------------------------------------------------
_RE_CLOSE_NEAR_COST = re.compile(r'(?:CLOSE|CUT|EXIT)\s+(?:NEAR|AT)\s+COST|COST\s+TO\s+COST', re.I)


def _classify_followup(text: str) -> tuple[str, float | None]:
    """Classify exit/follow-up messages.

    Only acts on 'close near cost' type instructions.
    Returns (action, exit_price | None).
    Actions: "book" (close near cost), "unknown"
    """
    text = text.strip()
    if _RE_CLOSE_NEAR_COST.search(text):
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
    *, monthly: bool = False,
) -> tuple[dict[str, Any] | None, int]:
    """Search the Upstox instrument master for a specific option contract.

    Returns (instrument_dict, lot_size).  Searches NSE_FO and BSE_FO segments.
    When monthly=True, picks the nearest month-end expiry (for stock options
    where the channel sends monthly/September signals).
    Otherwise picks the nearest expiry >= today.
    """
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    from src.broker.upstox_client import _expiry_to_date
    import calendar

    IST = ZoneInfo(config.TIMEZONE)
    today = datetime.now(IST).date()

    sym = symbol.replace(" ", "").upper()
    instruments = uc.load_instruments()

    _SYMBOL_ALIASES: dict[str, str] = {
        "KALYANJIL": "KALYANKJIL",
        "LIC": "LICI",
    }
    sym = _SYMBOL_ALIASES.get(sym, sym)

    candidates: list[tuple[date, dict[str, Any]]] = []
    for inst in instruments:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO", "MCX_FO"):
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
        fuzzy: list[tuple[date, dict[str, Any]]] = []
        for inst in instruments:
            seg = inst.get("segment", "")
            if seg not in ("NSE_FO", "BSE_FO", "MCX_FO"):
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

    if monthly and len(candidates) > 1:
        from datetime import timedelta
        min_expiry = today + timedelta(days=7)
        monthly_cands = [(e, i) for e, i in candidates if e >= min_expiry]
        if monthly_cands:
            monthly_cands.sort(key=lambda x: x[0])
            chosen = monthly_cands[0][1]
            exp_chosen = monthly_cands[0][0]
            log.info("Monthly expiry selected for %s %s %s: %s (skipped weeklies before %s)",
                     sym, strike, option_type, exp_chosen, min_expiry)
            lot_size = int(chosen.get("lot_size", 1)) or 1
            return chosen, lot_size

    candidates.sort(key=lambda x: x[0])
    chosen = candidates[0][1]
    lot_size = int(chosen.get("lot_size", 1)) or 1
    return chosen, lot_size


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------
def _todays_realised_pnl() -> float:
    """Sum of P&L from all closed trades today."""
    from src.storage import db
    today = __import__("src.utils.market_calendar", fromlist=["now_ist"]).now_ist().strftime("%Y-%m-%d")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM trades "
            "WHERE status LIKE 'CLOSED%' AND pnl IS NOT NULL AND ts LIKE ?",
            (f"{today}%",),
        ).fetchone()
    return row["total"] if row else 0


def execute_signal(sig: ParsedSignal, *, channel: str = "ch1", max_lots: int | None = None, filter_score: int | None = None) -> dict[str, Any]:
    """Place an option order through the Upstox broker (paper/sandbox)."""
    from src.broker.upstox_client import UpstoxClient
    from src.storage import db

    db.init_db()

    # Daily loss limit — stop taking new trades if today's losses exceeded cap
    day_pnl = _todays_realised_pnl()
    if day_pnl <= -MAX_DAILY_LOSS:
        log.warning("DAILY LOSS LIMIT: today's P&L is ₹%.0f (limit -₹%d). Skipping new trade.",
                    day_pnl, MAX_DAILY_LOSS)
        _notify(
            f"⛔ *DAILY LOSS LIMIT HIT*\n"
            f"Today's P&L: ₹{day_pnl:+,.0f} (limit: -₹{MAX_DAILY_LOSS:,})\n"
            f"Skipping: {sig.symbol} {int(sig.strike)} {sig.option_type}\n"
            f"No more trades today — protecting capital."
        )
        return {"placed": False, "reason": f"Daily loss limit hit: ₹{day_pnl:+,.0f}"}

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
        use_monthly = channel in ("ch1", "ch1b")
        opt, master_lot_size = _resolve_channel_option(
            uc, sig.symbol, sig.strike, sig.option_type,
            monthly=use_monthly,
        )
    except Exception as exc:  # noqa: BLE001
        return {"placed": False, "reason": f"Option resolution failed: {exc}"}

    if opt is None:
        return {"placed": False, "reason": f"Could not resolve {sig.symbol} {sig.strike} {sig.option_type}"}

    lot_key = sig.symbol.replace(" ", "").upper()
    lot_size = config.LOT_SIZES.get(lot_key, master_lot_size)

    is_index = lot_key in ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY")
    if channel in ("ch2", "ch3"):
        lots = 3 if is_index else 2
    else:
        lots = 1
    if max_lots is not None:
        lots = min(lots, max_lots)
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
        "filter_score": filter_score,
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


def _handle_followup(action: str, exit_price: float | None, channel: str, ch_label: str,
                     original_sig: ParsedSignal | None = None) -> None:
    """Act on 'close near cost': close the matching open trade at current LTP.

    If original_sig is provided (from a reply-to message), match by symbol.
    Otherwise fall back to closing the most recent open trade for the channel.
    """
    from src.storage import db
    from src.broker.upstox_data import UpstoxData
    db.init_db()
    with db.get_conn() as conn:
        ch_filter = "(channel IS NULL OR channel = 'ch1')" if channel == "ch1" else "channel = 'ch2'"
        rows = conn.execute(
            f"SELECT id, symbol, price, qty, broker_key FROM trades "
            f"WHERE status = 'OPEN' AND ({ch_filter}) ORDER BY id DESC"
        ).fetchall()

    if not rows:
        log.info("[%s] Close-near-cost but no open trades.", ch_label)
        _notify(f"[{ch_label}] Received close-near-cost but no open trades found.")
        return

    # Match the specific trade if we parsed the replied-to signal
    target_trade = None
    if original_sig:
        match_sym = f"{original_sig.symbol} {int(original_sig.strike)} {original_sig.option_type}"
        for trade in rows:
            if trade["symbol"].upper() == match_sym.upper():
                target_trade = trade
                break
        if not target_trade:
            log.warning("[%s] Reply-to signal '%s' didn't match any open trade. "
                        "Open trades: %s", ch_label, match_sym,
                        [r["symbol"] for r in rows])

    # Fall back to most recent open trade if no match
    if not target_trade:
        target_trade = rows[0]
        log.info("[%s] No reply match, closing most recent open: %s", ch_label, target_trade["symbol"])

    # Get current LTP
    if exit_price:
        ltp = exit_price
    elif target_trade["broker_key"]:
        try:
            ud = UpstoxData()
            ltp_data = ud._get("/v2/market-quote/ltp",
                               params={"instrument_key": target_trade["broker_key"]}).get("data", {})
            ltp = None
            for item in ltp_data.values():
                ltp = item.get("last_price")
                break
            if not ltp:
                log.warning("[%s] Could not get LTP for %s.", ch_label, target_trade["symbol"])
                _notify(f"[{ch_label}] Close-near-cost failed: could not get LTP for {target_trade['symbol']}")
                return
        except Exception as exc:
            log.warning("[%s] LTP fetch failed for %s: %s", ch_label, target_trade["symbol"], exc)
            _notify(f"[{ch_label}] Close-near-cost failed: LTP fetch error")
            return
    else:
        log.warning("[%s] No broker_key for trade %d.", ch_label, target_trade["id"])
        _notify(f"[{ch_label}] Close-near-cost failed: no broker key for {target_trade['symbol']}")
        return

    _close_trade_by_id(target_trade["id"], ltp, "channel_exit")
    log.info("[%s] Close-near-cost: %s at %.2f", ch_label, target_trade["symbol"], ltp)
    _notify(f"[{ch_label}] Close-near-cost executed: {target_trade['symbol']} at {ltp}")


# ---------------------------------------------------------------------------
# Channel 2 signal parser — "Shrivastav G Prime" format
#
# Signals come in two patterns:
#   A) All in one message:  NIFTY 24250 CE\nABOVE 118\nTGT 128/140/170+\nSl below 108
#   B) Split across 2 messages sent seconds apart:
#        msg1: NIFTY 24150 CE\nNear 150
#        msg2: TGT 160/175/200+\nSl 8-9 point
#
# We buffer a partial signal (header + entry but no TGT/SL) and complete it
# when the next message supplies TGT + SL within 60 seconds.
# ---------------------------------------------------------------------------
_ch2_pending: dict[str, Any] | None = None
_ch2_pending_ts: float = 0.0

_CH2_SYMBOL_RE = re.compile(
    r'(?:(?:Intra|positional|Note)[/\s]*)*'
    r'((?:BANK\s*NIFTY|NIFTY|SENSEX|FINNIFTY|MIDCPNIFTY|[A-Za-z&]{2,20}))'
    r'\s+(\d+)\s+(CE|PE)',
    re.IGNORECASE | re.MULTILINE,
)
_CH2_ENTRY_RE = re.compile(
    r'(?:ABOVE|NEAR|Entry\s+near|BUY\s*@|CMP)\s*[:\-]?\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)
_CH2_TGT_RE = re.compile(
    r'(?:TGT|TARGET)\s*[:\-]?\s*([\d\s,/.+\-]+)',
    re.IGNORECASE,
)
_CH2_SL_RE = re.compile(
    r'(?:^|[^A-Z])(?:SL|Stop\s*loss)\s*(?:bel\w*\s*|use\s*)?(\d+(?:\.\d+)?(?:\s*[-–]\s*\d+)?)\s*(point)?',
    re.IGNORECASE,
)
_CH2_SKIP: set[str] = set()  # no skips — commodities enabled


def parse_signal_ch2(text: str) -> ParsedSignal | None:
    """Parse Channel 2 (Shrivastav G Prime) signal format.

    Handles single-message and split-message signals with a buffer.
    """
    global _ch2_pending, _ch2_pending_ts
    text = text.strip()
    clean = text.replace("**", "")
    clean = re.sub(r'[\U0001F600-\U0001FAFF☀-➿❤️‍]+', ' ', clean).strip()

    if len(clean) < 5:
        return None

    upper = clean.upper()
    if any(skip in upper for skip in ("DISCLAIMER", "WATCH LIST", "IMPORTANT",
                                       "FAKE ALERT", "OFFER", "APPLICATION",
                                       "FOLLOW THIS", "PLS READ",
                                       "PERFORMANCE", "MEMBERS SEND",
                                       "CONGRATULATIONS", "ENTER AFTER BREAK")):
        return None

    if re.search(r'NOT\s+ACTIVE\s+AVOID', upper):
        _ch2_pending = None
        return None

    if re.search(r'WAIT\s+FOR\s+TRIGGER', upper):
        return None

    if "HAZING" in upper or "HEDGE" in upper:
        return None

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    sym_match = _CH2_SYMBOL_RE.search(clean)
    entry_match = _CH2_ENTRY_RE.search(clean)
    tgt_match = _CH2_TGT_RE.search(clean)
    sl_match = _CH2_SL_RE.search(clean)

    has_symbol = sym_match is not None
    has_tgt = tgt_match is not None
    has_sl = sl_match is not None

    if has_symbol:
        raw_sym = sym_match.group(1).upper().strip()
        raw_sym = re.sub(r'\s+', ' ', raw_sym)
        if raw_sym == "BANK NIFTY":
            raw_sym = "BANKNIFTY"

        if raw_sym in _CH2_SKIP:
            return None

        strike = float(sym_match.group(2))
        opt_type = sym_match.group(3).upper()
        trigger = float(entry_match.group(1)) if entry_match else 0.0

        if has_tgt and has_sl:
            targets = _ch2_extract_targets(tgt_match)
            sl = _ch2_extract_sl(sl_match, trigger)
            if sl <= 0 or not targets:
                return None

            is_swing = any(kw in upper for kw in ("SWING", "POSITIONAL", "HOLD WITH PATIENCE"))
            if is_swing and "INTRA" not in upper:
                return None

            _ch2_pending = None
            return ParsedSignal(
                action="BUY",
                symbol=raw_sym,
                strike=strike,
                option_type=opt_type,
                trigger_price=trigger,
                stop_loss=sl,
                targets=targets,
            )
        else:
            _ch2_pending = {
                "symbol": raw_sym, "strike": strike, "opt_type": opt_type,
                "trigger": trigger,
            }
            _ch2_pending_ts = _time.time()
            log.info("[CH2] Buffered partial signal: %s %s %s trigger=%.0f (waiting for TGT/SL)",
                     raw_sym, strike, opt_type, trigger)
            return None

    if not has_symbol and _ch2_pending:
        if _time.time() - _ch2_pending_ts > 120:
            _ch2_pending = None
            return None

        # Update entry if mentioned (e.g., "Try near 145", "Near 160 also")
        if entry_match and not _ch2_pending.get("trigger"):
            _ch2_pending["trigger"] = float(entry_match.group(1))
        if not entry_match and not _ch2_pending.get("trigger"):
            near_m = re.search(r'(?:try\s+)?near\s+(\d+)', upper)
            if near_m:
                _ch2_pending["trigger"] = float(near_m.group(1))

        # Store TGT in buffer if we got it
        if has_tgt and "targets" not in _ch2_pending:
            _ch2_pending["targets"] = _ch2_extract_targets(tgt_match)
            _ch2_pending["tgt_ts"] = _time.time()
            log.info("[CH2] Buffer updated with targets: %s", _ch2_pending["targets"][:3])

        # Store SL in buffer if we got it
        if has_sl and "sl" not in _ch2_pending:
            trigger = _ch2_pending.get("trigger", 0)
            _ch2_pending["sl"] = _ch2_extract_sl(sl_match, trigger)
            log.info("[CH2] Buffer updated with SL: %s", _ch2_pending["sl"])

        # "tight sl" → use 8% of entry as default SL
        if "TIGHT SL" in upper and "sl" not in _ch2_pending and _ch2_pending.get("trigger"):
            default_sl = round(_ch2_pending["trigger"] * 0.92)
            _ch2_pending["sl"] = default_sl
            log.info("[CH2] Buffer: tight SL → default %.0f", default_sl)

        # Try to complete: need at minimum targets + (SL or default)
        targets = _ch2_pending.get("targets")
        trigger = _ch2_pending.get("trigger", 0)
        sl = _ch2_pending.get("sl", 0)

        if targets and trigger > 0:
            if sl <= 0:
                # Have TGT but no SL yet — wait for one more message unless buffer is old
                tgt_age = _time.time() - _ch2_pending.get("tgt_ts", _ch2_pending_ts)
                if tgt_age < 8:
                    return None  # give SL message a chance to arrive
                # Timeout — use default SL
                idx_syms = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}
                if _ch2_pending["symbol"] in idx_syms:
                    sl = round(trigger * 0.90)
                else:
                    sl = round(trigger * 0.85)
                log.info("[CH2] No explicit SL after wait — using default: %.0f (entry=%.0f)", sl, trigger)

            if sl > 0:
                sig = ParsedSignal(
                    action="BUY",
                    symbol=_ch2_pending["symbol"],
                    strike=_ch2_pending["strike"],
                    option_type=_ch2_pending["opt_type"],
                    trigger_price=trigger,
                    stop_loss=sl,
                    targets=targets,
                )
                _ch2_pending = None
                return sig

    return None


def _ch2_extract_targets(tgt_match: re.Match) -> list[float]:
    raw = tgt_match.group(1)
    nums = re.findall(r'\d+(?:\.\d+)?', raw)
    return [float(n) for n in nums if float(n) > 0]


def _ch2_extract_sl(sl_match: re.Match, trigger: float) -> float:
    raw_val = sl_match.group(1).strip()
    is_points = sl_match.group(2) is not None

    if '-' in raw_val or '–' in raw_val:
        parts = re.split(r'[-–]', raw_val)
        sl_val = float(parts[0].strip())
    else:
        sl_val = float(raw_val)

    if is_points and trigger > 0:
        return trigger - sl_val
    if sl_val <= 25 and trigger > 50:
        return trigger - sl_val
    return sl_val


# ---------------------------------------------------------------------------
# Channel 3 signal parser (free channel — no SL/TGT provided)
# ---------------------------------------------------------------------------
def parse_signal_ch3(text: str) -> ParsedSignal | None:
    """Parse Channel 3 format: SYMBOL STRIKE CE/PE, BUY ABOVE/NEAR price, TGT paid, SL paid.

    No SL or targets from the channel — we set a 30% SL and no target (rely on floor + market close).
    """
    text = text.strip()
    clean = text.replace("**", "")
    clean = re.sub(r'[^\x00-\x7F]+', ' ', clean).strip()
    clean = re.sub(r'\s+', ' ', clean)

    if len(clean) < 10:
        return None

    upper = clean.upper()
    if "CE" not in upper and "PE" not in upper:
        return None
    if "BUY" not in upper:
        return None
    if "ABOVE" not in upper and "NEAR" not in upper:
        return None
    if "PAID" not in upper:
        return None

    # Skip CRUDEOIL — different segment/hours
    if "CRUDEOIL" in upper or "CRUDE" in upper:
        return None

    parse_text = re.sub(r'[^\w\s.&/-]', ' ', clean).strip()
    parse_text = re.sub(r'(\d)\s+(\d{3})(?=\s)', r'\1\2', parse_text)
    parse_text = re.sub(r'\s+', ' ', parse_text).upper()
    parse_text = re.sub(r'^[#\s]+', '', parse_text)

    m_opt = re.search(r'(\d[\d,]*(?:\.\d+)?)\s+(CE|PE)', parse_text)
    if not m_opt:
        return None

    strike = float(m_opt.group(1).replace(",", ""))
    option_type = m_opt.group(2)

    before_strike = parse_text[:m_opt.start()].strip()
    before_strike = re.sub(r'^(BUY|SELL)\s+', '', before_strike).strip()
    symbol = before_strike.strip()
    if not symbol:
        return None

    # Extract entry price
    trigger = 0.0
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines:
        m_entry = re.search(r'(?:ABOVE|NEAR)\s*[:\-]?\s*0?(\d+(?:\.\d+)?)', line, re.I)
        if m_entry:
            trigger = float(m_entry.group(1))
            break

    if trigger <= 0:
        return None

    # No SL from channel — set 30% below entry as safety net
    sl = round(trigger * 0.70, 2)
    # No target — rely on floor + market close exit
    target = round(trigger * 2.0, 2)

    return ParsedSignal(
        action="BUY",
        symbol=symbol,
        strike=strike,
        option_type=option_type,
        trigger_price=trigger,
        stop_loss=sl,
        targets=[target],
    )


# ---------------------------------------------------------------------------
# Scanner (ch5) — resolve ATM strike and execute
# ---------------------------------------------------------------------------
def _resolve_atm_strike(symbol: str, option_type: str) -> ParsedSignal | None:
    """Get the stock LTP, find nearest ATM strike, fetch option premium."""
    from src.broker.upstox_client import UpstoxClient
    from src.broker.upstox_data import UpstoxData

    try:
        ud = UpstoxData()
        uc = UpstoxClient()

        # Step 1: get stock LTP
        sym_upper = symbol.replace(" ", "").upper()
        upstox_key = config.UPSTOX_INDEX_KEYS.get(f"NSE:{sym_upper}")
        if not upstox_key:
            upstox_key = f"NSE_EQ|{sym_upper}"
            instruments = uc.load_instruments()
            for inst in instruments:
                if inst.get("segment") == "NSE_EQ" and inst.get("trading_symbol", "").upper() == sym_upper:
                    upstox_key = inst.get("instrument_key", upstox_key)
                    break

        ltp_data = ud._get("/v2/market-quote/ltp",
                           params={"instrument_key": upstox_key}).get("data", {})
        stock_ltp = None
        for item in ltp_data.values():
            stock_ltp = item.get("last_price")
            break
        if not stock_ltp or stock_ltp <= 0:
            log.warning("[CH5] Could not get LTP for %s", symbol)
            return None

        # Step 2: find ATM strike (round to nearest strike step)
        strike_step = config.OPTION_SPECS.get(f"NSE:{sym_upper}", {}).get("strike_step", 50)
        atm_strike = round(stock_ltp / strike_step) * strike_step

        # Step 3: resolve the option contract and get its premium
        opt, lot_size = _resolve_channel_option(uc, symbol, atm_strike, option_type)
        if opt is None:
            log.warning("[CH5] Could not resolve %s %s %s", symbol, atm_strike, option_type)
            return None

        opt_key = opt.get("instrument_key") or opt.get("instrument_token", "")
        opt_ltp_data = ud._get("/v2/market-quote/ltp",
                               params={"instrument_key": opt_key}).get("data", {})
        premium = None
        for item in opt_ltp_data.values():
            premium = item.get("last_price")
            break
        if not premium or premium <= 0:
            log.warning("[CH5] Could not get premium for %s %s %s", symbol, atm_strike, option_type)
            return None

        sl = round(premium * (1 - SCANNER_SL_PCT), 2)
        target = round(premium * SCANNER_TARGET_MULT, 2)

        log.info("[CH5] ATM resolved: %s LTP=%.2f → strike=%d %s premium=%.2f SL=%.2f TGT=%.2f",
                 symbol, stock_ltp, atm_strike, option_type, premium, sl, target)

        return ParsedSignal(
            action="BUY",
            symbol=symbol,
            strike=atm_strike,
            option_type=option_type,
            trigger_price=premium,
            stop_loss=sl,
            targets=[target],
        )
    except Exception as exc:
        log.error("[CH5] ATM resolution failed for %s: %s", symbol, exc)
        return None


async def _run_scanner_once():
    """Run the market scanner and auto-execute top signals as ch5."""
    from src.signals.market_scanner import MarketScanner
    from src.broker.upstox_data import load_cached_token

    log.info("[CH5] Scanner starting...")

    token = load_cached_token()
    if not token:
        log.error("[CH5] No valid Upstox token — scanner cannot resolve ATM strikes")
        _notify("*[CH5] Scanner SKIPPED* — No Upstox token for today. Cannot resolve option strikes.")
        return

    _notify("*[CH5] Scanner running — scanning for signals...*")

    try:
        scanner = MarketScanner()
        signals = scanner.scan()
    except Exception as exc:
        log.error("[CH5] Scanner failed: %s", exc, exc_info=True)
        _notify(f"[CH5] Scanner error: {exc}")
        return

    if not signals:
        log.info("[CH5] No signals found")
        _notify("[CH5] No signals found today")
        return

    qualified = [s for s in signals if s.confidence >= SCANNER_MIN_CONFIDENCE]
    if not qualified:
        log.info("[CH5] No signals above confidence threshold (%d)", SCANNER_MIN_CONFIDENCE)
        _notify(f"[CH5] {len(signals)} signals found but none above {SCANNER_MIN_CONFIDENCE} confidence")
        return

    top = qualified[:SCANNER_MAX_TRADES]
    summary_lines = []
    executed = 0

    for scan_sig in top:
        log.info("[CH5] Processing: %s %s (confidence=%d, strategy=%s)",
                 scan_sig.symbol, scan_sig.option_type, scan_sig.confidence, scan_sig.strategy)

        parsed = _resolve_atm_strike(scan_sig.symbol, scan_sig.option_type)
        if parsed is None:
            summary_lines.append(f"SKIP {scan_sig.symbol} {scan_sig.option_type} — could not resolve ATM")
            continue

        result = execute_signal(parsed, channel="ch5", max_lots=1)
        if result["placed"]:
            executed += 1
            reasons_str = "; ".join(scan_sig.reasons[:3])
            summary_lines.append(
                f"BUY {result['symbol']} x{result['qty']} @ {result['entry']:.2f} "
                f"(confidence {scan_sig.confidence}, {scan_sig.strategy})"
            )
            _notify(
                f"*[CH5] Auto-trade placed*\n"
                f"{result['symbol']} x{result['qty']}\n"
                f"Entry: {result['entry']} | SL: {result['sl']} | Target: {result['target']}\n"
                f"Confidence: {scan_sig.confidence}/100 | Strategy: {scan_sig.strategy}\n"
                f"Reasons: {reasons_str}"
            )
        else:
            summary_lines.append(f"FAIL {scan_sig.symbol} {scan_sig.option_type} — {result['reason']}")

    summary = "\n".join(summary_lines)
    log.info("[CH5] Scanner done: %d/%d executed\n%s", executed, len(top), summary)
    _notify(f"*[CH5] Scanner complete: {executed}/{len(top)} trades placed*\n{summary}")


# ---------------------------------------------------------------------------
# OEH Scanner — Open=High bearish signal scanner
# ---------------------------------------------------------------------------
async def _run_oeh_scan():
    """Scan F&O universe at 9:30 AM for OEH (Open=High) stocks, buy PEs."""
    import time as _t
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.broker.upstox_data import UpstoxData, load_cached_token

    IST = ZoneInfo(config.TIMEZONE)
    log.info("[OEH] Scanner starting...")

    token = load_cached_token()
    if not token:
        log.error("[OEH] No valid Upstox token — scanner cannot run")
        _notify("*[OEH] Scanner SKIPPED* — No Upstox token for today.")
        return

    _notify("*[OEH] Scanning for Open=High stocks...*")

    try:
        ud = UpstoxData(access_token=token)
        master = ud._load_master()
    except Exception as exc:
        log.error("[OEH] Failed to load instrument master: %s", exc)
        _notify(f"[OEH] Scanner error: {exc}")
        return

    eq_keys = {}
    for inst in master:
        if inst.get("segment") == "NSE_EQ":
            tsym = (inst.get("trading_symbol") or "").upper()
            if tsym:
                eq_keys[tsym] = inst.get("instrument_key")

    today = datetime.now(IST).date()
    from_dt = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=25)

    # NIFTY trend filter: skip if NIFTY is green (bullish day)
    nifty_key = config.UPSTOX_INDEX_KEYS.get("NSE:NIFTY 50")
    if nifty_key:
        try:
            nifty_candles = ud.historical_data(nifty_key, from_dt, to_dt, "5minute")
            if nifty_candles and len(nifty_candles) >= 1:
                nifty_open = nifty_candles[0]["open"]
                nifty_close = nifty_candles[0]["close"]
                if nifty_close > nifty_open:
                    log.info("[OEH] NIFTY is green (open=%.1f close=%.1f) — proceeding anyway (floor strategy)",
                             nifty_open, nifty_close)
                    _notify(f"[OEH] NIFTY is green at 9:20 "
                            f"(open={nifty_open:.1f} → {nifty_close:.1f}). "
                            f"Proceeding — floor strategy handles green days.")
                log.info("[OEH] NIFTY is red (open=%.1f close=%.1f) — proceeding with scan",
                         nifty_open, nifty_close)
        except Exception as exc:
            log.warning("[OEH] Could not fetch NIFTY data, proceeding anyway: %s", exc)

    candidates = []
    scanned = 0

    for sym in OEH_UNIVERSE:
        if sym in OEH_BLOCKLIST:
            continue

        inst_key = eq_keys.get(sym)
        if not inst_key:
            continue

        try:
            candles = ud.historical_data(inst_key, from_dt, to_dt, "5minute")
            _t.sleep(0.3)
        except Exception as exc:
            err = str(exc)
            if "429" in err or "rate" in err.lower():
                _t.sleep(3)
                try:
                    candles = ud.historical_data(inst_key, from_dt, to_dt, "5minute")
                except Exception:
                    continue
            else:
                continue

        scanned += 1
        if not candles or len(candles) < 1:
            continue

        open_price = candles[0]["open"]
        if open_price <= 0:
            continue

        max_high = candles[0]["high"]

        if max_high > open_price + OEH_TOLERANCE:
            continue

        entry_price = candles[0]["close"]
        drop_pct = (open_price - entry_price) / open_price * 100

        if drop_pct < OEH_MIN_DROP_PCT:
            continue

        candidates.append({
            "symbol": sym,
            "open": open_price,
            "entry": entry_price,
            "max_high": max_high,
            "drop_pct": drop_pct,
        })

    log.info("[OEH] Scanned %d stocks, found %d OEH candidates (blocked %d)",
             scanned, len(candidates), len(OEH_BLOCKLIST))

    if not candidates:
        _notify(f"[OEH] No OEH candidates found today (scanned {scanned} stocks)")
        return

    candidates.sort(key=lambda x: x["drop_pct"], reverse=True)
    top = candidates[:OEH_MAX_TRADES]

    summary_lines = []
    executed = 0

    for c in top:
        parsed = _resolve_atm_strike(c["symbol"], "PE")
        if parsed is None:
            summary_lines.append(f"SKIP {c['symbol']} PE — could not resolve ATM")
            continue

        parsed.stop_loss = round(parsed.trigger_price * (1 - OEH_SL_PCT), 2)
        parsed.targets = [round(parsed.trigger_price * OEH_TARGET_MULT, 2)]

        result = execute_signal(parsed, channel="oeh", max_lots=2)
        if result["placed"]:
            executed += 1
            summary_lines.append(
                f"BUY {result['symbol']} x{result['qty']} @ {result['entry']:.2f} "
                f"(OEH drop={c['drop_pct']:.1f}%)"
            )
            _notify(
                f"*[OEH] Trade placed*\n"
                f"{result['symbol']} x{result['qty']}\n"
                f"Entry: {result['entry']} | SL: {result['sl']} | Target: {result['target']}\n"
                f"Signal: {c['symbol']} Open={c['open']:.2f} Hi={c['max_high']:.2f} "
                f"(drop {c['drop_pct']:.1f}% in 15min)"
            )
        else:
            summary_lines.append(f"FAIL {c['symbol']} PE — {result['reason']}")

    summary = "\n".join(summary_lines)
    log.info("[OEH] Scan done: %d/%d executed\n%s", executed, len(top), summary)
    _notify(
        f"*[OEH] Scan complete: {executed}/{len(top)} trades placed*\n"
        f"Candidates found: {len(candidates)} | Scanned: {scanned}\n{summary}"
    )


# ---------------------------------------------------------------------------
# EOD Report — daily summary sent to Telegram
# ---------------------------------------------------------------------------
def _build_eod_report(target_date: str | None = None) -> str:
    """Build a formatted EOD report for the given date (default: today)."""
    from zoneinfo import ZoneInfo
    from datetime import datetime
    from src.storage import db as _db
    IST = ZoneInfo(config.TIMEZONE)

    if target_date is None:
        target_date = datetime.now(IST).strftime("%Y-%m-%d")

    _db.init_db()

    channels = [
        ("ch1", "CH1 Paid"),
        ("ch2", "CH2 G Prime"),
        ("oeh", "OEH Scanner"),
    ]

    lines = []
    lines.append(f"📊 *DAY END REPORT — {target_date}*")
    lines.append("")

    grand_pnl = 0
    grand_wins = 0
    grand_losses = 0
    grand_trades = 0
    grand_charges = 0

    with _db.get_conn() as conn:
        for ch_key, ch_label in channels:
            if ch_key == "oeh":
                ch_filter = "channel='oeh'"
            elif ch_key == "ch1":
                ch_filter = "channel IN ('ch1','ch1b')"
            else:
                ch_filter = f"channel='{ch_key}'"

            rows = conn.execute(
                f"SELECT id, ts, symbol, price, exit_price, pnl, status, "
                f"stop_price, target_price, qty, charges "
                f"FROM trades WHERE {ch_filter} AND ts >= ? AND ts < ? ORDER BY ts",
                (f"{target_date}T00:00:00", f"{target_date}T23:59:59")
            ).fetchall()

            if not rows:
                continue

            closed = [r for r in rows if r["status"] and r["status"] != "OPEN"]
            open_trades = [r for r in rows if r["status"] == "OPEN"]

            ch_pnl = sum((r["pnl"] or 0) for r in closed)
            ch_charges = sum((r["charges"] or 0) for r in closed)
            ch_wins = sum(1 for r in closed if (r["pnl"] or 0) > 0)
            ch_losses = sum(1 for r in closed if (r["pnl"] or 0) <= 0)
            ch_best = max((r["pnl"] or 0) for r in closed) if closed else 0
            ch_worst = min((r["pnl"] or 0) for r in closed) if closed else 0

            grand_pnl += ch_pnl
            grand_wins += ch_wins
            grand_losses += ch_losses
            grand_trades += len(closed)
            grand_charges += ch_charges

            wr = f"{ch_wins / len(closed) * 100:.0f}%" if closed else "—"
            icon = "🟢" if ch_pnl >= 0 else "🔴"

            lines.append(f"{icon} *{ch_label}*")
            lines.append(f"  Trades: {len(closed)} ({ch_wins}W / {ch_losses}L) | WR: {wr}")
            lines.append(f"  P&L: ₹{ch_pnl:+,.0f} | Charges: ₹{ch_charges:,.0f}")
            lines.append(f"  Best: ₹{ch_best:+,.0f} | Worst: ₹{ch_worst:+,.0f}")

            if open_trades:
                lines.append(f"  ⚠️ {len(open_trades)} still OPEN")

            lines.append("")

            for r in closed:
                pnl = r["pnl"] or 0
                icon_t = "✅" if pnl > 0 else "❌"
                status = (r["status"] or "").replace("CLOSED_", "").replace("_", " ").title()
                entry_p = f"{r['price']:.1f}" if r["price"] else "—"
                exit_p = f"{r['exit_price']:.1f}" if r["exit_price"] else "—"
                lines.append(
                    f"  {icon_t} {r['symbol']}"
                    f"\n     Entry: {entry_p} → Exit: {exit_p} | Qty: {r['qty']}"
                    f"\n     P&L: ₹{pnl:+,.0f} | {status}"
                )
            lines.append("")

    if grand_trades == 0:
        lines.append("No trades today.")
        return "\n".join(lines)

    net_pnl = grand_pnl - grand_charges
    grand_icon = "🟢" if grand_pnl >= 0 else "🔴"
    grand_wr = f"{grand_wins / grand_trades * 100:.0f}%" if grand_trades else "—"

    lines.append("━" * 28)
    lines.append(f"{grand_icon} *TOTAL: ₹{grand_pnl:+,.0f}*")
    lines.append(f"  Trades: {grand_trades} ({grand_wins}W / {grand_losses}L) | WR: {grand_wr}")
    lines.append(f"  Charges: ₹{grand_charges:,.0f} | Net: ₹{net_pnl:+,.0f}")
    lines.append("")
    lines.append("_Trading Buddy • Auto-generated_")

    return "\n".join(lines)


def send_eod_report(target_date: str | None = None) -> None:
    """Build and send the EOD report via Telegram."""
    report = _build_eod_report(target_date)
    log.info("[EOD] Sending day-end report...")
    _notify(report)
    log.info("[EOD] Report sent.")


# ---------------------------------------------------------------------------
# Telegram listener — tri-channel
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
    ch1b_id = os.getenv("SIGNAL_CHANNEL1B_ID", "")
    ch2_id = os.getenv("SIGNAL_CHANNEL2_ID", "")
    ch3_id = os.getenv("SIGNAL_CHANNEL3_ID", "")

    if not all([api_id, api_hash, phone, ch1_id]):
        log.error("Missing env vars: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, SIGNAL_CHANNEL_ID")
        return

    ch1_int = _normalize_channel_id(ch1_id)
    ch1b_int = _normalize_channel_id(ch1b_id) if ch1b_id else None
    ch2_int = _normalize_channel_id(ch2_id) if ch2_id else None
    ch3_int = _normalize_channel_id(ch3_id) if ch3_id else None

    listen_channels = [c for c in [ch1_int or ch1_id, ch1b_int, ch2_int] if c]
    ch1b_ids = {ch1b_int} if ch1b_int else set()
    ch2_ids = {ch2_int} if ch2_int else set()
    ch3_ids = {ch3_int} if ch3_int else set()

    session_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "telegram_user.session")
    client = TelegramClient(session_path, api_id, api_hash)

    await client.start(phone=phone)
    me = await client.get_me()
    log.info("Logged in as %s (id=%s)", me.first_name, me.id)
    ch_list = "ch1"
    if ch1b_int:
        ch_list += " + ch1b"
    if ch2_int:
        ch_list += " + ch2"
    if ch3_int:
        ch_list += " + ch3"
    _notify(f"Channel listener started as {me.first_name} ({ch_list})")

    # --- Background position monitor: checks LTP every 5s, auto-closes ---
    _peak_net: dict[int, float] = {}

    _monitor_fail_count = 0

    async def _monitor_positions():
        """Periodically check open positions and auto-close on target/SL/floor."""
        nonlocal _monitor_fail_count
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
                from src.broker.upstox_data import UpstoxData, load_cached_token
                token = load_cached_token()
                if not token:
                    if _monitor_fail_count % 360 == 0:
                        log.error("SL MONITOR: No valid Upstox token for today! "
                                  "SL/target checks DISABLED until token is refreshed. "
                                  "%d open trades unprotected.", len(rows))
                        _notify("*SL MONITOR DOWN* — No Upstox token for today. "
                                f"{len(rows)} open trades have NO SL protection! "
                                "Refresh token ASAP.")
                    _monitor_fail_count += 1
                    continue

                ud = UpstoxData(access_token=token)
                ltp_data = ud._get("/v2/market-quote/ltp",
                                   params={"instrument_key": ",".join(keys)}).get("data", {})

                if not ltp_data:
                    if _monitor_fail_count % 60 == 0:
                        log.warning("SL MONITOR: LTP API returned empty data for %d trades — "
                                    "token may be expired or API down", len(rows))
                    _monitor_fail_count += 1
                    continue

                _monitor_fail_count = 0

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
                        log.info("CHANNEL TARGET hit for %s: LTP=%.2f >= target=%.2f net=₹%.0f",
                                 trade["symbol"], ltp, trade["target_price"], net_pnl)
                        _close_trade_by_id(tid, ltp, "target_hit")
                        _peak_net.pop(tid, None)
                    elif trade["stop_price"] and ltp <= trade["stop_price"]:
                        log.info("SL HIT for %s: LTP=%.2f <= SL=%.2f",
                                 trade["symbol"], ltp, trade["stop_price"])
                        _close_trade_by_id(tid, ltp, "sl_hit")
                        _peak_net.pop(tid, None)
                    elif net_pnl <= -MAX_LOSS_PER_TRADE:
                        log.warning("MAX LOSS CAP for %s: net_pnl=₹%.0f hit -₹%d cap. Force closing.",
                                    trade["symbol"], net_pnl, MAX_LOSS_PER_TRADE)
                        _close_trade_by_id(tid, ltp, "max_loss_cap")
                        _peak_net.pop(tid, None)
                        _notify(
                            f"🛑 *MAX LOSS CAP* — {trade['symbol']}\n"
                            f"Loss hit ₹{abs(net_pnl):,.0f} (cap: ₹{MAX_LOSS_PER_TRADE:,})\n"
                            f"Auto-closed to protect capital."
                        )
                    elif _peak_net[tid] >= PROFIT_TARGET and net_pnl <= PROFIT_TARGET:
                        log.info("FLOOR EXIT for %s: peak=₹%.0f dipped to ₹%.0f (floor=₹%d)",
                                 trade["symbol"], _peak_net[tid], net_pnl, PROFIT_TARGET)
                        _close_trade_by_id(tid, ltp, "profit_floor")
                        _peak_net.pop(tid, None)
            except Exception as exc:  # noqa: BLE001
                _monitor_fail_count += 1
                if _monitor_fail_count % 60 == 0:
                    log.error("Monitor tick error (repeated %dx): %s", _monitor_fail_count, exc)
                else:
                    log.warning("Monitor tick error: %s", exc)

    asyncio.get_event_loop().create_task(_monitor_positions())
    log.info("Position monitor started (target=₹%d, max_loss=₹%d, check every 5s)",
             PROFIT_TARGET, MAX_LOSS_PER_TRADE)

    # --- Scanner (ch5): run once daily at SCANNER_RUN_TIME ---
    async def _scanner_scheduler():
        """Wait until SCANNER_RUN_TIME IST each day, then run the scanner."""
        from zoneinfo import ZoneInfo
        from datetime import datetime, time as dt_time, timedelta
        IST = ZoneInfo(config.TIMEZONE)

        h, m = map(int, SCANNER_RUN_TIME.split(":"))
        first_run = True

        while True:
            if not SCANNER_ENABLED:
                await asyncio.sleep(60)
                continue

            now = datetime.now(IST)

            # Catch-up: if process started after scheduled time but before 15:00,
            # run the scanner immediately on first iteration
            if first_run and mc.is_market_day():
                first_run = False
                scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if now > scheduled and now.hour < 15:
                    log.info("[CH5] Missed scheduled %s run — catching up now", SCANNER_RUN_TIME)
                    await _run_scanner_once()
                    continue
            first_run = False

            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)

            wait_secs = (target - now).total_seconds()
            log.info("[CH5] Next scanner run at %s IST (in %.0f min)",
                     target.strftime("%Y-%m-%d %H:%M"), wait_secs / 60)
            await asyncio.sleep(wait_secs)

            if not mc.is_market_day():
                log.info("[CH5] Not a trading day, skipping scanner")
                continue

            await _run_scanner_once()

    asyncio.get_event_loop().create_task(_scanner_scheduler())
    log.info("Scanner (ch5) scheduler started — runs daily at %s IST", SCANNER_RUN_TIME)

    # --- OEH Scanner: run once daily at OEH_RUN_TIME ---
    async def _oeh_scheduler():
        """Wait until OEH_RUN_TIME IST each day, then scan for OEH stocks."""
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta
        IST = ZoneInfo(config.TIMEZONE)

        h, m = map(int, OEH_RUN_TIME.split(":"))
        first_run = True

        while True:
            if not OEH_ENABLED:
                await asyncio.sleep(60)
                continue

            now = datetime.now(IST)

            if first_run and mc.is_market_day():
                first_run = False
                scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if now > scheduled and now.hour < 15:
                    log.info("[OEH] Missed scheduled %s run — catching up now", OEH_RUN_TIME)
                    await _run_oeh_scan()
                    continue
            first_run = False

            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)

            wait_secs = (target - now).total_seconds()
            log.info("[OEH] Next scan at %s IST (in %.0f min)",
                     target.strftime("%Y-%m-%d %H:%M"), wait_secs / 60)
            await asyncio.sleep(wait_secs)

            if not mc.is_market_day():
                log.info("[OEH] Not a trading day, skipping scan")
                continue

            await _run_oeh_scan()

    asyncio.get_event_loop().create_task(_oeh_scheduler())
    log.info("OEH scanner started — runs daily at %s IST", OEH_RUN_TIME)

    # --- EOD Report: send daily at 15:35 IST ---
    async def _eod_report_scheduler():
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta
        IST = ZoneInfo(config.TIMEZONE)

        h, m = map(int, EOD_REPORT_TIME.split(":"))

        while True:
            now = datetime.now(IST)
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)

            wait_secs = (target - now).total_seconds()
            log.info("[EOD] Next report at %s IST (in %.0f min)",
                     target.strftime("%Y-%m-%d %H:%M"), wait_secs / 60)
            await asyncio.sleep(wait_secs)

            if not mc.is_market_day():
                log.info("[EOD] Not a trading day, skipping report")
                continue

            try:
                send_eod_report()
            except Exception as exc:
                log.error("[EOD] Failed to send report: %s", exc)

    asyncio.get_event_loop().create_task(_eod_report_scheduler())
    log.info("EOD report scheduler started — runs daily at %s IST", EOD_REPORT_TIME)

    @client.on(events.NewMessage(chats=listen_channels))
    async def on_signal(event):
        text = event.message.text or ""
        if not text.strip():
            return

        chat_id = event.chat_id
        if chat_id in ch3_ids:
            channel, ch_label = "ch3", "CH3"
        elif chat_id in ch2_ids:
            channel, ch_label = "ch2", "CH2"
        elif chat_id in ch1b_ids:
            channel, ch_label = "ch1b", "CH1B"
        else:
            channel, ch_label = "ch1", "CH1"

        log.info("[%s] Channel message: %s", ch_label, text[:120])

        if channel == "ch3":
            sig = parse_signal_ch3(text)
        elif channel == "ch2":
            sig = parse_signal_ch2(text)
        else:
            sig = parse_signal(text)

        if sig is not None:
            log.info("[%s] Parsed signal: %s %s %s %s trigger=%.2f SL=%.2f targets=%s",
                     ch_label, sig.action, sig.symbol, sig.strike, sig.option_type,
                     sig.trigger_price, sig.stop_loss, sig.targets)

            # --- Smart Filter: score signal (always execute, save score for comparison) ---
            filt = None
            filter_score = None
            try:
                from src.signals.smart_filter import evaluate_signal
                filt = evaluate_signal(sig.symbol, sig.option_type, channel, sig.trigger_price)
                filter_score = filt.score
                log.info("[%s] FILTER: score=%d action=%s reasons=%s",
                         ch_label, filt.score, filt.action, filt.reasons)
            except Exception as exc:
                log.warning("Smart filter failed: %s", exc)

            filter_line = f"Filter: {filt.action} (score {filt.score}/100)" if filt else "Filter: unavailable"
            reason_lines = "\n".join(f"  • {r}" for r in filt.reasons[:5]) if filt else ""
            filter_tag = ""
            if filt and filt.action == "SKIP":
                filter_tag = " (filter: SKIP)"
            elif filt and filt.action == "REDUCE":
                filter_tag = " (filter: REDUCE)"

            _notify(
                f"*[{ch_label}] Signal received — executing{filter_tag}*\n"
                f"{sig.action} {sig.symbol} {int(sig.strike)} {sig.option_type}\n"
                f"Entry ABOVE {sig.trigger_price} | SL: {sig.stop_loss} | "
                f"Targets: {', '.join(str(t) for t in sig.targets)}\n"
                f"{filter_line}\n{reason_lines}"
            )

            result = execute_signal(sig, channel=channel, filter_score=filter_score)
            if result["placed"]:
                _notify(
                    f"*[{ch_label}] Order placed*\n"
                    f"{result['symbol']} x{result['qty']}\n"
                    f"Entry: {result['entry']} | SL: {result['sl']} | "
                    f"Target: {result['target']} | Floor: ₹{PROFIT_TARGET}\n"
                    f"{filter_line}"
                )
                log.info("[%s] Order placed: %s", ch_label, result)
            else:
                _notify(f"[{ch_label}] Signal not executed: {result['reason']}")
                log.warning("[%s] Signal not executed: %s", ch_label, result["reason"])
            return

        # --- Follow-up: "close near cost" → exit the tagged trade at current LTP ---
        action, exit_price = _classify_followup(text)
        if action == "book":
            # Try to identify which trade by parsing the replied-to message
            original_sig = None
            if event.message.reply_to and event.message.reply_to.reply_to_msg_id:
                try:
                    orig_msg = await client.get_messages(event.chat_id, ids=event.message.reply_to.reply_to_msg_id)
                    if orig_msg and orig_msg.text:
                        if channel == "ch3":
                            original_sig = parse_signal_ch3(orig_msg.text)
                        elif channel == "ch2":
                            original_sig = parse_signal_ch2(orig_msg.text)
                        else:
                            original_sig = parse_signal(orig_msg.text)
                        log.info("[%s] Reply-to signal: %s %s %s",
                                 ch_label, original_sig.symbol if original_sig else "?",
                                 original_sig.strike if original_sig else "?",
                                 original_sig.option_type if original_sig else "?")
                except Exception as exc:
                    log.warning("[%s] Could not fetch replied-to message: %s", ch_label, exc)
            log.info("[%s] Close-near-cost instruction detected", ch_label)
            _handle_followup(action, exit_price, channel, ch_label, original_sig)
            return

        log.info("[%s] Not an entry signal or follow-up, skipping.", ch_label)

    channels_str = f"ch1={ch1_id}"
    if ch1b_id:
        channels_str += f", ch1b={ch1b_id}"
    if ch2_id:
        channels_str += f", ch2={ch2_id}"
    if ch3_id:
        channels_str += f", ch3={ch3_id}"
    log.info("Listening for signals: %s ...", channels_str)
    print(f"Listening for signals: {channels_str} ... (Ctrl-C to stop)")
    await client.run_until_disconnected()


def main() -> int:
    asyncio.run(start_listener())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

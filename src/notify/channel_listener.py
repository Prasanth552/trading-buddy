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
CH2_MAX_LOSS = 4000  # ₹4,000 hard cap for CH2 trades
MAX_DAILY_LOSS = 10000  # ₹10,000 daily loss limit — stop trading after this
CH2_INDEX_ONLY = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"}

# CH2F (filtered) — optimized params from backtest
CH2F_ENABLED = True
CH2F_MAX_LOSS = 6000   # ₹6,000 SL cap (wider than ch2's ₹4K)
CH2F_PROFIT_FLOOR = 2000  # ₹2,000 floor (higher than ch2's ₹1.5K)
CH2F_PE_ONLY = True     # skip all CE signals
CH2F_SKIP_HOURS = {12, 13}  # skip 12:xx and 13:xx signals

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
OEH_LIST_TIME = "09:16"         # IST — early list using 1-min candle
OEH_MAX_TRADES = 5              # max trades per scan
OEH_SL_PCT = 0.30               # 30% of premium as stop-loss
OEH_TARGET_MULT = 2.0           # target = 2x entry premium
OEH_TOLERANCE = 0.05            # ₹0.05 tolerance for high <= open check
OEH_MIN_DROP_PCT = 0.3          # skip candidates with <0.3% drop (weak signal)
OEH_BLOCKLIST = {"GODREJCP", "GRASIM"}  # repeat losers — skip these

# ---------------------------------------------------------------------------
# OEL Scanner (Open=Low) — bullish counterpart to OEH
# ---------------------------------------------------------------------------
OEL_ENABLED = True
OEL_RUN_TIME = "09:20"
OEL_LIST_TIME = "09:16"
OEL_MAX_TRADES = 5
OEL_SL_PCT = 0.30
OEL_TARGET_MULT = 2.0
OEL_TOLERANCE = 0.05
OEL_MIN_RISE_PCT = 0.3
OEL_BLOCKLIST: set[str] = set()

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
OEL_UNIVERSE = OEH_UNIVERSE

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
        "BAJAJAUTO": "BAJAJ-AUTO",
        "BAJAJ AUTO": "BAJAJ-AUTO",
        "M&M": "M_M",
        "M&MFIN": "M_MFIN",
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

    if not candidates and strike >= 10000:
        alt_strike = strike / 10
        for inst in instruments:
            seg = inst.get("segment", "")
            if seg not in ("NSE_FO", "BSE_FO", "MCX_FO"):
                continue
            if inst.get("asset_symbol", "").upper() != sym:
                continue
            if inst.get("instrument_type") != option_type:
                continue
            if abs(float(inst.get("strike_price", -1)) - alt_strike) > 0.01:
                continue
            exp = _expiry_to_date(inst.get("expiry"))
            if exp is None or exp < today:
                continue
            candidates.append((exp, inst))
        if candidates:
            log.info("Strike fix: %s %s → %s (operator extra zero)", symbol, int(strike), int(alt_strike))

    # Nearest-strike fallback: snap to closest available strike (within 2%)
    if not candidates:
        search_strike = strike / 10 if strike >= 10000 else strike
        nearest = None
        nearest_dist = float("inf")
        for inst in instruments:
            seg = inst.get("segment", "")
            if seg not in ("NSE_FO", "BSE_FO", "MCX_FO"):
                continue
            if inst.get("asset_symbol", "").upper() != sym:
                continue
            if inst.get("instrument_type") != option_type:
                continue
            exp = _expiry_to_date(inst.get("expiry"))
            if exp is None or exp < today:
                continue
            inst_strike = float(inst.get("strike_price", -1))
            dist = abs(inst_strike - search_strike)
            if dist < nearest_dist:
                nearest_dist = dist
                nearest = (exp, inst, inst_strike)
        if nearest and nearest_dist <= search_strike * 0.02:
            candidates.append((nearest[0], nearest[1]))
            log.info("Strike snap: %s %s → %s (nearest, delta=%.0f)",
                     symbol, int(strike), int(nearest[2]), nearest_dist)

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
    if channel in ("oeh", "oel") and max_lots is not None:
        lots = max_lots
    elif channel in ("ch2", "ch2f", "ch3"):
        lots = 3 if is_index else 2
    elif channel in ("ch1", "ch1b"):
        lots = 2
    else:
        lots = 1
    if max_lots is not None and channel not in ("oeh", "oel"):
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
        "targets_remaining": ",".join(str(t) for t in sig.targets[1:]) if channel == "ch2" and len(sig.targets) > 1 else None,
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

_ch2_queued_signal: ParsedSignal | None = None
_ch2_queued_task: Any = None
_ch2_trigger_held: ParsedSignal | None = None
_ch2_last_is_above: bool = False
_ch2_last_executed: ParsedSignal | None = None
_ch2_last_reentry_ts: float = 0.0
_ch2_msg_signals: dict[int, ParsedSignal] = {}  # msg_id → signal for reply tracking
_ch2_msg_replies: dict[int, int] = {}  # msg_id → reply_to_msg_id (for chain walking)
_ch2_executed_roots: set[int] = set()  # root signal IDs already executed
_ch2_inst_to_root: dict[str, int] = {}  # instrument_key → root_id (fallback dedup)
_ch2_inst_last_exec_ts: dict[str, float] = {}  # instrument_key → epoch (cooldown)
_ch2_trigger_held_msg_id: int = 0
_ch2_trigger_held_is_fallback: bool = False
_ch2_trigger_held_is_reentry: bool = False
_ch2_reentry_origins: set[int] = set()
_ch2_buffer_start_id: int | None = None
_ch2_buffer_links: dict[int, int] = {}  # completion_msg_id → buffer_start_id (split-signal dedup)
CH2_INST_COOLDOWN = 10 * 60  # 10-min same-instrument cooldown

_RE_REENTRY = re.compile(
    r'(?:'
    r'(?:ABOVE|NEAR)\.?\s+(?:LAST\s+SWING\s+HIGH|HIGH|SAME\s+(?:RANGE|LEVEL)|THIS\s+LEVEL|(\d+))\s*'
    r'(?:AGAIN|NEW\s+(?:BUY|TRADE)|FOCUS|(?:U\s+(?:CAN\s+)?)?PLAN|ENTER|WITH\s+TIGHT|OPEN|ALSO\s+OPEN|TRY\s+WITH\s+TIGHT)'
    r'|SAME\s+(?:RANGE|LEVEL)\s+(?:AGAIN|OPEN|FOCUS)'
    r'|NEAR\s+SAME\s+(?:RANGE|LEVEL)'
    r'|ABOVE\.?\s+(\d+)\s+(?:NEW\s+(?:BUY|TRADE)|AGAIN|FOCUS|(?:U\s+(?:CAN\s+)?)?PLAN|WITH\s+TIGHT|THIS\s+LEVEL|KEEP\s+YOUR\s+EYES)'
    r'|ABOVE\s+(?:HIGH|LAST\s+SWING\s+HIGH|DAY\s+HIGH)\s+(?:AGAIN|FOCUS)'
    r'|ABOVE\s+(\d+)\s+(?:PE|CE)\s+SIDE'
    r'|(?:BELOW|BELWO)\s+(?:DAY\s+LOW|(\d+))\s+NEW\s+BUY'
    r'|SL\s+HIT\s*[,.]?\s*(?:REBUY|RE[\s-]*BUY|RE[\s-]*ENTER)\s+(?:FROM\s+)?(?:LOW\s+)?(?:NEAR\s+)?(\d+(?:\.\d+)?)?'
    r')',
    re.IGNORECASE,
)

_CH2_SYMBOL_RE = re.compile(
    r'(?:(?:Intra|positional|Note)[/\s]*)*'
    r'((?:BANK\s*NIFTY|NIFTY|SENSEX|FINNIFTY|MIDCPNIFTY|[A-Za-z&]{2,20}))'
    r'\s+(\d+)\s+(CE|PE)',
    re.IGNORECASE | re.MULTILINE,
)
_CH2_ENTRY_RE = re.compile(
    r'(?:ABOVE|ABO|NEAR|Entry\s+near|BUY\s*@|CMP)\s*[:\-]?\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)
_CH2_TGT_RE = re.compile(
    r'(?:TGT|TARGET)\s*[:\-]?\s*([\d\s,/.+\-l|]+)',
    re.IGNORECASE,
)
_CH2_SL_RE = re.compile(
    r'(?:^|[^A-Z])(?:SL|Stop\s*loss)\s*(?:bel\w*\s*|use\s*|just\s+below\s*)?(\d+(?:\.\d+)?(?:\s*[-–]\s*\d+)?)\s*(po(?:int)?)?',
    re.IGNORECASE,
)
_CH2_SKIP: set[str] = set()  # no skips — commodities enabled


def _ch2_inst_key(sig: ParsedSignal) -> str:
    return f"{sig.symbol.replace(' ', '').upper()} {int(sig.strike)} {sig.option_type}"


def _ch2_resolve_signal_via_chain(start_msg_id: int) -> ParsedSignal | None:
    """Walk reply chain upward until we find a msg_id registered in _ch2_msg_signals."""
    visited: set[int] = set()
    current = start_msg_id
    while current:
        if current in _ch2_msg_signals:
            return _ch2_msg_signals[current]
        if current in visited:
            break
        visited.add(current)
        parent_reply = _ch2_msg_replies.get(current)
        if parent_reply:
            current = parent_reply
        else:
            break
    return None


def _ch2_find_root_signal_id(msg_id: int) -> int:
    """Walk reply chain upward to find the topmost signal msg_id (root trade).

    Also follows buffer links: if a split signal was registered under both
    msg A (header) and msg B (TGT/SL), the buffer link B→A ensures both
    resolve to the same root.
    """
    visited: set[int] = set()
    current = _ch2_buffer_links.get(msg_id, msg_id)
    last_signal = current if current in _ch2_msg_signals else None
    while current:
        if current in visited:
            break
        visited.add(current)
        if current in _ch2_msg_signals:
            last_signal = current
        parent_reply = _ch2_msg_replies.get(current)
        if parent_reply:
            parent_reply = _ch2_buffer_links.get(parent_reply, parent_reply)
            current = parent_reply
            continue
        break
    if current in _ch2_msg_signals:
        last_signal = current
    return last_signal or msg_id


def _ch2_can_execute(sig: ParsedSignal, msg_id: int, origin_msg_id: int | None = None,
                     is_fallback: bool = False, own_root: bool = False) -> bool:
    """Check instrument cooldown + root-signal dedup. Returns True if trade can proceed.

    own_root=True skips reply-chain walk and uses msg_id directly as the root.
    Used for price-based re-entries which are genuinely new trades despite
    replying to an older signal.
    """
    if len(_ch2_msg_signals) > 500:
        _ch2_msg_signals.clear()
        _ch2_msg_replies.clear()
        _ch2_executed_roots.clear()
        _ch2_inst_to_root.clear()
        _ch2_inst_last_exec_ts.clear()
        log.info("[CH2] Cleared dedup state (>500 entries)")
    key = _ch2_inst_key(sig)
    now = _time.time()

    if key in _ch2_inst_last_exec_ts:
        elapsed = now - _ch2_inst_last_exec_ts[key]
        if elapsed < CH2_INST_COOLDOWN:
            log.info("[CH2] INST COOLDOWN: %s executed %.0fm ago (need %.0fm)",
                     key, elapsed / 60, CH2_INST_COOLDOWN / 60)
            return False

    root_id = msg_id if own_root else _ch2_find_root_signal_id(origin_msg_id or msg_id)

    if is_fallback and key in _ch2_inst_to_root:
        existing_root = _ch2_inst_to_root[key]
        if existing_root in _ch2_executed_roots:
            log.info("[CH2] FALLBACK DEDUP: %s → root=%d already executed", key, existing_root)
            return False

    if root_id in _ch2_executed_roots:
        log.info("[CH2] ROOT DEDUP: %s root=%d already executed", key, root_id)
        return False

    _ch2_executed_roots.add(root_id)
    _ch2_inst_to_root[key] = root_id
    _ch2_inst_last_exec_ts[key] = now
    return True


def _loss_cap_for_channel(ch: str) -> float:
    if ch == "ch2f":
        return CH2F_MAX_LOSS
    if ch == "ch2":
        return CH2_MAX_LOSS
    return MAX_LOSS_PER_TRADE


def _floor_for_channel(ch: str) -> float:
    if ch == "ch2f":
        return CH2F_PROFIT_FLOOR
    return PROFIT_TARGET


def _execute_and_notify(sig: ParsedSignal, channel: str, ch_label: str) -> None:
    """Execute a channel signal with filter scoring and notifications."""
    global _ch2_last_executed
    if channel == "ch2":
        _ch2_last_executed = sig
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

    if channel == "ch2" and CH2F_ENABLED:
        _maybe_execute_ch2f(sig)


def _maybe_execute_ch2f(sig: ParsedSignal) -> None:
    """Shadow-execute on ch2f if the signal passes optimized filters."""
    if CH2F_PE_ONLY and sig.option_type != "PE":
        log.info("[CH2F] SKIP CE signal: %s %s %s", sig.symbol, int(sig.strike), sig.option_type)
        return

    from src.utils.market_calendar import now_ist
    hr = now_ist().hour
    if hr in CH2F_SKIP_HOURS:
        log.info("[CH2F] SKIP hour %d signal: %s %s %s", hr, sig.symbol, int(sig.strike), sig.option_type)
        return

    log.info("[CH2F] Executing filtered signal: %s %s %s", sig.symbol, int(sig.strike), sig.option_type)
    result = execute_signal(sig, channel="ch2f")
    if result["placed"]:
        _notify(
            f"*[CH2F] Filtered order placed*\n"
            f"{result['symbol']} x{result['qty']}\n"
            f"Entry: {result['entry']} | SL: {result['sl']} | "
            f"Target: {result['target']} | Floor: ₹{CH2F_PROFIT_FLOOR}"
        )
        log.info("[CH2F] Order placed: %s", result)
    else:
        log.info("[CH2F] Not executed: %s", result["reason"])


def parse_signal_ch2(text: str) -> ParsedSignal | None:
    """Parse Channel 2 (Shrivastav G Prime) signal format.

    Handles single-message and split-message signals with a buffer.
    """
    global _ch2_pending, _ch2_pending_ts, _ch2_last_is_above
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

        has_above = bool(re.search(r'\bABO(?:VE)?\b', upper))

        if has_tgt and has_sl:
            targets = _ch2_extract_targets(tgt_match)
            sl = _ch2_extract_sl(sl_match, trigger)
            if sl <= 0 or not targets:
                return None

            is_swing = (
                ("POSITIONAL" in upper)
                or ("HOLD WITH PATIENCE" in upper)
                or (re.search(r'\bSWING\b', upper) and not re.search(r'SWING\s+HIGH|SWING\s+LOW', upper))
            )
            if is_swing and "INTRA" not in upper:
                return None

            _ch2_last_is_above = has_above
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
                "trigger": trigger, "is_above": has_above,
            }
            _ch2_pending_ts = _time.time()
            log.info("[CH2] Buffered partial signal: %s %s %s trigger=%.0f above=%s (waiting for TGT/SL)",
                     raw_sym, strike, opt_type, trigger, has_above)
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
                _ch2_last_is_above = _ch2_pending.get("is_above", False)
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
    raw = re.sub(r'\bll\b', '/', raw)
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
    if trigger > 0 and sl_val / trigger < 0.20:
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

        # Step 2: find ATM strike — auto-detect strike step from master
        strike_step = config.OPTION_SPECS.get(f"NSE:{sym_upper}", {}).get("strike_step", None)
        if strike_step is None:
            fo_strikes = sorted({
                float(i.get("strike_price", 0))
                for i in instruments
                if (i.get("asset_symbol") or "").upper() == sym_upper
                and i.get("segment") in ("NSE_FO", "BSE_FO")
                and i.get("instrument_type") in ("CE", "PE")
                and float(i.get("strike_price", 0)) > 0
            })
            if len(fo_strikes) >= 2:
                strike_step = min(fo_strikes[j+1] - fo_strikes[j] for j in range(min(10, len(fo_strikes)-1)))
            else:
                strike_step = 50
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
# OEH Early List — 1-min candle scan at 09:16, list only (no trades)
# ---------------------------------------------------------------------------
async def _run_oeh_list():
    """Scan OEH universe at 9:16 using the first 1-min candle, send list only."""
    import time as _t
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.broker.upstox_data import UpstoxData, load_cached_token

    IST = ZoneInfo(config.TIMEZONE)
    log.info("[OEH-LIST] Early scan starting (1-min candle)...")

    token = load_cached_token()
    if not token:
        log.error("[OEH-LIST] No Upstox token")
        _notify("*[OEH] Early list SKIPPED* — No Upstox token.")
        return

    try:
        ud = UpstoxData(access_token=token)
        master = ud._load_master()
    except Exception as exc:
        log.error("[OEH-LIST] Master load failed: %s", exc)
        _notify(f"[OEH] Early list error: {exc}")
        return

    eq_keys = {}
    for inst in master:
        if inst.get("segment") == "NSE_EQ":
            tsym = (inst.get("trading_symbol") or "").upper()
            if tsym:
                eq_keys[tsym] = inst.get("instrument_key")

    today = datetime.now(IST).date()
    from_dt = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=16)

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
            _t.sleep(0.3)
        except Exception as exc:
            err = str(exc)
            if "429" in err or "rate" in err.lower():
                _t.sleep(3)
                try:
                    candles = ud.historical_data(inst_key, from_dt, to_dt, "1minute")
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
            "close": entry_price,
            "high": max_high,
            "drop_pct": drop_pct,
        })

    candidates.sort(key=lambda x: x["drop_pct"], reverse=True)

    if not candidates:
        _notify(f"📋 *[OEH] No Open=High stocks at 9:16* (scanned {scanned})")
        log.info("[OEH-LIST] No candidates (scanned %d)", scanned)
        return

    lines = [f"📋 *[OEH] Open=High Stocks — {today.strftime('%d %b %Y')}*", ""]
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"{i}. *{c['symbol']}* — Open {c['open']:.2f}, "
            f"CMP {c['close']:.2f} (↓{c['drop_pct']:.1f}%)"
        )
    lines.append(f"\nScanned {scanned} stocks, {len(candidates)} OEH candidates")
    lines.append("_(1-min candle — early detection, verify at 9:20)_")

    msg = "\n".join(lines)
    _notify(msg)
    log.info("[OEH-LIST] Sent %d candidates:\n%s", len(candidates), msg)


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

        result = execute_signal(parsed, channel="oeh", max_lots=1)
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
# OEL Early List — 1-min candle scan at 09:16, list only (no trades)
# ---------------------------------------------------------------------------
async def _run_oel_list():
    """Scan OEL universe at 9:16 using the first 1-min candle, send list only."""
    import time as _t
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.broker.upstox_data import UpstoxData, load_cached_token

    IST = ZoneInfo(config.TIMEZONE)
    log.info("[OEL-LIST] Early scan starting (1-min candle)...")

    token = load_cached_token()
    if not token:
        log.error("[OEL-LIST] No Upstox token")
        _notify("*[OEL] Early list SKIPPED* — No Upstox token.")
        return

    try:
        ud = UpstoxData(access_token=token)
        master = ud._load_master()
    except Exception as exc:
        log.error("[OEL-LIST] Master load failed: %s", exc)
        _notify(f"[OEL] Early list error: {exc}")
        return

    eq_keys = {}
    for inst in master:
        if inst.get("segment") == "NSE_EQ":
            tsym = (inst.get("trading_symbol") or "").upper()
            if tsym:
                eq_keys[tsym] = inst.get("instrument_key")

    today = datetime.now(IST).date()
    from_dt = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(today, datetime.min.time()).replace(hour=9, minute=16)

    candidates = []
    scanned = 0

    for sym in OEL_UNIVERSE:
        if sym in OEL_BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if not inst_key:
            continue
        try:
            candles = ud.historical_data(inst_key, from_dt, to_dt, "1minute")
            _t.sleep(0.3)
        except Exception as exc:
            err = str(exc)
            if "429" in err or "rate" in err.lower():
                _t.sleep(3)
                try:
                    candles = ud.historical_data(inst_key, from_dt, to_dt, "1minute")
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

        min_low = candles[0]["low"]
        if min_low < open_price - OEL_TOLERANCE:
            continue

        entry_price = candles[0]["close"]
        rise_pct = (entry_price - open_price) / open_price * 100
        if rise_pct < OEL_MIN_RISE_PCT:
            continue

        candidates.append({
            "symbol": sym,
            "open": open_price,
            "close": entry_price,
            "low": min_low,
            "rise_pct": rise_pct,
        })

    candidates.sort(key=lambda x: x["rise_pct"], reverse=True)

    if not candidates:
        _notify(f"📋 *[OEL] No Open=Low stocks at 9:16* (scanned {scanned})")
        log.info("[OEL-LIST] No candidates (scanned %d)", scanned)
        return

    lines = [f"📋 *[OEL] Open=Low Stocks — {today.strftime('%d %b %Y')}*", ""]
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"{i}. *{c['symbol']}* — Open {c['open']:.2f}, "
            f"CMP {c['close']:.2f} (↑{c['rise_pct']:.1f}%)"
        )
    lines.append(f"\nScanned {scanned} stocks, {len(candidates)} OEL candidates")
    lines.append("_(1-min candle — early detection, verify at 9:20)_")

    msg = "\n".join(lines)
    _notify(msg)
    log.info("[OEL-LIST] Sent %d candidates:\n%s", len(candidates), msg)


# ---------------------------------------------------------------------------
# OEL Scanner — Open=Low bullish signal scanner
# ---------------------------------------------------------------------------
async def _run_oel_scan():
    """Scan F&O universe at 9:20 AM for OEL (Open=Low) stocks, buy CEs."""
    import time as _t
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.broker.upstox_data import UpstoxData, load_cached_token

    IST = ZoneInfo(config.TIMEZONE)
    log.info("[OEL] Scanner starting...")

    token = load_cached_token()
    if not token:
        log.error("[OEL] No valid Upstox token — scanner cannot run")
        _notify("*[OEL] Scanner SKIPPED* — No Upstox token for today.")
        return

    _notify("*[OEL] Scanning for Open=Low stocks...*")

    try:
        ud = UpstoxData(access_token=token)
        master = ud._load_master()
    except Exception as exc:
        log.error("[OEL] Failed to load instrument master: %s", exc)
        _notify(f"[OEL] Scanner error: {exc}")
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

    # NIFTY trend filter: log only (proceed regardless with floor strategy)
    nifty_key = config.UPSTOX_INDEX_KEYS.get("NSE:NIFTY 50")
    if nifty_key:
        try:
            nifty_candles = ud.historical_data(nifty_key, from_dt, to_dt, "5minute")
            if nifty_candles and len(nifty_candles) >= 1:
                nifty_open = nifty_candles[0]["open"]
                nifty_close = nifty_candles[0]["close"]
                if nifty_close < nifty_open:
                    log.info("[OEL] NIFTY is red (open=%.1f close=%.1f) — proceeding anyway (floor strategy)",
                             nifty_open, nifty_close)
                    _notify(f"[OEL] NIFTY is red at 9:20 "
                            f"(open={nifty_open:.1f} → {nifty_close:.1f}). "
                            f"Proceeding — floor strategy handles red days.")
                log.info("[OEL] NIFTY is green (open=%.1f close=%.1f) — proceeding with scan",
                         nifty_open, nifty_close)
        except Exception as exc:
            log.warning("[OEL] Could not fetch NIFTY data, proceeding anyway: %s", exc)

    candidates = []
    scanned = 0

    for sym in OEL_UNIVERSE:
        if sym in OEL_BLOCKLIST:
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

        min_low = candles[0]["low"]
        if min_low < open_price - OEL_TOLERANCE:
            continue

        entry_price = candles[0]["close"]
        rise_pct = (entry_price - open_price) / open_price * 100
        if rise_pct < OEL_MIN_RISE_PCT:
            continue

        candidates.append({
            "symbol": sym,
            "open": open_price,
            "entry": entry_price,
            "min_low": min_low,
            "rise_pct": rise_pct,
        })

    log.info("[OEL] Scanned %d stocks, found %d OEL candidates", scanned, len(candidates))

    if not candidates:
        _notify(f"[OEL] No OEL candidates found today (scanned {scanned} stocks)")
        return

    candidates.sort(key=lambda x: x["rise_pct"], reverse=True)
    top = candidates[:OEL_MAX_TRADES]

    summary_lines = []
    executed = 0

    for c in top:
        parsed = _resolve_atm_strike(c["symbol"], "CE")
        if parsed is None:
            summary_lines.append(f"SKIP {c['symbol']} CE — could not resolve ATM")
            continue

        parsed.stop_loss = round(parsed.trigger_price * (1 - OEL_SL_PCT), 2)
        parsed.targets = [round(parsed.trigger_price * OEL_TARGET_MULT, 2)]

        result = execute_signal(parsed, channel="oel", max_lots=1)
        if result["placed"]:
            executed += 1
            summary_lines.append(
                f"BUY {result['symbol']} x{result['qty']} @ {result['entry']:.2f} "
                f"(OEL rise={c['rise_pct']:.1f}%)"
            )
            _notify(
                f"*[OEL] Trade placed*\n"
                f"{result['symbol']} x{result['qty']}\n"
                f"Entry: {result['entry']} | SL: {result['sl']} | Target: {result['target']}\n"
                f"Signal: {c['symbol']} Open={c['open']:.2f} Low={c['min_low']:.2f} "
                f"(rise {c['rise_pct']:.1f}% in 15min)"
            )
        else:
            summary_lines.append(f"FAIL {c['symbol']} CE — {result['reason']}")

    summary = "\n".join(summary_lines)
    log.info("[OEL] Scan done: %d/%d executed\n%s", executed, len(top), summary)
    _notify(
        f"*[OEL] Scan complete: {executed}/{len(top)} trades placed*\n"
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
        ("oel", "OEL Scanner"),
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
            if ch_key in ("oeh", "oel"):
                ch_filter = f"channel='{ch_key}'"
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
    from src.utils import market_calendar as mc

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
                        "SELECT id, symbol, price, qty, stop_price, target_price, "
                        "broker_key, channel, targets_remaining "
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
                        remaining = trade["targets_remaining"] or ""
                        if remaining:
                            next_tgts = [float(t) for t in remaining.split(",") if t.strip()]
                            new_sl = trade["target_price"]
                            if next_tgts:
                                new_tgt = next_tgts.pop(0)
                                new_remaining = ",".join(str(t) for t in next_tgts) if next_tgts else None
                                with db.get_conn() as conn:
                                    conn.execute(
                                        "UPDATE trades SET stop_price=?, target_price=?, "
                                        "targets_remaining=? WHERE id=?",
                                        (new_sl, new_tgt, new_remaining, tid))
                                ch = trade["channel"] or "ch1"
                                log.info("[%s] TGT TRAIL %s: SL→%.0f TGT→%.0f remaining=%s",
                                         ch.upper(), trade["symbol"], new_sl, new_tgt, new_remaining)
                                _notify(f"🎯 *TGT hit* — {trade['symbol']}\n"
                                        f"SL trailed → {new_sl} | Next TGT → {new_tgt}")
                            else:
                                log.info("ALL TGT hit for %s: LTP=%.2f",
                                         trade["symbol"], ltp)
                                _close_trade_by_id(tid, ltp, "all_tgt_hit")
                                _peak_net.pop(tid, None)
                        else:
                            log.info("CHANNEL TARGET hit for %s: LTP=%.2f >= target=%.2f net=₹%.0f",
                                     trade["symbol"], ltp, trade["target_price"], net_pnl)
                            _close_trade_by_id(tid, ltp, "target_hit")
                            _peak_net.pop(tid, None)
                    elif trade["stop_price"] and ltp <= trade["stop_price"]:
                        log.info("SL HIT for %s: LTP=%.2f <= SL=%.2f",
                                 trade["symbol"], ltp, trade["stop_price"])
                        _close_trade_by_id(tid, ltp, "sl_hit")
                        _peak_net.pop(tid, None)
                    elif net_pnl <= -(_loss_cap_for_channel(trade["channel"] or "ch1")):
                        loss_cap = _loss_cap_for_channel(trade["channel"] or "ch1")
                        log.warning("MAX LOSS CAP for %s: net_pnl=₹%.0f hit -₹%d cap. Force closing.",
                                    trade["symbol"], net_pnl, loss_cap)
                        _close_trade_by_id(tid, ltp, "max_loss_cap")
                        _peak_net.pop(tid, None)
                        _notify(
                            f"🛑 *MAX LOSS CAP* — {trade['symbol']}\n"
                            f"Loss hit ₹{abs(net_pnl):,.0f} (cap: ₹{loss_cap:,})\n"
                            f"Auto-closed to protect capital."
                        )
                    elif _peak_net[tid] >= _floor_for_channel(trade["channel"] or "ch1") and net_pnl <= _floor_for_channel(trade["channel"] or "ch1"):
                        floor_val = _floor_for_channel(trade["channel"] or "ch1")
                        log.info("FLOOR EXIT for %s: peak=₹%.0f dipped to ₹%.0f (floor=₹%d)",
                                 trade["symbol"], _peak_net[tid], net_pnl, floor_val)
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
            if first_run and mc.is_trading_day():
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

            if not mc.is_trading_day():
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

            if first_run and mc.is_trading_day():
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

            if not mc.is_trading_day():
                log.info("[OEH] Not a trading day, skipping scan")
                continue

            await _run_oeh_scan()

    asyncio.get_event_loop().create_task(_oeh_scheduler())
    log.info("OEH scanner started — runs daily at %s IST", OEH_RUN_TIME)

    # --- OEH Early List: send stock list at 09:16 IST ---
    async def _oeh_list_scheduler():
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta
        IST = ZoneInfo(config.TIMEZONE)

        h, m = map(int, OEH_LIST_TIME.split(":"))

        while True:
            now = datetime.now(IST)
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)

            wait_secs = (target - now).total_seconds()
            log.info("[OEH-LIST] Next early list at %s IST (in %.0f min)",
                     target.strftime("%Y-%m-%d %H:%M"), wait_secs / 60)
            await asyncio.sleep(wait_secs)

            if not mc.is_trading_day():
                log.info("[OEH-LIST] Not a trading day, skipping")
                continue

            await _run_oeh_list()

    asyncio.get_event_loop().create_task(_oeh_list_scheduler())
    log.info("OEH early list scheduler started — runs daily at %s IST", OEH_LIST_TIME)

    # --- OEL Scanner: run once daily at OEL_RUN_TIME ---
    async def _oel_scheduler():
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta
        IST = ZoneInfo(config.TIMEZONE)

        h, m = map(int, OEL_RUN_TIME.split(":"))
        first_run = True

        while True:
            if not OEL_ENABLED:
                await asyncio.sleep(60)
                continue

            now = datetime.now(IST)

            if first_run and mc.is_trading_day():
                first_run = False
                scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if now > scheduled and now.hour < 15:
                    log.info("[OEL] Missed scheduled %s run — catching up now", OEL_RUN_TIME)
                    await _run_oel_scan()
                    continue
            first_run = False

            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)

            wait_secs = (target - now).total_seconds()
            log.info("[OEL] Next scan at %s IST (in %.0f min)",
                     target.strftime("%Y-%m-%d %H:%M"), wait_secs / 60)
            await asyncio.sleep(wait_secs)

            if not mc.is_trading_day():
                log.info("[OEL] Not a trading day, skipping scan")
                continue

            await _run_oel_scan()

    asyncio.get_event_loop().create_task(_oel_scheduler())
    log.info("OEL scanner started — runs daily at %s IST", OEL_RUN_TIME)

    # --- OEL Early List: send stock list at 09:16 IST ---
    async def _oel_list_scheduler():
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta
        IST = ZoneInfo(config.TIMEZONE)

        h, m = map(int, OEL_LIST_TIME.split(":"))

        while True:
            now = datetime.now(IST)
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)

            wait_secs = (target - now).total_seconds()
            log.info("[OEL-LIST] Next early list at %s IST (in %.0f min)",
                     target.strftime("%Y-%m-%d %H:%M"), wait_secs / 60)
            await asyncio.sleep(wait_secs)

            if not mc.is_trading_day():
                log.info("[OEL-LIST] Not a trading day, skipping")
                continue

            await _run_oel_list()

    asyncio.get_event_loop().create_task(_oel_list_scheduler())
    log.info("OEL early list scheduler started — runs daily at %s IST", OEL_LIST_TIME)

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

            if not mc.is_trading_day():
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
        global _ch2_queued_signal, _ch2_queued_task, _ch2_trigger_held, _ch2_last_executed, _ch2_last_reentry_ts
        global _ch2_trigger_held_msg_id, _ch2_trigger_held_is_fallback, _ch2_trigger_held_is_reentry
        global _ch2_buffer_start_id, _ch2_reentry_origins

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

        # --- CH2: handle control messages before parsing ---
        if channel == "ch2":
            # Track reply chains for chain-walking
            if event.message.reply_to and event.message.reply_to.reply_to_msg_id:
                _ch2_msg_replies[event.message.id] = event.message.reply_to.reply_to_msg_id

            upper_ctl = text.strip().upper()

            # "WAIT FOR TRIGGER" — hold the queued signal, don't execute yet
            if re.search(r'WAIT\s+FOR\s+TRIGGER', upper_ctl):
                if _ch2_queued_task and _ch2_queued_signal:
                    _ch2_queued_task.cancel()
                    _ch2_trigger_held = _ch2_queued_signal
                    _ch2_trigger_held_msg_id = event.message.id
                    _ch2_trigger_held_is_fallback = False
                    _ch2_trigger_held_is_reentry = False
                    _ch2_queued_signal = None
                    _ch2_queued_task = None
                    log.info("[CH2] WAIT FOR TRIGGER — held: %s %s %s",
                             _ch2_trigger_held.symbol, int(_ch2_trigger_held.strike),
                             _ch2_trigger_held.option_type)
                    _notify(f"[CH2] Signal held — waiting for trigger:\n"
                            f"{_ch2_trigger_held.symbol} {int(_ch2_trigger_held.strike)} "
                            f"{_ch2_trigger_held.option_type}")
                else:
                    log.info("[CH2] WAIT FOR TRIGGER — no queued signal to hold")
                return

            # STRIKE CORRECTION: "It's XXXXX CE/PE"
            corr_m = re.search(r"IT'?S\s+(\d+)\s+(CE|PE)", upper_ctl)
            if corr_m and len(text.strip()) < 30:
                new_strike = float(corr_m.group(1))
                new_opt = corr_m.group(2)
                if _ch2_trigger_held:
                    old = _ch2_trigger_held
                    _ch2_trigger_held = ParsedSignal(
                        action=old.action, symbol=old.symbol,
                        strike=new_strike, option_type=new_opt,
                        trigger_price=old.trigger_price,
                        stop_loss=old.stop_loss, targets=old.targets,
                    )
                    _ch2_msg_signals[event.message.id] = _ch2_trigger_held
                    log.info("[CH2] STRIKE CORRECTION: %s %s %s → %s %s",
                             old.symbol, int(old.strike), old.option_type,
                             int(new_strike), new_opt)
                elif _ch2_queued_signal:
                    old = _ch2_queued_signal
                    _ch2_queued_signal = ParsedSignal(
                        action=old.action, symbol=old.symbol,
                        strike=new_strike, option_type=new_opt,
                        trigger_price=old.trigger_price,
                        stop_loss=old.stop_loss, targets=old.targets,
                    )
                    _ch2_msg_signals[event.message.id] = _ch2_queued_signal
                    log.info("[CH2] STRIKE CORRECTION: %s %s %s → %s %s",
                             old.symbol, int(old.strike), old.option_type,
                             int(new_strike), new_opt)
                return

            # "Active"/"Actt" — execute held signal OR reply-to signal
            clean_ctl = re.sub(r'[\U0001F600-\U0001FAFF☀-➿❤️‍\s]+', '', text).strip()
            if (re.search(r'\bACTIVE\b|\bACTT\b', upper_ctl)
                    and not re.search(r'NOT\s+ACTIVE', upper_ctl)
                    and len(clean_ctl) < 15):
                act_sig = None
                act_origin = event.message.id
                act_is_fallback = False
                # Try reply chain first
                if event.message.reply_to and event.message.reply_to.reply_to_msg_id:
                    act_sig = _ch2_resolve_signal_via_chain(event.message.reply_to.reply_to_msg_id)
                    if act_sig:
                        act_origin = event.message.id
                        log.info("[CH2] ACTIVE via chain from #%d: %s %s %s",
                                 event.message.reply_to.reply_to_msg_id,
                                 act_sig.symbol, int(act_sig.strike), act_sig.option_type)
                # Check if reply chain resolved through a re-entry origin
                act_is_reentry = bool(
                    event.message.reply_to
                    and event.message.reply_to.reply_to_msg_id in _ch2_reentry_origins
                ) if act_sig else False
                # Fall back to trigger_held
                if not act_sig and _ch2_trigger_held:
                    act_sig = _ch2_trigger_held
                    act_origin = _ch2_trigger_held_msg_id or event.message.id
                    act_is_fallback = _ch2_trigger_held_is_fallback
                    act_is_reentry = _ch2_trigger_held_is_reentry
                if act_sig:
                    _ch2_trigger_held = None
                    _ch2_trigger_held_is_reentry = False
                    if _ch2_can_execute(act_sig, event.message.id,
                                        origin_msg_id=act_origin, is_fallback=act_is_fallback,
                                        own_root=act_is_reentry):
                        log.info("[CH2] ACTIVE — executing: %s %s %s",
                                 act_sig.symbol, int(act_sig.strike), act_sig.option_type)
                        _notify(f"[CH2] Trigger ACTIVE — executing:\n"
                                f"{act_sig.symbol} {int(act_sig.strike)} {act_sig.option_type}")
                        _execute_and_notify(act_sig, channel, ch_label)
                    _ch2_msg_signals[event.message.id] = act_sig
                return

            # "Focus" as reply — hold the replied-to signal for later activation
            if (re.search(r'\bFOCUS\b', upper_ctl) and len(clean_ctl) < 15
                    and event.message.reply_to and event.message.reply_to.reply_to_msg_id):
                ref_sig = _ch2_resolve_signal_via_chain(event.message.reply_to.reply_to_msg_id)
                if ref_sig:
                    _ch2_trigger_held = ref_sig
                    _ch2_trigger_held_msg_id = event.message.id
                    _ch2_trigger_held_is_fallback = False
                    _ch2_trigger_held_is_reentry = False
                    _ch2_msg_signals[event.message.id] = ref_sig
                    trade_sym = f"{ref_sig.symbol} {int(ref_sig.strike)} {ref_sig.option_type}"
                    log.info("[CH2] FOCUS via chain — held: %s", trade_sym)
                    _notify(f"[CH2] Focus (held): {trade_sym}")
                    return

            # "Avoid" as reply — cancel the referenced signal
            if (re.search(r'\bAVOID\b', upper_ctl) and len(clean_ctl) < 15
                    and event.message.reply_to and event.message.reply_to.reply_to_msg_id):
                ref_sig = _ch2_resolve_signal_via_chain(event.message.reply_to.reply_to_msg_id)
                if ref_sig:
                    trade_sym = f"{ref_sig.symbol} {int(ref_sig.strike)} {ref_sig.option_type}"
                    if _ch2_trigger_held and _ch2_inst_key(_ch2_trigger_held) == _ch2_inst_key(ref_sig):
                        _ch2_trigger_held = None
                    log.info("[CH2] AVOID via chain — skipping: %s", trade_sym)
                    _notify(f"[CH2] Avoid (cancelled): {trade_sym}")
                    return

            # "Not active avoid" — cancel queued or held signal
            if re.search(r'NOT\s+ACTIVE', upper_ctl):
                cancelled = []
                if _ch2_queued_task:
                    _ch2_queued_task.cancel()
                    if _ch2_queued_signal:
                        cancelled.append(f"{_ch2_queued_signal.symbol} "
                                         f"{int(_ch2_queued_signal.strike)} "
                                         f"{_ch2_queued_signal.option_type}")
                    _ch2_queued_signal = None
                    _ch2_queued_task = None
                if _ch2_trigger_held:
                    cancelled.append(f"{_ch2_trigger_held.symbol} "
                                     f"{int(_ch2_trigger_held.strike)} "
                                     f"{_ch2_trigger_held.option_type}")
                    _ch2_trigger_held = None
                if cancelled:
                    log.info("[CH2] NOT ACTIVE — cancelled: %s", cancelled)
                    _notify(f"[CH2] Signal cancelled (not active): {', '.join(cancelled)}")
                else:
                    log.info("[CH2] NOT ACTIVE — nothing queued to cancel")
                return

            # Re-entry: "Above X again/focus/new buy/plan", "same range again",
            # "Above last swing high", "Above X pe/ce side", "Below day low" etc.
            reentry_m = _RE_REENTRY.search(upper_ctl)
            if reentry_m:
                last = None
                is_fallback = False
                has_reply = bool(event.message.reply_to and event.message.reply_to.reply_to_msg_id)
                if has_reply:
                    last = _ch2_resolve_signal_via_chain(event.message.reply_to.reply_to_msg_id)
                    if last:
                        log.info("[CH2] RE-ENTRY via chain from #%d",
                                 event.message.reply_to.reply_to_msg_id)
                if not last and not has_reply:
                    last = _ch2_last_executed
                    is_fallback = True
                    if last:
                        log.info("[CH2] RE-ENTRY FALLBACK to last_executed: %s %s",
                                 last.symbol, int(last.strike))
                if not last:
                    return
                now_ts = _time.time()
                if now_ts - _ch2_last_reentry_ts < 60:
                    log.info("[CH2] RE-ENTRY skipped (duplicate within 60s)")
                    return
                new_entry = last.trigger_price
                for g in reentry_m.groups():
                    if g:
                        val = float(g)
                        if val < 1000:
                            new_entry = val
                        break
                side_m = re.search(r'(CE|PE)\s+SIDE', upper_ctl)
                opt_type = side_m.group(1) if side_m else last.option_type
                max_tgt = max(last.targets) if last.targets else 0
                if new_entry > max_tgt * 1.5 and max_tgt > 0:
                    log.info("[CH2] RE-ENTRY SKIP: entry=%.0f > max TGT=%.0f × 1.5 (wrong instrument)",
                             new_entry, max_tgt)
                    return
                sl_ratio = last.stop_loss / last.trigger_price if last.trigger_price > 0 else 0.90
                re_sig = ParsedSignal(
                    action="BUY",
                    symbol=last.symbol,
                    strike=last.strike,
                    option_type=opt_type,
                    trigger_price=new_entry,
                    stop_loss=round(new_entry * sl_ratio),
                    targets=last.targets,
                )
                trade_sym = f"{last.symbol} {int(last.strike)} {opt_type}"
                _ch2_last_reentry_ts = now_ts
                _ch2_msg_signals[event.message.id] = re_sig
                _ch2_reentry_origins.add(event.message.id)
                has_above = bool(re.search(r'\bABO(?:VE)?\b', upper_ctl))
                # A re-entry with a specific numeric price different from the
                # original trigger is a real entry instruction ("Near 320 try
                # with tight sl").  Execute it like a NEAR signal.
                # Vague guidance without a new price ("Above high again focus",
                # "Same level again") only sets trigger_held for ACTIVE.
                has_new_price = (new_entry != last.trigger_price)
                if has_new_price and not has_above:
                    if _ch2_can_execute(re_sig, event.message.id,
                                        origin_msg_id=event.message.id,
                                        is_fallback=is_fallback, own_root=True):
                        log.info("[CH2] RE-ENTRY executing (new price %.0f): %s @ %.0f SL=%.0f",
                                 new_entry, trade_sym, new_entry, re_sig.stop_loss)
                        _notify(f"[CH2] Re-entry executing (price {new_entry}):\n"
                                f"{trade_sym} @ {new_entry} SL={re_sig.stop_loss}")
                        _execute_and_notify(re_sig, channel, ch_label)
                    else:
                        log.info("[CH2] RE-ENTRY blocked by dedup: %s", trade_sym)
                else:
                    _ch2_trigger_held = re_sig
                    _ch2_trigger_held_msg_id = event.message.id
                    _ch2_trigger_held_is_fallback = is_fallback
                    _ch2_trigger_held_is_reentry = True
                    log.info("[CH2] RE-ENTRY held (%s%s): %s @ %.0f SL=%.0f",
                             "ABOVE" if has_above else "GUIDANCE",
                             " FALLBACK" if is_fallback else "", trade_sym, new_entry, re_sig.stop_loss)
                    _notify(f"[CH2] Re-entry held ({('ABOVE' if has_above else 'guidance')}):\n"
                            f"{trade_sym} @ {new_entry} SL={re_sig.stop_loss}\n"
                            f"Waiting for Active...")
                return

            # Re-entry via reply with "AGAIN" — parse original signal, execute
            if (event.message.reply_to and event.message.reply_to.reply_to_msg_id
                    and re.search(r'\bAGAIN\b', upper_ctl)):
                try:
                    reply_id = event.message.reply_to.reply_to_msg_id
                    ref_sig = _ch2_resolve_signal_via_chain(reply_id)
                    if not ref_sig:
                        orig_msg = await client.get_messages(event.chat_id, ids=reply_id)
                        if orig_msg and orig_msg.text:
                            ref_sig = parse_signal_ch2(orig_msg.text)
                    if ref_sig:
                        trade_sym = f"{ref_sig.symbol} {int(ref_sig.strike)} {ref_sig.option_type}"
                        reply_sig = parse_signal_ch2(text)
                        if reply_sig and reply_sig.stop_loss and reply_sig.targets:
                            ref_sig = reply_sig
                        if _ch2_can_execute(ref_sig, event.message.id,
                                            origin_msg_id=event.message.id):
                            log.info("[CH2] AGAIN executing: %s SL=%.1f TGT=%.1f",
                                     trade_sym, ref_sig.stop_loss, ref_sig.targets[0])
                            _notify(f"*[CH2] Re-entry executing:*\n{trade_sym}\n"
                                    f"SL: {ref_sig.stop_loss} | TGT: {ref_sig.targets[0]}")
                            _execute_and_notify(ref_sig, channel, ch_label)
                        else:
                            log.info("[CH2] AGAIN blocked by dedup: %s", trade_sym)
                        _ch2_msg_signals[event.message.id] = ref_sig
                        return
                except Exception as exc:
                    log.warning("[CH2] Re-entry parse failed: %s", exc)

        # --- Parse signal ---
        # --- Parse signal ---
        if channel == "ch3":
            sig = parse_signal_ch3(text)
        elif channel == "ch2":
            had_pending = _ch2_pending is not None
            sig = parse_signal_ch2(text)
            if not had_pending and _ch2_pending is not None:
                _ch2_buffer_start_id = event.message.id
                log.info("[CH2] Buffer start: msg %d", event.message.id)
        else:
            sig = parse_signal(text)

        if sig is not None:
            log.info("[%s] Parsed signal: %s %s %s %s trigger=%.2f SL=%.2f targets=%s",
                     ch_label, sig.action, sig.symbol, sig.strike, sig.option_type,
                     sig.trigger_price, sig.stop_loss, sig.targets)

            if channel == "ch2":
                # If this completed a split buffer, register under the first msg too
                completed_buffer_start = None
                if had_pending and _ch2_pending is None and _ch2_buffer_start_id:
                    _ch2_msg_signals[_ch2_buffer_start_id] = sig
                    completed_buffer_start = _ch2_buffer_start_id
                    _ch2_buffer_links[event.message.id] = _ch2_buffer_start_id
                    _ch2_buffer_start_id = None
                    log.info("[CH2] Buffer complete: registered under both %d and %d",
                             completed_buffer_start, event.message.id)

                _ch2_msg_signals[event.message.id] = sig
                is_above = bool(re.search(r'\bABO(?:VE)?\b', text, re.I)) or _ch2_last_is_above
                if is_above:
                    _ch2_trigger_held = sig
                    _ch2_trigger_held_msg_id = completed_buffer_start or event.message.id
                    _ch2_trigger_held_is_fallback = False
                    _ch2_trigger_held_is_reentry = False
                    _ch2_queued_signal = None
                    if _ch2_queued_task:
                        _ch2_queued_task.cancel()
                        _ch2_queued_task = None
                    log.info("[CH2] ABOVE signal — auto-held until Active: %s %s %s trigger=%.0f",
                             sig.symbol, int(sig.strike), sig.option_type, sig.trigger_price)
                    _notify(f"[CH2] Signal held (ABOVE trigger):\n"
                            f"{sig.symbol} {int(sig.strike)} {sig.option_type} ABOVE {sig.trigger_price}\n"
                            f"SL: {sig.stop_loss} | TGT: {sig.targets[0]}\n"
                            f"Waiting for Active...")
                    return

                # NEAR / immediate signal — queue with short delay for cancel protection
                if _ch2_queued_task:
                    _ch2_queued_task.cancel()
                _ch2_queued_signal = sig
                _frozen_ch = channel
                _frozen_lbl = ch_label
                _frozen_msg_id = event.message.id

                async def _ch2_delayed_exec():
                    global _ch2_queued_signal, _ch2_queued_task
                    await asyncio.sleep(5)
                    if _ch2_queued_signal is sig:
                        _ch2_queued_signal = None
                        _ch2_queued_task = None
                        if _ch2_can_execute(sig, _frozen_msg_id, origin_msg_id=_frozen_msg_id):
                            _execute_and_notify(sig, _frozen_ch, _frozen_lbl)
                        else:
                            log.info("[CH2] NEAR exec blocked by dedup: %s %s %s",
                                     sig.symbol, int(sig.strike), sig.option_type)

                _ch2_queued_task = asyncio.create_task(_ch2_delayed_exec())
                log.info("[CH2] NEAR signal queued (5s): %s %s %s trigger=%.0f",
                         sig.symbol, int(sig.strike), sig.option_type, sig.trigger_price)
                _notify(f"[CH2] Signal detected:\n"
                        f"{sig.symbol} {int(sig.strike)} {sig.option_type} @ {sig.trigger_price}\n"
                        f"SL: {sig.stop_loss} | TGT: {sig.targets[0]}")
                return

            # --- Non-CH2: execute immediately ---
            _execute_and_notify(sig, channel, ch_label)
            return

        # --- Follow-up: "close near cost" → exit the tagged trade at current LTP ---
        action, exit_price = _classify_followup(text)
        if action == "book":
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

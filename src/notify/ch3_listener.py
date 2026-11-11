"""Standalone CH3 (BullRun Premium) listener — isolated from the main channel listener.

Listens to a single Telegram channel (BullRun Premium) and executes trades
independently. Code changes to the main listener cannot affect CH3.

Run:  python -m src.notify.ch3_listener
Env:  TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, SIGNAL_CHANNEL3_ID
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

log = get_logger("ch3_listener")


# ---------------------------------------------------------------------------
# Shared types & helpers
# ---------------------------------------------------------------------------
@dataclass
class ParsedSignal:
    action: str          # BUY / SELL
    symbol: str          # e.g. "SENSEX"
    strike: float
    option_type: str     # CE / PE
    trigger_price: float
    stop_loss: float
    targets: list[float]
    entry_low: float = 0.0
    entry_high: float = 0.0


def calc_charges(entry_price: float, exit_price: float, qty: int) -> dict[str, float]:
    buy_turnover = entry_price * qty
    sell_turnover = exit_price * qty
    total_turnover = buy_turnover + sell_turnover
    brokerage = 40.0
    stt = sell_turnover * 0.001
    exchange_txn = total_turnover * 0.000495
    sebi = total_turnover * 0.000001
    stamp_duty = buy_turnover * 0.00003
    gst = (brokerage + exchange_txn) * 0.18
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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MAX_LOSS_PER_TRADE = 8000
MAX_DAILY_LOSS_CH3 = 25000
SLIPPAGE_PCT = 0.005

_CH3_FLAG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", ".ch3_enabled")


def _ch3_is_enabled() -> bool:
    try:
        return open(_CH3_FLAG_FILE).read().strip() != "0"
    except FileNotFoundError:
        return True


# ---------------------------------------------------------------------------
# Notifications (non-blocking, best-effort)
# ---------------------------------------------------------------------------
def _notify(msg: str) -> None:
    import threading

    def _send():
        try:
            from src.notify.telegram_bot import TelegramNotifier
            TelegramNotifier().send_message(msg)
        except Exception:
            log.warning("Could not send Telegram notification: %s", msg[:80])

    threading.Thread(target=_send, daemon=True).start()


# ---------------------------------------------------------------------------
# Signal parser
# ---------------------------------------------------------------------------
def parse_signal_ch3(text: str) -> ParsedSignal | None:
    """Parse BullRun Premium format.

    Signal: SENSEX 76900 CE ABOVE 540 - 45  (shorthand: 540-545)
    Targets: TARGET 580 - 620 - 660++
    SL: SL 500
    """
    clean = text.replace("**", "").replace("__", "").replace("*", "").strip()
    clean = re.sub(r'[^\x00-\x7F]+', ' ', clean).strip()
    clean = re.sub(r'\s+', ' ', clean)

    if len(clean) < 10:
        return None

    upper = clean.upper()

    if any(kw in upper for kw in (
        "TRIGGERED", "TARGET ACHIEVED", "TARGETS ACHIEVED", "DONE FOR THE DAY",
        "ADD WATCHLIST", "WATCHLIST", "PREMIUM PERFORMANCE",
    )):
        return None

    sym_m = re.search(r'(SENSEX|NIFTY|BANKNIFTY|BANK\s*NIFTY)\s+(\d+)\s*(CE|PE)', upper)
    if not sym_m:
        return None

    symbol_raw = sym_m.group(1).replace(" ", "")
    strike = float(sym_m.group(2))
    option_type = sym_m.group(3)

    entry_m = re.search(r'(?:BUY\s*)?ABOVE\s*[:\s]*(\d+)\s*[-–]\s*(\d+)', upper)
    if not entry_m:
        return None

    entry_low = int(entry_m.group(1))
    eh_raw = int(entry_m.group(2))
    if eh_raw < entry_low:
        entry_high = int(str(entry_low)[:-len(str(eh_raw))] + str(eh_raw))
    else:
        entry_high = eh_raw

    trigger = (entry_low + entry_high) / 2.0

    sl_m = re.search(r'(?:STOP\s*LOSS|SL)\s*[:\s]*(\d+)', upper)
    sl = int(sl_m.group(1)) if sl_m else round(trigger * 0.70, 2)

    tgt_m = re.search(r'(?:TARGET|TARGETS?)\s*[:\s]*([\d\s/\-+]+)', upper)
    targets = []
    if tgt_m:
        targets = [int(t) for t in re.findall(r'\d+', tgt_m.group(1))]

    if not targets:
        targets = [round(trigger + 40), round(trigger + 80), round(trigger + 120)]

    return ParsedSignal(
        action="BUY",
        symbol=symbol_raw,
        strike=strike,
        option_type=option_type,
        trigger_price=round(trigger, 2),
        stop_loss=sl,
        targets=targets,
        entry_low=float(entry_low),
        entry_high=float(entry_high),
    )


# ---------------------------------------------------------------------------
# Follow-up classifier
# ---------------------------------------------------------------------------
_RE_CLOSE_NEAR_COST = re.compile(r'(?:CLOSE|CUT|EXIT)\s+(?:NEAR|AT)\s+COST|COST\s+TO\s+COST', re.I)
_RE_FULL_EXIT = re.compile(r'(?:CLOSE|CUT|EXIT)\s+(?:ALL|FULL|POSITION|NOW|IMMEDIATELY)', re.I)
_RE_BARE_SL = re.compile(r'^\s*SL\s*$', re.I)
_RE_BARE_CUT = re.compile(r'^\s*(?:CUT|EXIT|CLOSE)\s*$', re.I)
# "Book partial" / "book or trail" / "target achieved" are NOT exit signals —
# we trade 1 lot so partial booking is impossible; trailing SL handles the rest.
_RE_PARTIAL_IGNORE = re.compile(r'BOOK\s+PARTIAL|TRAIL\s+(?:THE\s+)?REMAIN|TARGET\s+ACHIEVED', re.I)


def _classify_followup(text: str) -> tuple[str, float | None]:
    text = text.strip()
    # Explicitly ignore partial-book / target-achieved messages
    if _RE_PARTIAL_IGNORE.search(text):
        return "unknown", None
    if _RE_CLOSE_NEAR_COST.search(text):
        return "book", None
    if _RE_FULL_EXIT.search(text):
        return "book", None
    if _RE_BARE_SL.search(text):
        return "book", None
    if _RE_BARE_CUT.search(text):
        return "book", None
    return "unknown", None


# ---------------------------------------------------------------------------
# Option resolution (reuse from main client)
# ---------------------------------------------------------------------------
def _resolve_channel_option(uc, symbol: str, strike: float, option_type: str):
    """Search the Upstox instrument master for a specific option contract."""
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    from src.broker.upstox_client import _expiry_to_date

    IST = ZoneInfo(config.TIMEZONE)
    today = datetime.now(IST).date()
    sym = symbol.replace(" ", "").upper()
    instruments = uc.load_instruments()

    candidates = []
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
        return None, 1

    candidates.sort(key=lambda x: x[0])
    chosen = candidates[0][1]
    lot_size = int(chosen.get("lot_size", 1)) or 1
    return chosen, lot_size


# ---------------------------------------------------------------------------
# Today's realised PnL for CH3
# ---------------------------------------------------------------------------
def _todays_realised_pnl() -> float:
    from src.storage import db
    from src.utils.market_calendar import now_ist
    today = now_ist().strftime("%Y-%m-%d")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total FROM trades "
            "WHERE status LIKE 'CLOSED%%' AND pnl IS NOT NULL AND ts LIKE ? AND channel='ch3'",
            (f"{today}%",),
        ).fetchone()
    return row["total"] if row else 0


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------
def execute_signal(sig: ParsedSignal, *, filter_score: int | None = None) -> dict[str, Any]:
    from src.broker.upstox_client import UpstoxClient
    from src.storage import db

    db.init_db()

    ch_pnl = _todays_realised_pnl()
    if ch_pnl <= -MAX_DAILY_LOSS_CH3:
        log.warning("DAILY LOSS LIMIT [CH3]: P&L ₹%.0f (limit -₹%d). Skipping.", ch_pnl, MAX_DAILY_LOSS_CH3)
        _notify(f"⛔ *DAILY LOSS LIMIT [CH3]*\nP&L: ₹{ch_pnl:+,.0f} (limit: -₹{MAX_DAILY_LOSS_CH3:,})\n"
                f"Skipping: {sig.symbol} {int(sig.strike)} {sig.option_type}")
        return {"placed": False, "reason": f"Daily loss limit [ch3]: ₹{ch_pnl:+,.0f}"}

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
        opt, master_lot_size = _resolve_channel_option(uc, sig.symbol, sig.strike, sig.option_type)
    except Exception as exc:
        return {"placed": False, "reason": f"Option resolution failed: {exc}"}

    if opt is None:
        return {"placed": False, "reason": f"Could not resolve {sig.symbol} {sig.strike} {sig.option_type}"}

    lot_key = sig.symbol.replace(" ", "").upper()
    lot_size = config.LOT_SIZES.get(lot_key, master_lot_size)
    qty = 1 * lot_size  # CH3 always 1 lot

    instrument_token = opt.get("instrument_key") or opt.get("instrument_token", "")

    # Fetch live LTP
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
                log.info("Live LTP for %s: %.2f (signal: %.2f)", instrument_token, entry_price, sig.trigger_price)
                break
    except Exception as exc:
        log.warning("LTP fetch failed, using signal price: %s", exc)

    entry_price = round(entry_price * (1 + SLIPPAGE_PCT), 2)

    # Reject if LTP is too far above the entry range (e.g. entered late)
    if sig.entry_high > 0:
        max_entry = sig.entry_high * 1.15
        if entry_price > max_entry:
            log.warning("ENTRY REJECTED: LTP %.2f > %.2f (entry_high %.2f + 15%%)", entry_price, max_entry, sig.entry_high)
            _notify(f"⚠️ *ENTRY REJECTED*\nLTP ₹{entry_price:.2f} too far above entry range {sig.entry_low}-{sig.entry_high}\n{trade_symbol}")
            return {"placed": False, "reason": f"LTP {entry_price} above max entry {max_entry:.2f}"}
    elif sig.trigger_price > 0:
        ratio = entry_price / sig.trigger_price
        if ratio > 1.20 or ratio < 0.25:
            log.warning("ENTRY REJECTED: LTP %.2f is %.1fx signal %.2f", entry_price, ratio, sig.trigger_price)
            return {"placed": False, "reason": f"LTP {entry_price} too far from signal {sig.trigger_price}"}

    # SL cap
    sl_from_entry = entry_price - (MAX_LOSS_PER_TRADE / qty) if qty > 0 else sig.stop_loss
    actual_sl = round(max(sig.stop_loss, sl_from_entry), 2)

    trade_row = {
        "ts": __import__("src.utils.market_calendar", fromlist=["now_ist"]).now_ist().isoformat(timespec="seconds"),
        "symbol": trade_symbol,
        "side": "BUY",
        "qty": qty,
        "price": entry_price,
        "stop_price": actual_sl,
        "target_price": sig.targets[0] if sig.targets else 0,
        "broker_key": instrument_token,
        "mode": config.MODE,
        "status": "OPEN",
        "is_hedge": False,
        "index_entry": sig.strike,
        "pnl": None,
        "exit_price": None,
        "channel": "ch3",
        "filter_score": filter_score,
        "targets_remaining": ",".join(str(t) for t in sig.targets[1:]) if len(sig.targets) > 1 else None,
    }

    try:
        # CH3 is always LIVE — bypass UPSTOX_SIMULATE_ORDERS
        old_sim = getattr(config, "UPSTOX_SIMULATE_ORDERS", False)
        config.UPSTOX_SIMULATE_ORDERS = False
        try:
            result = uc.place_order(
                instrument_token=instrument_token,
                quantity=qty,
                transaction_type="BUY",
                price=0,
                trigger_price=0,
                order_type="MARKET",
            )
            order_id = (result.get("order_ids") or ["?"])[0]
        finally:
            config.UPSTOX_SIMULATE_ORDERS = old_sim
    except Exception as exc:
        return {"placed": False, "reason": f"Order failed: {exc}"}

    trade_id = db.insert_trade(trade_row)

    return {
        "placed": True,
        "trade_id": trade_id,
        "order_id": order_id,
        "symbol": trade_symbol,
        "qty": qty,
        "entry": entry_price,
        "sl": sig.stop_loss,
        "target": sig.targets[0] if sig.targets else 0,
        "broker_key": instrument_token,
    }


# ---------------------------------------------------------------------------
# Close trade (with live sell order)
# ---------------------------------------------------------------------------
_peak_net: dict[int, float] = {}


def _close_trade_by_id(trade_id: int, exit_price: float, reason: str) -> None:
    try:
        exit_price = round(exit_price * (1 - SLIPPAGE_PCT), 2)

        from src.storage import db
        db.init_db()
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT id, price, qty, symbol, channel, broker_key FROM trades WHERE id = ? AND status = 'OPEN'",
                (trade_id,),
            ).fetchone()
            if not row:
                return

            # Place real sell order
            if row["broker_key"]:
                try:
                    from src.broker.upstox_client import UpstoxClient
                    _uc = UpstoxClient()
                    old_sim = getattr(config, "UPSTOX_SIMULATE_ORDERS", False)
                    config.UPSTOX_SIMULATE_ORDERS = False
                    try:
                        _uc.place_order(
                            instrument_token=row["broker_key"],
                            quantity=row["qty"],
                            transaction_type="SELL",
                            order_type="MARKET",
                            price=0,
                            trigger_price=0,
                        )
                        log.info("[LIVE-EXIT] Sell order placed for trade %d (%s)", trade_id, row["symbol"])
                    finally:
                        config.UPSTOX_SIMULATE_ORDERS = old_sim
                except Exception as sell_exc:
                    log.error("[LIVE-EXIT] Failed to place sell order for trade %d: %s", trade_id, sell_exc)

            entry_price = row["price"]
            gross_pnl = (exit_price - entry_price) * row["qty"]
            charges = calc_charges(entry_price, exit_price, row["qty"])
            net_pnl = gross_pnl - charges["total"]
            status = "CLOSED" if reason != "sl_hit" else "CLOSED_SL"
            peak = _peak_net.get(row["id"])

            conn.execute(
                "UPDATE trades SET status = ?, exit_price = ?, pnl = ?, charges = ?, peak_price = ? WHERE id = ?",
                (status, exit_price, net_pnl, charges["total"], peak, row["id"]),
            )

            log.info(
                "AUTO-CLOSE (id=%d) %s: entry=%.2f exit=%.2f gross=%.2f charges=%.2f net=%.2f reason=%s",
                row["id"], row["symbol"], entry_price, exit_price, gross_pnl,
                charges["total"], net_pnl, reason,
            )
            _notify(
                f"*Auto-close: {reason}*\n"
                f"{row['symbol']}\n"
                f"Entry: {entry_price} | Exit: {exit_price}\n"
                f"Gross: {gross_pnl:+.0f} | Charges: {charges['total']:.0f} | Net: {net_pnl:+.0f}"
            )
    except Exception as exc:
        log.error("Failed to close trade %d: %s", trade_id, exc)


# ---------------------------------------------------------------------------
# Handle follow-up (BOOK / CLOSE)
# ---------------------------------------------------------------------------
def _handle_followup(exit_price: float | None, original_sig: ParsedSignal | None = None) -> None:
    from src.storage import db
    from src.broker.upstox_data import UpstoxData
    db.init_db()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, symbol, price, qty, broker_key FROM trades "
            "WHERE status = 'OPEN' AND channel = 'ch3' ORDER BY id DESC"
        ).fetchall()

    if not rows:
        log.info("[CH3] Close instruction but no open trades.")
        _notify("[CH3] Received close instruction but no open CH3 trades.")
        return

    target_trade = None
    if original_sig:
        match_sym = f"{original_sig.symbol} {int(original_sig.strike)} {original_sig.option_type}"
        for trade in rows:
            if trade["symbol"].upper() == match_sym.upper():
                target_trade = trade
                break
        if not target_trade:
            log.info("[CH3] Reply-to trade already closed, skipping.")
            _notify("[CH3] Close skipped: replied-to trade already closed.")
            return
    else:
        target_trade = rows[0]

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
                _notify(f"[CH3] Close failed: no LTP for {target_trade['symbol']}")
                return
        except Exception:
            _notify("[CH3] Close failed: LTP fetch error")
            return
    else:
        _notify(f"[CH3] Close failed: no broker key for {target_trade['symbol']}")
        return

    _close_trade_by_id(target_trade["id"], ltp, "channel_exit")
    log.info("[CH3] Close executed: %s at %.2f", target_trade["symbol"], ltp)
    _notify(f"[CH3] Close executed: {target_trade['symbol']} at {ltp}")


# ---------------------------------------------------------------------------
# Position monitor (CH3 trades only)
# ---------------------------------------------------------------------------
async def _monitor_positions():
    from src.storage import db
    fail_count = 0
    while True:
        await asyncio.sleep(1)
        try:
            db.init_db()
            with db.get_conn() as conn:
                rows = conn.execute(
                    "SELECT id, symbol, price, qty, stop_price, target_price, "
                    "broker_key, channel, targets_remaining, peak_price, ts "
                    "FROM trades WHERE status = 'OPEN' AND channel = 'ch3' AND broker_key IS NOT NULL"
                ).fetchall()
            if not rows:
                continue

            keys = {r["broker_key"]: r for r in rows}
            from src.broker.upstox_data import UpstoxData
            ud = UpstoxData()
            ltp_data = ud._get("/v2/market-quote/ltp",
                               params={"instrument_key": ",".join(keys)}).get("data", {})

            if not ltp_data:
                fail_count += 1
                if fail_count % 60 == 0:
                    log.warning("[CH3] Monitor: LTP API empty for %d trades", len(rows))
                continue

            fail_count = 0

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

                prev_peak = _peak_net.get(tid)
                if prev_peak is None:
                    prev_peak = trade["peak_price"] or 0
                _peak_net[tid] = max(prev_peak, net_pnl)

                if _peak_net[tid] > prev_peak:
                    with db.get_conn() as conn:
                        conn.execute("UPDATE trades SET peak_price = ? WHERE id = ?",
                                     (_peak_net[tid], tid))

                # Max loss cap
                if net_pnl <= -MAX_LOSS_PER_TRADE:
                    log.warning("MAX LOSS CAP for %s: net=₹%.0f hit -₹%d", trade["symbol"], net_pnl, MAX_LOSS_PER_TRADE)
                    _close_trade_by_id(tid, ltp, "max_loss_cap")
                    _peak_net.pop(tid, None)
                    _notify(f"🛑 *MAX LOSS CAP* — {trade['symbol']}\nLoss: ₹{abs(net_pnl):,.0f} (cap: ₹{MAX_LOSS_PER_TRADE:,})")

                # Target trailing
                elif trade["target_price"] and ltp >= trade["target_price"]:
                    remaining = trade["targets_remaining"] or ""
                    if remaining:
                        next_tgts = [float(t) for t in remaining.split(",") if t.strip()]
                        new_sl = trade["target_price"]
                        if next_tgts:
                            new_tgt = next_tgts.pop(0)
                            new_remaining = ",".join(str(t) for t in next_tgts) if next_tgts else None
                            with db.get_conn() as conn:
                                conn.execute(
                                    "UPDATE trades SET stop_price=?, target_price=?, targets_remaining=? WHERE id=?",
                                    (new_sl, new_tgt, new_remaining, tid))
                            log.info("[CH3] TGT TRAIL %s: SL→%.0f TGT→%.0f remaining=%s",
                                     trade["symbol"], new_sl, new_tgt, new_remaining)
                            _notify(f"🎯 *TGT hit* — {trade['symbol']}\nSL trailed → {new_sl} | Next TGT → {new_tgt}")
                        else:
                            log.info("[CH3] ALL TGT hit for %s: LTP=%.2f", trade["symbol"], ltp)
                            _close_trade_by_id(tid, ltp, "all_tgt_hit")
                            _peak_net.pop(tid, None)
                    else:
                        log.info("[CH3] TARGET hit for %s: LTP=%.2f >= target=%.2f",
                                 trade["symbol"], ltp, trade["target_price"])
                        _close_trade_by_id(tid, ltp, "target_hit")
                        _peak_net.pop(tid, None)

                # SL hit
                elif trade["stop_price"] and ltp <= trade["stop_price"]:
                    log.info("[CH3] SL HIT for %s: LTP=%.2f <= SL=%.2f net=₹%.0f",
                             trade["symbol"], ltp, trade["stop_price"], net_pnl)
                    _close_trade_by_id(tid, ltp, "sl_hit")
                    _peak_net.pop(tid, None)

                # No profit floor for CH3 — targets handle exit

        except Exception as exc:
            fail_count += 1
            if fail_count % 60 == 0:
                log.error("[CH3] Monitor error (repeated %dx): %s", fail_count, exc)
            else:
                log.warning("[CH3] Monitor error: %s", exc)


# ---------------------------------------------------------------------------
# Channel ID normalization
# ---------------------------------------------------------------------------
def _normalize_channel_id(raw: str | int) -> int | None:
    try:
        raw_id = int(str(raw).strip())
        if raw_id > 0:
            return int(f"-100{raw_id}")
        elif not str(raw_id).startswith("-100"):
            return int(f"-100{abs(raw_id)}")
        return raw_id
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Main listener
# ---------------------------------------------------------------------------
async def start_listener() -> None:
    from telethon import TelegramClient, events

    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    phone = os.getenv("TELEGRAM_PHONE", "")
    ch3_id = os.getenv("SIGNAL_CHANNEL3_ID", "")

    if not all([api_id, api_hash, phone, ch3_id]):
        log.error("Missing env vars: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, SIGNAL_CHANNEL3_ID")
        return

    ch3_int = _normalize_channel_id(ch3_id)
    if not ch3_int:
        log.error("Invalid SIGNAL_CHANNEL3_ID: %s", ch3_id)
        return

    session_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "telegram_ch3.session")
    client = TelegramClient(session_path, api_id, api_hash)

    _telegram_connected = False
    try:
        await client.start(phone=phone)
        me = await client.get_me()
        log.info("Logged in as %s (id=%s)", me.first_name, me.id)
        _notify(f"CH3 listener started as {me.first_name}")
        _telegram_connected = True
        try:
            await client.get_entity(ch3_int)
            log.info("Resolved entity for CH3 channel %s", ch3_int)
        except Exception as err:
            log.warning("Could not resolve CH3 entity %s: %s", ch3_int, err)
    except Exception as err:
        log.error("Telegram connection failed: %s", err)
        return

    # Start position monitor
    asyncio.get_event_loop().create_task(_monitor_positions())
    log.info("[CH3] Position monitor started (check every 1s)")

    @client.on(events.NewMessage(chats=[ch3_int] if _telegram_connected else []))
    async def handler(event):
        text = event.message.text or ""
        if not text.strip():
            return

        log.info("[CH3] Channel message: %s", text[:120])

        # Check enable flag
        if not _ch3_is_enabled():
            log.info("[CH3] Disabled — ignoring signal")
            return

        # Try parsing as entry signal
        sig = parse_signal_ch3(text)

        if sig is not None:
            log.info("[CH3] Parsed signal: %s %s %s trigger=%.2f SL=%.2f targets=%s",
                     sig.symbol, sig.strike, sig.option_type,
                     sig.trigger_price, sig.stop_loss, sig.targets)

            # Smart filter
            filt = None
            filter_score = None
            try:
                from src.signals.smart_filter import evaluate_signal
                filt = evaluate_signal(sig.symbol, sig.option_type, "ch3", sig.trigger_price)
                filter_score = filt.score
                log.info("[CH3] FILTER: score=%d action=%s reasons=%s", filt.score, filt.action, filt.reasons)
            except Exception as exc:
                log.warning("[CH3] Smart filter failed: %s", exc)

            filter_line = f"Filter: {filt.action} (score {filt.score}/100)" if filt else "Filter: unavailable"
            reason_lines = "\n".join(f"  • {r}" for r in filt.reasons[:5]) if filt else ""
            filter_tag = ""
            if filt and filt.action == "SKIP":
                filter_tag = " (filter: SKIP)"
            elif filt and filt.action == "REDUCE":
                filter_tag = " (filter: REDUCE)"

            _notify(
                f"*[CH3] Signal received — executing{filter_tag}*\n"
                f"{sig.action} {sig.symbol} {int(sig.strike)} {sig.option_type}\n"
                f"Entry ABOVE {sig.trigger_price} | SL: {sig.stop_loss} | "
                f"Targets: {', '.join(str(t) for t in sig.targets)}\n"
                f"{filter_line}\n{reason_lines}"
            )

            result = execute_signal(sig, filter_score=filter_score)
            if result["placed"]:
                _notify(
                    f"*[CH3] Order placed*\n"
                    f"{result['symbol']} x{result['qty']}\n"
                    f"Entry: {result['entry']} | SL: {result['sl']} | "
                    f"Target: {result['target']}\n"
                    f"{filter_line}"
                )
                log.info("[CH3] Order placed: %s", result)
            else:
                _notify(f"[CH3] Signal not executed: {result['reason']}")
                log.warning("[CH3] Signal not executed: %s", result["reason"])
            return

        # Follow-up: close/book/exit
        action, exit_price = _classify_followup(text)
        if action == "book":
            original_sig = None
            if event.message.reply_to and event.message.reply_to.reply_to_msg_id:
                try:
                    orig_msg = await client.get_messages(event.chat_id, ids=event.message.reply_to.reply_to_msg_id)
                    if orig_msg and orig_msg.text:
                        original_sig = parse_signal_ch3(orig_msg.text)
                except Exception as exc:
                    log.warning("[CH3] Could not fetch replied-to message: %s", exc)
            log.info("[CH3] Close instruction detected")
            _handle_followup(exit_price, original_sig)
            return

        log.info("[CH3] Not an entry signal or follow-up, skipping.")

    log.info("Listening for CH3 signals: ch3=%s ...", ch3_int)
    print(f"Listening for CH3 signals: ch3={ch3_int} ... (Ctrl-C to stop)")
    await client.run_until_disconnected()


def main():
    asyncio.run(start_listener())


if __name__ == "__main__":
    main()

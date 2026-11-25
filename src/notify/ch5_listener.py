"""Standalone CH5 (Premium Index F&O) listener — isolated from CH3/CH4.

Listens to Telegram channel -1003576786253 on the VIP account (+918903212575)
and executes index option trades independently.

Run:  python -m src.notify.ch5_listener
Env:  TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_VIP_PHONE, SIGNAL_CHANNEL5_ID
"""
from __future__ import annotations

import asyncio
import os
import re
import time as _time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import config
from src.utils.logging import get_logger

log = get_logger("ch5_listener")

_signal_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ch5-exec")


def _watchdog_ping():
    """Ping the systemd watchdog (no-op if not running under systemd)."""
    import socket
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        if addr[0] == "@":
            addr = "\0" + addr[1:]
        s.sendto(b"WATCHDOG=1", addr)
        s.close()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Shared types & helpers
# ---------------------------------------------------------------------------
@dataclass
class ParsedSignal:
    action: str          # BUY / SELL
    symbol: str          # e.g. "SENSEX", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"
    strike: float
    option_type: str     # CE / PE
    trigger_price: float
    stop_loss: float
    targets: list[float]
    is_hero_zero: bool = False
    is_btst: bool = False
    expiry_date: str | None = None  # "YYYY-MM-DD" if channel specifies expiry


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
MAX_DAILY_LOSS_CH5 = 25000
SLIPPAGE_PCT = 0.005

_CH5_FLAG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", ".ch5_enabled")


def _ch5_is_enabled() -> bool:
    try:
        return open(_CH5_FLAG_FILE).read().strip() != "0"
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
# Signal parser — Premium Index F&O format
# ---------------------------------------------------------------------------
def parse_signal_ch5(text: str) -> ParsedSignal | None:
    """Parse Premium Index F&O format.

    Signal:
        BUY SENSEX 83300 CE ABOVE 575
        TARGET :- 625 / 700
        SL :- 450
        26 NOVEMBER EXPIRY

    Also handles HERO-ZERO and BTST prefixes.
    Indices: SENSEX, NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY
    """
    clean = text.replace("**", "").replace("__", "").replace("*", "").strip()
    clean = re.sub(r'[^\x00-\x7F]+', ' ', clean).strip()
    clean = re.sub(r'\s+', ' ', clean)

    if len(clean) < 10:
        return None

    upper = clean.upper()

    # Skip result / performance messages
    if any(kw in upper for kw in (
        "TARGET ACHIEVED", "ALL TARGET", "POINTS GAINS", "BOOK PROFIT",
        "PREMIUM PERFORMANCE", "HIT IN", "EASY", "GAINING",
    )):
        return None

    # Detect HERO-ZERO and BTST
    is_hero_zero = "HERO" in upper and "ZERO" in upper
    is_btst = "BTST" in upper

    # Match index + strike + option type
    sym_m = re.search(
        r'(SENSEX|NIFTY|BANKNIFTY|BANK\s*NIFTY|FINNIFTY|MIDCPNIFTY|MIDCP\s*NIFTY)\s+(\d+)\s*(CE|PE)',
        upper,
    )
    if not sym_m:
        return None

    symbol_raw = sym_m.group(1).replace(" ", "")
    strike = float(sym_m.group(2))
    option_type = sym_m.group(3)

    # Entry trigger: "ABOVE 575" or "ABOVE     540" (extra spaces)
    entry_m = re.search(r'ABOVE\s+(\d+)', upper)
    if not entry_m:
        return None

    trigger = float(entry_m.group(1))

    # SL: "SL :- 450" or "SL :- 0" (hero zero)
    sl_m = re.search(r'SL\s*[:\-–\s]+\s*(\d+)', upper)
    sl = float(sl_m.group(1)) if sl_m else round(trigger * 0.70, 2)

    # Targets: "TARGET :- 625 / 700" or "TARGET :- 240 / 280"
    tgt_m = re.search(r'TARGET\s*[:\-–\s]+\s*([\d\s/,\-+]+)', upper)
    targets: list[float] = []
    if tgt_m:
        targets = [float(t) for t in re.findall(r'\d+', tgt_m.group(1))]

    if not targets:
        targets = [round(trigger + 40), round(trigger + 80)]

    # Parse expiry: "26 NOVEMBER EXPIRY" or "3 DEC EXPIRY" or "26 NOV"
    expiry_date = None
    _MONTHS = {
        "JAN": 1, "JANUARY": 1, "FEB": 2, "FEBRUARY": 2, "MAR": 3, "MARCH": 3,
        "APR": 4, "APRIL": 4, "MAY": 5, "JUN": 6, "JUNE": 6,
        "JUL": 7, "JULY": 7, "AUG": 8, "AUGUST": 8, "SEP": 9, "SEPTEMBER": 9,
        "OCT": 10, "OCTOBER": 10, "NOV": 11, "NOVEMBER": 11, "DEC": 12, "DECEMBER": 12,
    }
    exp_m = re.search(
        r'(\d{1,2})\s+(JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUNE?'
        r'|JULY?|AUG(?:UST)?|SEP(?:TEMBER)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)',
        upper,
    )
    if exp_m:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day = int(exp_m.group(1))
        month = _MONTHS.get(exp_m.group(2))
        if month:
            year = datetime.now(ZoneInfo(config.TIMEZONE)).year
            try:
                from datetime import date as _date
                exp_d = _date(year, month, day)
                if exp_d < datetime.now(ZoneInfo(config.TIMEZONE)).date():
                    exp_d = _date(year + 1, month, day)
                expiry_date = exp_d.isoformat()
            except ValueError:
                pass

    # Skip hero-zero signals (SL=0, tiny premium — too risky for auto-trading)
    if is_hero_zero:
        log.info("[CH5] Hero-zero signal skipped: %s %s %s", symbol_raw, strike, option_type)
        return None

    return ParsedSignal(
        action="BUY",
        symbol=symbol_raw,
        strike=strike,
        option_type=option_type,
        trigger_price=trigger,
        stop_loss=sl,
        targets=targets,
        is_hero_zero=is_hero_zero,
        is_btst=is_btst,
        expiry_date=expiry_date,
    )


# ---------------------------------------------------------------------------
# Follow-up classifier
# ---------------------------------------------------------------------------
_RE_CLOSE_NEAR_COST = re.compile(r'(?:CLOSE|CUT|EXIT)\s+(?:NEAR|AT)\s+COST|COST\s+TO\s+COST', re.I)
_RE_FULL_EXIT = re.compile(r'(?:CLOSE|CUT|EXIT)\s+(?:ALL|FULL|POSITION|NOW|IMMEDIATELY)', re.I)
_RE_BARE_SL = re.compile(r'^\s*SL\s*$', re.I)
_RE_BARE_CUT = re.compile(r'^\s*(?:CUT|EXIT|CLOSE)\s*$', re.I)
_RE_PARTIAL_IGNORE = re.compile(r'BOOK\s+PARTIAL|TRAIL\s+(?:THE\s+)?REMAIN|TARGET\s+ACHIEVED', re.I)


def _classify_followup(text: str) -> tuple[str, float | None]:
    text = text.strip()
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
_option_cache: dict[str, tuple] = {}
_instrument_index: dict[str, list[tuple]] = {}
_instrument_index_date: str | None = None


def _build_instrument_index(instruments: list) -> None:
    global _instrument_index, _instrument_index_date
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from src.broker.upstox_client import _expiry_to_date

    IST = ZoneInfo(config.TIMEZONE)
    today = datetime.now(IST).date()
    today_str = today.isoformat()

    if _instrument_index_date == today_str and _instrument_index:
        return

    idx: dict[str, list[tuple]] = {}
    for inst in instruments:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO"):
            continue
        sym = (inst.get("asset_symbol") or "").upper()
        ot = inst.get("instrument_type", "")
        sp = inst.get("strike_price")
        if not sym or not ot or sp is None:
            continue
        exp = _expiry_to_date(inst.get("expiry"))
        if exp is None or exp < today:
            continue
        key = f"{sym}|{float(sp)}|{ot}"
        idx.setdefault(key, []).append((exp, inst))

    for v in idx.values():
        v.sort(key=lambda x: x[0])

    _instrument_index = idx
    _instrument_index_date = today_str
    log.info("Instrument index built: %d keys", len(idx))


def _resolve_channel_option(uc, symbol: str, strike: float, option_type: str,
                            expiry_date: str | None = None):
    cache_key = f"{symbol}|{strike}|{option_type}|{expiry_date or ''}"
    if cache_key in _option_cache:
        log.info("Option cache hit: %s", cache_key)
        return _option_cache[cache_key]

    instruments = uc.load_instruments()
    _build_instrument_index(instruments)

    sym = symbol.replace(" ", "").upper()
    idx_key = f"{sym}|{strike}|{option_type}"
    candidates = _instrument_index.get(idx_key, [])

    if not candidates:
        return None, 1

    chosen = None
    if expiry_date:
        from datetime import date as _date
        target_exp = _date.fromisoformat(expiry_date)
        for exp, inst in candidates:
            if exp == target_exp:
                chosen = inst
                break
        if chosen:
            log.info("Matched exact expiry %s for %s", expiry_date, idx_key)
        else:
            log.warning("Expiry %s not found for %s, falling back to nearest", expiry_date, idx_key)

    if chosen is None:
        chosen = candidates[0][1]

    lot_size = int(chosen.get("lot_size", 1)) or 1
    _option_cache[cache_key] = (chosen, lot_size)
    return chosen, lot_size


# ---------------------------------------------------------------------------
# Today's realised PnL for CH5
# ---------------------------------------------------------------------------
def _todays_realised_pnl() -> float:
    from src.storage import db
    from src.utils.market_calendar import now_ist
    db.init_db()
    today = now_ist().date().isoformat()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS total "
            "FROM trades WHERE channel = 'ch5' AND date(ts) = ? AND status IN ('CLOSED', 'CLOSED_SL')",
            (today,),
        ).fetchone()
    return row["total"] if row else 0.0


# ---------------------------------------------------------------------------
# Execute signal
# ---------------------------------------------------------------------------
def execute_signal(sig: ParsedSignal, filter_score: int | None = None) -> dict[str, Any]:
    t0 = _time.monotonic()
    from src.storage import db
    db.init_db()

    ch_pnl = _todays_realised_pnl()
    if ch_pnl <= -MAX_DAILY_LOSS_CH5:
        log.warning("DAILY LOSS LIMIT [CH5]: P&L %.0f (limit -%d). Skipping.", ch_pnl, MAX_DAILY_LOSS_CH5)
        _notify(
            f"⛔ *DAILY LOSS LIMIT [CH5]*\nP&L: ₹{ch_pnl:+,.0f} (limit: -₹{MAX_DAILY_LOSS_CH5:,})\n"
            f"Skipping: {sig.symbol} {int(sig.strike)} {sig.option_type}"
        )
        return {"placed": False, "reason": "Daily loss limit [ch5]: " + str(ch_pnl)}

    trade_symbol = f"{sig.symbol} {int(sig.strike)} {sig.option_type}"
    with db.get_conn() as conn:
        dup = conn.execute(
            "SELECT id FROM trades WHERE symbol = ? AND status = 'OPEN' LIMIT 1",
            (trade_symbol,),
        ).fetchone()
    if dup:
        log.info("Duplicate signal skipped — %s already OPEN (id=%d)", trade_symbol, dup["id"])
        return {"placed": False, "reason": "Duplicate: " + trade_symbol + " already open"}

    risk_per_unit = sig.trigger_price - sig.stop_loss
    if risk_per_unit <= 0:
        return {"placed": False, "reason": "SL >= trigger price"}

    t1 = _time.monotonic()
    log.info("[CH5] TIMING: pre-checks %.1fs", t1 - t0)

    try:
        from src.broker.upstox_client import UpstoxClient
        uc = UpstoxClient()
        t2 = _time.monotonic()
        log.info("[CH5] TIMING: UpstoxClient init %.1fs", t2 - t1)
        opt, master_lot_size = _resolve_channel_option(uc, sig.symbol, sig.strike, sig.option_type,
                                                        sig.expiry_date)
        t3 = _time.monotonic()
        log.info("[CH5] TIMING: resolve_option %.1fs", t3 - t2)
    except Exception as exc:
        return {"placed": False, "reason": "Option resolution failed: " + str(exc)}

    if opt is None:
        return {"placed": False, "reason": "Could not resolve " + trade_symbol}

    lot_key = sig.symbol.replace(" ", "").upper()
    lot_size = config.LOT_SIZES.get(lot_key, master_lot_size)
    qty = 1 * lot_size  # CH5: 1 lot

    instrument_token = opt.get("instrument_key") or opt.get("instrument_token", "")

    # Fetch live LTP
    from src.broker.upstox_data import UpstoxData

    def _fetch_ltp() -> float:
        try:
            ud = UpstoxData()
            ltp_data = ud._get("/v2/market-quote/ltp",
                               params={"instrument_key": instrument_token}).get("data", {})
            for item in ltp_data.values():
                lp = item.get("last_price")
                if lp and lp > 0:
                    return float(lp)
        except Exception as exc:
            log.warning("LTP fetch failed: %s", exc)
        return 0.0

    t4 = _time.monotonic()
    entry_price = _fetch_ltp() or sig.trigger_price
    log.info("Live LTP for %s: %.2f (signal: %.2f) [fetch %.1fs, total %.1fs]",
             instrument_token, entry_price, sig.trigger_price, _time.monotonic() - t4, _time.monotonic() - t0)

    # Wait for trigger: if LTP < trigger, poll every 1s for up to 3 min
    if entry_price < sig.trigger_price:
        log.info("[CH5] WAITING for trigger: LTP %.2f < ABOVE %.0f — polling every 1s (max 3 min)",
                 entry_price, sig.trigger_price)
        _notify(f"⏳ *Waiting for trigger*\nLTP ₹{entry_price:.2f} < ABOVE {sig.trigger_price:.0f}\n{trade_symbol}")
        deadline = _time.time() + 180
        while _time.time() < deadline:
            _time.sleep(1)
            entry_price = _fetch_ltp()
            if entry_price <= 0:
                continue
            if entry_price >= sig.trigger_price:
                log.info("[CH5] TRIGGER HIT: LTP %.2f >= %.0f — entering now", entry_price, sig.trigger_price)
                break
        else:
            log.warning("[CH5] Trigger wait expired (3 min): LTP %.2f < %.0f", entry_price, sig.trigger_price)
            _notify(f"⚠️ *Signal expired* — trigger {sig.trigger_price:.0f} not hit in 3 min\n{trade_symbol}")
            return {"placed": False, "reason": "Trigger not hit in 3 min (LTP " + str(entry_price) + ")"}

    entry_price = round(entry_price * (1 + SLIPPAGE_PCT), 2)

    # Reject if price too far past trigger (must have >=50% of trigger->T1 range left)
    max_entry = sig.targets[0] if sig.targets else 0
    if max_entry > 0 and sig.trigger_price > 0:
        midpoint = sig.trigger_price + (max_entry - sig.trigger_price) * 0.5
        if entry_price > midpoint:
            log.warning("ENTRY REJECTED: LTP %.2f > midpoint %.2f (trigger=%.0f T1=%.0f)",
                        entry_price, midpoint, sig.trigger_price, max_entry)
            _notify(
                f"⚠️ *ENTRY REJECTED — too far past trigger*\n"
                f"LTP ₹{entry_price:.2f} > mid {midpoint:.0f} (trigger {sig.trigger_price:.0f}→T1 {max_entry:.0f})\n"
                f"{trade_symbol}"
            )
            return {"placed": False, "reason": "LTP " + str(entry_price) + " past midpoint " + str(midpoint)}
    if max_entry > 0 and entry_price > max_entry:
        log.warning("ENTRY REJECTED: LTP %.2f > T1 %.2f (too far past entry)", entry_price, max_entry)
        _notify(f"⚠️ *ENTRY REJECTED — price past T1*\nLTP ₹{entry_price:.2f} > T1 {max_entry}\n{trade_symbol}")
        return {"placed": False, "reason": "LTP " + str(entry_price) + " past T1 " + str(max_entry)}

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
        "channel": "ch5",
        "filter_score": filter_score,
        "targets_remaining": ",".join(str(int(t)) for t in sig.targets[1:]) if len(sig.targets) > 1 else None,
    }

    try:
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
        return {"placed": False, "reason": "Order failed: " + str(exc)}

    trade_id = db.insert_trade(trade_row)

    # Place GTT SL order
    gtt_id = None
    try:
        old_sim2 = getattr(config, "UPSTOX_SIMULATE_ORDERS", False)
        config.UPSTOX_SIMULATE_ORDERS = False
        try:
            gtt_id = uc.place_gtt_sl(
                instrument_token=instrument_token,
                quantity=qty,
                trigger_price=sig.stop_loss,
                product=config.UPSTOX_PRODUCT,
            )
        finally:
            config.UPSTOX_SIMULATE_ORDERS = old_sim2
        if gtt_id:
            with db.get_conn() as conn:
                conn.execute("UPDATE trades SET gtt_order_id=? WHERE id=?", (gtt_id, trade_id))
            log.info("[CH5] GTT SL placed: id=%s trade=%d trigger=%.2f", gtt_id, trade_id, sig.stop_loss)
        else:
            log.warning("[CH5] GTT SL placement failed for trade %d — monitor SL only", trade_id)
    except Exception as gtt_exc:
        log.warning("[CH5] GTT SL error for trade %d: %s — monitor SL only", trade_id, gtt_exc)

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
        "gtt_order_id": gtt_id,
    }


# ---------------------------------------------------------------------------
# Close trade (with live sell order)
# ---------------------------------------------------------------------------
_peak_net: dict[int, float] = {}
_tgt_levels_hit: dict[int, int] = {}
_peak_ltp: dict[int, float] = {}


def _close_trade_by_id(trade_id: int, exit_price: float, reason: str) -> None:
    try:
        exit_price = round(exit_price * (1 - SLIPPAGE_PCT), 2)

        from src.storage import db
        db.init_db()
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT id, price, qty, symbol, channel, broker_key, gtt_order_id FROM trades WHERE id = ? AND status = 'OPEN'",
                (trade_id,),
            ).fetchone()
            if not row:
                return

            gtt_id = row["gtt_order_id"] if "gtt_order_id" in row.keys() else None
            if gtt_id:
                try:
                    from src.broker.upstox_client import UpstoxClient as _Uc
                    _gc = _Uc()
                    old_sim_g = getattr(config, "UPSTOX_SIMULATE_ORDERS", False)
                    config.UPSTOX_SIMULATE_ORDERS = False
                    try:
                        _gc.cancel_gtt(gtt_id)
                    finally:
                        config.UPSTOX_SIMULATE_ORDERS = old_sim_g
                except Exception as gtt_exc:
                    log.warning("[LIVE-EXIT] GTT cancel failed for trade %d: %s", trade_id, gtt_exc)

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
            "WHERE status = 'OPEN' AND channel = 'ch5' ORDER BY id DESC"
        ).fetchall()

    if not rows:
        log.info("[CH5] Close instruction but no open trades.")
        return

    target_trade = None
    if original_sig:
        match_sym = f"{original_sig.symbol} {int(original_sig.strike)} {original_sig.option_type}"
        for trade in rows:
            if trade["symbol"].upper() == match_sym.upper():
                target_trade = trade
                break
        if not target_trade:
            log.info("[CH5] Reply-to trade already closed, skipping.")
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
                return
        except Exception:
            return
    else:
        return

    _close_trade_by_id(target_trade["id"], ltp, "channel_exit")
    log.info("[CH5] Close executed: %s at %.2f", target_trade["symbol"], ltp)
    _notify(f"[CH5] Close executed: {target_trade['symbol']} at {ltp}")


# ---------------------------------------------------------------------------
# Position monitor (CH5 trades only)
# ---------------------------------------------------------------------------
async def _monitor_positions():
    from src.storage import db
    fail_count = 0
    _wd_counter = 0
    while True:
        await asyncio.sleep(1)
        _wd_counter += 1
        if _wd_counter >= 60:
            _watchdog_ping()
            _wd_counter = 0
        try:
            db.init_db()
            with db.get_conn() as conn:
                rows = conn.execute(
                    "SELECT id, symbol, price, qty, stop_price, target_price, "
                    "broker_key, channel, targets_remaining, peak_price, ts, gtt_order_id "
                    "FROM trades WHERE status = 'OPEN' AND channel = 'ch5' AND broker_key IS NOT NULL"
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
                    log.warning("[CH5] Monitor: LTP API empty for %d trades", len(rows))
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
                    log.warning("MAX LOSS CAP for %s: net=%.0f hit -%d", trade["symbol"], net_pnl, MAX_LOSS_PER_TRADE)
                    _close_trade_by_id(tid, ltp, "max_loss_cap")
                    _peak_net.pop(tid, None)
                    _notify(f"🛑 *MAX LOSS CAP* — {trade['symbol']}\nLoss: ₹{abs(net_pnl):,.0f} (cap: ₹{MAX_LOSS_PER_TRADE:,})")

                # Target trailing — SL = target - 15 (breathing room)
                elif trade["target_price"] and ltp >= trade["target_price"]:
                    remaining = trade["targets_remaining"] or ""
                    if remaining:
                        next_tgts = [float(t) for t in remaining.split(",") if t.strip()]
                        new_sl = trade["target_price"] - 15
                        if next_tgts:
                            new_tgt = next_tgts.pop(0)
                            new_remaining = ",".join(str(int(t)) for t in next_tgts) if next_tgts else None
                            with db.get_conn() as conn:
                                conn.execute(
                                    "UPDATE trades SET stop_price=?, target_price=?, targets_remaining=? WHERE id=?",
                                    (new_sl, new_tgt, new_remaining, tid))
                            _gtt_id = trade["gtt_order_id"]
                            if _gtt_id:
                                try:
                                    from src.broker.upstox_client import UpstoxClient as _Uc2
                                    _guc = _Uc2()
                                    _old_s = getattr(config, "UPSTOX_SIMULATE_ORDERS", False)
                                    config.UPSTOX_SIMULATE_ORDERS = False
                                    try:
                                        _guc.modify_gtt_sl(_gtt_id, new_sl, qty)
                                    finally:
                                        config.UPSTOX_SIMULATE_ORDERS = _old_s
                                    log.info("[CH5] GTT SL modified: %s -> %.0f", _gtt_id, new_sl)
                                except Exception as _ge:
                                    log.warning("[CH5] GTT SL modify failed: %s", _ge)
                            log.info("[CH5] TGT TRAIL %s: SL->%.0f TGT->%.0f remaining=%s",
                                     trade["symbol"], new_sl, new_tgt, new_remaining)
                            _notify(f"🎯 *TGT hit* — {trade['symbol']}\nSL → {new_sl:.0f} | Next TGT → {new_tgt:.0f}")
                        else:
                            log.info("[CH5] ALL TGT hit for %s: LTP=%.2f", trade["symbol"], ltp)
                            _close_trade_by_id(tid, ltp, "all_tgt_hit")
                            _peak_net.pop(tid, None)
                    else:
                        log.info("[CH5] TARGET hit for %s: LTP=%.2f >= target=%.2f",
                                 trade["symbol"], ltp, trade["target_price"])
                        _close_trade_by_id(tid, ltp, "target_hit")
                        _peak_net.pop(tid, None)

                # SL hit
                elif trade["stop_price"] and ltp <= trade["stop_price"]:
                    log.info("[CH5] SL HIT for %s: LTP=%.2f <= SL=%.2f net=%.0f",
                             trade["symbol"], ltp, trade["stop_price"], net_pnl)
                    _close_trade_by_id(tid, ltp, "sl_hit")
                    _peak_net.pop(tid, None)

        except Exception as exc:
            fail_count += 1
            if fail_count % 60 == 0:
                log.error("[CH5] Monitor error (repeated %dx): %s", fail_count, exc)
            else:
                log.warning("[CH5] Monitor error: %s", exc)


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
    phone = os.getenv("TELEGRAM_VIP_PHONE", "")
    ch5_id = os.getenv("SIGNAL_CHANNEL5_ID", "")

    if not all([api_id, api_hash, phone, ch5_id]):
        log.error("Missing env vars: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_VIP_PHONE, SIGNAL_CHANNEL5_ID")
        return

    ch5_int = _normalize_channel_id(ch5_id)
    if not ch5_int:
        log.error("Invalid SIGNAL_CHANNEL5_ID: %s", ch5_id)
        return

    # Dedicated CH5 session (same VIP account, separate SQLite to avoid locks)
    session_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "telegram_ch5")
    client = TelegramClient(session_path, api_id, api_hash, catch_up=True)

    _telegram_connected = False
    try:
        await client.start(phone=phone)
        me = await client.get_me()
        log.info("Logged in as %s (id=%s)", me.first_name, me.id)
        _notify(f"CH5 listener started as {me.first_name}")
        _telegram_connected = True
        try:
            await client.get_entity(ch5_int)
            log.info("Resolved entity for CH5 channel %s", ch5_int)
        except Exception as err:
            log.warning("Could not resolve CH5 entity %s: %s", ch5_int, err)
    except Exception as err:
        log.error("Telegram connection failed: %s", err)
        return

    # Pre-load instrument master
    try:
        from src.broker.upstox_client import UpstoxClient
        uc = UpstoxClient()
        instruments = uc.load_instruments()
        _build_instrument_index(instruments)
        log.info("[CH5] Instrument master pre-loaded + indexed")
    except Exception as exc:
        log.warning("[CH5] Instrument pre-load failed (will retry on first trade): %s", exc)

    # Start position monitor
    asyncio.get_event_loop().create_task(_monitor_positions())
    log.info("[CH5] Position monitor started (check every 1s)")

    # Catch-up: check recent messages we may have missed during restart
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        _IST = ZoneInfo(config.TIMEZONE)
        _cutoff = datetime.now(_IST) - timedelta(seconds=120)
        _entity = await client.get_entity(ch5_int)
        async for _msg in client.iter_messages(_entity, limit=5):
            if not _msg.text or _msg.date.astimezone(_IST) < _cutoff:
                continue
            _sig = parse_signal_ch5(_msg.text)
            if _sig:
                log.info("[CH5] CATCH-UP signal found (%s ago): %s %s %s",
                         datetime.now(_IST) - _msg.date.astimezone(_IST), _sig.symbol, _sig.strike, _sig.option_type)
                loop = asyncio.get_event_loop()
                _res = await loop.run_in_executor(_signal_executor, lambda: execute_signal(_sig))
                if _res["placed"]:
                    _notify(f"*[CH5] CATCH-UP order placed*\n{_res['symbol']} x{_res['qty']}\nEntry: {_res['entry']}")
                    log.info("[CH5] Catch-up order placed: %s", _res)
                else:
                    log.info("[CH5] Catch-up signal not executed: %s", _res["reason"])
                break
        log.info("[CH5] Catch-up check done")
    except Exception as exc:
        log.warning("[CH5] Catch-up check failed: %s", exc)

    _last_msg_time = [_time.time()]
    _last_seen_msg_id = [0]
    try:
        _init_entity = await client.get_entity(ch5_int)
        _init_msgs = await client.get_messages(_init_entity, limit=1)
        if _init_msgs:
            _last_seen_msg_id[0] = _init_msgs[0].id
            log.info("[CH5] Seeded last_seen_msg_id=%d", _last_seen_msg_id[0])
    except Exception as _e:
        log.warning("[CH5] Could not seed last_seen_msg_id: %s", _e)

    @client.on(events.NewMessage(chats=[ch5_int] if _telegram_connected else []))
    async def handler(event):
        _last_msg_time[0] = _time.time()
        _last_seen_msg_id[0] = event.message.id
        text = event.message.text or ""
        if not text.strip():
            return

        log.info("[CH5] Channel message: %s", text[:120])

        if not _ch5_is_enabled():
            log.info("[CH5] Disabled — ignoring signal")
            return

        sig = parse_signal_ch5(text)

        if sig is not None:
            log.info("[CH5] Parsed signal: %s %s %s trigger=%.2f SL=%.2f targets=%s expiry=%s",
                     sig.symbol, sig.strike, sig.option_type,
                     sig.trigger_price, sig.stop_loss, sig.targets, sig.expiry_date)

            async def _execute_in_background(_sig=sig):
                try:
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(_signal_executor, lambda: execute_signal(_sig))
                    _notify(
                        f"*[CH5] Signal received — {'executed' if result['placed'] else 'skipped'}*\n"
                        f"{_sig.action} {_sig.symbol} {int(_sig.strike)} {_sig.option_type}\n"
                        f"Entry ABOVE {_sig.trigger_price:.0f} | SL: {_sig.stop_loss:.0f} | "
                        f"Targets: {', '.join(str(int(t)) for t in _sig.targets)}"
                    )
                    if result["placed"]:
                        _notify(
                            f"*[CH5] Order placed*\n"
                            f"{result['symbol']} x{result['qty']}\n"
                            f"Entry: {result['entry']} | SL: {result['sl']} | "
                            f"Target: {result['target']}"
                        )
                        log.info("[CH5] Order placed: %s", result)
                    else:
                        _notify(f"[CH5] Signal not executed: {result['reason']}")
                        log.warning("[CH5] Signal not executed: %s", result["reason"])
                except Exception as exc:
                    log.error("[CH5] Background execute error: %s", exc)

            asyncio.get_event_loop().create_task(_execute_in_background())
            return

        # Follow-up: close/book/exit
        action, exit_price = _classify_followup(text)
        if action == "book":
            original_sig = None
            if event.message.reply_to and event.message.reply_to.reply_to_msg_id:
                try:
                    orig_msg = await client.get_messages(event.chat_id, ids=event.message.reply_to.reply_to_msg_id)
                    if orig_msg and orig_msg.text:
                        original_sig = parse_signal_ch5(orig_msg.text)
                except Exception as exc:
                    log.warning("[CH5] Could not fetch replied-to message: %s", exc)
            log.info("[CH5] Close instruction detected")
            _handle_followup(exit_price, original_sig)
            return

        log.info("[CH5] Not an entry signal or follow-up, skipping.")

    # Polling fallback — poll channel every 5s during market hours
    async def _health_check():
        _consecutive_stale = [0]
        while True:
            from src.utils.market_calendar import now_ist
            ist_now = now_ist()
            is_market = ist_now.weekday() < 5 and 9 <= ist_now.hour < 16
            await asyncio.sleep(5 if is_market else 60)
            try:
                if not client.is_connected():
                    log.warning("[CH5] Health: connection lost — reconnecting...")
                    await client.connect()
                    log.info("[CH5] Health: reconnected (was disconnected)")
                    continue

                if not is_market:
                    await client.get_me()
                    _consecutive_stale[0] = 0
                    continue

                entity = await client.get_entity(ch5_int)
                msgs = await client.get_messages(entity, limit=1)
                if msgs and msgs[0].id > _last_seen_msg_id[0]:
                    if _last_seen_msg_id[0] > 0:
                        missed_count = msgs[0].id - _last_seen_msg_id[0]
                        log.warning("[CH5] Poll: handler missed %d msg(s) (last_seen=%d, channel=%d) — fetching & processing",
                                    missed_count, _last_seen_msg_id[0], msgs[0].id)
                        _consecutive_stale[0] += 1
                        try:
                            missed = await client.get_messages(entity, min_id=_last_seen_msg_id[0], limit=20)
                            missed = list(reversed(missed))
                            for m in missed:
                                if not m.text or not m.text.strip():
                                    continue
                                log.info("[CH5] Poll processing msg %d: %s", m.id, m.text[:120])
                                sig = parse_signal_ch5(m.text)
                                if sig:
                                    log.info("[CH5] POLL signal: %s %s %s trigger=%.2f",
                                             sig.symbol, sig.strike, sig.option_type, sig.trigger_price)

                                    async def _poll_exec(_s=sig):
                                        try:
                                            _notify(f"📡 [CH5] Poll caught signal: {_s.symbol} {_s.strike} {_s.option_type}")
                                            _loop = asyncio.get_event_loop()
                                            res = await _loop.run_in_executor(_signal_executor, lambda: execute_signal(_s))
                                            if res["placed"]:
                                                _notify(f"✅ [CH5] POLL order placed\n{res['symbol']} x{res['qty']}\nEntry: {res['entry']}")
                                                log.info("[CH5] Poll order placed: %s", res)
                                            else:
                                                log.info("[CH5] Poll signal not executed: %s", res.get("reason"))
                                        except Exception as _e:
                                            log.error("[CH5] Poll execute error: %s", _e)
                                    asyncio.get_event_loop().create_task(_poll_exec())
                                else:
                                    log.info("[CH5] Poll msg %d: not a signal, skipping", m.id)
                        except Exception as fetch_exc:
                            log.error("[CH5] Poll fetch failed: %s", fetch_exc)
                        _last_seen_msg_id[0] = msgs[0].id
                        if _consecutive_stale[0] >= 20:
                            log.error("[CH5] Stream stale 20 times in a row — restarting process")
                            _notify("⚠️ [CH5] Stream permanently stale — restarting")
                            os._exit(1)
                    else:
                        _last_seen_msg_id[0] = msgs[0].id
                else:
                    _consecutive_stale[0] = 0
                    await client.get_me()
            except Exception as exc:
                log.warning("[CH5] Health check failed (%s) — reconnecting...", exc)
                try:
                    await client.connect()
                    log.info("[CH5] Health: reconnected after failure")
                except Exception as exc2:
                    log.error("[CH5] Health: reconnect failed: %s — will retry", exc2)

    asyncio.get_event_loop().create_task(_health_check())

    log.info("Listening for CH5 signals: ch5=%s ...", ch5_int)
    print(f"Listening for CH5 signals: ch5={ch5_int} ... (Ctrl-C to stop)")
    await client.run_until_disconnected()


def main():
    asyncio.run(start_listener())


if __name__ == "__main__":
    main()

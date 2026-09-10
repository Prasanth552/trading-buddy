"""Live stock credit-spread executor — real option prices + Upstox orders.

Replaces the B-S simulation in stock_runner for the chosen live strategy
(ema20_rsi60). Uses the same signal detection logic but:
  1. Resolves real option instruments from the Upstox master
  2. Gets real LTP for both legs (sell + buy)
  3. Places spread orders via UpstoxClient (simulated while UPSTOX_SIMULATE_ORDERS=True)
  4. Stores positions in stock_spread_live table
  5. Monitors daily — exits on profit target / stop loss / DTE close / expiry
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import config
from src.broker.upstox_client import UpstoxClient
from src.broker.upstox_data import UpstoxData
from src.storage import db
from src.strategy.stock_runner import (
    STOCKS, STRATEGIES, _find_signal_for_date, _monthly_expiry_for,
    calc_charges, fetch_daily_candles, init_stock_strategy_db, round_strike,
)
from src.utils.logging import get_logger

log = get_logger("stock_executor")
IST = ZoneInfo("Asia/Kolkata")

LIVE_STRATEGY = "ema20_rsi60"

SPREAD_LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_spread_live (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date          TEXT NOT NULL,
    stock               TEXT NOT NULL,
    strategy            TEXT NOT NULL,
    direction           TEXT NOT NULL,
    sell_strike         REAL NOT NULL,
    buy_strike          REAL NOT NULL,
    option_type         TEXT NOT NULL,
    sell_instrument_key TEXT,
    buy_instrument_key  TEXT,
    sell_premium        REAL,
    buy_premium         REAL,
    net_credit          REAL,
    lot_size            INTEGER,
    lots                INTEGER DEFAULT 1,
    expiry_date         TEXT,
    dte_at_entry        INTEGER,
    spot_entry          REAL,
    rsi                 REAL,
    ema                 REAL,
    profit_target_pct   REAL,
    stop_loss_mult      REAL,
    close_dte           INTEGER,
    status              TEXT DEFAULT 'OPEN',
    exit_date           TEXT,
    exit_spread_val     REAL,
    exit_sell_premium   REAL,
    exit_buy_premium    REAL,
    gross_pnl           REAL,
    charges             REAL,
    net_pnl             REAL,
    sell_order_id       TEXT,
    buy_order_id        TEXT,
    exit_sell_order_id  TEXT,
    exit_buy_order_id   TEXT,
    UNIQUE(entry_date, strategy, stock)
)
"""

_schema_done = False


def init_spread_live_db():
    global _schema_done
    if _schema_done:
        return
    with db.get_conn() as conn:
        conn.executescript(SPREAD_LIVE_SCHEMA)
    _schema_done = True


def _resolve_option(uclient: UpstoxClient, stock: str, expiry: date,
                    strike: float, opt_type: str) -> dict | None:
    """Resolve an Upstox instrument for a stock option."""
    index_key = f"NSE:{stock}"
    spec = config.UPSTOX_OPTION_SEGMENTS.get(index_key)
    if not spec:
        log.warning("No Upstox segment mapping for %s", index_key)
        return None
    from src.broker.upstox_client import pick_upstox_option
    instruments = uclient.load_instruments()
    return pick_upstox_option(instruments, spec["name"], expiry, strike,
                              opt_type, spec["segment"])


def _get_option_ltp(udata: UpstoxData, instrument_key: str) -> float | None:
    """Get LTP for an option by its instrument_key."""
    try:
        result = udata.ltp([instrument_key])
        info = result.get(instrument_key)
        if info and info.get("last_price"):
            return float(info["last_price"])
    except Exception as exc:
        log.warning("LTP fetch failed for %s: %s", instrument_key, exc)
    return None


def enter_spread(ref_date: date, lots: int = 1) -> list[dict]:
    """Detect signals and enter live spreads for the live strategy.

    Returns list of entered positions (dicts).
    """
    init_spread_live_db()
    params = STRATEGIES[LIVE_STRATEGY]

    udata = UpstoxData()
    uclient = UpstoxClient()

    buffer_start = ref_date - timedelta(days=60)
    entered = []

    for stock_name, stk in STOCKS.items():
        with db.get_conn() as conn:
            existing = conn.execute(
                "SELECT 1 FROM stock_spread_live WHERE entry_date=? AND strategy=? AND stock=?",
                (ref_date.isoformat(), LIVE_STRATEGY, stock_name)).fetchone()
        if existing:
            continue

        daily = fetch_daily_candles(udata, stock_name, buffer_start,
                                    ref_date + timedelta(days=35))
        if not daily or len(daily) < 30:
            continue

        sig = _find_signal_for_date(daily, ref_date, stock_name, params)
        if sig is None:
            continue

        expiry = sig["expiry"]
        spot = sig["spot"]
        dte = sig["dte"]
        step = stk["strike_step"]

        if sig["direction"] == "bullish":
            opt_type = "PE"
            sell_strike = round_strike(spot * 0.98, step)
            buy_strike = sell_strike - 2 * step
        else:
            opt_type = "CE"
            sell_strike = round_strike(spot * 1.02, step)
            buy_strike = sell_strike + 2 * step

        if buy_strike <= 0:
            continue

        sell_inst = _resolve_option(uclient, stock_name, expiry, sell_strike, opt_type)
        buy_inst = _resolve_option(uclient, stock_name, expiry, buy_strike, opt_type)
        if not sell_inst or not buy_inst:
            log.warning("%s: could not resolve option instruments (sell=%s buy=%s)",
                        stock_name, sell_strike, buy_strike)
            continue

        sell_key = sell_inst["instrument_key"]
        buy_key = buy_inst["instrument_key"]

        sell_ltp = _get_option_ltp(udata, sell_key)
        buy_ltp = _get_option_ltp(udata, buy_key)

        if sell_ltp is None or buy_ltp is None:
            log.warning("%s: LTP unavailable (sell=%s buy=%s)", stock_name, sell_ltp, buy_ltp)
            continue

        net_credit = sell_ltp - buy_ltp
        if net_credit <= 0.5:
            log.info("%s: net credit too low (%.2f), skipping", stock_name, net_credit)
            continue

        lot_size = stk["lot_size"] * lots

        sell_resp = uclient.place_order(
            instrument_token=sell_key,
            quantity=lot_size,
            transaction_type="SELL",
            order_type="MARKET",
            tag="stock-spread",
        )
        buy_resp = uclient.place_order(
            instrument_token=buy_key,
            quantity=lot_size,
            transaction_type="BUY",
            order_type="MARKET",
            tag="stock-spread",
        )

        sell_oids = sell_resp.get("order_ids") or []
        buy_oids = buy_resp.get("order_ids") or []
        sell_oid = str(sell_oids[0]) if sell_oids else "NA"
        buy_oid = str(buy_oids[0]) if buy_oids else "NA"

        charges = calc_charges(net_credit, lot_size)

        row = {
            "entry_date": ref_date.isoformat(),
            "stock": stock_name,
            "strategy": LIVE_STRATEGY,
            "direction": sig["direction"],
            "sell_strike": sell_strike,
            "buy_strike": buy_strike,
            "option_type": opt_type,
            "sell_instrument_key": sell_key,
            "buy_instrument_key": buy_key,
            "sell_premium": round(sell_ltp, 2),
            "buy_premium": round(buy_ltp, 2),
            "net_credit": round(net_credit, 2),
            "lot_size": lot_size,
            "lots": lots,
            "expiry_date": expiry.isoformat(),
            "dte_at_entry": dte,
            "spot_entry": round(spot, 2),
            "rsi": sig["rsi"],
            "ema": sig["ema"],
            "profit_target_pct": params["profit_target_pct"],
            "stop_loss_mult": params["stop_loss_mult"],
            "close_dte": params["close_dte"],
            "status": "OPEN",
            "sell_order_id": sell_oid,
            "buy_order_id": buy_oid,
        }

        with db.get_conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO stock_spread_live
                (entry_date, stock, strategy, direction, sell_strike, buy_strike,
                 option_type, sell_instrument_key, buy_instrument_key,
                 sell_premium, buy_premium, net_credit, lot_size, lots,
                 expiry_date, dte_at_entry, spot_entry, rsi, ema,
                 profit_target_pct, stop_loss_mult, close_dte, status,
                 sell_order_id, buy_order_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["entry_date"], row["stock"], row["strategy"],
                 row["direction"], row["sell_strike"], row["buy_strike"],
                 row["option_type"], row["sell_instrument_key"], row["buy_instrument_key"],
                 row["sell_premium"], row["buy_premium"], row["net_credit"],
                 row["lot_size"], row["lots"], row["expiry_date"],
                 row["dte_at_entry"], row["spot_entry"], row["rsi"], row["ema"],
                 row["profit_target_pct"], row["stop_loss_mult"], row["close_dte"],
                 row["status"], row["sell_order_id"], row["buy_order_id"]))

        tag = "BULL PUT" if sig["direction"] == "bullish" else "BEAR CALL"
        log.info("ENTERED %s %s %s/%s %s credit=%.2f lot_size=%d",
                 tag, stock_name, sell_strike, buy_strike, opt_type,
                 net_credit, lot_size)
        entered.append(row)

    return entered


def monitor_open_positions(ref_date: date) -> list[dict]:
    """Check all OPEN stock spread positions and exit if conditions met.

    Runs daily at 15:35. Gets real LTP for both legs, computes current
    spread value, and exits on:
      - Profit target: spread narrowed enough
      - Stop loss: spread widened past threshold
      - DTE close: approaching expiry
      - Expiry: on or past expiry date
    """
    init_spread_live_db()

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_spread_live WHERE status='OPEN'"
        ).fetchall()

    if not rows:
        return []

    udata = UpstoxData()
    uclient = UpstoxClient()
    closed = []

    for row in rows:
        p = dict(row)
        stock = p["stock"]
        expiry = date.fromisoformat(p["expiry_date"])
        remaining_dte = (expiry - ref_date).days

        net_credit = p["net_credit"]
        profit_target_pct = p["profit_target_pct"]
        stop_loss_mult = p["stop_loss_mult"]
        close_dte = p["close_dte"]
        lot_size = p["lot_size"]

        if ref_date >= expiry:
            _close_position(p, ref_date, "expiry", 0.0, 0.0, 0.0, uclient)
            closed.append(p)
            continue

        sell_ltp = _get_option_ltp(udata, p["sell_instrument_key"])
        buy_ltp = _get_option_ltp(udata, p["buy_instrument_key"])

        if sell_ltp is None or buy_ltp is None:
            log.warning("%s: cannot price spread (sell=%s buy=%s), skipping monitor",
                        stock, sell_ltp, buy_ltp)
            continue

        current_spread = sell_ltp - buy_ltp
        unrealised = (net_credit - current_spread) * lot_size
        profit_target_val = net_credit * profit_target_pct
        stop_loss_spread = net_credit + (net_credit * stop_loss_mult)

        exit_reason = None
        if (net_credit - current_spread) >= profit_target_val:
            exit_reason = "profit_target"
        elif current_spread >= stop_loss_spread:
            exit_reason = "stop_loss"
        elif remaining_dte <= close_dte:
            exit_reason = "dte_exit"

        if exit_reason:
            _close_position(p, ref_date, exit_reason, sell_ltp, buy_ltp,
                            current_spread, uclient)
            closed.append({**p, "exit_reason": exit_reason,
                           "exit_spread": current_spread, "unrealised": unrealised})

    return closed


def _close_position(p: dict, exit_date: date, reason: str,
                    sell_ltp: float, buy_ltp: float,
                    current_spread: float, uclient: UpstoxClient):
    """Close a live spread position — place exit orders and update DB."""
    lot_size = p["lot_size"]
    net_credit = p["net_credit"]

    exit_sell_oid = "NA"
    exit_buy_oid = "NA"

    if reason != "expiry" and sell_ltp > 0 and buy_ltp > 0:
        try:
            resp1 = uclient.place_order(
                instrument_token=p["sell_instrument_key"],
                quantity=lot_size,
                transaction_type="BUY",
                order_type="MARKET",
                tag="stock-spread-exit",
            )
            oids1 = resp1.get("order_ids") or []
            exit_sell_oid = str(oids1[0]) if oids1 else "NA"
        except Exception as exc:
            log.error("Exit sell-leg order failed for %s: %s", p["stock"], exc)

        try:
            resp2 = uclient.place_order(
                instrument_token=p["buy_instrument_key"],
                quantity=lot_size,
                transaction_type="SELL",
                order_type="MARKET",
                tag="stock-spread-exit",
            )
            oids2 = resp2.get("order_ids") or []
            exit_buy_oid = str(oids2[0]) if oids2 else "NA"
        except Exception as exc:
            log.error("Exit buy-leg order failed for %s: %s", p["stock"], exc)

    gross_pnl = (net_credit - current_spread) * lot_size
    charges = calc_charges(net_credit + current_spread, lot_size)
    net_pnl = gross_pnl - charges
    status = f"CLOSED_{reason.upper()}"

    with db.get_conn() as conn:
        conn.execute("""UPDATE stock_spread_live SET
            status=?, exit_date=?, exit_spread_val=?,
            exit_sell_premium=?, exit_buy_premium=?,
            gross_pnl=?, charges=?, net_pnl=?,
            exit_sell_order_id=?, exit_buy_order_id=?
            WHERE id=?""",
            (status, exit_date.isoformat(), round(current_spread, 2),
             round(sell_ltp, 2) if sell_ltp else None,
             round(buy_ltp, 2) if buy_ltp else None,
             round(gross_pnl, 2), round(charges, 2), round(net_pnl, 2),
             exit_sell_oid, exit_buy_oid, p["id"]))

    log.info("CLOSED %s %s reason=%s spread=%.2f pnl=%.0f",
             p["stock"], p["direction"], reason, current_spread, net_pnl)


def get_open_positions() -> list[dict]:
    """Return all OPEN live stock spread positions."""
    init_spread_live_db()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_spread_live WHERE status='OPEN' ORDER BY entry_date, stock"
        ).fetchall()
    return [dict(r) for r in rows]


def get_today_entries(ref_date: date | None = None) -> list[dict]:
    """Return positions entered today."""
    if ref_date is None:
        ref_date = date.today()
    init_spread_live_db()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_spread_live WHERE entry_date=? ORDER BY stock",
            (ref_date.isoformat(),)).fetchall()
    return [dict(r) for r in rows]


def get_all_positions(days: int = 90) -> list[dict]:
    """Return all positions from the last N days."""
    init_spread_live_db()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_spread_live WHERE entry_date>=? ORDER BY entry_date DESC, stock",
            (cutoff,)).fetchall()
    return [dict(r) for r in rows]

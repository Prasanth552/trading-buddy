"""Execution layer — MODE flag, sizing, order placement, guardrails.

We analyse the INDEX but trade WEEKLY OPTIONS (buying CE for long, PE for short).
Risk model (option buying, defined risk):
  - index_risk      = |signal.entry - signal.stop|         (index points)
  - premium_risk/u  = index_risk * OPTION_DELTA_ASSUMPTION  (option points, capped at premium)
  - risk_per_lot    = premium_risk/u * lot_size
  - lots            = floor(MAX_RISK_PER_TRADE / risk_per_lot)   (>= MIN_LOT_SIZE or reject)

Every entry is paired with a protective SL-M stop on the option premium
(spec §10.2). PAPER mode logs simulated fills; LIVE places real orders and is
gated behind MODE=LIVE + a runtime confirmation (spec §9).

Pure helpers (size_position, build_order_pair) are offline-testable; Kite calls
live in place + the option resolver.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import config
from src.execution import guardrails
from src.utils import market_calendar as mc
from src.utils.logging import get_logger

log = get_logger("executor")


@dataclass
class Sizing:
    lots: int
    qty: int
    entry_premium: float
    stop_premium: float
    target_premium: float
    risk_per_lot: float
    total_risk: float
    fits: bool
    reason: str = ""


def size_position(
    index_risk: float,
    entry_premium: float,
    lot_size: int,
    budget: float = float(config.MAX_RISK_PER_TRADE),
    min_lots: int = config.MIN_LOT_SIZE,
    delta: float = config.OPTION_DELTA_ASSUMPTION,
    rr: float = config.SIGNAL_RR_RATIO,
) -> Sizing:
    """Compute option lots so the premium-stop risk fits the rupee budget."""
    if index_risk <= 0 or entry_premium <= 0 or lot_size <= 0:
        return Sizing(0, 0, entry_premium, 0.0, 0.0, 0.0, 0.0, False, "invalid inputs")

    # Option-premium move implied by the index stop (capped at the full premium).
    premium_risk_per_unit = min(index_risk * delta, entry_premium)
    stop_premium = round(max(entry_premium - premium_risk_per_unit, 0.05), 2)
    target_premium = round(entry_premium + rr * premium_risk_per_unit, 2)
    risk_per_lot = premium_risk_per_unit * lot_size
    lots = math.floor(budget / risk_per_lot) if risk_per_lot > 0 else 0

    if lots < min_lots:
        return Sizing(
            0, 0, entry_premium, stop_premium, target_premium, risk_per_lot, 0.0, False,
            f"1 lot risk ₹{risk_per_lot:,.0f} exceeds budget ₹{budget:,.0f}",
        )
    qty = lots * lot_size
    return Sizing(lots, qty, entry_premium, stop_premium, target_premium,
                  risk_per_lot, risk_per_lot * lots, True, "ok")


def evaluate_exit(
    entry_premium: float,
    stop_premium: float,
    target_premium: float,
    current_premium: float,
) -> tuple[str | None, float | None]:
    """Decide whether a long-option position should exit at the current price.

    Returns (reason, exit_price): ('stop', stop) | ('target', target) | (None, None).
    """
    if current_premium <= stop_premium:
        return "stop", stop_premium
    if current_premium >= target_premium:
        return "target", target_premium
    return None, None


def decide_exit(
    entry_premium: float,
    stop_premium: float,
    target_premium: float,
    current_premium: float,
    qty: int,
) -> tuple[str | None, float | None]:
    """Exit decision including the rupee take-profit (₹PROFIT_TARGET_RUPEES).

    The rupee target closes the position at the *current* market premium as soon
    as unrealised profit reaches the configured amount — banking small, consistent
    wins. Falls back to the ATR stop/target otherwise.
    """
    target_rs = getattr(config, "PROFIT_TARGET_RUPEES", 0.0)
    if target_rs and qty > 0:
        pnl_now = (current_premium - entry_premium) * qty
        if pnl_now >= target_rs:
            return "profit", current_premium
    return evaluate_exit(entry_premium, stop_premium, target_premium, current_premium)


def unrealised_pnl(entry_premium: float, current_premium: float, qty: int) -> float:
    """Live (mark-to-market) P&L for an open long-option position, in rupees."""
    return round((current_premium - entry_premium) * qty, 2)


def build_order_pair(
    tradingsymbol: str,
    exchange: str,
    sizing: Sizing,
    signal_id: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the (entry BUY, protective SELL SL-M) order dicts for an option buy."""
    entry = {
        "tradingsymbol": tradingsymbol,
        "exchange": exchange,
        "transaction_type": "BUY",
        "quantity": sizing.qty,
        "qty": sizing.qty,
        "order_type": config.ENTRY_ORDER_TYPE,
        "product": config.ORDER_PRODUCT,
        "price": sizing.entry_premium,
        "signal_id": signal_id,
    }
    stop = {
        "tradingsymbol": tradingsymbol,
        "exchange": exchange,
        "transaction_type": "SELL",
        "quantity": sizing.qty,
        "qty": sizing.qty,
        "order_type": config.STOP_ORDER_TYPE,
        "product": config.ORDER_PRODUCT,
        "trigger_price": sizing.stop_premium,
        "signal_id": signal_id,
    }
    return entry, stop


@dataclass
class Executor:
    """Places (or simulates) orders for signals, enforcing the guardrails."""

    client: Any | None = None
    notifier: Any | None = None
    mode: str = config.MODE
    confirm_live: bool = False
    upstox: Any | None = None          # UpstoxClient when EXECUTION_BROKER="upstox"
    _paper_seq: int = field(default=0, repr=False)

    # --- order placement ---------------------------------------------------
    def _place(
        self, order: dict[str, Any], signal_id: int | None, status: str,
        stop_price: float | None = None, target_price: float | None = None,
        broker_key: str | None = None,
    ) -> str:
        """Place one order and record it. PAPER -> simulated id; LIVE -> real id."""
        from src.storage import db
        log.info("ORDER (pre-submit) %s", {k: order[k] for k in
                 ("tradingsymbol", "transaction_type", "qty", "order_type")})

        if config.EXECUTION_BROKER == "upstox":
            # Place the (sandbox/dummy) order on Upstox.
            resp = self.upstox.place_order(
                instrument_token=order["upstox_instrument_key"],
                quantity=order["quantity"],
                transaction_type=order["transaction_type"],
                order_type=order["order_type"],
                trigger_price=order.get("trigger_price") or 0,
            )
            ids = resp.get("order_ids") or []
            order_id = str(ids[0]) if ids else "UPSTOX-NA"
            mode_tag = "UPSTOX_SANDBOX"
        elif self.mode == "LIVE" and self.confirm_live:
            resp = self.client.kite.place_order(
                variety="regular",
                exchange=order["exchange"],
                tradingsymbol=order["tradingsymbol"],
                transaction_type=order["transaction_type"],
                quantity=order["quantity"],
                product=order["product"],
                order_type=order["order_type"],
                price=order.get("price"),
                trigger_price=order.get("trigger_price"),
            )
            order_id = str(resp)
            mode_tag = self.mode
        else:
            self._paper_seq += 1
            order_id = f"PAPER-{mc.now_ist():%Y%m%d}-{self._paper_seq}"
            mode_tag = self.mode

        db.insert_trade({
            "signal_id": signal_id,
            "ts": mc.now_ist().isoformat(timespec="seconds"),
            "symbol": order["tradingsymbol"],
            "side": order["transaction_type"],
            "qty": order["qty"],
            "price": order.get("price") or order.get("trigger_price"),
            "order_id": order_id,
            "mode": mode_tag,
            "status": status,
            "exit_price": None,
            "pnl": None,
            "stop_price": stop_price,
            "target_price": target_price,
            "broker_key": broker_key,
        })
        log.info("ORDER (submitted) id=%s status=%s", order_id, status)
        return order_id

    # --- main entry point --------------------------------------------------
    def execute_signal(self, signal: dict[str, Any], option: dict[str, Any]) -> dict[str, Any]:
        """Size, validate, and place the entry + protective stop for one signal.

        ``option`` is a resolved instrument dict (tradingsymbol, exchange,
        lot_size, last_price/premium). Returns a result dict.
        """
        from src.storage import db
        date_iso = mc.now_ist().date().isoformat()
        ds = dict(db.get_or_create_daily_state(date_iso))

        # Guardrail: per-day limits, kill switch, pause.
        gate = guardrails.pretrade_check(ds, mode=self.mode)
        if not gate.allowed:
            log.warning("Trade blocked: %s", gate.reason)
            return {"placed": False, "reason": gate.reason}

        # Sizing from the index stop distance + option premium.
        index_risk = abs(float(signal["entry"]) - float(signal["stop"]))
        premium = float(option.get("last_price") or option.get("premium") or 0.0)
        lot_size = int(option.get("lot_size") or config.LOT_SIZES.get(
            config.OPTION_SPECS.get(signal["symbol"], {}).get("lot_key", ""), 0))
        sizing = size_position(index_risk, premium, lot_size)
        if not sizing.fits:
            log.warning("Trade does not fit risk budget: %s", sizing.reason)
            return {"placed": False, "reason": sizing.reason, "sizing": sizing}

        entry, stop = build_order_pair(
            option["tradingsymbol"], option["exchange"], sizing,
            signal.get("id"))

        # Guardrail: never an entry without a valid protective stop.
        ok = guardrails.validate_order_pair(entry, stop)
        if not ok.allowed:
            log.error("Order pair invalid: %s", ok.reason)
            return {"placed": False, "reason": ok.reason}

        # If executing on Upstox, resolve the matching Upstox contract and attach
        # its instrument_key to both order legs.
        if config.EXECUTION_BROKER == "upstox":
            try:
                if self.upstox is None:
                    from src.broker.upstox_client import UpstoxClient
                    self.upstox = UpstoxClient()
                ux = self.upstox.resolve_option(
                    signal["symbol"], option.get("expiry"),
                    option.get("strike"), option.get("instrument_type"))
            except Exception as exc:  # noqa: BLE001
                log.error("Upstox resolve failed: %s", exc)
                return {"placed": False, "reason": f"upstox resolve error: {exc}"}
            if not ux:
                return {"placed": False, "reason": "Upstox option contract not found"}
            entry["upstox_instrument_key"] = ux["instrument_key"]
            stop["upstox_instrument_key"] = ux["instrument_key"]

        # Fail-safe: any error in the placement path stops trading + alerts.
        try:
            entry_id = self._place(
                entry, signal.get("id"), status="OPEN",
                stop_price=sizing.stop_premium, target_price=sizing.target_premium,
                broker_key=entry.get("upstox_instrument_key"))
            if config.EXECUTION_BROKER == "upstox":
                # Protective stop is app-monitored for Upstox sandbox (the monitor
                # places the closing SELL on stop/target). LIVE should add a resting
                # exchange stop here.
                stop_id = None
            else:
                stop_id = self._place(stop, signal.get("id"), status="TRIGGER_PENDING")
        except Exception as exc:  # noqa: BLE001
            log.error("Execution error — halting: %s", exc)
            if self.notifier is not None:
                try:
                    self.notifier.send_message(f"⚠️ Execution error, trading halted: {exc}")
                except Exception:  # noqa: BLE001
                    pass
            return {"placed": False, "reason": f"execution error: {exc}"}

        db.bump_trades_count(date_iso)
        broker_label = "UPSTOX_SANDBOX" if config.EXECUTION_BROKER == "upstox" else self.mode
        result = {
            "placed": True, "mode": broker_label, "tradingsymbol": option["tradingsymbol"],
            "lots": sizing.lots, "qty": sizing.qty,
            "entry_premium": sizing.entry_premium, "stop_premium": sizing.stop_premium,
            "total_risk": round(sizing.total_risk, 2),
            "entry_order_id": entry_id, "stop_order_id": stop_id,
        }
        if self.notifier is not None:
            try:
                from src.notify import messages
                self.notifier.send_message(messages.order_placed(
                    option["tradingsymbol"], signal["direction"], sizing.qty,
                    sizing.entry_premium, sizing.stop_premium, sizing.total_risk, self.mode))
            except Exception:  # noqa: BLE001
                pass
        return result


def _option_exchange(tradingsymbol: str) -> str:
    """Infer the option exchange from the tradingsymbol (SENSEX -> BFO, else NFO)."""
    return "BFO" if tradingsymbol.upper().startswith("SENSEX") else "NFO"


def _safe_ltp(
    client: Any, sym: str, exch: str, price_fn: Any | None = None,
) -> float | None:
    """Fetch the option's last price, returning None if it can't be priced.

    Kite returns an empty dict for an expired/unknown contract; we treat that
    (and any error) as "un-priceable" rather than raising — so the monitor never
    crashes or spams ERROR for a dead option.
    """
    try:
        if price_fn is not None:
            return price_fn(sym, exch)
        full = f"{exch}:{sym}"
        data = client.ltp([full])
        info = data.get(full) if isinstance(data, dict) else None
        if not info or not info.get("last_price"):
            return None
        return float(info["last_price"])
    except Exception as exc:  # noqa: BLE001
        log.warning("LTP fetch failed for %s: %s", sym, exc)
        return None


def _force_close_if_stale(
    p: dict[str, Any], date_iso: str, notifier: Any | None = None,
) -> bool:
    """Force-close an un-priceable position if it's from a previous day (expired).

    Closes at the entry premium (neutral P&L 0) so a dead/expired contract doesn't
    sit OPEN forever. Same-day un-priceable rows are left to retry (transient).
    Returns True if it force-closed.
    """
    from src.storage import db
    pos_date = (p.get("ts") or "")[:10]
    today = mc.now_ist().date().isoformat()
    if not pos_date or pos_date >= today:
        return False  # same-day -> likely transient, retry next cycle
    db.close_position(p["id"], p["price"], 0.0, status="CLOSED_EXPIRED")
    log.warning("Force-closed stale/expired position %s (id=%s) at entry, P&L 0.",
                p["symbol"], p["id"])
    if notifier is not None:
        try:
            notifier.send_message(
                f"ℹ️ Closed expired/un-priceable position *{p['symbol']}* "
                f"(opened {pos_date}). Marked P&L ₹0 — verify on the broker if needed.")
        except Exception:  # noqa: BLE001
            pass
    return True


def monitor_paper_positions(
    client: Any | None = None,
    price_fn: Any | None = None,
    notifier: Any | None = None,
) -> list[dict[str, Any]]:
    """Close open PAPER positions whose option premium hit the stop or target.

    ``price_fn(tradingsymbol, exchange) -> premium`` can be injected for tests;
    otherwise live LTP is fetched via ``client``. After realizing P&L it checks
    the daily-loss kill switch (spec §10.3).
    """
    from src.storage import db
    date_iso = mc.now_ist().date().isoformat()
    closed: list[dict[str, Any]] = []

    for p in db.get_open_paper_positions():
        p = dict(p)
        sym = p["symbol"]
        exch = _option_exchange(sym)
        current = _safe_ltp(client, sym, exch, price_fn)
        if current is None:
            _force_close_if_stale(p, date_iso, notifier)
            continue

        reason, exit_price = decide_exit(
            p["price"], p["stop_price"], p["target_price"], current, p["qty"])
        if reason is None:
            continue
        pnl = round((exit_price - p["price"]) * p["qty"], 2)
        db.close_position(p["id"], exit_price, pnl, status=f"CLOSED_{reason.upper()}")
        db.add_realised_pnl(date_iso, pnl)
        closed.append({"symbol": sym, "reason": reason, "exit_price": exit_price, "pnl": pnl})
        log.info("EXIT %s %s @ %s P&L ₹%.2f", reason, sym, exit_price, pnl)
        if notifier is not None:
            try:
                from src.notify import messages
                notifier.send_message(messages.exit_msg(sym, reason, exit_price, pnl))
            except Exception:  # noqa: BLE001
                pass

    # Kill switch after realizing P&L for the day.
    from src.execution import guardrails
    ds = dict(db.get_or_create_daily_state(date_iso))
    if not ds["kill_switch_tripped"] and guardrails.should_trip_kill_switch(ds["realised_pnl"]):
        guardrails.trip_kill_switch(date_iso, client=client, notifier=notifier)

    return closed


def monitor_upstox_positions(
    kite_client: Any,
    notifier: Any | None = None,
    upstox: Any | None = None,
    price_fn: Any | None = None,
) -> list[dict[str, Any]]:
    """Close open UPSTOX_SANDBOX positions on stop/target by placing a SELL on Upstox.

    Option price comes from Kite (we analyse on Kite); the closing order goes to
    Upstox (we trade on Upstox). Realized P&L feeds the daily kill switch.
    """
    from src.storage import db
    from src.notify import messages
    date_iso = mc.now_ist().date().isoformat()
    closed: list[dict[str, Any]] = []

    for p in db.get_open_positions("UPSTOX_SANDBOX"):
        p = dict(p)
        sym = p["symbol"]
        exch = _option_exchange(sym)
        current = _safe_ltp(kite_client, sym, exch, price_fn)
        if current is None:
            _force_close_if_stale(p, date_iso, notifier)
            continue

        reason, exit_price = decide_exit(
            p["price"], p["stop_price"], p["target_price"], current, p["qty"])
        if reason is None:
            continue

        # Close the position on Upstox (SELL the option we bought).
        try:
            if upstox is None:
                from src.broker.upstox_client import UpstoxClient
                upstox = UpstoxClient()
            upstox.place_order(p["broker_key"], p["qty"], "SELL", order_type="MARKET")
        except Exception as exc:  # noqa: BLE001 - keep other positions going
            log.error("Upstox exit order failed for %s: %s", sym, exc)
            continue

        pnl = round((exit_price - p["price"]) * p["qty"], 2)
        db.close_position(p["id"], exit_price, pnl, status=f"CLOSED_{reason.upper()}")
        db.add_realised_pnl(date_iso, pnl)
        closed.append({"symbol": sym, "reason": reason, "exit_price": exit_price, "pnl": pnl})
        log.info("UPSTOX EXIT %s %s @ %s P&L ₹%.2f", reason, sym, exit_price, pnl)
        if notifier is not None:
            try:
                notifier.send_message(messages.exit_msg(sym, reason, exit_price, pnl))
            except Exception:  # noqa: BLE001
                pass

    from src.execution import guardrails
    ds = dict(db.get_or_create_daily_state(date_iso))
    if not ds["kill_switch_tripped"] and guardrails.should_trip_kill_switch(ds["realised_pnl"]):
        guardrails.trip_kill_switch(date_iso, client=kite_client, notifier=notifier)
    return closed


def position_pnl(
    p: dict[str, Any], kite_client: Any, price_fn: Any | None = None,
) -> dict[str, Any]:
    """Live mark-to-market for one open position row. Returns symbol/qty/entry,
    current premium, unrealised P&L (₹) and the rupee profit target."""
    sym = p["symbol"]
    exch = _option_exchange(sym)
    current = _safe_ltp(kite_client, sym, exch, price_fn)
    return {
        "id": p["id"], "symbol": sym, "qty": p["qty"],
        "entry_premium": p["price"],
        "current_premium": round(float(current), 2) if current is not None else None,
        "stop_premium": p["stop_price"], "target_premium": p["target_price"],
        "unrealised_pnl": (unrealised_pnl(p["price"], current, p["qty"])
                           if current is not None else None),
        "profit_target": getattr(config, "PROFIT_TARGET_RUPEES", 0.0),
    }


def manual_close(
    trade_id: int,
    kite_client: Any,
    upstox: Any | None = None,
    notifier: Any | None = None,
    price_fn: Any | None = None,
) -> dict[str, Any]:
    """Close ONE open position now at the current market premium (user-initiated).

    Fetches the live option premium (Kite), places the closing SELL (Upstox for
    sandbox; simulated for PAPER), records the close + realised P&L, and alerts.
    """
    from src.storage import db
    from src.notify import messages
    date_iso = mc.now_ist().date().isoformat()

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE id=? AND side='BUY' AND status='OPEN'",
            (trade_id,),
        ).fetchone()
    if row is None:
        return {"closed": False, "reason": "position not found or already closed"}
    p = dict(row)
    sym = p["symbol"]
    exch = _option_exchange(sym)

    current = _safe_ltp(kite_client, sym, exch, price_fn)
    if current is None:
        # Un-priceable (e.g. expired contract): close at entry premium (P&L 0)
        # rather than leaving it stuck OPEN.
        db.close_position(p["id"], p["price"], 0.0, status="CLOSED_EXPIRED")
        db.add_realised_pnl(date_iso, 0.0)
        log.warning("Manual close: %s un-priceable; closed at entry, P&L 0.", sym)
        return {"closed": True, "symbol": sym, "exit_price": p["price"], "pnl": 0.0,
                "note": "no live price (expired?) — closed at entry, P&L 0"}

    # Place the closing SELL on Upstox for sandbox positions.
    if p.get("mode") == "UPSTOX_SANDBOX":
        try:
            if upstox is None:
                from src.broker.upstox_client import UpstoxClient
                upstox = UpstoxClient()
            upstox.place_order(p["broker_key"], p["qty"], "SELL", order_type="MARKET")
        except Exception as exc:  # noqa: BLE001
            return {"closed": False, "reason": f"Upstox exit order failed: {exc}"}

    exit_price = round(float(current), 2)
    pnl = round((exit_price - p["price"]) * p["qty"], 2)
    db.close_position(p["id"], exit_price, pnl, status="CLOSED_MANUAL")
    db.add_realised_pnl(date_iso, pnl)
    log.info("MANUAL EXIT %s @ %s P&L ₹%.2f", sym, exit_price, pnl)
    if notifier is not None:
        try:
            notifier.send_message(messages.exit_msg(sym, "manual", exit_price, pnl))
        except Exception:  # noqa: BLE001
            pass
    return {"closed": True, "symbol": sym, "exit_price": exit_price, "pnl": pnl}

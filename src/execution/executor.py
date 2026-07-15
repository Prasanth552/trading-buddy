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
    # Optional hard cap (user preference: fixed small size regardless of budget).
    cap = getattr(config, "MAX_LOTS_PER_TRADE", 0)
    if cap and cap > 0:
        lots = min(lots, cap)
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


# Square-off time near the close (used daily in intraday mode, or only on the
# contract's own expiry day in positional mode).
from datetime import time as _dtime
_EOD_SQUAREOFF = _dtime(15, 15)


def _squareoff_due(p: dict[str, Any]) -> bool:
    """Should this position be force-closed at 15:15 today?

    Intraday mode: every day. Positional mode: only on (or past) the contract's
    own expiry day — options settle at expiry and cannot be carried beyond it.
    Rows with unknown expiry fall back to intraday behaviour for safety.
    """
    if not (mc.is_market_open() and mc.now_ist().time() >= _EOD_SQUAREOFF):
        return False
    if getattr(config, "INTRADAY_SQUAREOFF", True):
        return True
    exp = p.get("expiry")
    if not exp:
        return True  # unknown expiry -> safe intraday behaviour
    from datetime import date as _date
    try:
        return mc.now_ist().date() >= _date.fromisoformat(str(exp)[:10])
    except ValueError:
        return True


def trailing_exit(
    entry_premium: float, stop_premium: float, current: float, peak: float | None,
) -> tuple[bool, float, float]:
    """Chandelier ATR trailing stop on the option premium.

    The trail distance is ATR_TRAIL_MULT × the initial premium risk
    (entry − initial stop), which equals the ATR-based distance the signal used.
    Returns (exit_now, exit_price, new_peak).
    """
    new_peak = max(peak if peak else entry_premium, current)
    init_risk = max(entry_premium - stop_premium, 0.05)
    trail_stop = new_peak - config.ATR_TRAIL_MULT * init_risk
    eff_stop = max(stop_premium, trail_stop)
    return (current <= eff_stop, current, new_peak)


def resolve_exit(p: dict[str, Any], current: float) -> tuple[str | None, float | None, float | None]:
    """Decide a position's exit under ``config.EXIT_MODE``.

    Returns (reason, exit_price, new_peak). ``new_peak`` is non-None only for the
    trailing mode (so the caller can persist it). Square-off near the close wins.
    """
    if _squareoff_due(p):
        peak = max(p.get("peak_price") or p["price"], current)
        return "squareoff", round(float(current), 2), peak
    # Rupee take-profit — applies in ALL exit modes: bank the win once the
    # position is up PROFIT_TARGET_RUPEES (user preference: ~₹3k is enough).
    target_rs = getattr(config, "PROFIT_TARGET_RUPEES", 0.0)
    if target_rs and p["qty"] > 0 and (current - p["price"]) * p["qty"] >= target_rs:
        peak = max(p.get("peak_price") or p["price"], current)
        return "profit", round(float(current), 2), peak
    # "Became zero": premium decayed to a fraction of entry — nothing left to
    # recover; close and free the slot (hedge-recovery flow rule 3).
    zc = getattr(config, "ZERO_CLOSE_PCT", 0.0)
    if zc and current <= float(p["price"]) * zc:
        peak = max(p.get("peak_price") or p["price"], current)
        return "zero", round(float(current), 2), peak
    # Manual-exit experiment: automatic stops disabled — only the take-profit
    # above and the EOD square-off act; the user cuts losers from the dashboard.
    if not getattr(config, "STOP_LOSS_ENABLED", True):
        return None, None, max(p.get("peak_price") or p["price"], current)
    mode = getattr(config, "EXIT_MODE", "target")
    if mode in ("trail_atr", "trail_st"):   # live trail uses the ATR chandelier
        exit_now, px, new_peak = trailing_exit(
            p["price"], p["stop_price"], current, p.get("peak_price"))
        return ("trail" if exit_now else None, round(px, 2) if exit_now else None, new_peak)
    reason, exit_price = decide_exit(
        p["price"], p["stop_price"], p["target_price"], current, p["qty"])
    return reason, exit_price, None


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
        broker_key: str | None = None, expiry: str | None = None,
        index_entry: float | None = None,
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
            "expiry": expiry,
            "index_entry": index_entry,
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
            opt_expiry = option.get("expiry")
            entry_id = self._place(
                entry, signal.get("id"), status="OPEN",
                stop_price=sizing.stop_premium, target_price=sizing.target_premium,
                broker_key=entry.get("upstox_instrument_key"),
                expiry=str(opt_expiry)[:10] if opt_expiry else None,
                index_entry=float(signal.get("entry") or 0) or None)
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
    key: str | None = None,
) -> float | None:
    """Fetch the option's last price, returning None if it can't be priced.

    Prefers the exact broker instrument ``key`` when available (robust across
    symbol-format differences between brokers); falls back to EXCHANGE:SYMBOL.
    Empty quotes / errors return None so the monitor never crashes on a dead
    option.
    """
    try:
        if price_fn is not None:
            return price_fn(sym, exch)
        lookup = key if (key and getattr(config, "DATA_BROKER", "kite") == "upstox") \
            else f"{exch}:{sym}"
        data = client.ltp([lookup])
        info = data.get(lookup) if isinstance(data, dict) else None
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


# --------------------------------------------------------------------------
# Hedge-recovery flow (sandbox experiment)
# --------------------------------------------------------------------------
def _opt_type(tradingsymbol: str) -> str:
    # Contains (not endswith): Kite format ends with CE/PE, Upstox format embeds
    # it mid-symbol ("NIFTY 24100 CE 17 JUL 26").
    return "CE" if "CE" in tradingsymbol.upper() else "PE"


def _underlying_of(tradingsymbol: str) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    """Map an option tradingsymbol back to its index symbol + spec.

    Longest name first so BANKNIFTY doesn't match the NIFTY prefix.
    """
    specs = sorted(config.OPTION_SPECS.items(),
                   key=lambda kv: -len(kv[1]["name"]))
    for index_symbol, spec in specs:
        if tradingsymbol.upper().startswith(spec["name"]):
            return index_symbol, spec
    return None, None


def _in_entry_window() -> bool:
    from datetime import time as _t

    def _hhmm(s: str) -> _t:
        h, m = s.split(":")
        return _t(int(h), int(m))

    now_t = mc.now_ist().time()
    return (_hhmm(getattr(config, "ENTRY_START", "09:15")) <= now_t
            <= _hhmm(getattr(config, "ENTRY_END", "15:30")))


# Race guard: the 15-min cycle's monitor pass and the 1-min monitor job can run
# in the same instant and both place the same hedge (seen live: duplicate rows
# one second apart). Track the last hedge time per underlying.
_LAST_HEDGE_AT: dict[str, float] = {}
_HEDGE_COOLDOWN_SECONDS = 120

# Market trend view per underlying (+1 up / -1 down) — refreshed by the 15-min
# analysis cycle from the Supertrend. Hedges must AGREE with this trend.
_TREND_VIEW: dict[str, float] = {}


def update_trend_view(index_symbol: str, st_dir: Any) -> None:
    """Record the latest Supertrend direction for an index (called each cycle)."""
    try:
        if st_dir is not None and st_dir == st_dir:  # NaN guard
            _TREND_VIEW[index_symbol] = 1.0 if float(st_dir) > 0 else -1.0
    except (TypeError, ValueError):
        pass


def _hedge_direction_allowed(bleeding_symbol: str, und: str) -> bool:
    """True if the would-be hedge (opposite of the bleeding leg) agrees with the
    current Supertrend. Blocks counter-trend hedges; blocks when no trend view
    exists yet (pre-first-cycle)."""
    if not getattr(config, "HEDGE_TREND_GATE", False):
        return True
    trend = _TREND_VIEW.get(und)
    if trend is None:
        return False
    new_type = "PE" if _opt_type(bleeding_symbol) == "CE" else "CE"
    return (new_type == "CE" and trend > 0) or (new_type == "PE" and trend < 0)


def should_hedge(p: dict[str, Any], current: float,
                 open_positions: list[dict[str, Any]]) -> bool:
    """True when a losing ORIGINAL position should get an opposite-side hedge.

    Rules: hedging enabled; unrealised loss has reached HEDGE_TRIGGER_RUPEES;
    and no opposite-type position is already open for the same underlying (one
    counter-position at a time — re-hedge only after the previous one banked).

    Bleeding HEDGES are themselves compensated (the chain continues until a leg
    hits zero-close or square-off) — the opposite-open check is what prevents
    runaway stacking: with a CE and PE both open, neither can spawn another.
    """
    if not getattr(config, "HEDGE_ON_LOSS", False):
        return False
    # In swap mode, bleeding hedges are SWAPPED (closed + reversed), not stacked
    # over — the swap path in the monitor handles them.
    if p.get("is_hedge") and getattr(config, "HEDGE_SWAP_ON_REVERSE", False):
        return False
    pnl = (current - float(p["price"])) * p["qty"]
    if pnl > -getattr(config, "HEDGE_TRIGGER_RUPEES", 4000.0):
        return False
    und, _ = _underlying_of(p["symbol"])
    if und is None:
        return False
    if not _hedge_direction_allowed(p["symbol"], und):
        return False  # counter-trend hedge blocked (trend gate)
    import time as _time
    if _time.time() - _LAST_HEDGE_AT.get(und, 0) < _HEDGE_COOLDOWN_SECONDS:
        return False  # a hedge for this underlying was just placed (race guard)
    my_type = _opt_type(p["symbol"])
    for o in open_positions:
        o_und, _ = _underlying_of(o["symbol"])
        if o_und == und and _opt_type(o["symbol"]) != my_type:
            return False  # opposite hedge already open
    return True


def place_hedge(
    p: dict[str, Any],
    kite_client: Any,
    upstox: Any,
    notifier: Any | None = None,
) -> dict[str, Any] | None:
    """Open the opposite-direction ATM option for a bleeding position.

    Original CE -> buy PE (and vice versa), same index, MAX_LOTS_PER_TRADE lots,
    recorded with is_hedge=1 so it is never itself hedged. The normal ₹
    take-profit banks it; the monitor re-hedges if the original keeps bleeding.
    """
    from src.storage import db
    from src.broker import instruments as inst_mod

    index_symbol, spec = _underlying_of(p["symbol"])
    if index_symbol is None:
        return None
    # Reserve the cooldown slot BEFORE placing so a concurrent caller can't
    # pass the cooldown check while this order is in flight (duplicate guard).
    import time as _time
    _LAST_HEDGE_AT[index_symbol] = _time.time()
    direction = "short" if _opt_type(p["symbol"]) == "CE" else "long"

    ltp = kite_client.ltp([index_symbol])[index_symbol]["last_price"]
    opt = inst_mod.resolve_weekly_atm_option(kite_client, index_symbol, direction, ltp)
    if not opt:
        log.warning("Hedge: no option resolved for %s %s", index_symbol, direction)
        return None
    full = f"{opt['exchange']}:{opt['tradingsymbol']}"
    premium = float(kite_client.ltp([full])[full]["last_price"])
    lot_size = int(opt.get("lot_size") or config.LOT_SIZES.get(spec["lot_key"], 0))
    lots = max(1, getattr(config, "MAX_LOTS_PER_TRADE", 1) or 1)
    qty = lots * lot_size
    if qty <= 0 or premium <= 0:
        return None

    ux = upstox.resolve_option(index_symbol, opt.get("expiry"),
                               opt.get("strike"), opt.get("instrument_type"))
    if not ux:
        log.warning("Hedge: Upstox contract not found for %s", opt["tradingsymbol"])
        return None
    resp = upstox.place_order(ux["instrument_key"], qty, "BUY", order_type="MARKET")
    ids = resp.get("order_ids") or []
    order_id = str(ids[0]) if ids else "UPSTOX-NA"

    date_iso = mc.now_ist().date().isoformat()
    db.insert_trade({
        "signal_id": None,
        "ts": mc.now_ist().isoformat(timespec="seconds"),
        "symbol": opt["tradingsymbol"], "side": "BUY", "qty": qty,
        "price": premium, "order_id": order_id, "mode": "UPSTOX_SANDBOX",
        "status": "OPEN", "exit_price": None, "pnl": None,
        # Reference only (auto stops are managed by config flags).
        "stop_price": round(premium * 0.8, 2), "target_price": None,
        "broker_key": ux["instrument_key"], "is_hedge": 1,
        "expiry": str(opt.get("expiry"))[:10] if opt.get("expiry") else None,
        "index_entry": round(float(ltp), 2),
    })
    db.bump_trades_count(date_iso)
    log.info("HEDGE opened %s x%d @ %.2f (against %s)",
             opt["tradingsymbol"], qty, premium, p["symbol"])
    if notifier is not None:
        try:
            notifier.send_message(
                f"🛡️ *Hedge opened* {opt['tradingsymbol']} x{qty} @ {premium}\n"
                f"எதிர் திசை வர்த்தகம் — {p['symbol']} நஷ்டத்தை ஈடுசெய்ய. "
                f"₹{config.PROFIT_TARGET_RUPEES:,.0f} லாபத்தில் தானாக வெளியேறும்.")
        except Exception:  # noqa: BLE001
            pass
    return {"placed": True, "tradingsymbol": opt["tradingsymbol"], "qty": qty,
            "premium": premium}


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
        current = _safe_ltp(client, sym, exch, price_fn, key=p.get("broker_key"))
        if current is None:
            _force_close_if_stale(p, date_iso, notifier)
            continue

        reason, exit_price, new_peak = resolve_exit(p, current)
        if new_peak is not None and new_peak != (p.get("peak_price") or 0):
            db.update_peak_price(p["id"], new_peak)
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

    open_rows = [dict(r) for r in db.get_open_positions("UPSTOX_SANDBOX")]
    hedged_now: set[str] = set()
    for p in open_rows:
        sym = p["symbol"]
        exch = _option_exchange(sym)
        current = _safe_ltp(kite_client, sym, exch, price_fn, key=p.get("broker_key"))
        if current is None:
            _force_close_if_stale(p, date_iso, notifier)
            continue

        reason, exit_price, new_peak = resolve_exit(p, current)
        if new_peak is not None and new_peak != (p.get("peak_price") or 0):
            db.update_peak_price(p["id"], new_peak)
        if reason is None:
            # Swap, don't stack: a HEDGE that bleeds to the trigger is CLOSED and
            # replaced by the opposite side (one working leg at a time).
            if (p.get("is_hedge") and getattr(config, "HEDGE_SWAP_ON_REVERSE", False)
                    and (current - float(p["price"])) * p["qty"]
                    <= -getattr(config, "HEDGE_SWAP_TRIGGER_RUPEES",
                                getattr(config, "HEDGE_TRIGGER_RUPEES", 4000.0))):
                try:
                    if upstox is None:
                        from src.broker.upstox_client import UpstoxClient
                        upstox = UpstoxClient()
                    upstox.place_order(p["broker_key"], p["qty"], "SELL",
                                       order_type="MARKET")
                    pnl = round((current - p["price"]) * p["qty"], 2)
                    db.close_position(p["id"], round(float(current), 2), pnl,
                                      status="CLOSED_SWAP")
                    db.add_realised_pnl(date_iso, pnl)
                    closed.append({"symbol": sym, "reason": "swap",
                                   "exit_price": round(float(current), 2), "pnl": pnl})
                    log.info("HEDGE SWAP: closed %s @ %.2f (P&L %.0f), reversing.",
                             sym, current, pnl)
                    if notifier is not None:
                        try:
                            notifier.send_message(messages.exit_msg(
                                sym, "swap", round(float(current), 2), pnl))
                        except Exception:  # noqa: BLE001
                            pass
                    # Open the opposite leg unless a position that direction is
                    # already open (e.g. the original) or we're outside the window.
                    und2, _ = _underlying_of(sym)
                    new_type = "PE" if _opt_type(sym) == "CE" else "CE"
                    already = any(
                        o["id"] != p["id"]
                        and _underlying_of(o["symbol"])[0] == und2
                        and _opt_type(o["symbol"]) == new_type
                        for o in open_rows)
                    import time as _time
                    cooled = (_time.time() - _LAST_HEDGE_AT.get(und2, 0)
                              >= _HEDGE_COOLDOWN_SECONDS)
                    if (not already and cooled and _in_entry_window()
                            and _hedge_direction_allowed(sym, und2)):
                        if place_hedge(p, kite_client, upstox, notifier):
                            hedged_now.add(und2)
                except Exception as exc:  # noqa: BLE001 - never break the monitor
                    log.error("Hedge swap failed for %s: %s", sym, exc)
                continue
            # Hedge-recovery: bleeding original + no opposite open -> counter-trade.
            und, _spec = _underlying_of(sym)
            if (und and und not in hedged_now and _in_entry_window()
                    and should_hedge(p, current, open_rows)):
                ds_now = dict(db.get_or_create_daily_state(date_iso))
                if ds_now.get("trades_count", 0) < config.MAX_TRADES_PER_DAY:
                    try:
                        if upstox is None:
                            from src.broker.upstox_client import UpstoxClient
                            upstox = UpstoxClient()
                        if place_hedge(p, kite_client, upstox, notifier):
                            hedged_now.add(und)
                    except Exception as exc:  # noqa: BLE001 - hedging must not break exits
                        log.error("Hedge placement failed for %s: %s", sym, exc)
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
    current = _safe_ltp(kite_client, sym, exch, price_fn, key=p.get("broker_key"))
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

    current = _safe_ltp(kite_client, sym, exch, price_fn, key=p.get("broker_key"))
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

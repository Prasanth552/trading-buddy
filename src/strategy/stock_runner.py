"""Live credit-spread strategy runner for NSE stock options.

Runs four validated strategies (ema20_rsi50, ema20_rsi60, ema20_rsi50_tight,
ema20_rsi50_wide) on liquid stocks and records results to SQLite.

Usage (standalone):
    .venv/bin/python3 -m src.strategy.stock_runner

Integrated into channel_app via the /api/stock-strategy/* endpoints.
"""
from __future__ import annotations

import math
import os
import pickle
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config
from src.broker.upstox_data import UpstoxData
from src.storage import db

IST = ZoneInfo("Asia/Kolkata")

STOCKS = {
    "RELIANCE": {
        "key": "NSE_EQ|INE002A01018",
        "lot_size": 250, "strike_step": 10, "iv_annual": 0.25,
    },
    "HDFCBANK": {
        "key": "NSE_EQ|INE040A01034",
        "lot_size": 550, "strike_step": 10, "iv_annual": 0.22,
    },
    "ICICIBANK": {
        "key": "NSE_EQ|INE090A01021",
        "lot_size": 700, "strike_step": 10, "iv_annual": 0.24,
    },
    "TCS": {
        "key": "NSE_EQ|INE467B01029",
        "lot_size": 175, "strike_step": 20, "iv_annual": 0.22,
    },
    "INFY": {
        "key": "NSE_EQ|INE009A01021",
        "lot_size": 400, "strike_step": 20, "iv_annual": 0.25,
    },
    "SBIN": {
        "key": "NSE_EQ|INE062A01020",
        "lot_size": 750, "strike_step": 10, "iv_annual": 0.28,
    },
    "BAJFINANCE": {
        "key": "NSE_EQ|INE296A01032",
        "lot_size": 125, "strike_step": 10, "iv_annual": 0.30,
    },
    "LT": {
        "key": "NSE_EQ|INE018A01030",
        "lot_size": 150, "strike_step": 50, "iv_annual": 0.25,
    },
    "TATASTEEL": {
        "key": "NSE_EQ|INE081A01020",
        "lot_size": 5000, "strike_step": 2.5, "iv_annual": 0.35,
    },
}

STRATEGIES = {
    "ema20_rsi50": dict(
        ema_period=20, rsi_period=14, rsi_bull=50, rsi_bear=50,
        entry_dte_range=(15, 28), profit_target_pct=0.30,
        stop_loss_mult=2.0, close_dte=5, book_profit_after_days=2,
    ),
    "ema20_rsi60": dict(
        ema_period=20, rsi_period=14, rsi_bull=60, rsi_bear=40,
        entry_dte_range=(5, 35), profit_target_pct=0.30,
        stop_loss_mult=2.0, close_dte=2, book_profit_after_days=2,
    ),
    "ema20_rsi50_tight": dict(
        ema_period=20, rsi_period=14, rsi_bull=50, rsi_bear=50,
        entry_dte_range=(15, 28), profit_target_pct=0.30,
        stop_loss_mult=1.5, close_dte=5, book_profit_after_days=2,
    ),
    "ema20_rsi50_wide": dict(
        ema_period=20, rsi_period=14, rsi_bull=50, rsi_bear=50,
        entry_dte_range=(15, 28), profit_target_pct=0.30,
        stop_loss_mult=3.0, close_dte=5, book_profit_after_days=2,
    ),
}

CACHE_DIR = os.path.join(config.DATA_DIR, "stock_candle_cache")

STOCK_STRATEGY_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_strategy_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    stock       TEXT NOT NULL,
    direction   TEXT,
    lots        INTEGER DEFAULT 1,
    entry_date  TEXT,
    exit_date   TEXT,
    exit_reason TEXT,
    spot_entry  REAL,
    sell_strike  REAL,
    buy_strike   REAL,
    net_credit   REAL,
    exit_spread_val REAL,
    gross_pnl   REAL,
    charges     REAL,
    net_pnl     REAL,
    dte_at_entry INTEGER,
    rsi         REAL,
    ema         REAL,
    skipped     INTEGER DEFAULT 0,
    skip_reason TEXT,
    expiry_date TEXT,
    premium_source TEXT DEFAULT 'bs',
    UNIQUE(date, strategy, stock)
)
"""

_schema_done = False

def init_stock_strategy_db():
    global _schema_done
    if _schema_done:
        return
    with db.get_conn() as conn:
        conn.executescript(STOCK_STRATEGY_SCHEMA)
        try:
            conn.execute("ALTER TABLE stock_strategy_results ADD COLUMN premium_source TEXT DEFAULT 'bs'")
        except Exception:
            pass
    _schema_done = True


# ---------------------------------------------------------------------------
# Real option LTP (Upstox)
# ---------------------------------------------------------------------------
import logging as _logging
_log = _logging.getLogger("strategy.stock_runner")

_stock_option_cache: dict | None = None


def _build_stock_option_master():
    """Build (stock, expiry, strike, opt_type) -> instrument_key map."""
    global _stock_option_cache
    if _stock_option_cache is not None:
        return _stock_option_cache

    import re
    from src.broker.upstox_client import UpstoxClient, _expiry_to_date
    uc = UpstoxClient()
    master = uc.load_instruments()

    opts: dict[tuple, str] = {}
    for inst in master:
        seg = inst.get("segment", "")
        if seg != "NSE_FO":
            continue
        itype = inst.get("instrument_type", "")
        if itype not in ("CE", "PE"):
            continue
        tsym = (inst.get("trading_symbol") or "").upper()
        base = re.match(r'^([A-Z&]+)', tsym)
        if not base:
            continue
        sym = base.group(1)
        if sym not in STOCKS:
            continue
        strike = float(inst.get("strike_price", 0))
        ed = _expiry_to_date(inst.get("expiry"))
        if ed and strike > 0:
            opts[(sym, ed, strike, itype)] = inst.get("instrument_key")
    _stock_option_cache = opts
    _log.info("Stock option master built: %d entries", len(opts))
    return opts


def _stock_next_expiry(stock_name: str, ref_date: date) -> date | None:
    """Find nearest expiry >= ref_date for a stock."""
    master = _build_stock_option_master()
    available = sorted(
        {k[1] for k in master if k[0] == stock_name and k[1] >= ref_date}
    )
    return available[0] if available else None


def fetch_stock_option_ltp(uclient, stock_name: str, expiry: date,
                           sell_strike: float, buy_strike: float,
                           opt_type: str) -> dict | None:
    """Fetch real LTP for sell + buy option legs.

    Returns {"sell": float, "buy": float, "sell_key": str, "buy_key": str}
    or None if resolution/fetch fails.
    """
    master = _build_stock_option_master()
    sell_key = master.get((stock_name, expiry, sell_strike, opt_type))
    buy_key = master.get((stock_name, expiry, buy_strike, opt_type))
    if not sell_key or not buy_key:
        _log.debug("Option keys not found: %s %s/%s %s exp=%s",
                   stock_name, sell_strike, buy_strike, opt_type, expiry)
        return None

    try:
        ltp_data = uclient.ltp_by_key([sell_key, buy_key])
        sell_ltp = ltp_data.get(sell_key)
        buy_ltp = ltp_data.get(buy_key)
        if sell_ltp is None or buy_ltp is None:
            return None
        return {"sell": sell_ltp, "buy": buy_ltp,
                "sell_key": sell_key, "buy_key": buy_key}
    except Exception as e:
        _log.warning("Stock option LTP fetch failed: %s", e)
        return None


def invalidate_stock_option_master():
    """Clear cached stock option master (call on new trading day)."""
    global _stock_option_cache
    _stock_option_cache = None


# ---------------------------------------------------------------------------
# Black-Scholes
# ---------------------------------------------------------------------------
def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def _bs_put(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

def _bs_call(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)

def round_strike(price, step):
    return round(price / step) * step

def est_put_prem(spot, strike, dte_days, iv, r=0.06):
    T = max(dte_days, 0.5) / 365.0
    return _bs_put(spot, strike, T, r, iv)

def est_call_prem(spot, strike, dte_days, iv, r=0.06):
    T = max(dte_days, 0.5) / 365.0
    return _bs_call(spot, strike, T, r, iv)


# ---------------------------------------------------------------------------
# EMA / RSI
# ---------------------------------------------------------------------------
def calc_ema(closes, period):
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    mult = 2 / (period + 1)
    for c in closes[period:]:
        ema = (c - ema) * mult + ema
    return ema

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    if len(gains) < period:
        return None
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ---------------------------------------------------------------------------
# Monthly expiry (last Tuesday — NSE stock F&O moved from Thu to Tue)
# ---------------------------------------------------------------------------
def _last_tuesday(year, month):
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    day = next_month - timedelta(days=1)
    while day.weekday() != 1:
        day -= timedelta(days=1)
    return day

def _monthly_expiry_for(ref_date):
    exp = _last_tuesday(ref_date.year, ref_date.month)
    if ref_date > exp:
        if ref_date.month == 12:
            exp = _last_tuesday(ref_date.year + 1, 1)
        else:
            exp = _last_tuesday(ref_date.year, ref_date.month + 1)
    return exp

def _days_to_expiry(ref_date):
    return (_monthly_expiry_for(ref_date) - ref_date).days


# ---------------------------------------------------------------------------
# Candle fetch with caching
# ---------------------------------------------------------------------------
def fetch_daily_candles(uclient, stock_name, from_date, to_date):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, f"{stock_name}_{from_date}_{to_date}_day.pkl")
    if os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            return pickle.load(f)
    stk = STOCKS[stock_name]
    from_dt = datetime(from_date.year, from_date.month, from_date.day, 9, 0, tzinfo=IST)
    to_dt = datetime(to_date.year, to_date.month, to_date.day, 16, 0, tzinfo=IST)
    candles = uclient.historical_data(stk["key"], from_dt, to_dt, "day")
    if candles and len(candles) > 5:
        with open(cache_file, "wb") as f:
            pickle.dump(candles, f)
    return candles


# ---------------------------------------------------------------------------
# Charges
# ---------------------------------------------------------------------------
def calc_charges(premium, lot_size, num_legs=4):
    turnover = premium * lot_size * num_legs
    brokerage = min(40, turnover * 0.0003) * 2
    stt = turnover * 0.000625
    exchange = turnover * 0.00053
    gst = (brokerage + exchange) * 0.18
    sebi = turnover * 0.000001
    stamp = turnover * 0.00003
    return round(brokerage + stt + exchange + gst + sebi + stamp, 2)


# ---------------------------------------------------------------------------
# Core spread runners
# ---------------------------------------------------------------------------
def run_bull_put_spread(daily_candles, stock_name, entry_date, expiry_date, *,
                        lots=1, profit_target_pct=0.30, stop_loss_mult=2.0,
                        close_dte=5, book_profit_after_days=2, uclient=None):
    stk = STOCKS[stock_name]
    iv, step = stk["iv_annual"], stk["strike_step"]
    lot_size = stk["lot_size"] * lots

    entry_candle = None
    for c in daily_candles:
        if c["date"][:10] == entry_date.isoformat():
            entry_candle = c
            break
    if not entry_candle:
        return {"skipped": True, "skip_reason": "no_entry_candle", "net_pnl": 0}

    spot = entry_candle["close"]
    dte = (expiry_date - entry_date).days
    sell_strike = round_strike(spot * 0.98, step)
    buy_strike = sell_strike - 2 * step
    if buy_strike <= 0:
        return {"skipped": True, "skip_reason": "invalid_strikes", "net_pnl": 0}

    # Try real option LTP, fall back to B-S
    prem_source = "bs"
    real = None
    if uclient:
        real = fetch_stock_option_ltp(uclient, stock_name, expiry_date,
                                      sell_strike, buy_strike, "PE")
    if real:
        sell_prem, buy_prem = real["sell"], real["buy"]
        prem_source = "real"
        _log.info("  REAL LTP %s bull put: sell=%.2f buy=%.2f", stock_name, sell_prem, buy_prem)
    else:
        sell_prem = est_put_prem(spot, sell_strike, dte, iv)
        buy_prem = est_put_prem(spot, buy_strike, dte, iv)

    net_credit = sell_prem - buy_prem
    if net_credit <= 0.5:
        return {"skipped": True, "skip_reason": "no_credit", "net_pnl": 0}

    profit_target = net_credit * profit_target_pct
    stop_loss_premium = net_credit * stop_loss_mult

    exit_date = exit_reason = exit_spread_val = None
    exit_pnl = 0

    trading_days = sorted(
        [c for c in daily_candles
         if entry_date.isoformat() < c["date"][:10] <= expiry_date.isoformat()],
        key=lambda c: c["date"])

    for dc in trading_days:
        dd = date.fromisoformat(dc["date"][:10])
        ds = dc["close"]
        rem = (expiry_date - dd).days

        # Try real LTP for exit monitoring too
        real_exit = None
        if uclient:
            real_exit = fetch_stock_option_ltp(uclient, stock_name, expiry_date,
                                              sell_strike, buy_strike, "PE")
        if real_exit:
            csv = real_exit["sell"] - real_exit["buy"]
        else:
            csv = est_put_prem(ds, sell_strike, rem, iv) - est_put_prem(ds, buy_strike, rem, iv)
        upnl = net_credit - csv

        if upnl >= profit_target:
            exit_date, exit_reason, exit_spread_val = dd, "profit_target", csv
            exit_pnl = upnl * lot_size; break
        if csv >= net_credit + stop_loss_premium:
            exit_date, exit_reason, exit_spread_val = dd, "stop_loss", csv
            exit_pnl = upnl * lot_size; break
        if rem <= close_dte:
            exit_date, exit_reason, exit_spread_val = dd, "dte_exit", csv
            exit_pnl = upnl * lot_size; break
        days_held = (dd - entry_date).days
        if days_held >= book_profit_after_days and upnl > 0:
            exit_date, exit_reason, exit_spread_val = dd, "eod_book", csv
            exit_pnl = upnl * lot_size; break

    if exit_date is None:
        exit_date, exit_reason = expiry_date, "expiry"
        fs = max(sell_strike - (trading_days[-1]["close"] if trading_days else spot), 0)
        fb = max(buy_strike - (trading_days[-1]["close"] if trading_days else spot), 0)
        exit_spread_val = fs - fb
        exit_pnl = (net_credit - exit_spread_val) * lot_size

    charges = calc_charges(net_credit + (exit_spread_val or 0), lot_size)
    return {
        "stock": stock_name, "direction": "bullish",
        "entry_date": entry_date.isoformat(), "exit_date": exit_date.isoformat(),
        "exit_reason": exit_reason, "spot_entry": round(spot, 2),
        "sell_strike": sell_strike, "buy_strike": buy_strike,
        "net_credit": round(net_credit, 2), "sell_premium": round(sell_prem, 2),
        "buy_premium": round(buy_prem, 2), "premium_source": prem_source,
        "exit_spread_val": round(exit_spread_val, 2) if exit_spread_val else 0,
        "gross_pnl": round(exit_pnl, 2), "charges": charges,
        "net_pnl": round(exit_pnl - charges, 2),
        "dte_at_entry": dte, "lot_size": lot_size, "skipped": False,
    }


def run_bear_call_spread(daily_candles, stock_name, entry_date, expiry_date, *,
                         lots=1, profit_target_pct=0.30, stop_loss_mult=2.0,
                         close_dte=5, book_profit_after_days=2, uclient=None):
    stk = STOCKS[stock_name]
    iv, step = stk["iv_annual"], stk["strike_step"]
    lot_size = stk["lot_size"] * lots

    entry_candle = None
    for c in daily_candles:
        if c["date"][:10] == entry_date.isoformat():
            entry_candle = c
            break
    if not entry_candle:
        return {"skipped": True, "skip_reason": "no_entry_candle", "net_pnl": 0}

    spot = entry_candle["close"]
    dte = (expiry_date - entry_date).days
    sell_strike = round_strike(spot * 1.02, step)
    buy_strike = sell_strike + 2 * step

    # Try real option LTP, fall back to B-S
    prem_source = "bs"
    real = None
    if uclient:
        real = fetch_stock_option_ltp(uclient, stock_name, expiry_date,
                                      sell_strike, buy_strike, "CE")
    if real:
        sell_prem, buy_prem = real["sell"], real["buy"]
        prem_source = "real"
        _log.info("  REAL LTP %s bear call: sell=%.2f buy=%.2f", stock_name, sell_prem, buy_prem)
    else:
        sell_prem = est_call_prem(spot, sell_strike, dte, iv)
        buy_prem = est_call_prem(spot, buy_strike, dte, iv)

    net_credit = sell_prem - buy_prem
    if net_credit <= 0.5:
        return {"skipped": True, "skip_reason": "no_credit", "net_pnl": 0}

    profit_target = net_credit * profit_target_pct
    stop_loss_premium = net_credit * stop_loss_mult

    exit_date = exit_reason = exit_spread_val = None
    exit_pnl = 0

    trading_days = sorted(
        [c for c in daily_candles
         if entry_date.isoformat() < c["date"][:10] <= expiry_date.isoformat()],
        key=lambda c: c["date"])

    for dc in trading_days:
        dd = date.fromisoformat(dc["date"][:10])
        ds = dc["close"]
        rem = (expiry_date - dd).days

        # Try real LTP for exit monitoring too
        real_exit = None
        if uclient:
            real_exit = fetch_stock_option_ltp(uclient, stock_name, expiry_date,
                                              sell_strike, buy_strike, "CE")
        if real_exit:
            csv = real_exit["sell"] - real_exit["buy"]
        else:
            csv = est_call_prem(ds, sell_strike, rem, iv) - est_call_prem(ds, buy_strike, rem, iv)
        upnl = net_credit - csv

        if upnl >= profit_target:
            exit_date, exit_reason, exit_spread_val = dd, "profit_target", csv
            exit_pnl = upnl * lot_size; break
        if csv >= net_credit + stop_loss_premium:
            exit_date, exit_reason, exit_spread_val = dd, "stop_loss", csv
            exit_pnl = upnl * lot_size; break
        if rem <= close_dte:
            exit_date, exit_reason, exit_spread_val = dd, "dte_exit", csv
            exit_pnl = upnl * lot_size; break
        days_held = (dd - entry_date).days
        if days_held >= book_profit_after_days and upnl > 0:
            exit_date, exit_reason, exit_spread_val = dd, "eod_book", csv
            exit_pnl = upnl * lot_size; break

    if exit_date is None:
        exit_date, exit_reason = expiry_date, "expiry"
        fs = max((trading_days[-1]["close"] if trading_days else spot) - sell_strike, 0)
        fb = max((trading_days[-1]["close"] if trading_days else spot) - buy_strike, 0)
        exit_spread_val = fs - fb
        exit_pnl = (net_credit - exit_spread_val) * lot_size

    charges = calc_charges(net_credit + (exit_spread_val or 0), lot_size)
    return {
        "stock": stock_name, "direction": "bearish",
        "entry_date": entry_date.isoformat(), "exit_date": exit_date.isoformat(),
        "exit_reason": exit_reason, "spot_entry": round(spot, 2),
        "sell_strike": sell_strike, "buy_strike": buy_strike,
        "net_credit": round(net_credit, 2), "sell_premium": round(sell_prem, 2),
        "buy_premium": round(buy_prem, 2), "premium_source": prem_source,
        "exit_spread_val": round(exit_spread_val, 2) if exit_spread_val else 0,
        "gross_pnl": round(exit_pnl, 2), "charges": charges,
        "net_pnl": round(exit_pnl - charges, 2),
        "dte_at_entry": dte, "lot_size": lot_size, "skipped": False,
    }


# ---------------------------------------------------------------------------
# Signal detection + run for a single date
# ---------------------------------------------------------------------------
def _find_signal_for_date(daily_candles, ref_date, stock_name, strategy):
    ema_period = strategy["ema_period"]
    rsi_period = strategy["rsi_period"]
    min_dte, max_dte = strategy["entry_dte_range"]

    dte = _days_to_expiry(ref_date)
    if not (min_dte <= dte <= max_dte):
        return None

    closes = []
    for c in daily_candles:
        cd = c["date"][:10]
        if cd > ref_date.isoformat():
            break
        closes.append(c["close"])

    if len(closes) < max(ema_period, rsi_period + 1):
        return None

    ema = calc_ema(closes, ema_period)
    rsi = calc_rsi(closes, rsi_period)
    if ema is None or rsi is None:
        return None

    spot = closes[-1]
    expiry = _monthly_expiry_for(ref_date)

    if spot > ema and rsi > strategy["rsi_bull"]:
        return {"direction": "bullish", "spot": spot, "ema": round(ema, 2),
                "rsi": round(rsi, 1), "expiry": expiry, "dte": dte}
    elif spot < ema and rsi < strategy["rsi_bear"]:
        return {"direction": "bearish", "spot": spot, "ema": round(ema, 2),
                "rsi": round(rsi, 1), "expiry": expiry, "dte": dte}
    return None


def _has_active_trade(conn, strategy, stock, expiry_date):
    """Check if there's already an entry for this stock+strategy in the current expiry cycle."""
    row = conn.execute(
        "SELECT 1 FROM stock_strategy_results WHERE strategy=? AND stock=? "
        "AND expiry_date=? AND skipped=0 LIMIT 1",
        (strategy, stock, expiry_date.isoformat())).fetchone()
    return row is not None


def run_day(ref_date: date, lots: int = 1, *, force: bool = False) -> dict:
    init_stock_strategy_db()

    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT strategy, stock FROM stock_strategy_results WHERE date=?",
            (ref_date.isoformat(),)).fetchall()
    if existing and not force:
        return _load_day_from_db(ref_date)

    uclient = UpstoxData()
    buffer_start = ref_date - timedelta(days=60)
    candle_cache = {}
    for sname in STOCKS:
        candle_cache[sname] = fetch_daily_candles(uclient, sname, buffer_start, ref_date + timedelta(days=35))

    results = {}
    for strat_name, params in STRATEGIES.items():
        strat_trades = {}
        strat_pnl = 0.0
        for stock_name in STOCKS:
            daily = candle_cache.get(stock_name, [])
            if not daily or len(daily) < 30:
                r = {"skipped": True, "skip_reason": "no_data", "net_pnl": 0}
            else:
                sig = _find_signal_for_date(daily, ref_date, stock_name, params)
                if sig is None:
                    r = {"skipped": True, "skip_reason": "no_signal", "net_pnl": 0}
                else:
                    r = None
                    with db.get_conn() as conn:
                        if _has_active_trade(conn, strat_name, stock_name, sig["expiry"]):
                            r = {"skipped": True, "skip_reason": "active_position", "net_pnl": 0}
                    if r is None:
                        if sig["direction"] == "bullish":
                            r = run_bull_put_spread(daily, stock_name, ref_date, sig["expiry"],
                                                    lots=lots, profit_target_pct=params["profit_target_pct"],
                                                    stop_loss_mult=params["stop_loss_mult"],
                                                    close_dte=params["close_dte"],
                                                    book_profit_after_days=params.get("book_profit_after_days", 2),
                                                    uclient=uclient)
                        else:
                            r = run_bear_call_spread(daily, stock_name, ref_date, sig["expiry"],
                                                     lots=lots, profit_target_pct=params["profit_target_pct"],
                                                     stop_loss_mult=params["stop_loss_mult"],
                                                     close_dte=params["close_dte"],
                                                     book_profit_after_days=params.get("book_profit_after_days", 2),
                                                     uclient=uclient)
                        r["rsi"] = sig["rsi"]
                        r["ema"] = sig["ema"]
                        r["expiry_date"] = sig["expiry"].isoformat()

            strat_trades[stock_name] = r
            strat_pnl += r.get("net_pnl", 0) or 0

            if not r.get("skipped"):
                with db.get_conn() as conn:
                    conn.execute("""INSERT OR REPLACE INTO stock_strategy_results
                        (date, strategy, stock, direction, lots, entry_date, exit_date,
                         exit_reason, spot_entry, sell_strike, buy_strike, net_credit,
                         exit_spread_val, gross_pnl, charges, net_pnl, dte_at_entry,
                         rsi, ema, skipped, skip_reason, expiry_date, premium_source)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ref_date.isoformat(), strat_name, stock_name,
                         r.get("direction"), lots,
                     r.get("entry_date"), r.get("exit_date"), r.get("exit_reason"),
                     r.get("spot_entry"), r.get("sell_strike"), r.get("buy_strike"),
                     r.get("net_credit"), r.get("exit_spread_val"),
                     r.get("gross_pnl"), r.get("charges"), r.get("net_pnl", 0),
                     r.get("dte_at_entry"), r.get("rsi"), r.get("ema"),
                     0, None,
                     r.get("expiry_date"), r.get("premium_source", "bs")))

        results[strat_name] = {"day_pnl": round(strat_pnl, 2), "stocks": strat_trades}
    return results


def _load_day_from_db(ref_date):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_strategy_results WHERE date=?",
            (ref_date.isoformat(),)).fetchall()
    results = {}
    for r in rows:
        r = dict(r)
        sname = r["strategy"]
        if sname not in results:
            results[sname] = {"day_pnl": 0.0, "stocks": {}}
        results[sname]["stocks"][r["stock"]] = r
        results[sname]["day_pnl"] += r["net_pnl"] or 0
    for s in results:
        results[s]["day_pnl"] = round(results[s]["day_pnl"], 2)
    return results


# ---------------------------------------------------------------------------
# Daily refresh of active positions
# ---------------------------------------------------------------------------
def refresh_active_positions(today: date | None = None) -> list[dict]:
    """Re-evaluate open positions with fresh candle data.

    Finds trades where exit_reason='expiry' and expiry_date > today, then
    re-simulates with updated candles to check profit target / eod_book / SL.
    """
    init_stock_strategy_db()
    if today is None:
        today = date.today()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_strategy_results WHERE skipped=0 "
            "AND exit_reason='expiry' AND expiry_date>?",
            (today.isoformat(),)).fetchall()
    if not rows:
        return []

    uclient = UpstoxData()
    updated = []
    for row in rows:
        row = dict(row)
        stock_name = row["stock"]
        if stock_name not in STOCKS:
            continue
        stk = STOCKS[stock_name]
        entry_date = date.fromisoformat(row["entry_date"])
        expiry_date = date.fromisoformat(row["expiry_date"])
        net_credit = row["net_credit"]
        sell_strike = row["sell_strike"]
        buy_strike = row["buy_strike"]
        direction = row["direction"]
        lot_size = stk["lot_size"] * (row.get("lots") or 1)
        strategy = row["strategy"]
        params = STRATEGIES.get(strategy, {})
        profit_target_pct = params.get("profit_target_pct", 0.30)
        stop_loss_mult = params.get("stop_loss_mult", 2.0)
        close_dte = params.get("close_dte", 5)
        book_profit_after_days = params.get("book_profit_after_days", 2)

        profit_target = net_credit * profit_target_pct
        stop_loss_premium = net_credit * stop_loss_mult

        buffer_start = entry_date - timedelta(days=5)
        candles = fetch_daily_candles(uclient, stock_name, buffer_start,
                                     today + timedelta(days=1))
        trading_days = sorted(
            [c for c in candles
             if entry_date.isoformat() < c["date"][:10] <= today.isoformat()],
            key=lambda c: c["date"])

        exit_date = exit_reason = exit_spread_val = None
        exit_pnl = 0
        opt_type = "PE" if direction == "bullish" else "CE"
        iv = stk["iv_annual"]

        for dc in trading_days:
            dd = date.fromisoformat(dc["date"][:10])
            ds = dc["close"]
            rem = (expiry_date - dd).days

            real_exit = fetch_stock_option_ltp(uclient, stock_name, expiry_date,
                                              sell_strike, buy_strike, opt_type)
            if real_exit:
                csv = real_exit["sell"] - real_exit["buy"]
            else:
                if opt_type == "PE":
                    csv = est_put_prem(ds, sell_strike, rem, iv) - est_put_prem(ds, buy_strike, rem, iv)
                else:
                    csv = est_call_prem(ds, sell_strike, rem, iv) - est_call_prem(ds, buy_strike, rem, iv)
            upnl = net_credit - csv

            if upnl >= profit_target:
                exit_date, exit_reason, exit_spread_val = dd, "profit_target", csv
                exit_pnl = upnl * lot_size; break
            if csv >= net_credit + stop_loss_premium:
                exit_date, exit_reason, exit_spread_val = dd, "stop_loss", csv
                exit_pnl = upnl * lot_size; break
            if rem <= close_dte:
                exit_date, exit_reason, exit_spread_val = dd, "dte_exit", csv
                exit_pnl = upnl * lot_size; break
            days_held = (dd - entry_date).days
            if days_held >= book_profit_after_days and upnl > 0:
                exit_date, exit_reason, exit_spread_val = dd, "eod_book", csv
                exit_pnl = upnl * lot_size; break

        if exit_date is None:
            continue

        charges = calc_charges(net_credit + (exit_spread_val or 0), lot_size)
        net_pnl = round(exit_pnl - charges, 2)

        with db.get_conn() as conn:
            conn.execute(
                "UPDATE stock_strategy_results SET exit_date=?, exit_reason=?, "
                "exit_spread_val=?, gross_pnl=?, charges=?, net_pnl=? "
                "WHERE date=? AND strategy=? AND stock=?",
                (exit_date.isoformat(), exit_reason, round(exit_spread_val, 2),
                 round(exit_pnl, 2), charges, net_pnl,
                 row["date"], strategy, stock_name))

        updated.append({
            "stock": stock_name, "strategy": strategy,
            "entry_date": row["entry_date"], "exit_date": exit_date.isoformat(),
            "exit_reason": exit_reason, "net_pnl": net_pnl,
        })
        _log.info("Refreshed %s/%s: %s on %s pnl=%+.0f",
                  stock_name, strategy, exit_reason, exit_date, net_pnl)

    return updated


# ---------------------------------------------------------------------------
# Backfill + query helpers
# ---------------------------------------------------------------------------
def backfill(from_date: date, to_date: date, lots: int = 1, *, force: bool = False):
    if force:
        init_stock_strategy_db()
        with db.get_conn() as conn:
            conn.execute("DELETE FROM stock_strategy_results WHERE date>=? AND date<=?",
                         (from_date.isoformat(), to_date.isoformat()))
            print(f"  Cleared existing data for {from_date} to {to_date}")
    d = from_date
    while d <= to_date:
        if d.weekday() < 5:
            print(f"  Running {d}...", end=" ", flush=True)
            try:
                res = run_day(d, lots, force=force)
                pnl = sum(res[s]["day_pnl"] for s in res if res[s]["day_pnl"])
                active = sum(1 for s in res for st, r in res[s].get("stocks", {}).items() if not r.get("skipped"))
                print(f"trades={active} pnl={pnl:+,.0f}")
            except Exception as e:
                print(f"ERROR: {e}")
        d += timedelta(days=1)


def get_history(days: int = 90) -> list[dict]:
    init_stock_strategy_db()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_strategy_results WHERE date>=? ORDER BY date, strategy, stock",
            (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def get_daily_summary(days: int = 90) -> dict:
    rows = get_history(days)
    strats = {}
    for r in rows:
        s = r["strategy"]
        d = r["date"]
        if r.get("skipped"):
            continue
        if s not in strats:
            strats[s] = {}
        if d not in strats[s]:
            strats[s][d] = {"pnl": 0.0, "trades": 0, "wins": 0, "stocks": []}
        strats[s][d]["pnl"] += r["net_pnl"] or 0
        strats[s][d]["trades"] += 1
        if (r["net_pnl"] or 0) > 0:
            strats[s][d]["wins"] += 1
        strats[s][d]["stocks"].append({
            "stock": r["stock"], "pnl": r["net_pnl"],
            "direction": r["direction"], "exit_reason": r["exit_reason"]
        })

    result = {}
    for s, daily in strats.items():
        days_list = sorted(daily.items())
        dates = [d for d, _ in days_list]
        pnls = [round(v["pnl"], 2) for _, v in days_list]
        total = round(sum(pnls), 2)
        green = sum(1 for p in pnls if p > 0)
        traded = len(pnls)
        cum = []
        running = 0
        for d, v in days_list:
            running += v["pnl"]
            cum.append({"date": d, "pnl": round(v["pnl"], 2),
                        "cumulative": round(running, 2),
                        "trades": v["trades"], "wins": v["wins"]})

        result[s] = {
            "dates": dates, "pnls": pnls, "total": total,
            "green": green, "traded": traded,
            "win_rate": round(green / traded * 100, 1) if traded else 0,
            "avg_day": round(total / traded, 2) if traded else 0,
            "max_day": max(pnls) if pnls else 0,
            "min_day": min(pnls) if pnls else 0,
            "cumulative": cum,
        }
    return result


def get_today_detail() -> dict:
    today = date.today().isoformat()
    init_stock_strategy_db()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_strategy_results WHERE date=? ORDER BY strategy, stock",
            (today,)).fetchall()
    result = {}
    for r in rows:
        r = dict(r)
        s = r["strategy"]
        if s not in result:
            result[s] = {"day_pnl": 0.0, "stocks": {}}
        if not r.get("skipped"):
            result[s]["stocks"][r["stock"]] = r
            result[s]["day_pnl"] += r["net_pnl"] or 0
    for s in result:
        result[s]["day_pnl"] = round(result[s]["day_pnl"], 2)
    return result


def get_stock_summary(days: int = 90) -> dict:
    """Per-stock breakdown across all strategies."""
    rows = get_history(days)
    by_stock = {}
    for r in rows:
        if r.get("skipped"):
            continue
        stock = r["stock"]
        strat = r["strategy"]
        key = f"{stock}_{strat}"
        if key not in by_stock:
            by_stock[key] = {"stock": stock, "strategy": strat,
                             "trades": 0, "wins": 0, "total_pnl": 0}
        by_stock[key]["trades"] += 1
        by_stock[key]["total_pnl"] += r["net_pnl"] or 0
        if (r["net_pnl"] or 0) > 0:
            by_stock[key]["wins"] += 1
    for k in by_stock:
        by_stock[k]["total_pnl"] = round(by_stock[k]["total_pnl"], 2)
        t = by_stock[k]["trades"]
        by_stock[k]["win_rate"] = round(by_stock[k]["wins"] / t * 100, 1) if t else 0
    return list(by_stock.values())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, default=None)
    parser.add_argument("--backfill-from", type=str, default=None)
    parser.add_argument("--backfill-to", type=str, default=None)
    parser.add_argument("--lots", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    from src.utils import market_calendar as mc

    if args.backfill_from and args.backfill_to:
        f = date.fromisoformat(args.backfill_from)
        t = date.fromisoformat(args.backfill_to)
        print(f"Backfilling {f} to {t}{'  (force)' if args.force else ''}...")
        backfill(f, t, args.lots, force=args.force)
    else:
        d = date.fromisoformat(args.date) if args.date else mc.now_ist().date()
        print(f"Running stock strategies for {d}...")
        res = run_day(d, args.lots, force=args.force)
        for s, data in res.items():
            active = {k: v for k, v in data["stocks"].items() if not v.get("skipped")}
            print(f"  {s}: {data['day_pnl']:+,.0f} ({len(active)} trades)")

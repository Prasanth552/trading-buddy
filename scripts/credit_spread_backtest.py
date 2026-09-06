"""Credit Spread (Bull Put Spread) backtester for NSE stock options.

Tests selling OTM put spreads on liquid stocks when:
  - Stock is above 20 EMA (bullish bias)
  - RSI(14) > 50 (momentum confirmation)
  - Entry 20-25 days before monthly expiry
  - Exit at 50% profit target, 2x premium stop, or 5 DTE

Uses Black-Scholes to estimate option premiums from daily candle data.

Usage:
    .venv/bin/python3 scripts/credit_spread_backtest.py \
        --from 2026-06-01 --to 2026-08-31 --lots 1

    # Single stock:
    .venv/bin/python3 scripts/credit_spread_backtest.py \
        --stock RELIANCE --from 2026-06-01 --to 2026-08-31
"""
from __future__ import annotations

import argparse
import math
import os
import pickle
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from src.broker.upstox_data import UpstoxData

IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Stock configs — lot sizes from NSE (as of mid-2026), strike steps, IV
# ---------------------------------------------------------------------------
STOCKS = {
    "RELIANCE": {
        "key": "NSE_EQ|INE002A01018",
        "lot_size": 250,
        "strike_step": 20,
        "iv_annual": 0.25,
    },
    "HDFCBANK": {
        "key": "NSE_EQ|INE040A01034",
        "lot_size": 550,
        "strike_step": 20,
        "iv_annual": 0.22,
    },
    "ICICIBANK": {
        "key": "NSE_EQ|INE090A01021",
        "lot_size": 700,
        "strike_step": 20,
        "iv_annual": 0.24,
    },
    "TCS": {
        "key": "NSE_EQ|INE467B01029",
        "lot_size": 175,
        "strike_step": 50,
        "iv_annual": 0.22,
    },
    "INFY": {
        "key": "NSE_EQ|INE009A01021",
        "lot_size": 400,
        "strike_step": 20,
        "iv_annual": 0.25,
    },
    "SBIN": {
        "key": "NSE_EQ|INE062A01020",
        "lot_size": 750,
        "strike_step": 10,
        "iv_annual": 0.28,
    },
    "TATAMOTORS": {
        "key": "NSE_EQ|INE155A01022",
        "lot_size": 1400,
        "strike_step": 10,
        "iv_annual": 0.35,
    },
    "BAJFINANCE": {
        "key": "NSE_EQ|INE296A01032",
        "lot_size": 125,
        "strike_step": 50,
        "iv_annual": 0.30,
    },
    "LT": {
        "key": "NSE_EQ|INE018A01030",
        "lot_size": 150,
        "strike_step": 25,
        "iv_annual": 0.25,
    },
    "TATASTEEL": {
        "key": "NSE_EQ|INE081A01020",
        "lot_size": 5000,
        "strike_step": 5,
        "iv_annual": 0.35,
    },
}

CACHE_DIR = os.path.join(config.DATA_DIR, "stock_candle_cache")

# ---------------------------------------------------------------------------
# Black-Scholes helpers
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
# EMA and RSI calculations
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
# Monthly expiry helpers — stock options expire last Thursday of month
# ---------------------------------------------------------------------------
def _last_thursday(year, month):
    """Find last Thursday of the given month."""
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    day = next_month - timedelta(days=1)
    while day.weekday() != 3:  # Thursday = 3
        day -= timedelta(days=1)
    return day

def _monthly_expiry_for(ref_date):
    """Get the current month's expiry. If past it, get next month's."""
    exp = _last_thursday(ref_date.year, ref_date.month)
    if ref_date > exp:
        if ref_date.month == 12:
            exp = _last_thursday(ref_date.year + 1, 1)
        else:
            exp = _last_thursday(ref_date.year, ref_date.month + 1)
    return exp

def _days_to_expiry(ref_date):
    exp = _monthly_expiry_for(ref_date)
    return (exp - ref_date).days

# ---------------------------------------------------------------------------
# Fetch daily candles with caching
# ---------------------------------------------------------------------------
def fetch_daily_candles(uclient, stock_name, from_date, to_date):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_key = f"{stock_name}_{from_date}_{to_date}_day"
    cache_file = os.path.join(CACHE_DIR, f"{cache_key}.pkl")
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

def fetch_intraday_candles(uclient, stock_name, ref_date):
    """Fetch 5-min candles for a single day (for intraday spread monitoring)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, f"{stock_name}_{ref_date}_5min.pkl")
    if os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            return pickle.load(f)
    stk = STOCKS[stock_name]
    from_dt = datetime(ref_date.year, ref_date.month, ref_date.day, 9, 0, tzinfo=IST)
    to_dt = datetime(ref_date.year, ref_date.month, ref_date.day, 16, 0, tzinfo=IST)
    candles = uclient.historical_data(stk["key"], from_dt, to_dt, "5minute")
    if candles and len(candles) > 5:
        with open(cache_file, "wb") as f:
            pickle.dump(candles, f)
    return candles

# ---------------------------------------------------------------------------
# Charges estimation (equity F&O)
# ---------------------------------------------------------------------------
def calc_charges(premium_collected, lot_size, num_legs=2):
    turnover = premium_collected * lot_size * num_legs
    brokerage = min(40, turnover * 0.0003) * 2  # entry + exit
    stt = turnover * 0.000625  # sell side STT for options
    exchange = turnover * 0.00053
    gst = (brokerage + exchange) * 0.18
    sebi = turnover * 0.000001
    stamp = turnover * 0.00003
    return round(brokerage + stt + exchange + gst + sebi + stamp, 2)

# ---------------------------------------------------------------------------
# Core: run a bull put spread for one entry
# ---------------------------------------------------------------------------
def run_bull_put_spread(daily_candles, intraday_candles_by_date, stock_name,
                        entry_date, expiry_date, *, lots=1,
                        profit_target_pct=0.50, stop_loss_mult=2.0,
                        close_dte=5):
    """
    Simulate a bull put spread:
    - Sell OTM put at ~0.20 delta (approx 1.5-2% OTM)
    - Buy further OTM put (2 strikes lower) as protection
    - Monitor daily until exit condition hit

    Returns result dict with P&L and trade details.
    """
    stk = STOCKS[stock_name]
    iv = stk["iv_annual"]
    step = stk["strike_step"]
    lot_size = stk["lot_size"] * lots

    # Find entry day candle
    entry_candle = None
    for c in daily_candles:
        cdate = c["date"][:10]
        if cdate == entry_date.isoformat():
            entry_candle = c
            break
    if not entry_candle:
        return {"skipped": True, "skip_reason": "no_entry_candle", "net_pnl": 0}

    spot = entry_candle["close"]
    dte = (expiry_date - entry_date).days

    # Sell put at ~2% OTM (0.20 delta approx)
    sell_strike = round_strike(spot * 0.98, step)
    # Buy put 2 strikes further OTM
    buy_strike = sell_strike - 2 * step

    if buy_strike <= 0:
        return {"skipped": True, "skip_reason": "invalid_strikes", "net_pnl": 0}

    # Estimate premiums at entry
    sell_prem = est_put_prem(spot, sell_strike, dte, iv)
    buy_prem = est_put_prem(spot, buy_strike, dte, iv)
    net_credit = sell_prem - buy_prem

    if net_credit <= 0.5:
        return {"skipped": True, "skip_reason": "no_credit", "net_pnl": 0}

    max_profit = net_credit * lot_size
    spread_width = sell_strike - buy_strike
    max_loss = (spread_width - net_credit) * lot_size

    profit_target = net_credit * profit_target_pct
    stop_loss_premium = net_credit * stop_loss_mult  # close if spread widens to 2x credit

    # Monitor daily from entry+1 to expiry
    exit_date = None
    exit_reason = None
    exit_pnl = None
    exit_spread_val = None

    trading_days = sorted([c for c in daily_candles
                           if entry_date.isoformat() < c["date"][:10] <= expiry_date.isoformat()],
                          key=lambda c: c["date"])

    for day_candle in trading_days:
        day_date = date.fromisoformat(day_candle["date"][:10])
        day_spot = day_candle["close"]
        remaining_dte = (expiry_date - day_date).days

        # Re-estimate spread value
        cur_sell_prem = est_put_prem(day_spot, sell_strike, remaining_dte, iv)
        cur_buy_prem = est_put_prem(day_spot, buy_strike, remaining_dte, iv)
        cur_spread_val = cur_sell_prem - cur_buy_prem

        # P&L = credit received - current spread value
        unrealized_pnl_per_unit = net_credit - cur_spread_val

        # Check profit target: spread value dropped to < 50% of credit
        if unrealized_pnl_per_unit >= profit_target:
            exit_date = day_date
            exit_reason = "profit_target"
            exit_spread_val = cur_spread_val
            exit_pnl = unrealized_pnl_per_unit * lot_size
            break

        # Check stop loss: spread widened beyond 2x credit
        if cur_spread_val >= net_credit + stop_loss_premium:
            exit_date = day_date
            exit_reason = "stop_loss"
            exit_spread_val = cur_spread_val
            exit_pnl = unrealized_pnl_per_unit * lot_size
            break

        # Check DTE exit: close 5 days before expiry
        if remaining_dte <= close_dte:
            exit_date = day_date
            exit_reason = "dte_exit"
            exit_spread_val = cur_spread_val
            exit_pnl = unrealized_pnl_per_unit * lot_size
            break

    if exit_date is None:
        # Hold to expiry
        exit_date = expiry_date
        exit_reason = "expiry"
        # At expiry, value is intrinsic only
        final_sell = max(sell_strike - trading_days[-1]["close"] if trading_days else 0, 0)
        final_buy = max(buy_strike - trading_days[-1]["close"] if trading_days else 0, 0)
        exit_spread_val = final_sell - final_buy
        exit_pnl = (net_credit - exit_spread_val) * lot_size

    charges = calc_charges(net_credit + (exit_spread_val or 0), lot_size, num_legs=4)
    net_pnl = round(exit_pnl - charges, 2)

    return {
        "stock": stock_name,
        "entry_date": entry_date.isoformat(),
        "exit_date": exit_date.isoformat(),
        "exit_reason": exit_reason,
        "spot_entry": round(spot, 2),
        "sell_strike": sell_strike,
        "buy_strike": buy_strike,
        "net_credit": round(net_credit, 2),
        "exit_spread_val": round(exit_spread_val, 2) if exit_spread_val else 0,
        "max_profit": round(max_profit, 2),
        "max_loss": round(max_loss, 2),
        "gross_pnl": round(exit_pnl, 2),
        "charges": charges,
        "net_pnl": net_pnl,
        "dte_at_entry": dte,
        "lot_size": lot_size,
        "skipped": False,
    }


# ---------------------------------------------------------------------------
# Bear Call Spread (for bearish signals)
# ---------------------------------------------------------------------------
def run_bear_call_spread(daily_candles, intraday_candles_by_date, stock_name,
                         entry_date, expiry_date, *, lots=1,
                         profit_target_pct=0.50, stop_loss_mult=2.0,
                         close_dte=5):
    stk = STOCKS[stock_name]
    iv = stk["iv_annual"]
    step = stk["strike_step"]
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

    sell_prem = est_call_prem(spot, sell_strike, dte, iv)
    buy_prem = est_call_prem(spot, buy_strike, dte, iv)
    net_credit = sell_prem - buy_prem

    if net_credit <= 0.5:
        return {"skipped": True, "skip_reason": "no_credit", "net_pnl": 0}

    max_profit = net_credit * lot_size
    spread_width = buy_strike - sell_strike
    max_loss = (spread_width - net_credit) * lot_size

    profit_target = net_credit * profit_target_pct
    stop_loss_premium = net_credit * stop_loss_mult

    exit_date = None
    exit_reason = None
    exit_pnl = None
    exit_spread_val = None

    trading_days = sorted([c for c in daily_candles
                           if entry_date.isoformat() < c["date"][:10] <= expiry_date.isoformat()],
                          key=lambda c: c["date"])

    for day_candle in trading_days:
        day_date = date.fromisoformat(day_candle["date"][:10])
        day_spot = day_candle["close"]
        remaining_dte = (expiry_date - day_date).days

        cur_sell_prem = est_call_prem(day_spot, sell_strike, remaining_dte, iv)
        cur_buy_prem = est_call_prem(day_spot, buy_strike, remaining_dte, iv)
        cur_spread_val = cur_sell_prem - cur_buy_prem

        unrealized_pnl_per_unit = net_credit - cur_spread_val

        if unrealized_pnl_per_unit >= profit_target:
            exit_date = day_date
            exit_reason = "profit_target"
            exit_spread_val = cur_spread_val
            exit_pnl = unrealized_pnl_per_unit * lot_size
            break

        if cur_spread_val >= net_credit + stop_loss_premium:
            exit_date = day_date
            exit_reason = "stop_loss"
            exit_spread_val = cur_spread_val
            exit_pnl = unrealized_pnl_per_unit * lot_size
            break

        if remaining_dte <= close_dte:
            exit_date = day_date
            exit_reason = "dte_exit"
            exit_spread_val = cur_spread_val
            exit_pnl = unrealized_pnl_per_unit * lot_size
            break

    if exit_date is None:
        exit_date = expiry_date
        exit_reason = "expiry"
        final_sell = max(trading_days[-1]["close"] - sell_strike if trading_days else 0, 0)
        final_buy = max(trading_days[-1]["close"] - buy_strike if trading_days else 0, 0)
        exit_spread_val = final_sell - final_buy
        exit_pnl = (net_credit - exit_spread_val) * lot_size

    charges = calc_charges(net_credit + (exit_spread_val or 0), lot_size, num_legs=4)
    net_pnl = round(exit_pnl - charges, 2)

    return {
        "stock": stock_name,
        "entry_date": entry_date.isoformat(),
        "exit_date": exit_date.isoformat(),
        "exit_reason": exit_reason,
        "spot_entry": round(spot, 2),
        "sell_strike": sell_strike,
        "buy_strike": buy_strike,
        "net_credit": round(net_credit, 2),
        "exit_spread_val": round(exit_spread_val, 2) if exit_spread_val else 0,
        "max_profit": round(max_profit, 2),
        "max_loss": round(max_loss, 2),
        "gross_pnl": round(exit_pnl, 2),
        "charges": charges,
        "net_pnl": net_pnl,
        "dte_at_entry": dte,
        "lot_size": lot_size,
        "skipped": False,
        "direction": "bearish",
    }


# ---------------------------------------------------------------------------
# Strategy variants
# ---------------------------------------------------------------------------
STRATEGY_VARIANTS = {
    "ema20_rsi50": {
        "description": "Bull put when above EMA20 & RSI>50; Bear call when below & RSI<50",
        "ema_period": 20,
        "rsi_period": 14,
        "rsi_bull_threshold": 50,
        "rsi_bear_threshold": 50,
        "entry_dte_range": (15, 28),
        "profit_target_pct": 0.50,
        "stop_loss_mult": 2.0,
        "close_dte": 5,
    },
    "ema20_rsi60": {
        "description": "Stricter RSI filter (>60 bull, <40 bear)",
        "ema_period": 20,
        "rsi_period": 14,
        "rsi_bull_threshold": 60,
        "rsi_bear_threshold": 40,
        "entry_dte_range": (15, 28),
        "profit_target_pct": 0.50,
        "stop_loss_mult": 2.0,
        "close_dte": 5,
    },
    "ema20_rsi50_tight": {
        "description": "Tighter stop (1.5x) and quicker profit (40%)",
        "ema_period": 20,
        "rsi_period": 14,
        "rsi_bull_threshold": 50,
        "rsi_bear_threshold": 50,
        "entry_dte_range": (15, 28),
        "profit_target_pct": 0.40,
        "stop_loss_mult": 1.5,
        "close_dte": 5,
    },
    "ema20_rsi50_wide": {
        "description": "Wider stop (3x), higher target (60%)",
        "ema_period": 20,
        "rsi_period": 14,
        "rsi_bull_threshold": 50,
        "rsi_bear_threshold": 50,
        "entry_dte_range": (15, 28),
        "profit_target_pct": 0.60,
        "stop_loss_mult": 3.0,
        "close_dte": 5,
    },
}


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------
def find_entry_signals(daily_candles, stock_name, from_date, to_date, strategy):
    """Scan daily candles for entry signals based on EMA + RSI."""
    ema_period = strategy["ema_period"]
    rsi_period = strategy["rsi_period"]
    rsi_bull = strategy["rsi_bull_threshold"]
    rsi_bear = strategy["rsi_bear_threshold"]
    min_dte, max_dte = strategy["entry_dte_range"]

    signals = []
    closes = []

    for c in daily_candles:
        cdate = date.fromisoformat(c["date"][:10])
        closes.append(c["close"])

        if cdate < from_date or cdate > to_date:
            continue
        if cdate.weekday() >= 5:
            continue

        dte = _days_to_expiry(cdate)
        if not (min_dte <= dte <= max_dte):
            continue

        if len(closes) < max(ema_period, rsi_period + 1):
            continue

        ema = calc_ema(closes, ema_period)
        rsi = calc_rsi(closes, rsi_period)
        if ema is None or rsi is None:
            continue

        spot = c["close"]
        expiry = _monthly_expiry_for(cdate)

        if spot > ema and rsi > rsi_bull:
            signals.append({
                "date": cdate, "direction": "bullish",
                "spot": spot, "ema": round(ema, 2), "rsi": round(rsi, 1),
                "expiry": expiry, "dte": dte
            })
        elif spot < ema and rsi < rsi_bear:
            signals.append({
                "date": cdate, "direction": "bearish",
                "spot": spot, "ema": round(ema, 2), "rsi": round(rsi, 1),
                "expiry": expiry, "dte": dte
            })

    return signals


def backtest_stock(uclient, stock_name, from_date, to_date, strategy_name, lots=1):
    """Run backtest for one stock across the date range."""
    strategy = STRATEGY_VARIANTS[strategy_name]

    # Fetch daily candles with buffer for EMA calculation
    buffer_start = from_date - timedelta(days=60)
    daily = fetch_daily_candles(uclient, stock_name, buffer_start, to_date)
    if not daily or len(daily) < 30:
        return {"stock": stock_name, "error": "insufficient data", "trades": []}

    signals = find_entry_signals(daily, stock_name, from_date, to_date, strategy)

    trades = []
    active_expiry = None

    for sig in signals:
        # Only one trade per expiry cycle per stock
        if active_expiry and sig["expiry"] == active_expiry:
            continue

        if sig["direction"] == "bullish":
            result = run_bull_put_spread(
                daily, {}, stock_name, sig["date"], sig["expiry"],
                lots=lots,
                profit_target_pct=strategy["profit_target_pct"],
                stop_loss_mult=strategy["stop_loss_mult"],
                close_dte=strategy["close_dte"],
            )
        else:
            result = run_bear_call_spread(
                daily, {}, stock_name, sig["date"], sig["expiry"],
                lots=lots,
                profit_target_pct=strategy["profit_target_pct"],
                stop_loss_mult=strategy["stop_loss_mult"],
                close_dte=strategy["close_dte"],
            )

        if not result.get("skipped"):
            result["direction"] = sig["direction"]
            result["rsi"] = sig["rsi"]
            result["ema"] = sig["ema"]
            trades.append(result)
            active_expiry = sig["expiry"]

    return {"stock": stock_name, "trades": trades}


def print_results(all_results, strategy_name):
    """Print formatted backtest results."""
    print(f"\n{'='*70}")
    print(f"  CREDIT SPREAD BACKTEST — {strategy_name.upper()}")
    print(f"  {STRATEGY_VARIANTS[strategy_name]['description']}")
    print(f"{'='*70}")

    grand_trades = []
    grand_pnl = 0

    for res in all_results:
        stock = res["stock"]
        trades = res.get("trades", [])
        if res.get("error"):
            print(f"\n  {stock}: {res['error']}")
            continue
        if not trades:
            print(f"\n  {stock}: no signals in range")
            continue

        total_pnl = sum(t["net_pnl"] for t in trades)
        wins = sum(1 for t in trades if t["net_pnl"] > 0)
        losses = len(trades) - wins
        wr = wins / len(trades) * 100 if trades else 0

        print(f"\n  {stock}  |  {len(trades)} trades  |  W:{wins} L:{losses}  |  WR:{wr:.0f}%  |  Total: {total_pnl:+,.0f}")

        for t in trades:
            tag = "WIN " if t["net_pnl"] > 0 else "LOSS"
            direction = t.get("direction", "bullish")[:4].upper()
            print(f"    {t['entry_date']} → {t['exit_date']}  {direction}  "
                  f"Spot:{t['spot_entry']:,.0f}  "
                  f"Sell:{t['sell_strike']:,.0f}  Buy:{t['buy_strike']:,.0f}  "
                  f"Credit:{t['net_credit']:.1f}  "
                  f"Exit:{t['exit_reason']:14s}  "
                  f"{tag}  {t['net_pnl']:>+9,.0f}")

        grand_trades.extend(trades)
        grand_pnl += total_pnl

    # Grand summary
    if grand_trades:
        wins = sum(1 for t in grand_trades if t["net_pnl"] > 0)
        losses = len(grand_trades) - wins
        wr = wins / len(grand_trades) * 100
        avg = grand_pnl / len(grand_trades)
        best = max(t["net_pnl"] for t in grand_trades)
        worst = min(t["net_pnl"] for t in grand_trades)
        max_loss_trades = [t for t in grand_trades if t["exit_reason"] == "stop_loss"]
        target_trades = [t for t in grand_trades if t["exit_reason"] == "profit_target"]
        dte_trades = [t for t in grand_trades if t["exit_reason"] == "dte_exit"]

        print(f"\n{'─'*70}")
        print(f"  GRAND TOTAL  |  {len(grand_trades)} trades across {len([r for r in all_results if r.get('trades')])} stocks")
        print(f"  P&L: {grand_pnl:+,.0f}  |  Win Rate: {wr:.1f}%  ({wins}W / {losses}L)")
        print(f"  Avg/trade: {avg:+,.0f}  |  Best: {best:+,.0f}  |  Worst: {worst:+,.0f}")
        print(f"  Exits: {len(target_trades)} profit_target  |  {len(max_loss_trades)} stop_loss  |  {len(dte_trades)} dte_exit")
        print(f"{'─'*70}\n")

    return grand_trades


def main():
    parser = argparse.ArgumentParser(description="Credit Spread Backtester")
    parser.add_argument("--from", dest="from_date", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--lots", type=int, default=1)
    parser.add_argument("--stock", type=str, default=None, help="Single stock to test (e.g. RELIANCE)")
    parser.add_argument("--strategy", type=str, default=None,
                        help="Strategy variant (default: run all). Options: " +
                             ", ".join(STRATEGY_VARIANTS.keys()))
    args = parser.parse_args()

    from_date = date.fromisoformat(args.from_date)
    to_date = date.fromisoformat(args.to_date)

    stocks_to_test = [args.stock] if args.stock else list(STOCKS.keys())
    strategies_to_test = [args.strategy] if args.strategy else list(STRATEGY_VARIANTS.keys())

    uclient = UpstoxData()

    for sname in strategies_to_test:
        if sname not in STRATEGY_VARIANTS:
            print(f"Unknown strategy: {sname}")
            continue

        all_results = []
        for stock in stocks_to_test:
            if stock not in STOCKS:
                print(f"Unknown stock: {stock}")
                continue
            print(f"  Testing {stock} with {sname}...", flush=True)
            result = backtest_stock(uclient, stock, from_date, to_date, sname, args.lots)
            all_results.append(result)

        print_results(all_results, sname)


if __name__ == "__main__":
    main()

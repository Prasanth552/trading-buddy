"""Backtest the scanner strategies over historical data.

Uses yfinance for stock prices, Nifty, and Brent crude.
Approximates option P&L using ATM delta (~0.5) on stock moves.

Strategies:
  1. Earnings Momentum — big movers (>3% gap) as proxy for earnings
  2. Sector Rotation — crude oil drives airline/energy/metal plays
  3. FII Flow Momentum — Nifty trend as proxy for FII flow
  4. Pre-market Gap — stocks gapping >1.5%
  5. Intraday Momentum — stocks moving >1% from open with volume

Run:  python scripts/backtest_scanner.py
"""
from __future__ import annotations

import sys
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import yfinance as yf
import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BACKTEST_DAYS = 90
INITIAL_CAPITAL = 100_000
MAX_TRADES_PER_DAY = 3
LOTS = 1

ATM_DELTA = 0.50
AVG_PREMIUM_PCT = 0.015
SL_PCT = 0.30
TARGET_MULT = 2.0
CHARGES_PER_TRADE = 150

SECTOR_MAP = {
    "HINDALCO": "METAL", "TATASTEEL": "METAL", "JSWSTEEL": "METAL",
    "VEDL": "METAL", "JINDALSTEL": "METAL", "COALINDIA": "METAL",
    "NATIONALUM": "METAL", "SAIL": "METAL",
    "MARUTI": "AUTO", "TATAMOTORS": "AUTO", "M&M": "AUTO",
    "BAJAJ-AUTO": "AUTO", "HEROMOTOCO": "AUTO", "EICHERMOT": "AUTO",
    "INDIGO": "AIRLINE",
    "RELIANCE": "ENERGY", "ONGC": "ENERGY", "BPCL": "ENERGY",
    "IOC": "ENERGY", "GAIL": "ENERGY",
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT",
    "TECHM": "IT", "LTIM": "IT",
    "HDFCBANK": "BANKING", "ICICIBANK": "BANKING", "SBIN": "BANKING",
    "AXISBANK": "BANKING", "KOTAKBANK": "BANKING",
    "BAJFINANCE": "FINANCE",
    "SUNPHARMA": "PHARMA", "DRREDDY": "PHARMA", "CIPLA": "PHARMA",
    "DIVISLAB": "PHARMA",
    "ITC": "FMCG", "HINDUNILVR": "FMCG", "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG",
    "NTPC": "POWER", "POWERGRID": "POWER", "TATAPOWER": "POWER",
    "LT": "INFRA", "ADANIENT": "INFRA", "ADANIPORTS": "INFRA",
    "BHARTIARTL": "TELECOM",
}

LOT_SIZES = {
    "NIFTY": 65, "RELIANCE": 250, "TCS": 175, "INFY": 400,
    "HDFCBANK": 550, "ICICIBANK": 700, "SBIN": 750, "AXISBANK": 625,
    "KOTAKBANK": 400, "BAJFINANCE": 125, "ITC": 1600, "LT": 150,
    "BHARTIARTL": 475, "HINDUNILVR": 300, "MARUTI": 100,
    "TATAMOTORS": 1400, "WIPRO": 1500, "HCLTECH": 350,
    "TECHM": 600, "SUNPHARMA": 700, "DRREDDY": 125,
    "CIPLA": 650, "DIVISLAB": 200, "NESTLEIND": 200,
    "HINDALCO": 1400, "TATASTEEL": 550, "JSWSTEEL": 600,
    "VEDL": 1550, "JINDALSTEL": 750, "COALINDIA": 2100,
    "NTPC": 2925, "POWERGRID": 2700, "TATAPOWER": 1350,
    "BAJAJ-AUTO": 125, "HEROMOTOCO": 150, "EICHERMOT": 175,
    "M&M": 175, "ADANIENT": 250, "ADANIPORTS": 500,
    "ONGC": 1925, "BPCL": 1500, "IOC": 3250, "GAIL": 2750,
    "BRITANNIA": 200, "INDIGO": 300, "LTIM": 200,
    "SAIL": 3200, "NATIONALUM": 2750,
}

STOCKS = list(SECTOR_MAP.keys())
YFINANCE_SUFFIX = ".NS"


@dataclass
class BacktestTrade:
    date: str
    symbol: str
    option_type: str
    strategy: str
    entry_price: float
    premium: float
    exit_premium: float
    pnl: float
    lot_size: int
    lots: int
    won: bool
    move_pct: float


@dataclass
class DailyResult:
    date: str
    trades: list[BacktestTrade]
    day_pnl: float
    cumulative: float


# ---------------------------------------------------------------------------
# Data fetching with CSV cache
# ---------------------------------------------------------------------------
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_cache")


def build_date_index(df: pd.DataFrame) -> dict[date, int]:
    return {d.date(): i for i, d in enumerate(df.index)}


def _yf_fetch(fn, retries=3, delay=5):
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if "RateLimit" in type(e).__name__ or "Too Many Requests" in str(e):
                wait = delay * (attempt + 1)
                print(f"  [rate limited, waiting {wait}s]", end=" ", flush=True)
                time.sleep(wait)
            else:
                raise
    return fn()


def save_cache(data: dict[str, pd.DataFrame]):
    os.makedirs(CACHE_DIR, exist_ok=True)
    for sym, df in data.items():
        df.to_csv(os.path.join(CACHE_DIR, f"{sym}.csv"))
    print(f"  Cache saved to {CACHE_DIR}/ ({len(data)} files)")


def load_cache() -> dict[str, pd.DataFrame] | None:
    if not os.path.isdir(CACHE_DIR):
        return None
    files = [f for f in os.listdir(CACHE_DIR) if f.endswith(".csv")]
    if len(files) < 10:
        return None
    data = {}
    for f in files:
        sym = f.replace(".csv", "")
        df = pd.read_csv(os.path.join(CACHE_DIR, f), index_col=0, parse_dates=True)
        if not df.empty:
            data[sym] = df
    return data if len(data) > 10 else None


def fetch_all_data(days: int) -> tuple[dict[str, pd.DataFrame], dict[str, dict[date, int]]]:
    cached = load_cache()
    if cached is not None:
        print(f"Loaded {len(cached)} datasets from cache ({CACHE_DIR}/)")
        date_maps = {sym: build_date_index(df) for sym, df in cached.items()}
        return cached, date_maps

    period_str = f"{days + 30}d"
    print(f"Fetching {len(STOCKS)} stocks + Nifty + Brent crude from Yahoo Finance...")
    data: dict[str, pd.DataFrame] = {}
    date_maps: dict[str, dict[date, int]] = {}

    print("  Nifty 50...", end=" ", flush=True)
    nifty = _yf_fetch(lambda: yf.Ticker("^NSEI").history(period=period_str))
    if nifty is not None and not nifty.empty:
        data["NIFTY"] = nifty
        date_maps["NIFTY"] = build_date_index(nifty)
        print(f"{len(nifty)} days")
    else:
        print("FAILED")

    time.sleep(2)

    print("  Brent Crude...", end=" ", flush=True)
    crude = _yf_fetch(lambda: yf.Ticker("BZ=F").history(period=period_str))
    if crude is not None and not crude.empty:
        data["CRUDE"] = crude
        date_maps["CRUDE"] = build_date_index(crude)
        print(f"{len(crude)} days")
    else:
        print("FAILED")

    batch_size = 5
    for i in range(0, len(STOCKS), batch_size):
        batch = STOCKS[i:i + batch_size]
        tickers_str = " ".join(s + YFINANCE_SUFFIX for s in batch)
        print(f"  Batch {i // batch_size + 1}: {', '.join(batch)}...", end=" ", flush=True)
        time.sleep(3)
        try:
            batch_data = _yf_fetch(lambda: yf.download(
                tickers_str, period=period_str, group_by="ticker",
                progress=False, threads=True))
            if batch_data is None or batch_data.empty:
                print("EMPTY")
                continue
            for sym in batch:
                ticker = sym + YFINANCE_SUFFIX
                try:
                    if len(batch) == 1:
                        df = batch_data
                    else:
                        df = batch_data[ticker] if ticker in batch_data.columns.get_level_values(0) else None
                    if df is not None and not df.empty and len(df) > 20:
                        df = df.dropna(subset=["Close"])
                        data[sym] = df
                        date_maps[sym] = build_date_index(df)
                except Exception:
                    pass
            print(f"OK ({sum(1 for s in batch if s in data)}/{len(batch)})")
        except Exception as e:
            print(f"FAILED: {e}")

    print(f"\nTotal: {len(data)} datasets loaded")
    save_cache(data)
    return data, date_maps


# ---------------------------------------------------------------------------
# Option P&L simulation
# ---------------------------------------------------------------------------
def simulate_option_trade(
    stock_price: float, move_pct: float, option_type: str, symbol: str,
) -> tuple[float, float, float, bool]:
    premium = stock_price * AVG_PREMIUM_PCT
    lot_size = LOT_SIZES.get(symbol, 500)

    stock_move = stock_price * (move_pct / 100)
    premium_change = ATM_DELTA * stock_move if option_type == "CE" else -ATM_DELTA * stock_move
    new_premium = premium + premium_change

    sl_level = premium * (1 - SL_PCT)
    target_level = premium * TARGET_MULT

    if new_premium <= sl_level:
        exit_premium, won = sl_level, False
    elif new_premium >= target_level:
        exit_premium, won = target_level, True
    else:
        exit_premium, won = new_premium, (new_premium > premium)

    pnl = (exit_premium - premium) * lot_size * LOTS - CHARGES_PER_TRADE
    return premium, exit_premium, pnl, won


def _get_ohlc(data, date_maps, sym, td):
    """Get (prev_close, open, close) for a symbol on a date, or None."""
    dm = date_maps.get(sym)
    if dm is None:
        return None
    idx = dm.get(td)
    if idx is None or idx < 1:
        return None
    df = data[sym]
    return (
        float(df["Close"].iloc[idx - 1]),
        float(df["Open"].iloc[idx]),
        float(df["Close"].iloc[idx]),
    )


# ---------------------------------------------------------------------------
# Strategies — all use date-based lookups
# ---------------------------------------------------------------------------
def strategy_earnings_momentum(data, date_maps, td):
    trades = []
    for sym in STOCKS:
        ohlc = _get_ohlc(data, date_maps, sym, td)
        if ohlc is None:
            continue
        prev_close, today_open, today_close = ohlc
        if prev_close <= 0:
            continue

        gap_pct = ((today_open - prev_close) / prev_close) * 100
        if abs(gap_pct) < 3.0:
            continue

        option_type = "CE" if gap_pct > 0 else "PE"
        intraday_move = ((today_close - today_open) / today_open) * 100

        premium, exit_prem, pnl, won = simulate_option_trade(
            today_open, intraday_move, option_type, sym)
        trades.append(BacktestTrade(
            date=str(td), symbol=sym, option_type=option_type,
            strategy="earnings_momentum", entry_price=round(today_open, 2),
            premium=round(premium, 2), exit_premium=round(exit_prem, 2),
            pnl=round(pnl, 2), lot_size=LOT_SIZES.get(sym, 500),
            lots=LOTS, won=won, move_pct=round(intraday_move, 2),
        ))
    return trades


def strategy_sector_rotation(data, date_maps, td):
    trades = []
    crude_dm = date_maps.get("CRUDE")
    if crude_dm is None:
        return trades
    ci = crude_dm.get(td)
    if ci is None or ci < 1:
        return trades

    crude_df = data["CRUDE"]
    crude_prev = float(crude_df["Close"].iloc[ci - 1])
    crude_today = float(crude_df["Close"].iloc[ci])
    crude_chg = ((crude_today - crude_prev) / crude_prev) * 100

    targets = []
    if crude_chg < -2:
        targets.extend((sym, "CE") for sym in ["INDIGO"] if sym in data)
    if crude_chg > 3:
        targets.extend((sym, "CE") for sym in ["RELIANCE", "ONGC", "BPCL"] if sym in data)
    if crude_chg > 2:
        targets.extend((sym, "PE") for sym in ["TATASTEEL", "HINDALCO", "JSWSTEEL"] if sym in data)

    for sym, opt_type in targets:
        ohlc = _get_ohlc(data, date_maps, sym, td)
        if ohlc is None:
            continue
        _, today_open, today_close = ohlc
        if today_open <= 0:
            continue
        move = ((today_close - today_open) / today_open) * 100

        premium, exit_prem, pnl, won = simulate_option_trade(today_open, move, opt_type, sym)
        trades.append(BacktestTrade(
            date=str(td), symbol=sym, option_type=opt_type,
            strategy="sector_rotation", entry_price=round(today_open, 2),
            premium=round(premium, 2), exit_premium=round(exit_prem, 2),
            pnl=round(pnl, 2), lot_size=LOT_SIZES.get(sym, 500),
            lots=LOTS, won=won, move_pct=round(move, 2),
        ))
    return trades


def strategy_fii_momentum(data, date_maps, td):
    trades = []
    nifty_dm = date_maps.get("NIFTY")
    if nifty_dm is None:
        return trades
    ni = nifty_dm.get(td)
    if ni is None or ni < 3:
        return trades

    nifty_df = data["NIFTY"]
    closes = [float(nifty_df["Close"].iloc[ni - i]) for i in range(4)]

    green_streak = sum(1 for i in range(len(closes) - 1)
                       if closes[i] > closes[i + 1]
                       and all(closes[j] > closes[j + 1] for j in range(i)))
    red_streak = sum(1 for i in range(len(closes) - 1)
                     if closes[i] < closes[i + 1]
                     and all(closes[j] < closes[j + 1] for j in range(i)))

    targets = []
    if green_streak >= 3:
        targets.extend((sym, "CE") for sym in ["HDFCBANK", "ICICIBANK", "RELIANCE", "INFY", "TCS"] if sym in data)
    if red_streak >= 3:
        targets.extend((sym, "CE") for sym in ["HDFCBANK", "ICICIBANK", "SBIN"] if sym in data)

    for sym, opt_type in targets:
        ohlc = _get_ohlc(data, date_maps, sym, td)
        if ohlc is None:
            continue
        _, today_open, today_close = ohlc
        if today_open <= 0:
            continue
        move = ((today_close - today_open) / today_open) * 100

        premium, exit_prem, pnl, won = simulate_option_trade(today_open, move, opt_type, sym)
        trades.append(BacktestTrade(
            date=str(td), symbol=sym, option_type=opt_type,
            strategy="fii_momentum", entry_price=round(today_open, 2),
            premium=round(premium, 2), exit_premium=round(exit_prem, 2),
            pnl=round(pnl, 2), lot_size=LOT_SIZES.get(sym, 500),
            lots=LOTS, won=won, move_pct=round(move, 2),
        ))
    return trades


def strategy_intraday_momentum(data, date_maps, td):
    """Backtest proxy for intraday momentum.

    Realistic simulation: use previous day's open-to-close direction to
    predict today's early move (yesterday's momentum carries into today).
    Then evaluate using today's actual OHLC — the trade outcome is based
    on today's move, not the signal day. This avoids hindsight bias.
    """
    trades = []
    scan_list = [
        "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN",
        "AXISBANK", "KOTAKBANK", "BAJFINANCE", "LT", "BHARTIARTL",
        "TATAMOTORS", "MARUTI", "M&M", "HINDALCO", "TATASTEEL",
        "SUNPHARMA", "DRREDDY", "WIPRO", "HCLTECH", "TECHM",
        "ITC", "HINDUNILVR", "ADANIENT", "NTPC", "TATAPOWER",
        "INDIGO", "CIPLA", "JSWSTEEL", "VEDL",
    ]

    nifty_ohlc = _get_ohlc(data, date_maps, "NIFTY", td)
    market_bias = 0.0
    if nifty_ohlc:
        _, n_open, n_close = nifty_ohlc
        if n_open > 0:
            market_bias = ((n_close - n_open) / n_open) * 100

    movers = []
    for sym in scan_list:
        if sym not in data:
            continue
        dm = date_maps.get(sym)
        if dm is None:
            continue
        idx = dm.get(td)
        if idx is None or idx < 5:
            continue

        df = data[sym]
        today_open = float(df["Open"].iloc[idx])
        today_close = float(df["Close"].iloc[idx])
        today_high = float(df["High"].iloc[idx])
        today_low = float(df["Low"].iloc[idx])
        today_vol = float(df["Volume"].iloc[idx])

        if today_open <= 0:
            continue

        # Signal: use previous day's move as the "early momentum" signal
        # (in live trading, scanner sees first 5-min candle direction)
        prev_open = float(df["Open"].iloc[idx - 1])
        prev_close = float(df["Close"].iloc[idx - 1])
        if prev_open <= 0:
            continue
        prev_move = ((prev_close - prev_open) / prev_open) * 100

        # Also check gap: today's open vs yesterday's close
        gap_pct = ((today_open - prev_close) / prev_close) * 100

        # Combined early signal: previous day momentum + today's gap
        early_signal = prev_move * 0.5 + gap_pct * 0.5

        if abs(early_signal) < 0.8:
            continue

        # Volume ratio vs 5-day average
        recent_vols = [float(df["Volume"].iloc[idx - i]) for i in range(1, 6)
                       if idx - i >= 0]
        avg_vol = np.mean(recent_vols) if recent_vols else today_vol
        vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

        movers.append({
            "symbol": sym,
            "early_signal": early_signal,
            "vol_ratio": vol_ratio,
            "open": today_open,
            "close": today_close,
            "high": today_high,
            "low": today_low,
        })

    movers.sort(key=lambda m: abs(m["early_signal"]) * m["vol_ratio"], reverse=True)

    for m in movers[:3]:
        sym = m["symbol"]
        signal = m["early_signal"]
        vol_r = m["vol_ratio"]
        opt_type = "CE" if signal > 0 else "PE"

        # Confidence scoring (mirrors live scanner)
        conf = 55
        conf += min(20, int((abs(signal) - 0.8) / 0.5) * 5)
        if vol_r > 1.5:
            conf += 15
        elif vol_r > 1.2:
            conf += 10
        elif vol_r > 0.8:
            conf += 5
        if (signal > 0 and market_bias > 0) or (signal < 0 and market_bias < 0):
            conf += 5
        if (signal > 0 and market_bias < -0.5) or (signal < 0 and market_bias > 0.5):
            conf -= 5
        conf = max(30, min(85, conf))

        if conf < 65:
            continue

        # Outcome: use today's actual open-to-close move
        actual_move = ((m["close"] - m["open"]) / m["open"]) * 100

        premium, exit_prem, pnl, won = simulate_option_trade(
            m["open"], actual_move, opt_type, sym)
        trades.append(BacktestTrade(
            date=str(td), symbol=sym, option_type=opt_type,
            strategy="intraday_momentum", entry_price=round(m["open"], 2),
            premium=round(premium, 2), exit_premium=round(exit_prem, 2),
            pnl=round(pnl, 2), lot_size=LOT_SIZES.get(sym, 500),
            lots=LOTS, won=won, move_pct=round(actual_move, 2),
        ))

    return trades


def strategy_gap_play(data, date_maps, td):
    trades = []
    check_list = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "SBIN",
                  "TATAMOTORS", "MARUTI", "BAJFINANCE", "LT", "ITC",
                  "ICICIBANK", "AXISBANK", "KOTAKBANK", "BHARTIARTL"]

    for sym in check_list:
        ohlc = _get_ohlc(data, date_maps, sym, td)
        if ohlc is None:
            continue
        prev_close, today_open, today_close = ohlc
        if prev_close <= 0:
            continue

        gap_pct = ((today_open - prev_close) / prev_close) * 100
        if abs(gap_pct) < 1.5:
            continue

        option_type = "CE" if gap_pct > 0 else "PE"
        move = ((today_close - today_open) / today_open) * 100

        premium, exit_prem, pnl, won = simulate_option_trade(today_open, move, option_type, sym)
        trades.append(BacktestTrade(
            date=str(td), symbol=sym, option_type=option_type,
            strategy="gap_play", entry_price=round(today_open, 2),
            premium=round(premium, 2), exit_premium=round(exit_prem, 2),
            pnl=round(pnl, 2), lot_size=LOT_SIZES.get(sym, 500),
            lots=LOTS, won=won, move_pct=round(move, 2),
        ))
    return trades


# ---------------------------------------------------------------------------
# Merge & select top trades
# ---------------------------------------------------------------------------
def merge_signals(trades: list[BacktestTrade]) -> list[BacktestTrade]:
    by_sym: dict[str, list[BacktestTrade]] = {}
    for t in trades:
        key = f"{t.symbol}_{t.option_type}"
        by_sym.setdefault(key, []).append(t)

    merged = []
    priority = {"earnings_momentum": 0, "intraday_momentum": 1,
                "sector_rotation": 2, "fii_momentum": 3, "gap_play": 4}
    for key, group in by_sym.items():
        group.sort(key=lambda t: priority.get(t.strategy, 99))
        best = group[0]
        if len(group) > 1:
            best.strategy += "+" + "+".join(t.strategy for t in group[1:])
        merged.append(best)

    merged.sort(key=lambda t: abs(t.move_pct), reverse=True)
    if MAX_TRADES_PER_DAY > 0:
        return merged[:MAX_TRADES_PER_DAY]
    return merged


# ---------------------------------------------------------------------------
# Main backtest
# ---------------------------------------------------------------------------
def run_backtest():
    print("=" * 70)
    print(f"SCANNER STRATEGY BACKTESTER — {BACKTEST_DAYS} Days")
    print("=" * 70)
    limit_str = "Unlimited" if MAX_TRADES_PER_DAY == 0 else f"Max {MAX_TRADES_PER_DAY}"
    print(f"Capital: ₹{INITIAL_CAPITAL:,} | {limit_str} trades/day | "
          f"{LOTS} lot(s) | SL: {SL_PCT:.0%} | Target: {TARGET_MULT}x")
    print()

    data, date_maps = fetch_all_data(BACKTEST_DAYS)
    if "NIFTY" not in data:
        print("ERROR: Could not fetch Nifty data")
        return

    nifty_dates = sorted(date_maps["NIFTY"].keys())

    all_trades: list[BacktestTrade] = []
    daily_results: list[DailyResult] = []
    cumulative = 0.0
    strat_stats: dict[str, dict] = {}

    print(f"Running backtest over {len(nifty_dates)} trading days...\n")

    for td in nifty_dates:
        if td.weekday() >= 5:
            continue

        day_trades: list[BacktestTrade] = []
        day_trades.extend(strategy_earnings_momentum(data, date_maps, td))
        day_trades.extend(strategy_sector_rotation(data, date_maps, td))
        day_trades.extend(strategy_fii_momentum(data, date_maps, td))
        day_trades.extend(strategy_gap_play(data, date_maps, td))
        day_trades.extend(strategy_intraday_momentum(data, date_maps, td))

        if not day_trades:
            continue

        selected = merge_signals(day_trades)
        day_pnl = 0.0
        for t in selected:
            day_pnl += t.pnl
            all_trades.append(t)

            base_strat = t.strategy.split("+")[0]
            if base_strat not in strat_stats:
                strat_stats[base_strat] = {"trades": 0, "wins": 0, "pnl": 0.0,
                                           "best": -999999, "worst": 999999}
            ss = strat_stats[base_strat]
            ss["trades"] += 1
            ss["wins"] += 1 if t.won else 0
            ss["pnl"] += t.pnl
            ss["best"] = max(ss["best"], t.pnl)
            ss["worst"] = min(ss["worst"], t.pnl)

        cumulative += day_pnl
        daily_results.append(DailyResult(
            date=str(td), trades=selected,
            day_pnl=round(day_pnl, 2), cumulative=round(cumulative, 2),
        ))

    # ---------------------------------------------------------------------------
    # Results
    # ---------------------------------------------------------------------------
    print("=" * 70)
    print("BACKTEST RESULTS")
    print("=" * 70)

    total = len(all_trades)
    if total == 0:
        print("  No trades generated.")
        return

    wins = sum(1 for t in all_trades if t.won)
    losses = total - wins
    win_rate = wins / total * 100
    total_pnl = sum(t.pnl for t in all_trades)
    avg_win = np.mean([t.pnl for t in all_trades if t.won]) if wins > 0 else 0
    avg_loss = np.mean([t.pnl for t in all_trades if not t.won]) if losses > 0 else 0
    best_trade = max(t.pnl for t in all_trades)
    worst_trade = min(t.pnl for t in all_trades)

    peak = 0.0
    max_dd = 0.0
    for dr in daily_results:
        peak = max(peak, dr.cumulative)
        max_dd = max(max_dd, peak - dr.cumulative)

    gross_wins = sum(t.pnl for t in all_trades if t.won)
    gross_losses = abs(sum(t.pnl for t in all_trades if not t.won))
    pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    monthly: dict[str, dict] = {}
    for t in all_trades:
        month = t.date[:7]
        if month not in monthly:
            monthly[month] = {"trades": 0, "wins": 0, "pnl": 0.0}
        monthly[month]["trades"] += 1
        monthly[month]["wins"] += 1 if t.won else 0
        monthly[month]["pnl"] += t.pnl

    print(f"""
  Total Trades:      {total}
  Wins:              {wins} ({win_rate:.1f}%)
  Losses:            {losses}
  Total P&L:         ₹{total_pnl:+,.0f}
  Return on Capital: {total_pnl / INITIAL_CAPITAL * 100:+.1f}%
  Avg Win:           ₹{avg_win:+,.0f}
  Avg Loss:          ₹{avg_loss:+,.0f}
  Best Trade:        ₹{best_trade:+,.0f}
  Worst Trade:       ₹{worst_trade:+,.0f}
  Max Drawdown:      ₹{max_dd:,.0f}
  Profit Factor:     {pf:.2f}
  Trading Days:      {len(daily_results)}
  Avg Trades/Day:    {total / len(daily_results):.1f}
""")

    print("-" * 70)
    print("STRATEGY BREAKDOWN")
    print("-" * 70)
    print(f"  {'Strategy':<22} {'Trades':>7} {'Wins':>6} {'WR%':>6} {'P&L':>12} {'Best':>10} {'Worst':>10}")
    print(f"  {'-'*22} {'-'*7} {'-'*6} {'-'*6} {'-'*12} {'-'*10} {'-'*10}")
    for strat, ss in sorted(strat_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = ss["wins"] / ss["trades"] * 100 if ss["trades"] > 0 else 0
        print(f"  {strat:<22} {ss['trades']:>7} {ss['wins']:>6} {wr:>5.1f}% "
              f"₹{ss['pnl']:>+10,.0f} ₹{ss['best']:>+8,.0f} ₹{ss['worst']:>+8,.0f}")

    print()
    print("-" * 70)
    print("MONTHLY BREAKDOWN")
    print("-" * 70)
    print(f"  {'Month':<10} {'Trades':>7} {'Wins':>6} {'WR%':>6} {'P&L':>12}")
    print(f"  {'-'*10} {'-'*7} {'-'*6} {'-'*6} {'-'*12}")
    for month in sorted(monthly.keys()):
        m = monthly[month]
        wr = m["wins"] / m["trades"] * 100 if m["trades"] > 0 else 0
        bar = "+" * min(int(m["pnl"] / 1000), 30) if m["pnl"] > 0 else "-" * min(int(abs(m["pnl"]) / 1000), 30)
        print(f"  {month:<10} {m['trades']:>7} {m['wins']:>6} {wr:>5.1f}% ₹{m['pnl']:>+10,.0f}  {bar}")

    print()
    print("-" * 70)
    print("TOP 10 WINNING TRADES")
    print("-" * 70)
    for t in sorted(all_trades, key=lambda t: t.pnl, reverse=True)[:10]:
        print(f"  {t.date} {t.symbol:<12} {t.option_type} {t.strategy:<30} "
              f"move={t.move_pct:+.1f}%  P&L=₹{t.pnl:+,.0f}")

    print()
    print("-" * 70)
    print("TOP 10 LOSING TRADES")
    print("-" * 70)
    for t in sorted(all_trades, key=lambda t: t.pnl)[:10]:
        print(f"  {t.date} {t.symbol:<12} {t.option_type} {t.strategy:<30} "
              f"move={t.move_pct:+.1f}%  P&L=₹{t.pnl:+,.0f}")

    print()
    print("-" * 70)
    print("EQUITY CURVE (monthly snapshots)")
    print("-" * 70)
    last_month = ""
    for dr in daily_results:
        m = dr.date[:7]
        if m != last_month:
            bar_len = max(0, min(int(dr.cumulative / 2000), 40))
            bar = "█" * bar_len
            print(f"  {dr.date}  ₹{INITIAL_CAPITAL + dr.cumulative:>10,.0f}  "
                  f"({dr.cumulative:>+10,.0f})  {bar}")
            last_month = m

    if daily_results:
        dr = daily_results[-1]
        print(f"  {dr.date}  ₹{INITIAL_CAPITAL + dr.cumulative:>10,.0f}  "
              f"({dr.cumulative:>+10,.0f})  *** FINAL ***")

    print()
    print("=" * 70)
    print(f"VERDICT: {'PROFITABLE' if total_pnl > 0 else 'UNPROFITABLE'} — "
          f"₹{INITIAL_CAPITAL:,} → ₹{INITIAL_CAPITAL + total_pnl:,.0f} "
          f"({total_pnl / INITIAL_CAPITAL * 100:+.1f}%) over {BACKTEST_DAYS} days")
    print("=" * 70)


if __name__ == "__main__":
    run_backtest()

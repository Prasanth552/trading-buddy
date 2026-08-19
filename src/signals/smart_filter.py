"""Adaptive smart filter — learns from your own trade journal.

Instead of rigid if-else rules, this filter thinks like an experienced trader:
  1. Checks YOUR past results for this stock + option type → stock track record
  2. Checks how similar setups (same sector, same market condition) performed
  3. Uses live market context (FII flow, crude, sector trend) as soft modifiers
  4. Adapts every day as new closed trades flow into the journal

The only hardcoded rule: earnings-day trades get a boost (universally high WR).
Everything else is learned from data. When data is thin (<5 similar trades),
falls back to neutral bias — doesn't penalize what it hasn't seen enough of.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.logging import get_logger

log = get_logger("smart_filter")

IST = ZoneInfo("Asia/Kolkata")

TAKE_THRESHOLD = 50
SKIP_THRESHOLD = 30

SECTOR_MAP: dict[str, str] = {
    "NATIONALUM": "METAL", "NALCO": "METAL", "HINDZINC": "METAL",
    "HINDALCO": "METAL", "TATASTEEL": "METAL", "JSWSTEEL": "METAL",
    "SAIL": "METAL", "VEDL": "METAL", "NMDC": "METAL",
    "JINDALSTEL": "METAL", "HINDCOPPER": "METAL", "COALINDIA": "METAL",
    "HYUNDAI": "AUTO", "MARUTI": "AUTO", "TATAMOTORS": "AUTO",
    "M&M": "AUTO", "BAJAJ-AUTO": "AUTO", "HEROMOTOCO": "AUTO",
    "EICHERMOT": "AUTO", "ASHOKLEY": "AUTO", "TVSMOTOR": "AUTO",
    "MOTHERSON": "AUTO", "BOSCHLTD": "AUTO", "MRF": "AUTO",
    "INDIGO": "AIRLINE", "SPICEJET": "AIRLINE",
    "BHARTIARTL": "TELECOM", "IDEA": "TELECOM",
    "RELIANCE": "ENERGY", "ONGC": "ENERGY", "BPCL": "ENERGY",
    "IOC": "ENERGY", "GAIL": "ENERGY", "PETRONET": "ENERGY",
    "HINDPETRO": "ENERGY",
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT",
    "TECHM": "IT", "LTIM": "IT", "MPHASIS": "IT", "COFORGE": "IT",
    "PERSISTENT": "IT",
    "HDFCBANK": "BANKING", "ICICIBANK": "BANKING", "SBIN": "BANKING",
    "AXISBANK": "BANKING", "KOTAKBANK": "BANKING", "BAJFINANCE": "FINANCE",
    "BAJAJFINSV": "FINANCE", "BANDHANBNK": "BANKING", "IDFCFIRSTB": "BANKING",
    "PNB": "BANKING", "BANKBARODA": "BANKING", "INDUSINDBK": "BANKING",
    "SUNPHARMA": "PHARMA", "DRREDDY": "PHARMA", "CIPLA": "PHARMA",
    "DIVISLAB": "PHARMA", "AUROPHARMA": "PHARMA", "BIOCON": "PHARMA",
    "LUPIN": "PHARMA", "APOLLOHOSP": "PHARMA",
    "ADANIENT": "POWER", "ADANIPORTS": "INFRA", "ADANIGREEN": "POWER",
    "NTPC": "POWER", "POWERGRID": "POWER", "TATAPOWER": "POWER",
    "NHPC": "POWER", "SJVN": "POWER", "IREDA": "POWER",
    "JSWENERGY": "POWER", "CESC": "POWER",
    "ITC": "FMCG", "HINDUNILVR": "FMCG", "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG", "DABUR": "FMCG", "MARICO": "FMCG",
    "TATACONSUM": "FMCG", "COLPAL": "FMCG", "GODREJCP": "FMCG",
    "LT": "INFRA", "DLF": "REALTY", "GODREJPROP": "REALTY",
    "OBEROIRLTY": "REALTY", "PRESTIGE": "REALTY",
}


@dataclass
class FilterResult:
    score: int
    action: str          # TAKE / SKIP / REDUCE
    reasons: list[str] = field(default_factory=list)
    adjusted_lots: int | None = None


def _get_sector(symbol: str) -> str:
    sym = symbol.replace(" ", "").upper()
    return SECTOR_MAP.get(sym, "UNKNOWN")


def _extract_stock(symbol: str) -> str:
    """Pull stock name from trade symbol like 'PAYTM 25AUG 900 CE'."""
    parts = symbol.strip().split()
    return parts[0].upper() if parts else symbol.upper()


def _extract_opt_type(symbol: str) -> str:
    parts = symbol.strip().split()
    if parts and parts[-1] in ("CE", "PE"):
        return parts[-1]
    return "CE"


# ---------------------------------------------------------------------------
# Trade journal reader — the bot's memory of what actually worked
# ---------------------------------------------------------------------------
_journal_cache: dict[str, tuple[float, Any]] = {}
JOURNAL_TTL = 600  # 10 min — journal doesn't change mid-session often


def _get_trade_journal(lookback_days: int = 30) -> list[dict]:
    """Pull closed CH1 trades from the last N days as the learning set."""
    cache_key = f"journal_{lookback_days}"
    entry = _journal_cache.get(cache_key)
    now_ts = datetime.now(IST).timestamp()
    if entry and (now_ts - entry[0]) < JOURNAL_TTL:
        return entry[1]

    import config
    db_path = config.DB_PATH
    trades = []
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        cutoff = (datetime.now(IST) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            """SELECT id, ts, symbol, side, qty, price, exit_price, pnl, status,
                      channel, filter_score
               FROM trades
               WHERE side = 'BUY'
                 AND (channel IS NULL OR channel = 'ch1')
                 AND status LIKE 'CLOSED%'
                 AND pnl IS NOT NULL
                 AND ts >= ?
               ORDER BY ts DESC""",
            (cutoff,),
        ).fetchall()
        for r in rows:
            trades.append({
                "stock": _extract_stock(r["symbol"]),
                "opt_type": _extract_opt_type(r["symbol"]),
                "pnl": r["pnl"],
                "won": r["pnl"] > 0,
                "ts": r["ts"],
                "weekday": datetime.fromisoformat(r["ts"][:19]).weekday(),
                "hour": int(r["ts"][11:13]) if len(r["ts"]) > 13 else 9,
            })
        conn.close()
    except Exception as e:
        log.warning("Journal read failed: %s", e)

    _journal_cache[cache_key] = (now_ts, trades)
    return trades


def _win_rate(trades: list[dict]) -> float | None:
    """Win rate from a list of trade dicts. None if too few trades."""
    if len(trades) < 3:
        return None
    wins = sum(1 for t in trades if t["won"])
    return wins / len(trades)


def _avg_pnl(trades: list[dict]) -> float:
    if not trades:
        return 0
    return sum(t["pnl"] for t in trades) / len(trades)


# ---------------------------------------------------------------------------
# Data fetchers (cached per session with TTL)
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 300


def _cached(key: str):
    entry = _cache.get(key)
    if entry and (datetime.now(IST).timestamp() - entry[0]) < CACHE_TTL:
        return entry[1]
    return None


def _set_cache(key: str, val: Any):
    _cache[key] = (datetime.now(IST).timestamp(), val)


def get_nifty_trend() -> dict[str, Any]:
    cached = _cached("nifty_trend")
    if cached:
        return cached
    try:
        import yfinance as yf
        nifty = yf.Ticker("^NSEI")
        hist = nifty.history(period="5d")
        if hist.empty:
            return {"consecutive_red": 0, "consecutive_green": 0, "change_pct": 0}
        closes = hist["Close"].tolist()
        red_days = 0
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] < closes[i - 1]:
                red_days += 1
            else:
                break
        green_days = 0
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] > closes[i - 1]:
                green_days += 1
            else:
                break
        latest_change = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if len(closes) >= 2 else 0
        result = {"consecutive_red": red_days, "consecutive_green": green_days,
                  "change_pct": round(latest_change, 2)}
        _set_cache("nifty_trend", result)
        return result
    except Exception as e:
        log.warning("Nifty trend fetch failed: %s", e)
        return {"consecutive_red": 0, "consecutive_green": 0, "change_pct": 0}


def get_crude_change() -> float:
    cached = _cached("crude_change")
    if cached is not None:
        return cached
    try:
        import yfinance as yf
        brent = yf.Ticker("BZ=F")
        hist = brent.history(period="5d")
        if len(hist) < 2:
            return 0.0
        closes = hist["Close"].tolist()
        change = ((closes[-1] - closes[-2]) / closes[-2]) * 100
        result = round(change, 2)
        _set_cache("crude_change", result)
        return result
    except Exception as e:
        log.warning("Crude price fetch failed: %s", e)
        return 0.0


def get_fii_dii_flow() -> dict[str, float]:
    cached = _cached("fii_dii")
    if cached:
        return cached
    try:
        import requests
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=5)
        resp = session.get(
            "https://www.nseindia.com/api/fiidiiTradeReact",
            headers=headers, timeout=10,
        )
        data = resp.json()
        fii_net = 0.0
        dii_net = 0.0
        for item in data:
            cat = item.get("category", "").upper()
            net_val = float(item.get("netValue", 0))
            if "FII" in cat or "FPI" in cat:
                fii_net += net_val
            elif "DII" in cat:
                dii_net += net_val
        result = {"fii": round(fii_net, 2), "dii": round(dii_net, 2)}
        _set_cache("fii_dii", result)
        return result
    except Exception as e:
        log.warning("FII/DII fetch failed: %s", e)
        return {"fii": 0, "dii": 0}


def check_earnings_today(symbol: str) -> bool:
    cached = _cached("earnings_list")
    if cached is None:
        try:
            import requests
            headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
            session = requests.Session()
            session.get("https://www.nseindia.com", headers=headers, timeout=5)
            today_str = datetime.now(IST).strftime("%d-%m-%Y")
            resp = session.get(
                "https://www.nseindia.com/api/corporate-announcements",
                params={"index": "equities", "from_date": today_str, "to_date": today_str},
                headers=headers, timeout=10,
            )
            data = resp.json()
            earnings_symbols = set()
            for item in data:
                subject = (item.get("subject") or "").upper()
                if any(k in subject for k in ["FINANCIAL RESULT", "BOARD MEETING", "QUARTERLY RESULT"]):
                    sym = (item.get("symbol") or "").upper()
                    earnings_symbols.add(sym)
            _set_cache("earnings_list", earnings_symbols)
            cached = earnings_symbols
        except Exception as e:
            log.warning("Earnings calendar fetch failed: %s", e)
            cached = set()
            _set_cache("earnings_list", cached)

    sym = symbol.replace(" ", "").upper()
    return sym in cached


def get_sector_index_trend(sector: str) -> float:
    sector_tickers = {
        "METAL": "^CNXMETAL", "AUTO": "^CNXAUTO", "IT": "^CNXIT",
        "BANKING": "^NSEBANK", "PHARMA": "^CNXPHARMA", "ENERGY": "^CNXENERGY",
        "FMCG": "^CNXFMCG", "REALTY": "^CNXREALTY",
    }
    ticker = sector_tickers.get(sector)
    if not ticker:
        return 0.0
    cache_key = f"sector_{sector}"
    cached = _cached(cache_key)
    if cached is not None:
        return cached
    try:
        import yfinance as yf
        idx = yf.Ticker(ticker)
        hist = idx.history(period="2d")
        if len(hist) < 2:
            return 0.0
        closes = hist["Close"].tolist()
        change = ((closes[-1] - closes[-2]) / closes[-2]) * 100
        result = round(change, 2)
        _set_cache(cache_key, result)
        return result
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# ADAPTIVE FILTER — learns from your trade journal
# ---------------------------------------------------------------------------
def evaluate_signal(
    symbol: str,
    option_type: str,
    channel: str = "ch1",
    trigger_price: float = 0,
) -> FilterResult:
    """Score using trade history + live market context.

    Scoring layers (each contributes to the final score):
      1. Stock track record — how this specific stock performed in your trades
      2. Option-type track record — CE vs PE overall win rate
      3. Time pattern — what time of day works best in YOUR data
      4. Day pattern — which weekdays work in YOUR data
      5. Earnings catalyst — universally strong (hardcoded boost)
      6. Sector + crude alignment — soft modifier from market data
      7. FII flow — extreme selling is a real warning
      8. Market trend context — soft, not a blanket penalty
    """
    now = datetime.now(IST)
    score = 50
    reasons: list[str] = []

    journal = _get_trade_journal(lookback_days=30)
    sym_clean = symbol.replace(" ", "").upper()
    stock_name = _extract_stock(sym_clean)
    sector = _get_sector(sym_clean)

    # ------ 1. STOCK TRACK RECORD (most important) ------
    stock_trades = [t for t in journal if t["stock"] == stock_name]
    stock_same_type = [t for t in stock_trades if t["opt_type"] == option_type]

    if len(stock_same_type) >= 3:
        wr = _win_rate(stock_same_type)
        avg = _avg_pnl(stock_same_type)
        if wr >= 0.65:
            bonus = min(20, int((wr - 0.5) * 60))
            score += bonus
            reasons.append(f"{stock_name} {option_type}: {wr:.0%} WR over {len(stock_same_type)} trades (+{bonus})")
        elif wr <= 0.35:
            penalty = min(20, int((0.5 - wr) * 60))
            score -= penalty
            reasons.append(f"{stock_name} {option_type}: {wr:.0%} WR over {len(stock_same_type)} trades (-{penalty})")
        else:
            reasons.append(f"{stock_name} {option_type}: {wr:.0%} WR over {len(stock_same_type)} trades (neutral)")

        if avg > 500:
            score += 5
            reasons.append(f"Avg P&L ₹{avg:+,.0f} per trade — profitable stock")
        elif avg < -500:
            score -= 5
            reasons.append(f"Avg P&L ₹{avg:+,.0f} per trade — losing stock")
    elif len(stock_trades) >= 3:
        wr = _win_rate(stock_trades)
        if wr is not None:
            if wr >= 0.60:
                score += 8
                reasons.append(f"{stock_name} overall: {wr:.0%} WR ({len(stock_trades)} trades) (+8)")
            elif wr <= 0.35:
                score -= 8
                reasons.append(f"{stock_name} overall: {wr:.0%} WR ({len(stock_trades)} trades) (-8)")
    else:
        reasons.append(f"{stock_name}: <3 past trades — no track record yet")

    # ------ 2. OPTION TYPE TRACK RECORD (CE vs PE overall) ------
    type_trades = [t for t in journal if t["opt_type"] == option_type]
    if len(type_trades) >= 10:
        wr = _win_rate(type_trades)
        if wr >= 0.55:
            score += 5
            reasons.append(f"{option_type} trades overall: {wr:.0%} WR ({len(type_trades)} trades)")
        elif wr <= 0.40:
            score -= 5
            reasons.append(f"{option_type} trades weak: {wr:.0%} WR ({len(type_trades)} trades)")

    # ------ 3. TIME OF DAY (learned from your trades) ------
    current_hour = now.hour
    hour_trades = [t for t in journal if t["hour"] == current_hour]
    if len(hour_trades) >= 5:
        wr = _win_rate(hour_trades)
        if wr is not None:
            if wr >= 0.60:
                score += 10
                reasons.append(f"{current_hour}:xx trades: {wr:.0%} WR — your strong hour (+10)")
            elif wr <= 0.35:
                score -= 10
                reasons.append(f"{current_hour}:xx trades: {wr:.0%} WR — your weak hour (-10)")
    else:
        if current_hour == 9:
            score += 5
            reasons.append("Opening hour — generally active (+5)")
        elif current_hour >= 14:
            score -= 3
            reasons.append("Late session — less edge (-3)")

    # ------ 4. DAY OF WEEK (learned from your trades) ------
    weekday = now.weekday()
    day_trades = [t for t in journal if t["weekday"] == weekday]
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    if len(day_trades) >= 5:
        wr = _win_rate(day_trades)
        if wr is not None:
            if wr >= 0.60:
                score += 5
                reasons.append(f"{day_names[weekday]}: {wr:.0%} WR — your strong day (+5)")
            elif wr <= 0.35:
                score -= 5
                reasons.append(f"{day_names[weekday]}: {wr:.0%} WR — your weak day (-5)")
    # no fallback needed — day of week alone isn't a strong signal

    # ------ 5. EARNINGS CATALYST (universal edge, always keep) ------
    if check_earnings_today(sym_clean):
        score += 20
        reasons.append(f"{stock_name} has earnings today — strong catalyst (+20)")

    # ------ 6. SECTOR + CRUDE (soft modifiers, not blanket rules) ------
    crude_chg = get_crude_change()

    if sector == "METAL" and crude_chg > 2 and option_type == "CE":
        score -= 10
        reasons.append(f"Metal CE + crude up {crude_chg:.1f}% — headwind (-10)")
    elif sector == "AIRLINE" and crude_chg < -2 and option_type == "CE":
        score += 8
        reasons.append(f"Airline CE + crude down {crude_chg:.1f}% — tailwind (+8)")
    elif sector == "AIRLINE" and crude_chg > 2 and option_type == "CE":
        score -= 8
        reasons.append(f"Airline CE + crude up {crude_chg:.1f}% — headwind (-8)")
    elif sector == "ENERGY" and crude_chg > 2 and option_type == "CE":
        score += 5
        reasons.append(f"Energy CE + crude up {crude_chg:.1f}% — aligned (+5)")

    if sector != "UNKNOWN":
        sector_chg = get_sector_index_trend(sector)
        if abs(sector_chg) > 1.5:
            if (option_type == "CE" and sector_chg > 1.5) or (option_type == "PE" and sector_chg < -1.5):
                score += 8
                reasons.append(f"{sector} {sector_chg:+.1f}% — {option_type} aligned with sector (+8)")
            elif (option_type == "CE" and sector_chg < -1.5) or (option_type == "PE" and sector_chg > 1.5):
                score -= 8
                reasons.append(f"{sector} {sector_chg:+.1f}% — {option_type} against sector (-8)")

    # ------ 7. FII FLOW (only extreme levels matter) ------
    flows = get_fii_dii_flow()
    fii = flows["fii"]
    if fii < -500:
        if option_type == "CE":
            score -= 12
            reasons.append(f"FII heavy selling ₹{fii:.0f}cr + CE — caution (-12)")
        else:
            score += 5
            reasons.append(f"FII heavy selling ₹{fii:.0f}cr + PE — aligned (+5)")
    elif fii > 500:
        if option_type == "CE":
            score += 5
            reasons.append(f"FII buying ₹{fii:.0f}cr + CE — flow support (+5)")

    # ------ 8. MARKET TREND (soft context, NOT blanket penalty) ------
    nifty = get_nifty_trend()
    red_days = nifty.get("consecutive_red", 0)
    green_days = nifty.get("consecutive_green", 0)
    mkt_chg = nifty.get("change_pct", 0)

    if red_days >= 3:
        if option_type == "PE":
            score += 8
            reasons.append(f"Market red {red_days} days + PE — trend aligned (+8)")
        elif option_type == "CE":
            # DON'T blanket-skip CE in red markets — check if this stock
            # has a track record of winning CE trades regardless
            ce_in_red = len(stock_same_type) >= 3 and option_type == "CE"
            stock_wr = _win_rate(stock_same_type) if ce_in_red else None
            if stock_wr and stock_wr >= 0.55:
                score -= 3
                reasons.append(f"Market red {red_days}d but {stock_name} CE WR {stock_wr:.0%} — mild caution only (-3)")
            else:
                score -= 10
                reasons.append(f"Market red {red_days} days + CE — caution (-10)")
    elif green_days >= 3:
        if option_type == "CE":
            score += 5
            reasons.append(f"Market green {green_days} days + CE — momentum (+5)")
        elif option_type == "PE":
            score -= 5
            reasons.append(f"Market green {green_days} days + PE — against trend (-5)")

    # ------ 9. SECTOR PEER PERFORMANCE (learned) ------
    if sector != "UNKNOWN" and len(stock_same_type) < 3:
        sector_trades = [t for t in journal
                         if _get_sector(t["stock"]) == sector
                         and t["opt_type"] == option_type]
        if len(sector_trades) >= 5:
            wr = _win_rate(sector_trades)
            if wr is not None:
                if wr >= 0.60:
                    score += 5
                    reasons.append(f"{sector} {option_type} peers: {wr:.0%} WR — sector doing well (+5)")
                elif wr <= 0.35:
                    score -= 5
                    reasons.append(f"{sector} {option_type} peers: {wr:.0%} WR — sector struggling (-5)")

    # ------ Decision ------
    if score >= TAKE_THRESHOLD:
        action = "TAKE"
    elif score < SKIP_THRESHOLD:
        action = "SKIP"
    else:
        action = "REDUCE"

    adjusted_lots = None
    if action == "REDUCE":
        adjusted_lots = 1

    result = FilterResult(
        score=score, action=action, reasons=reasons, adjusted_lots=adjusted_lots,
    )
    log.info(
        "FILTER %s %s %s: score=%d action=%s | %s",
        symbol, option_type, channel, score, action,
        " | ".join(reasons),
    )
    return result

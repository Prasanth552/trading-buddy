"""Smart signal filter — scores channel signals before execution.

Uses market conditions (FII/DII flow, crude oil, earnings calendar, sector
trends, time of day) to decide TAKE / SKIP / REDUCE_SIZE on each signal.
Patterns derived from CH1 historical analysis (Jul-Aug 2026):
  - 9 AM CE trades: 66% win rate
  - Earnings-day trades: ~75% win rate
  - Metal stocks during crude spike: ~40% win rate → skip
  - Market-wide selloff (FII selling > ₹500cr): ~30% win rate → skip
  - Mon/Tue best days, Thu/Fri weakest

Integration: called from channel_listener.on_signal() AFTER parsing,
BEFORE execute_signal().
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.logging import get_logger

log = get_logger("smart_filter")

IST = ZoneInfo("Asia/Kolkata")

# Confidence thresholds
TAKE_THRESHOLD = 50
SKIP_THRESHOLD = 30

# Sector classification for F&O stocks
SECTOR_MAP: dict[str, str] = {
    # Metal / Mining
    "NATIONALUM": "METAL", "NALCO": "METAL", "HINDZINC": "METAL",
    "HINDALCO": "METAL", "TATASTEEL": "METAL", "JSWSTEEL": "METAL",
    "SAIL": "METAL", "VEDL": "METAL", "NMDC": "METAL",
    "JINDALSTEL": "METAL", "HINDCOPPER": "METAL", "COALINDIA": "METAL",
    # Auto
    "HYUNDAI": "AUTO", "MARUTI": "AUTO", "TATAMOTORS": "AUTO",
    "M&M": "AUTO", "BAJAJ-AUTO": "AUTO", "HEROMOTOCO": "AUTO",
    "EICHERMOT": "AUTO", "ASHOKLEY": "AUTO", "TVSMOTOR": "AUTO",
    "MOTHERSON": "AUTO", "BOSCHLTD": "AUTO", "MRF": "AUTO",
    # Airline / Aviation
    "INDIGO": "AIRLINE", "SPICEJET": "AIRLINE",
    # Telecom
    "BHARTIARTL": "TELECOM", "IDEA": "TELECOM",
    # Energy / Oil & Gas
    "RELIANCE": "ENERGY", "ONGC": "ENERGY", "BPCL": "ENERGY",
    "IOC": "ENERGY", "GAIL": "ENERGY", "PETRONET": "ENERGY",
    "HINDPETRO": "ENERGY",
    # IT
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT",
    "TECHM": "IT", "LTIM": "IT", "MPHASIS": "IT", "COFORGE": "IT",
    "PERSISTENT": "IT",
    # Banking / Finance
    "HDFCBANK": "BANKING", "ICICIBANK": "BANKING", "SBIN": "BANKING",
    "AXISBANK": "BANKING", "KOTAKBANK": "BANKING", "BAJFINANCE": "FINANCE",
    "BAJAJFINSV": "FINANCE", "BANDHANBNK": "BANKING", "IDFCFIRSTB": "BANKING",
    "PNB": "BANKING", "BANKBARODA": "BANKING", "INDUSINDBK": "BANKING",
    # Pharma
    "SUNPHARMA": "PHARMA", "DRREDDY": "PHARMA", "CIPLA": "PHARMA",
    "DIVISLAB": "PHARMA", "AUROPHARMA": "PHARMA", "BIOCON": "PHARMA",
    "LUPIN": "PHARMA", "APOLLOHOSP": "PHARMA",
    # Electrical / Power
    "ADANIENT": "POWER", "ADANIPORTS": "INFRA", "ADANIGREEN": "POWER",
    "NTPC": "POWER", "POWERGRID": "POWER", "TATAPOWER": "POWER",
    "NHPC": "POWER", "SJVN": "POWER", "IREDA": "POWER",
    "JSWENERGY": "POWER", "CESC": "POWER",
    # FMCG
    "ITC": "FMCG", "HINDUNILVR": "FMCG", "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG", "DABUR": "FMCG", "MARICO": "FMCG",
    "TATACONSUM": "FMCG", "COLPAL": "FMCG", "GODREJCP": "FMCG",
    # Infra / Real Estate
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


# ---------------------------------------------------------------------------
# Data fetchers (cached per session with TTL)
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 300  # 5 minutes


def _cached(key: str):
    entry = _cache.get(key)
    if entry and (datetime.now(IST).timestamp() - entry[0]) < CACHE_TTL:
        return entry[1]
    return None


def _set_cache(key: str, val: Any):
    _cache[key] = (datetime.now(IST).timestamp(), val)


def get_nifty_trend() -> dict[str, Any]:
    """Fetch Nifty 50 recent trend data via yfinance."""
    cached = _cached("nifty_trend")
    if cached:
        return cached

    try:
        import yfinance as yf
        nifty = yf.Ticker("^NSEI")
        hist = nifty.history(period="5d")
        if hist.empty:
            return {"consecutive_red": 0, "change_pct": 0}

        closes = hist["Close"].tolist()
        red_days = 0
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] < closes[i - 1]:
                red_days += 1
            else:
                break

        latest_change = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if len(closes) >= 2 else 0
        result = {"consecutive_red": red_days, "change_pct": round(latest_change, 2)}
        _set_cache("nifty_trend", result)
        return result
    except Exception as e:
        log.warning("Nifty trend fetch failed: %s", e)
        return {"consecutive_red": 0, "change_pct": 0}


def get_crude_change() -> float:
    """Get Brent crude oil % change (last session)."""
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
    """Fetch today's FII/DII net flow from NSE API."""
    cached = _cached("fii_dii")
    if cached:
        return cached

    try:
        import requests
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        }
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
    """Check if a stock has earnings/results announcement today."""
    cached = _cached("earnings_list")
    if cached is None:
        try:
            import requests
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            }
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
    """Get sector index % change for today."""
    sector_tickers = {
        "METAL": "^CNXMETAL",
        "AUTO": "^CNXAUTO",
        "IT": "^CNXIT",
        "BANKING": "^NSEBANK",
        "PHARMA": "^CNXPHARMA",
        "ENERGY": "^CNXENERGY",
        "FMCG": "^CNXFMCG",
        "REALTY": "^CNXREALTY",
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
# Main filter logic
# ---------------------------------------------------------------------------
def evaluate_signal(
    symbol: str,
    option_type: str,
    channel: str = "ch1",
    trigger_price: float = 0,
) -> FilterResult:
    """Score a channel signal and return TAKE / SKIP / REDUCE with reasons."""
    now = datetime.now(IST)
    score = 50
    reasons: list[str] = []

    # ------ 1. Time of day ------
    hour = now.hour
    minute = now.minute
    if hour == 9 and minute < 30 and option_type == "CE":
        score += 15
        reasons.append("9 AM CE trade — historically 66% win rate")
    elif hour >= 14:
        score -= 5
        reasons.append("Late session — reduced edge")

    # ------ 2. Day of week ------
    weekday = now.weekday()
    if weekday in (0, 1):  # Mon, Tue
        score += 5
        reasons.append(f"{'Monday' if weekday == 0 else 'Tuesday'} — strongest days")
    elif weekday in (3, 4):  # Thu, Fri
        score -= 5
        reasons.append(f"{'Thursday' if weekday == 3 else 'Friday'} — weakest days")

    # ------ 3. Earnings catalyst ------
    sym_clean = symbol.replace(" ", "").upper()
    if check_earnings_today(sym_clean):
        score += 20
        reasons.append(f"{symbol} has earnings today — catalyst trade (+20)")

    # ------ 4. FII/DII flow ------
    flows = get_fii_dii_flow()
    fii = flows["fii"]
    if fii < -500:
        score -= 20
        reasons.append(f"FII heavy selling: ₹{fii:.0f}cr — risk-off market (-20)")
    elif fii < -200:
        score -= 10
        reasons.append(f"FII net sellers: ₹{fii:.0f}cr (-10)")
    elif fii > 500:
        score += 10
        reasons.append(f"FII buying: ₹{fii:.0f}cr — bullish flow (+10)")

    # ------ 5. Crude oil vs sector ------
    sector = _get_sector(sym_clean)
    crude_chg = get_crude_change()

    if sector == "METAL" and crude_chg > 2:
        score -= 25
        reasons.append(f"Metal stock + crude up {crude_chg}% — strong skip signal (-25)")
    elif sector == "METAL" and crude_chg > 0.5:
        score -= 10
        reasons.append(f"Metal stock + crude rising {crude_chg}% — caution (-10)")

    if sector == "AIRLINE" and crude_chg < -2:
        score += 15
        reasons.append(f"Airline stock + crude down {crude_chg}% — bullish setup (+15)")
    elif sector == "AIRLINE" and crude_chg > 2:
        score -= 15
        reasons.append(f"Airline stock + crude up {crude_chg}% — headwind (-15)")

    if sector == "ENERGY" and crude_chg > 2:
        score += 10
        reasons.append(f"Energy stock + crude up {crude_chg}% — tailwind (+10)")

    # ------ 6. Market trend ------
    nifty = get_nifty_trend()
    red_days = nifty["consecutive_red"]
    if red_days >= 3:
        score -= 25
        reasons.append(f"Market red {red_days} consecutive days — strong skip (-25)")
    elif red_days >= 2:
        score -= 15
        reasons.append(f"Market red {red_days} consecutive days — caution (-15)")

    # ------ 7. Sector trend alignment ------
    if sector != "UNKNOWN":
        sector_chg = get_sector_index_trend(sector)
        if option_type == "CE" and sector_chg < -1.5:
            score -= 10
            reasons.append(f"{sector} index down {sector_chg}% — CE against sector trend (-10)")
        elif option_type == "PE" and sector_chg > 1.5:
            score -= 10
            reasons.append(f"{sector} index up {sector_chg}% — PE against sector trend (-10)")
        elif option_type == "CE" and sector_chg > 1:
            score += 10
            reasons.append(f"{sector} index up {sector_chg}% — CE with sector momentum (+10)")

    # ------ 8. Channel-specific adjustments ------
    if channel in ("ch2", "ch3"):
        score -= 5  # ch2/ch3 historically lower accuracy
        reasons.append(f"Channel {channel} — slightly lower historical accuracy (-5)")

    # ------ Decision ------
    if score >= TAKE_THRESHOLD:
        action = "TAKE"
    elif score < SKIP_THRESHOLD:
        action = "SKIP"
    else:
        action = "REDUCE"

    adjusted_lots = None
    if action == "REDUCE":
        adjusted_lots = 1  # minimum lot count

    result = FilterResult(
        score=score, action=action, reasons=reasons, adjusted_lots=adjusted_lots,
    )
    log.info(
        "FILTER %s %s %s: score=%d action=%s reasons=%s",
        symbol, option_type, channel, score, action, reasons,
    )
    return result

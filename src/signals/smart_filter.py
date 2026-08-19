"""Adaptive smart filter — thinks like a trader, not a programmer.

The walk-forward test on 247 trades showed: 62% win rate but -₹46K P&L.
Problem isn't WHICH trades to take — it's risk control. 8 monster losses
(₹-30K to ₹-58K each) wiped out 153 winning trades.

This filter focuses on RISK, not just selection:
  1. Recent streak — last 2-3 trades on this stock lost? Reduce or skip
  2. Loss magnitude — stock's worst loss vs avg win (risk-reward ratio)
  3. Stock track record — but weighted toward RECENT performance
  4. Expectancy — avg win × WR vs avg loss × loss rate
  5. Live market context — soft modifiers, never blanket rules
  6. Sizing output — TAKE (full), REDUCE (half), SKIP (don't touch)

Philosophy: don't try to predict winners (62% WR is already good).
Instead, protect against the trades that can blow up your account.
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
    if len(trades) < 3:
        return None
    wins = sum(1 for t in trades if t["won"])
    return wins / len(trades)


def _avg_pnl(trades: list[dict]) -> float:
    if not trades:
        return 0
    return sum(t["pnl"] for t in trades) / len(trades)


def _avg_win(trades: list[dict]) -> float:
    winners = [t["pnl"] for t in trades if t["pnl"] > 0]
    return sum(winners) / len(winners) if winners else 0


def _avg_loss(trades: list[dict]) -> float:
    losers = [t["pnl"] for t in trades if t["pnl"] <= 0]
    return sum(losers) / len(losers) if losers else 0


def _worst_loss(trades: list[dict]) -> float:
    losers = [t["pnl"] for t in trades if t["pnl"] <= 0]
    return min(losers) if losers else 0


def _recent_streak(trades: list[dict], n: int = 3) -> str:
    """Check the last N trades: 'winning', 'losing', or 'mixed'."""
    if len(trades) < n:
        return "mixed"
    recent = sorted(trades, key=lambda t: t["ts"], reverse=True)[:n]
    if all(t["won"] for t in recent):
        return "winning"
    if all(not t["won"] for t in recent):
        return "losing"
    return "mixed"


def _expectancy(trades: list[dict]) -> float | None:
    """Expected value per trade: (WR × avg_win) + (LR × avg_loss)."""
    if len(trades) < 5:
        return None
    wr = _win_rate(trades)
    if wr is None:
        return None
    aw = _avg_win(trades)
    al = _avg_loss(trades)
    return (wr * aw) + ((1 - wr) * al)


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
# RISK-FOCUSED FILTER — protect against blowups, not just pick winners
# ---------------------------------------------------------------------------
def evaluate_signal(
    symbol: str,
    option_type: str,
    channel: str = "ch1",
    trigger_price: float = 0,
) -> FilterResult:
    """Score a signal based on risk analysis from trade journal.

    Starts at 50 (neutral). Unlike v1 which only boosted, this version
    applies meaningful PENALTIES for risk signals. The goal is not to
    predict winners (62% WR is fine) but to avoid the ₹30K-60K blowups.

    Risk layers (penalties are heavier than boosts — asymmetric by design):
      1. Recent losing streak on this stock → strong penalty
      2. Stock has history of outsized losses → penalty
      3. Negative expectancy on this stock → penalty
      4. Overall stock track record → mild boost/penalty
      5. Recent overall losing streak (last 5 trades all lost) → penalty
      6. Earnings catalyst → boost
      7. Live market context → soft modifiers
    """
    now = datetime.now(IST)
    score = 50
    reasons: list[str] = []

    journal = _get_trade_journal(lookback_days=45)
    sym_clean = symbol.replace(" ", "").upper()
    stock_name = _extract_stock(sym_clean)
    sector = _get_sector(sym_clean)

    stock_trades = [t for t in journal if t["stock"] == stock_name]
    stock_same = [t for t in stock_trades if t["opt_type"] == option_type]

    # ------ 1. RECENT STREAK on this stock (most important risk signal) ------
    if len(stock_same) >= 2:
        streak = _recent_streak(stock_same, n=min(3, len(stock_same)))
        if streak == "losing":
            n = min(3, len(stock_same))
            score -= 20
            reasons.append(f"{stock_name} {option_type}: last {n} trades ALL lost — losing streak (-20)")
        elif streak == "winning":
            score += 5
            reasons.append(f"{stock_name} {option_type}: on a winning streak (+5)")
    elif len(stock_trades) >= 2:
        streak = _recent_streak(stock_trades, n=min(3, len(stock_trades)))
        if streak == "losing":
            score -= 15
            reasons.append(f"{stock_name}: last trades ALL lost — caution (-15)")

    # ------ 2. LOSS MAGNITUDE — has this stock blown up before? ------
    if len(stock_same) >= 3:
        worst = _worst_loss(stock_same)
        aw = _avg_win(stock_same)
        if worst < -15000:
            score -= 15
            reasons.append(f"{stock_name} {option_type}: has a ₹{abs(worst):,.0f} blowup in history (-15)")
        elif worst < -8000 and aw > 0 and abs(worst) > aw * 3:
            score -= 10
            reasons.append(f"{stock_name} {option_type}: worst loss ₹{abs(worst):,.0f} is {abs(worst)/aw:.0f}x avg win — risky (-10)")
    elif len(stock_trades) >= 3:
        worst = _worst_loss(stock_trades)
        if worst < -15000:
            score -= 12
            reasons.append(f"{stock_name}: has a ₹{abs(worst):,.0f} blowup — careful (-12)")

    # ------ 3. EXPECTANCY — is trading this stock actually profitable? ------
    if len(stock_same) >= 5:
        exp = _expectancy(stock_same)
        if exp is not None:
            if exp < -500:
                score -= 15
                reasons.append(f"{stock_name} {option_type}: negative expectancy ₹{exp:+,.0f}/trade — losing setup (-15)")
            elif exp < 0:
                score -= 5
                reasons.append(f"{stock_name} {option_type}: weak expectancy ₹{exp:+,.0f}/trade (-5)")
            elif exp > 2000:
                score += 10
                reasons.append(f"{stock_name} {option_type}: strong expectancy ₹{exp:+,.0f}/trade (+10)")
            elif exp > 500:
                score += 5
                reasons.append(f"{stock_name} {option_type}: positive expectancy ₹{exp:+,.0f}/trade (+5)")

    # ------ 4. STOCK WIN RATE — mild factor, not dominant ------
    if len(stock_same) >= 5:
        wr = _win_rate(stock_same)
        if wr is not None:
            if wr >= 0.70:
                score += 8
                reasons.append(f"{stock_name} {option_type}: {wr:.0%} WR ({len(stock_same)} trades) (+8)")
            elif wr <= 0.35:
                score -= 10
                reasons.append(f"{stock_name} {option_type}: {wr:.0%} WR ({len(stock_same)} trades) (-10)")
    elif len(stock_same) == 0 and len(stock_trades) == 0:
        reasons.append(f"{stock_name}: first time trading — no history")

    # ------ 5. OVERALL RECENT LOSING STREAK ------
    if len(journal) >= 5:
        recent_5 = sorted(journal, key=lambda t: t["ts"], reverse=True)[:5]
        if all(not t["won"] for t in recent_5):
            score -= 15
            reasons.append("Last 5 trades overall ALL lost — bad day, reduce risk (-15)")
        elif sum(1 for t in recent_5 if not t["won"]) >= 4:
            score -= 8
            reasons.append("4 of last 5 trades lost — tough stretch (-8)")

    # ------ 6. EARNINGS CATALYST ------
    if check_earnings_today(sym_clean):
        score += 15
        reasons.append(f"{stock_name} has earnings today — catalyst (+15)")

    # ------ 7. MARKET CONTEXT (soft, never dominant) ------
    crude_chg = get_crude_change()
    if sector == "METAL" and crude_chg > 2 and option_type == "CE":
        score -= 5
        reasons.append(f"Metal CE + crude up {crude_chg:.1f}% — headwind (-5)")
    elif sector == "AIRLINE" and crude_chg > 2 and option_type == "CE":
        score -= 5
        reasons.append(f"Airline CE + crude up {crude_chg:.1f}% — headwind (-5)")
    elif sector == "AIRLINE" and crude_chg < -2 and option_type == "CE":
        score += 5
        reasons.append(f"Airline CE + crude down — tailwind (+5)")

    if sector != "UNKNOWN":
        sector_chg = get_sector_index_trend(sector)
        if abs(sector_chg) > 1.5:
            if (option_type == "CE" and sector_chg < -1.5) or (option_type == "PE" and sector_chg > 1.5):
                score -= 5
                reasons.append(f"{sector} {sector_chg:+.1f}% — {option_type} against sector (-5)")

    flows = get_fii_dii_flow()
    fii = flows["fii"]
    if fii < -500 and option_type == "CE":
        score -= 8
        reasons.append(f"FII heavy selling ₹{fii:.0f}cr + CE — risk-off (-8)")

    nifty = get_nifty_trend()
    red_days = nifty.get("consecutive_red", 0)
    green_days = nifty.get("consecutive_green", 0)

    if red_days >= 3 and option_type == "CE":
        score -= 5
        reasons.append(f"Market red {red_days} days + CE — mild headwind (-5)")
    elif red_days >= 3 and option_type == "PE":
        score += 5
        reasons.append(f"Market red {red_days} days + PE — aligned (+5)")

    # ------ 8. SECTOR PEER RISK (when no stock history) ------
    if len(stock_same) < 3 and sector != "UNKNOWN":
        sector_trades = [t for t in journal
                         if _get_sector(t["stock"]) == sector
                         and t["opt_type"] == option_type]
        if len(sector_trades) >= 5:
            s_exp = _expectancy(sector_trades)
            if s_exp is not None and s_exp < -500:
                score -= 8
                reasons.append(f"{sector} {option_type} peers: negative expectancy — sector struggling (-8)")
            s_worst = _worst_loss(sector_trades)
            if s_worst < -20000:
                score -= 5
                reasons.append(f"{sector} peers had ₹{abs(s_worst):,.0f} blowup — sector risky (-5)")

    # ------ Decision ------
    if score >= TAKE_THRESHOLD:
        action = "TAKE"
        adjusted_lots = None
    elif score >= SKIP_THRESHOLD:
        action = "REDUCE"
        adjusted_lots = 1
    else:
        action = "SKIP"
        adjusted_lots = None

    result = FilterResult(
        score=score, action=action, reasons=reasons, adjusted_lots=adjusted_lots,
    )
    log.info(
        "FILTER %s %s %s: score=%d action=%s | %s",
        symbol, option_type, channel, score, action,
        " | ".join(reasons),
    )
    return result

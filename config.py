"""Non-secret configuration for Trading Buddy.

Secrets (API keys, tokens) live in ``.env`` only — never here.
All times are Asia/Kolkata (IST). See the build spec for rationale.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Execution mode
# --------------------------------------------------------------------------
MODE: str = "PAPER"  # "PAPER" | "LIVE"  — keep PAPER until proven.

# --------------------------------------------------------------------------
# Instruments
# --------------------------------------------------------------------------
# Instruments to ANALYSE (the underlying index charts).
# Trades are executed as WEEKLY OPTIONS / defined-risk spreads on these.
WATCHLIST: list[str] = [
    "NSE:NIFTY 50",   # primary  — weekly expiry, lot 65
    "BSE:SENSEX",     # secondary — weekly expiry (different day), lot 20
]

WATCH_ONLY: list[str] = [
    "NSE:NIFTY BANK",  # sentiment read only — monthly expiry, too volatile for this capital
]

# --------------------------------------------------------------------------
# Capital & risk
# --------------------------------------------------------------------------
# Sandbox capital raised to gather trade data (dummy money). For a REAL ₹50k
# account, drop these back to 50_000 / 500 / 3 / 1_500 before going live.
CAPITAL: int = 200_000            # Rs sandbox capital (dummy)
MAX_RISK_PER_TRADE: int = 2_000   # Rs ~1% of capital
MAX_TRADES_PER_DAY: int = 5
MAX_DAILY_LOSS: int = 6_000       # Rs ~3% of capital — kill-switch threshold
MIN_LOT_SIZE: int = 1             # start at 1 lot; scale only after proven

# Lot sizes (Jan 2026 SEBI/NSE revision) — VERIFY against broker before live use.
LOT_SIZES: dict[str, int] = {"NIFTY": 65, "SENSEX": 20, "BANKNIFTY": 30}

# Tradable style on this capital: option BUYING and DEBIT/CREDIT SPREADS only.
# Naked option selling and futures need ~Rs 1-1.5 lakh+ margin -> out of scope.
STRATEGY_UNIVERSE: list[str] = [
    "long_option",
    "bull_call_spread",
    "bull_put_spread",
    "bear_call_spread",
    "bear_put_spread",
]

# --------------------------------------------------------------------------
# Charting / analysis cadence
# --------------------------------------------------------------------------
PRIMARY_TIMEFRAME: str = "15min"   # trend, pivots, RSI, patterns
ENTRY_TIMEFRAME: str = "5min"      # entry-timing confirmation
ANALYSIS_INTERVAL_MIN: int = 15    # how often to re-analyse the watchlist

# Kite historical-data interval strings keyed by our timeframe labels.
KITE_INTERVALS: dict[str, str] = {"15min": "15minute", "5min": "5minute", "day": "day"}

# RSI lookback periods (Wilder's). "fast" reacts quicker; "slow" is the classic 14.
RSI_FAST_PERIOD: int = 9
RSI_SLOW_PERIOD: int = 14
RSI_OVERBOUGHT: float = 70.0
RSI_OVERSOLD: float = 30.0

# --------------------------------------------------------------------------
# Signal engine (Phase 3) — low-risk rule parameters
# --------------------------------------------------------------------------
# How close (as a fraction of price) the LTP must be to a pivot to count as
# "near" it — adapts to each instrument's price scale.
PIVOT_PROXIMITY_PCT: float = 0.004      # 0.4%
# Protective-stop buffer placed just beyond the pivot, as a fraction of price.
STOP_BUFFER_PCT: float = 0.0015         # 0.15%
# Target distance as a multiple of the (entry - stop) risk.
SIGNAL_RR_RATIO: float = 1.5
# Look-back window for news sentiment that informs a signal.
NEWS_LOOKBACK_HOURS: int = 12
# Map an analysed watchlist symbol to the keyword used to match news_items.symbol.
SYMBOL_NEWS_KEYWORDS: dict[str, str] = {
    "NSE:NIFTY 50": "NIFTY",
    "BSE:SENSEX": "SENSEX",
    "NSE:NIFTY BANK": "BANK",
}

# --------------------------------------------------------------------------
# Execution (Phase 5) — option selection & sizing
# --------------------------------------------------------------------------
# Index -> (NFO/BFO option exchange, F&O underlying name, strike step, lot key).
OPTION_SPECS: dict[str, dict[str, Any]] = {
    "NSE:NIFTY 50":  {"exchange": "NFO", "name": "NIFTY",     "strike_step": 50,  "lot_key": "NIFTY"},
    "BSE:SENSEX":    {"exchange": "BFO", "name": "SENSEX",    "strike_step": 100, "lot_key": "SENSEX"},
    "NSE:NIFTY BANK":{"exchange": "NFO", "name": "BANKNIFTY", "strike_step": 100, "lot_key": "BANKNIFTY"},
}
# Approx ATM option delta — maps an index stop distance to an option-premium stop.
OPTION_DELTA_ASSUMPTION: float = 0.5
# Order product/type defaults (Zerodha: no Bracket/Cover orders — use SL-M + separate leg).
ORDER_PRODUCT: str = "MIS"   # intraday (Kite naming; Upstox uses "I" — see below)
ENTRY_ORDER_TYPE: str = "MARKET"
STOP_ORDER_TYPE: str = "SL-M"

# --------------------------------------------------------------------------
# Execution broker (Phase: Upstox split)
# Plan: analyse/data on KITE, place (dummy/sandbox) trades on UPSTOX.
# --------------------------------------------------------------------------
EXECUTION_BROKER: str = "upstox"   # "internal" (simulate in-app) | "upstox" (sandbox orders)

# Upstox API (orders only; data stays on Kite). Sandbox uses a separate 30-day
# token (UPSTOX_SANDBOX_TOKEN) — no daily login on the execution side.
# Sandbox host is api-sandbox.upstox.com (verified). For LIVE, switch to
# https://api-hft.upstox.com/v3/order/place (or api.upstox.com/v2/order/place).
UPSTOX_ORDER_URL: str = "https://api-sandbox.upstox.com/v3/order/place"
UPSTOX_INSTRUMENTS_URL: str = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
)
UPSTOX_PRODUCT: str = "I"        # intraday
UPSTOX_VALIDITY: str = "DAY"
UPSTOX_ENTRY_ORDER_TYPE: str = "MARKET"
UPSTOX_STOP_ORDER_TYPE: str = "SL-M"

# Map our analysed index symbol -> Upstox option (segment + underlying name).
UPSTOX_OPTION_SEGMENTS: dict[str, dict[str, str]] = {
    "NSE:NIFTY 50":   {"segment": "NSE_FO", "name": "NIFTY"},
    "BSE:SENSEX":     {"segment": "BSE_FO", "name": "SENSEX"},
    "NSE:NIFTY BANK": {"segment": "NSE_FO", "name": "BANKNIFTY"},
}

TIMEZONE: str = "Asia/Kolkata"

# Language for the Telegram messages the end-user reads ("tamil" | "english").
# Console/logs always stay English (for the operator). Numbers and instrument
# names (NIFTY/SENSEX) are kept as-is in both languages.
ALERT_LANGUAGE: str = "tamil"

# --------------------------------------------------------------------------
# Market hours (IST)
# --------------------------------------------------------------------------
MARKET_OPEN: str = "09:15"
MARKET_CLOSE: str = "15:30"

# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------
NEWS_FEEDS: list[str] = [
    # Confirm/extend these RSS sources before relying on them.
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.livemint.com/rss/markets",
    "https://www.business-standard.com/rss/markets-106.rss",
]

# --------------------------------------------------------------------------
# LLM models (anthropic)
# --------------------------------------------------------------------------
LLM_FAST_MODEL: str = "claude-haiku-4-5-20251001"  # news / routine analysis
LLM_SMART_MODEL: str = "claude-sonnet-4-6"          # trade decisions

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
import os

BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
DATA_DIR: str = os.path.join(BASE_DIR, "data")
LOG_DIR: str = os.path.join(BASE_DIR, "logs")
DB_PATH: str = os.path.join(DATA_DIR, "trading_buddy.db")


def as_dict() -> dict:
    """Return the non-secret config as a plain dict (for printing / --selftest)."""
    return {
        "MODE": MODE,
        "WATCHLIST": WATCHLIST,
        "WATCH_ONLY": WATCH_ONLY,
        "CAPITAL": CAPITAL,
        "MAX_RISK_PER_TRADE": MAX_RISK_PER_TRADE,
        "MAX_TRADES_PER_DAY": MAX_TRADES_PER_DAY,
        "MAX_DAILY_LOSS": MAX_DAILY_LOSS,
        "MIN_LOT_SIZE": MIN_LOT_SIZE,
        "LOT_SIZES": LOT_SIZES,
        "STRATEGY_UNIVERSE": STRATEGY_UNIVERSE,
        "PRIMARY_TIMEFRAME": PRIMARY_TIMEFRAME,
        "ENTRY_TIMEFRAME": ENTRY_TIMEFRAME,
        "ANALYSIS_INTERVAL_MIN": ANALYSIS_INTERVAL_MIN,
        "TIMEZONE": TIMEZONE,
        "MARKET_OPEN": MARKET_OPEN,
        "MARKET_CLOSE": MARKET_CLOSE,
        "NEWS_FEEDS": NEWS_FEEDS,
        "LLM_FAST_MODEL": LLM_FAST_MODEL,
        "LLM_SMART_MODEL": LLM_SMART_MODEL,
        "DB_PATH": DB_PATH,
    }

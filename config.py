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
    # --- Indices (weekly/monthly options) ---
    "NSE:NIFTY 50",
    "BSE:SENSEX",
    "NSE:NIFTY BANK",
    # --- F&O stocks (profitable in 90d backtest, trail_atr) ---
    "NSE:TCS",         # +₹30k, 83% win, PF 1.50
    "NSE:LT",          # +₹14k, 79% win, PF 1.14
    "NSE:ITC",         # +₹6.9k, 77% win, PF 1.09
    "NSE:SBIN",        # +₹3k, 78% win, PF 1.03
]

WATCH_ONLY: list[str] = []   # all analysed symbols are now traded

# --------------------------------------------------------------------------
# Capital & risk
# --------------------------------------------------------------------------
# Sandbox capital raised to gather trade data (dummy money). For a REAL ₹50k
# account, drop these back to 50_000 / 500 / 3 / 1_500 before going live.
CAPITAL: int = 200_000            # Rs sandbox capital (dummy)
MAX_RISK_PER_TRADE: int = 10_000  # Rs sandbox — sized for NIFTY lot 65
# Effectively uncapped for the sandbox hedge-recovery week: the compensation
# chain must not be cut off mid-rescue (Jul-9: the 10-trade cap blocked the
# compensating trades and turned a ~10k day into -45k).
MAX_TRADES_PER_DAY: int = 100
MAX_DAILY_LOSS: int = 200_000     # Rs SANDBOX ONLY — effectively disables the
# kill switch so every trade plays out and we gather a full performance sample.
# ⚠️ MUST drop back to ~3% of real capital (e.g. 6_000 on ₹2L) BEFORE going LIVE.
MIN_LOT_SIZE: int = 1             # start at 1 lot; scale only after proven
# Hard cap on lots per trade (0 = no cap, size by risk budget). User preference
# during the manual-exit week: 5 lots per trade (half of budget-based ~10).
MAX_LOTS_PER_TRADE: int = 5
# Never buy an option expiring today (expiry-day OTM premium melts to zero by
# close — see the Jul-7 NIFTY 24500CE trade: 36.55 -> 0.10). Roll to next expiry.
MIN_DAYS_TO_EXPIRY: int = 1

# Lot sizes (Jan 2026 SEBI/NSE revision) — VERIFY against broker before live use.
LOT_SIZES: dict[str, int] = {
    "NIFTY": 65, "SENSEX": 20, "BANKNIFTY": 30,
    "RELIANCE": 250, "HDFCBANK": 550, "ICICIBANK": 700,
    "INFY": 400, "TCS": 175, "SBIN": 750, "AXISBANK": 625,
    "KOTAKBANK": 400, "ITC": 1600, "LT": 150, "BHARTIARTL": 475,
    "HINDUNILVR": 300, "MARUTI": 100, "TATAMOTORS": 1400, "BAJFINANCE": 125,
}

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
# How often to check OPEN positions for exits/take-profit/hedge triggers.
# Every minute: hedges open and bank as close to their levels as possible.
MONITOR_INTERVAL_MIN: int = 1

# Kite historical-data interval strings keyed by our timeframe labels.
KITE_INTERVALS: dict[str, str] = {"15min": "15minute", "5min": "5minute", "day": "day"}

# RSI lookback periods (Wilder's). "fast" reacts quicker; "slow" is the classic 14.
# Weekly-review hypothesis (2026-07-03): "require a candlestick pattern for
# entry". OFF until validated on the backtest — the review's sample mixed the
# old mean-reversion era's wins with the trend era's losses (confounded).
REQUIRE_PATTERN: bool = False

RSI_FAST_PERIOD: int = 9
RSI_SLOW_PERIOD: int = 14
RSI_OVERBOUGHT: float = 70.0
RSI_OVERSOLD: float = 30.0

# --------------------------------------------------------------------------
# Signal engine (Phase 3) — low-risk rule parameters
# --------------------------------------------------------------------------
# How close (as a fraction of price) the LTP must be to a pivot to count as
# "near" it — adapts to each instrument's price scale. Wider = more setups.
PIVOT_PROXIMITY_PCT: float = 0.015      # 1.5%
# Protective-stop buffer placed just beyond the pivot, as a fraction of price.
STOP_BUFFER_PCT: float = 0.0015         # 0.15%
# Target distance as a multiple of the (entry - stop) risk.
SIGNAL_RR_RATIO: float = 2.0

# --------------------------------------------------------------------------
# Trend-following strategy (EMA / VWAP / Supertrend / ADX / ATR)
# --------------------------------------------------------------------------
# The engine now trades WITH the trend (not mean-reversion against it). A signal
# fires only when trend, momentum and trend-strength all agree, and only when
# the market is actually trending (ADX above threshold) — never in chop.
STRATEGY_MODE: str = "trend"        # "trend" (new) | "meanrev" (legacy pivots)

EMA_FAST_PERIOD: int = 9            # short EMA (trend direction)
EMA_SLOW_PERIOD: int = 21          # long EMA (trend direction)

SUPERTREND_ATR_PERIOD: int = 10    # Supertrend ATR lookback
SUPERTREND_MULTIPLIER: float = 3.0 # Supertrend ATR multiplier (India default)

ADX_PERIOD: int = 14               # ADX lookback
# Filter-sweep (in- & out-of-sample) showed ADX>=25 entered trends late and lost
# in choppy markets; ADX>=20 was the only setting profitable in BOTH periods.
ADX_MIN_TREND: float = 20.0        # below this = sideways/chop → no trade

# Entry-quality filters (reduce over-trading / low-conviction entries).
# Price must be at least this fraction beyond EMA_slow to confirm a real trend.
# Sweep found 0.00% most robust (the ADX floor already filters chop), so the EMA
# only needs to be on the right side of the average.
EMA_TREND_MIN_PCT: float = 0.0000
# Trade only within this intraday window (skip the noisy open/close auctions).
ENTRY_START: str = "09:45"
ENTRY_END: str = "15:00"
# Cap re-entries per symbol per day, and hold one position per symbol at a time.
MAX_TRADES_PER_SYMBOL_PER_DAY: int = 3

ATR_PERIOD: int = 14               # ATR for stops/targets
ATR_STOP_MULT: float = 1.0         # stop = entry ∓ 1 × ATR
ATR_TARGET_MULT: float = 2.0       # target = entry ± 2 × ATR (1:2 RR)

# Exit style (backtest experimentation; live currently uses "target").
#   "target"    — fixed ₹/ATR profit target + initial ATR stop (current live)
#   "trail_st"  — ride the trend, exit when Supertrend flips against the trade
#   "trail_atr" — chandelier trailing stop (peak ∓ ATR_TRAIL_MULT × ATR)
# Backtest (in- & out-of-sample) showed trail_atr is the best exit; the old
# fixed ₹ target was the worst. Live now rides the trend with a trailing stop.
EXIT_MODE: str = "trail_atr"
# Stop-and-reverse (backtest experiment): when an OPPOSITE signal fires while a
# position is open, close it and enter the new direction — the validated form
# of "averaging with an opposite trade" (no dead leg, no double theta). OFF in
# live until the backtest approves it on both windows.
STOP_AND_REVERSE: bool = False
ATR_TRAIL_MULT: float = 2.0        # chandelier trailing-stop distance in ATRs
# For the "no_stop" backtest experiment only: how many ATRs of adverse index
# move wipes out the option premium (ATM option → ~0, theta included). Loss is
# floored at PREMIUM_RISK_MULT × the 1-ATR risk = the full premium paid.
PREMIUM_RISK_MULT: float = 3.0

# Rupee take-profit — applies in ALL exit modes: banks the win once unrealised
# profit reaches this amount (user preference: ₹3,000/trade is enough). The
# trailing stop still manages the downside. Set 0 to let winners run fully.
# NOTE: with MAX_RISK_PER_TRADE=10k, a ₹3k cap needs a ~77% win rate to break
# even — validate via backtest and watch the forward data.
PROFIT_TARGET_RUPEES: float = 3000.0

# Manual-exit experiment (week of Jul 6): automatic stop-losses DISABLED — the
# user cuts losers manually via the dashboard Close button. The ₹ take-profit
# and the 15:15 EOD square-off remain active. Set True to restore auto stops.
STOP_LOSS_ENABLED: bool = False

# Hedge-recovery flow (sandbox experiment, week of Jul 8): when a position is
# down HEDGE_TRIGGER_RUPEES, open an OPPOSITE-direction ATM option on the same
# index. The hedge banks via the normal ₹ take-profit; if the original is still
# bleeding afterwards, the monitor hedges again (repeat). The original is closed
# automatically once its premium decays to ZERO_CLOSE_PCT of entry ("became
# zero" — nothing left to recover).
HEDGE_ON_LOSS: bool = True
HEDGE_TRIGGER_RUPEES: float = 4000.0
ZERO_CLOSE_PCT: float = 0.10
# Swap, don't stack (user, Jul 11): when a HEDGE itself bleeds to the trigger,
# CLOSE it (realise ~-4k) and open the opposite side in its place — instead of
# keeping the bleeding leg open alongside a new one (Friday's -44k pileup:
# bottom-bought PEs + top-bought CE all open together). Originals unchanged:
# still held for recovery / zero-close.
HEDGE_SWAP_ON_REVERSE: bool = True
# Swap trigger widened (user, Jul 13): hold a bleeding hedge through normal
# dips down to -8k; swap only beyond that. Halves chop false-alarms (Jul-13:
# 13 swaps x ~4.6k) while keeping the per-leg worst case defined (~-8k, never
# Friday's open-ended -44k pileup).
HEDGE_SWAP_TRIGGER_RUPEES: float = 8000.0
# Trend gate (Jul 14, from live ledger evidence): a hedge may only OPEN in the
# direction of the current Supertrend. Stops the swap bleed caused by buying
# counter-trend hedges on ordinary dips (Jul-14: five CE hedges bought into a
# falling market = -43k swaps while the PE originals won all day).
HEDGE_TREND_GATE: bool = True

# 15-minute market pulse on Telegram: every analysis cycle sends a compact
# per-symbol read (price, ST/ADX/RSI, and which gate blocks a trade). ~25
# messages/day during market hours — set False to silence.
CANDLE_PULSE: bool = True
# Add an LLM prediction (fast model) to each 15-min pulse: market read + next
# 15-60 min direction per index. Informational only — not a trading input.
PULSE_PREDICTION: bool = True

# Morning briefing at 08:30 IST: top-10 overnight/pre-market news + an overall
# day prediction built from them (smart model, once a day).
MORNING_BRIEF: bool = True

# Positional weekly options (user request, Jul 9): hold positions overnight —
# NO daily 15:15 square-off. A contract still MUST close on its own expiry day
# (options settle; can't be held past expiry). Set True to restore intraday.
INTRADAY_SQUAREOFF: bool = False

# RSI momentum window allowed for entries. Wide on purpose: in trend-following
# a strong trend can ride RSI high/low for a long time, so we only exclude truly
# blown-off extremes rather than normal trend momentum.
RSI_LONG_MIN: float = 45.0         # longs: momentum must be up
RSI_LONG_MAX: float = 88.0         # longs: block only an extreme blow-off top
RSI_SHORT_MIN: float = 12.0        # shorts: block only an extreme capitulation
RSI_SHORT_MAX: float = 55.0        # shorts: momentum must be down
# Look-back window for news sentiment that informs a signal.
NEWS_LOOKBACK_HOURS: int = 12
# Map an analysed watchlist symbol to the keyword used to match news_items.symbol.
SYMBOL_NEWS_KEYWORDS: dict[str, str] = {
    "NSE:NIFTY 50": "NIFTY",
    "BSE:SENSEX": "SENSEX",
    "NSE:NIFTY BANK": "BANK",
    "NSE:RELIANCE": "RELIANCE",
    "NSE:HDFCBANK": "HDFC",
    "NSE:ICICIBANK": "ICICI",
    "NSE:INFY": "INFOSYS",
    "NSE:TCS": "TCS",
    "NSE:SBIN": "SBI",
    "NSE:AXISBANK": "AXIS",
    "NSE:KOTAKBANK": "KOTAK",
    "NSE:ITC": "ITC",
    "NSE:LT": "LARSEN",
    "NSE:BHARTIARTL": "AIRTEL",
    "NSE:HINDUNILVR": "HINDUSTAN",
    "NSE:MARUTI": "MARUTI",
    "NSE:TATAMOTORS": "TATA MOTORS",
    "NSE:BAJFINANCE": "BAJAJ",
}

# --------------------------------------------------------------------------
# Execution (Phase 5) — option selection & sizing
# --------------------------------------------------------------------------
# Index -> (NFO/BFO option exchange, F&O underlying name, strike step, lot key).
OPTION_SPECS: dict[str, dict[str, Any]] = {
    "NSE:NIFTY 50":   {"exchange": "NFO", "name": "NIFTY",      "strike_step": 50,   "lot_key": "NIFTY"},
    "BSE:SENSEX":     {"exchange": "BFO", "name": "SENSEX",     "strike_step": 100,  "lot_key": "SENSEX"},
    "NSE:NIFTY BANK": {"exchange": "NFO", "name": "BANKNIFTY",  "strike_step": 100,  "lot_key": "BANKNIFTY"},
    "NSE:RELIANCE":   {"exchange": "NFO", "name": "RELIANCE",   "strike_step": 20,   "lot_key": "RELIANCE"},
    "NSE:HDFCBANK":   {"exchange": "NFO", "name": "HDFCBANK",   "strike_step": 20,   "lot_key": "HDFCBANK"},
    "NSE:ICICIBANK":  {"exchange": "NFO", "name": "ICICIBANK",  "strike_step": 10,   "lot_key": "ICICIBANK"},
    "NSE:INFY":       {"exchange": "NFO", "name": "INFY",       "strike_step": 20,   "lot_key": "INFY"},
    "NSE:TCS":        {"exchange": "NFO", "name": "TCS",        "strike_step": 50,   "lot_key": "TCS"},
    "NSE:SBIN":       {"exchange": "NFO", "name": "SBIN",       "strike_step": 5,    "lot_key": "SBIN"},
    "NSE:AXISBANK":   {"exchange": "NFO", "name": "AXISBANK",   "strike_step": 20,   "lot_key": "AXISBANK"},
    "NSE:KOTAKBANK":  {"exchange": "NFO", "name": "KOTAKBANK",  "strike_step": 20,   "lot_key": "KOTAKBANK"},
    "NSE:ITC":        {"exchange": "NFO", "name": "ITC",        "strike_step": 5,    "lot_key": "ITC"},
    "NSE:LT":         {"exchange": "NFO", "name": "LT",         "strike_step": 20,   "lot_key": "LT"},
    "NSE:BHARTIARTL": {"exchange": "NFO", "name": "BHARTIARTL", "strike_step": 20,   "lot_key": "BHARTIARTL"},
    "NSE:HINDUNILVR": {"exchange": "NFO", "name": "HINDUNILVR", "strike_step": 20,   "lot_key": "HINDUNILVR"},
    "NSE:MARUTI":     {"exchange": "NFO", "name": "MARUTI",     "strike_step": 100,  "lot_key": "MARUTI"},
    "NSE:TATAMOTORS": {"exchange": "NFO", "name": "TATAMOTORS", "strike_step": 10,   "lot_key": "TATAMOTORS"},
    "NSE:BAJFINANCE": {"exchange": "NFO", "name": "BAJFINANCE", "strike_step": 50,   "lot_key": "BAJFINANCE"},
}
# Approx ATM option delta — maps an index stop distance to an option-premium stop.
OPTION_DELTA_ASSUMPTION: float = 0.5
# Order product/type defaults (Zerodha: no Bracket/Cover orders — use SL-M + separate leg).
ORDER_PRODUCT: str = "NRML"  # carry-forward (Kite naming; Upstox uses "D" — see below)
ENTRY_ORDER_TYPE: str = "MARKET"
STOP_ORDER_TYPE: str = "SL-M"

# --------------------------------------------------------------------------
# Execution broker (Phase: Upstox split)
# Plan: analyse/data on KITE, place (dummy/sandbox) trades on UPSTOX.
# --------------------------------------------------------------------------
EXECUTION_BROKER: str = "upstox"   # "internal" (simulate in-app) | "upstox" (sandbox orders)
# Simulate order fills instead of sending them to the Upstox sandbox. Keeps the
# FULL upstox flow (option resolution, hedges, swaps, monitor) but replaces the
# actual order HTTP call with a simulated fill — so a dead sandbox order token
# never halts trading. P&L still uses real premium prices from the data feed.
UPSTOX_SIMULATE_ORDERS: bool = True

# Market-DATA broker (Jul 15): Kite Connect subscription lapsed (paid 1 month
# by design); Upstox's API is free. "upstox" serves candles/LTP/instruments via
# a drop-in client with the same interface as KiteClient. Needs UPSTOX_API_KEY,
# UPSTOX_API_SECRET in .env and a daily login (~30s):
#   .venv/bin/python -m src.broker.upstox_data
DATA_BROKER: str = "upstox"        # "kite" | "upstox"
UPSTOX_API_BASE: str = "https://api.upstox.com"
UPSTOX_REDIRECT_URI: str = "https://127.0.0.1"
# Watchlist symbol -> Upstox index instrument key.
UPSTOX_INDEX_KEYS: dict[str, str] = {
    "NSE:NIFTY 50":   "NSE_INDEX|Nifty 50",
    "BSE:SENSEX":     "BSE_INDEX|SENSEX",
    "NSE:NIFTY BANK": "NSE_INDEX|Nifty Bank",
    "NSE:RELIANCE":   "NSE_EQ|INE002A01018",
    "NSE:HDFCBANK":   "NSE_EQ|INE040A01034",
    "NSE:ICICIBANK":  "NSE_EQ|INE090A01021",
    "NSE:INFY":       "NSE_EQ|INE009A01021",
    "NSE:TCS":        "NSE_EQ|INE467B01029",
    "NSE:SBIN":       "NSE_EQ|INE062A01020",
    "NSE:AXISBANK":   "NSE_EQ|INE238A01034",
    "NSE:KOTAKBANK":  "NSE_EQ|INE237A01036",
    "NSE:ITC":        "NSE_EQ|INE154A01025",
    "NSE:LT":         "NSE_EQ|INE018A01030",
    "NSE:BHARTIARTL": "NSE_EQ|INE397D01024",
    "NSE:HINDUNILVR": "NSE_EQ|INE030A01027",
    "NSE:MARUTI":     "NSE_EQ|INE585B01010",
    "NSE:TATAMOTORS": "NSE_EQ|INE155A01022",
    "NSE:BAJFINANCE": "NSE_EQ|INE296A01032",
}
# Our timeframe labels -> Upstox v3 historical (unit, interval).
UPSTOX_INTERVALS: dict[str, tuple] = {
    "15minute": ("minutes", 15), "5minute": ("minutes", 5), "day": ("days", 1),
}

# Upstox API (orders only; data stays on Kite). Sandbox uses a separate 30-day
# token (UPSTOX_SANDBOX_TOKEN) — no daily login on the execution side.
# Sandbox host is api-sandbox.upstox.com (verified). For LIVE, switch to
# https://api-hft.upstox.com/v3/order/place (or api.upstox.com/v2/order/place).
UPSTOX_ORDER_URL: str = "https://api-sandbox.upstox.com/v3/order/place"
UPSTOX_INSTRUMENTS_URL: str = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
)
UPSTOX_PRODUCT: str = "D"        # delivery/carry-forward — hold weekly options overnight
UPSTOX_VALIDITY: str = "DAY"
UPSTOX_ENTRY_ORDER_TYPE: str = "MARKET"
UPSTOX_STOP_ORDER_TYPE: str = "SL-M"

# Map our analysed index symbol -> Upstox option (segment + underlying name).
UPSTOX_OPTION_SEGMENTS: dict[str, dict[str, str]] = {
    "NSE:NIFTY 50":   {"segment": "NSE_FO", "name": "NIFTY"},
    "BSE:SENSEX":     {"segment": "BSE_FO", "name": "SENSEX"},
    "NSE:NIFTY BANK": {"segment": "NSE_FO", "name": "BANKNIFTY"},
    "NSE:RELIANCE":   {"segment": "NSE_FO", "name": "RELIANCE"},
    "NSE:HDFCBANK":   {"segment": "NSE_FO", "name": "HDFCBANK"},
    "NSE:ICICIBANK":  {"segment": "NSE_FO", "name": "ICICIBANK"},
    "NSE:INFY":       {"segment": "NSE_FO", "name": "INFY"},
    "NSE:TCS":        {"segment": "NSE_FO", "name": "TCS"},
    "NSE:SBIN":       {"segment": "NSE_FO", "name": "SBIN"},
    "NSE:AXISBANK":   {"segment": "NSE_FO", "name": "AXISBANK"},
    "NSE:KOTAKBANK":  {"segment": "NSE_FO", "name": "KOTAKBANK"},
    "NSE:ITC":        {"segment": "NSE_FO", "name": "ITC"},
    "NSE:LT":         {"segment": "NSE_FO", "name": "LT"},
    "NSE:BHARTIARTL": {"segment": "NSE_FO", "name": "BHARTIARTL"},
    "NSE:HINDUNILVR": {"segment": "NSE_FO", "name": "HINDUNILVR"},
    "NSE:MARUTI":     {"segment": "NSE_FO", "name": "MARUTI"},
    "NSE:TATAMOTORS": {"segment": "NSE_FO", "name": "TATAMOTORS"},
    "NSE:BAJFINANCE": {"segment": "NSE_FO", "name": "BAJFINANCE"},
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

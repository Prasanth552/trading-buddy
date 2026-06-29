"""Signal engine — combine technicals + news into low-risk Signal objects.

Rules (see build spec §8 Phase 3). A signal is raised only when ALL hold:
  - the LTP sits near a pivot (support for longs, resistance for shorts)
  - RSI is not over-extended *against* the trade
    (longs blocked when overbought; shorts blocked when oversold)
  - news sentiment does not conflict (no opposing high-confidence news)
  - a defined-risk position fits MAX_RISK_PER_TRADE

When unsure, do nothing -> return None. **No orders are placed here.**

NOTE ON SIZING: we analyse the underlying INDEX, so entry/stop/target are
index levels. The actual trade is a weekly option / defined-risk spread whose
strike and premium-based lot sizing are finalised in the execution module
(Phase 5). Here ``qty`` is the starting lot count (MIN_LOT_SIZE) and
``max_risk`` is the rupee risk budget cap (MAX_RISK_PER_TRADE).
"""
from __future__ import annotations

from typing import Any

import config
from src.signals.models import Signal
from src.utils import market_calendar as mc

# Patterns that support a bullish vs bearish bias.
_BULLISH_PATTERNS = {"hammer", "bullish_engulfing", "doji"}
_BEARISH_PATTERNS = {"shooting_star", "bearish_engulfing", "doji"}


def aggregate_news_sentiment(news_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a list of tagged news items to a directional view.

    Returns ``{"net": "bullish"|"bearish"|"neutral", "bull": float, "bear": float,
    "has_high_bull": bool, "has_high_bear": bool}``. Confidence weights:
    high=3, medium=2, low=1.
    """
    weights = {"high": 3, "medium": 2, "low": 1}
    bull = bear = 0.0
    has_high_bull = has_high_bear = False
    for r in news_rows:
        w = weights.get((r.get("confidence") or "").lower(), 1)
        sent = (r.get("sentiment") or "neutral").lower()
        if sent == "bullish":
            bull += w
            has_high_bull = has_high_bull or w >= 3
        elif sent == "bearish":
            bear += w
            has_high_bear = has_high_bear or w >= 3
    if bull > bear:
        net = "bullish"
    elif bear > bull:
        net = "bearish"
    else:
        net = "neutral"
    return {
        "net": net, "bull": bull, "bear": bear,
        "has_high_bull": has_high_bull, "has_high_bear": has_high_bear,
    }


def _nearest_below(levels: list[float], price: float) -> float | None:
    below = [v for v in levels if v <= price]
    return max(below) if below else None


def _nearest_above(levels: list[float], price: float) -> float | None:
    above = [v for v in levels if v > price]
    return min(above) if above else None


def evaluate(
    snapshot: dict[str, Any],
    news: dict[str, Any] | None = None,
) -> Signal | None:
    """Evaluate one symbol's snapshot into a Signal or None.

    Dispatches on ``config.STRATEGY_MODE``: "trend" (EMA/VWAP/Supertrend/ADX,
    trades WITH the trend) or "meanrev" (legacy pivot mean-reversion).
    """
    news = news or {"net": "neutral", "has_high_bull": False, "has_high_bear": False}
    if getattr(config, "STRATEGY_MODE", "trend") == "trend":
        return _evaluate_trend(snapshot, news)
    return _evaluate_meanrev(snapshot, news)


def _nan(x: Any) -> bool:
    return x is None or (isinstance(x, float) and x != x)


def _evaluate_trend(
    snapshot: dict[str, Any],
    news: dict[str, Any],
) -> Signal | None:
    """Trend-following: trade only WITH a confirmed, strong trend.

    LONG  : price > VWAP and > EMA_slow, EMA_fast > EMA_slow, Supertrend up,
            ADX > threshold, RSI in [RSI_LONG_MIN, RSI_LONG_MAX].
    SHORT : mirror image.
    Stops/targets are ATR-based (1×ATR stop, 2×ATR target). When unsure -> None.
    """
    ltp = snapshot.get("ltp")
    ema_fast = snapshot.get("ema_fast")
    ema_slow = snapshot.get("ema_slow")
    vwap = snapshot.get("vwap")
    atr = snapshot.get("atr")
    st_dir = snapshot.get("supertrend_dir")
    adx = snapshot.get("adx")
    rsi_fast = snapshot.get("rsi_fast")
    patterns = set(snapshot.get("patterns") or [])

    # Need every trend input; bail (do nothing) if any is missing/NaN.
    if any(_nan(v) for v in (ltp, ema_fast, ema_slow, vwap, atr, st_dir, adx, rsi_fast)):
        return None
    if atr <= 0:
        return None

    # Trend-strength gate: only trade when the market is actually trending.
    if adx < config.ADX_MIN_TREND:
        return None

    # Require price to be a minimum distance beyond EMA_slow (real trend, not
    # hugging the average) — filters low-conviction whipsaw entries.
    dist = getattr(config, "EMA_TREND_MIN_PCT", 0.0) * ema_slow
    bull = (ltp > vwap and ltp > ema_slow + dist and ema_fast > ema_slow and st_dir > 0
            and config.RSI_LONG_MIN <= rsi_fast <= config.RSI_LONG_MAX)
    bear = (ltp < vwap and ltp < ema_slow - dist and ema_fast < ema_slow and st_dir < 0
            and config.RSI_SHORT_MIN <= rsi_fast <= config.RSI_SHORT_MAX)

    # News conflict gate — veto only when net sentiment strongly opposes.
    net_news = news.get("net", "neutral")
    if net_news == "bearish" and news.get("has_high_bear"):
        bull = False
    if net_news == "bullish" and news.get("has_high_bull"):
        bear = False

    if bull == bear:  # both or neither -> ambiguous -> do nothing
        return None

    entry = float(ltp)
    if bull:
        direction = "long"
        stop = entry - config.ATR_STOP_MULT * atr
        target = entry + config.ATR_TARGET_MULT * atr
        pattern_ok = bool(patterns & _BULLISH_PATTERNS)
    else:
        direction = "short"
        stop = entry + config.ATR_STOP_MULT * atr
        target = entry - config.ATR_TARGET_MULT * atr
        pattern_ok = bool(patterns & _BEARISH_PATTERNS)

    confidence = "high" if (pattern_ok and adx >= 30) else ("medium" if adx >= 25 else "low")
    rationale = (
        f"{direction.upper()} (trend) LTP {entry:,.2f} | "
        f"EMA{config.EMA_FAST_PERIOD}>{config.EMA_SLOW_PERIOD}: "
        f"{ema_fast:,.2f}/{ema_slow:,.2f}, VWAP {vwap:,.2f}, "
        f"Supertrend {'up' if st_dir > 0 else 'down'}, ADX {adx:.1f}, RSI {rsi_fast:.1f}. "
        f"News {net_news}. ATR {atr:,.2f} -> stop {stop:,.2f}, target {target:,.2f} "
        f"(RR {config.ATR_TARGET_MULT/config.ATR_STOP_MULT:.1f}). Confidence {confidence}."
    )

    return Signal(
        symbol=snapshot["symbol"],
        direction=direction,
        entry=round(entry, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        qty=config.MIN_LOT_SIZE,
        max_risk=float(config.MAX_RISK_PER_TRADE),
        rationale=rationale,
        status="new",
    )


def _evaluate_meanrev(
    snapshot: dict[str, Any],
    news: dict[str, Any] | None = None,
) -> Signal | None:
    """Legacy pivot mean-reversion strategy (kept for fallback / comparison)."""
    news = news or {"net": "neutral", "has_high_bull": False, "has_high_bear": False}
    pivots = snapshot.get("pivots") or {}
    ltp = snapshot.get("ltp")
    rsi_fast = snapshot.get("rsi_fast")
    patterns = set(snapshot.get("patterns") or [])

    if not pivots or ltp is None or rsi_fast is None or rsi_fast != rsi_fast:  # NaN guard
        return None

    levels = list(pivots.values())
    support = _nearest_below(levels, ltp)
    resistance = _nearest_above(levels, ltp)
    threshold = ltp * config.PIVOT_PROXIMITY_PCT
    buffer = ltp * config.STOP_BUFFER_PCT

    near_support = support is not None and (ltp - support) <= threshold
    near_resistance = resistance is not None and (resistance - ltp) <= threshold

    # When LTP is close to both a support and resistance, pick the nearer pivot.
    if near_support and near_resistance:
        if (ltp - support) <= (resistance - ltp):
            near_resistance = False
        else:
            near_support = False

    # RSI gates: don't buy into overbought, don't sell into oversold.
    long_ok = near_support and rsi_fast < config.RSI_OVERBOUGHT
    short_ok = near_resistance and rsi_fast > config.RSI_OVERSOLD

    # News conflict gate — only active in LIVE mode.
    # In PAPER/sandbox we skip this so overbought/oversold setups still fire.
    if config.MODE == "LIVE":
        net_news = news.get("net", "neutral")
        if net_news == "bearish" and news.get("has_high_bear"):
            long_ok = False
        if net_news == "bullish" and news.get("has_high_bull"):
            short_ok = False

    # Ambiguous (both or neither) -> do nothing.
    if long_ok == short_ok:
        return None

    if long_ok:
        direction = "long"
        entry = float(ltp)
        stop = float(support) - buffer
        risk = entry - stop
        target = entry + config.SIGNAL_RR_RATIO * risk
        pattern_ok = bool(patterns & _BULLISH_PATTERNS)
    else:
        direction = "short"
        entry = float(ltp)
        stop = float(resistance) + buffer
        risk = stop - entry
        target = entry - config.SIGNAL_RR_RATIO * risk
        pattern_ok = bool(patterns & _BEARISH_PATTERNS)

    if risk <= 0:
        return None

    confidence = "high" if (pattern_ok and news.get("net") != "neutral") else (
        "medium" if pattern_ok else "low"
    )
    pivot_level = support if long_ok else resistance
    rationale = (
        f"{direction.upper()} near pivot {pivot_level:,.2f} "
        f"(LTP {entry:,.2f}, RSI_fast {rsi_fast:.1f}). "
        f"News: {news.get('net')}. Patterns: {', '.join(sorted(patterns)) or 'none'}. "
        f"Index stop {stop:,.2f}, target {target:,.2f} (RR {config.SIGNAL_RR_RATIO}). "
        f"Confidence {confidence}. Option strike + premium sizing at execution."
    )

    return Signal(
        symbol=snapshot["symbol"],
        direction=direction,
        entry=round(entry, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        qty=config.MIN_LOT_SIZE,
        max_risk=float(config.MAX_RISK_PER_TRADE),
        rationale=rationale,
        status="new",
    )


def news_view_for_symbol(symbol: str) -> dict[str, Any]:
    """Pull recent tagged news from the DB and aggregate it for ``symbol``."""
    from src.storage import db
    keyword = config.SYMBOL_NEWS_KEYWORDS.get(symbol, symbol.split(":")[-1])
    since = (mc.now_ist() - _timedelta_hours(config.NEWS_LOOKBACK_HOURS)).isoformat()
    rows = [dict(r) for r in db.recent_news_for_keyword(keyword, since)]
    return aggregate_news_sentiment(rows)


def _timedelta_hours(hours: int):
    from datetime import timedelta
    return timedelta(hours=hours)

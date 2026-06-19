"""8-section market analysis generation (LLM / Sonnet).

Produces the exact 8 sections required by the spec for a watchlist symbol:
  Chart Overview, Candlestick Patterns, Technical Indicators,
  Support/Resistance, Prediction, Reasoning, Risk Warning, Confidence Level.

Prompt construction is pure (offline-testable); the LLM call is separate.
"""
from __future__ import annotations

from typing import Any

import config
from src.utils.logging import get_logger

log = get_logger("analysis")

SECTIONS = [
    "Chart Overview",
    "Candlestick Patterns",
    "Technical Indicators",
    "Support/Resistance",
    "Prediction",
    "Reasoning",
    "Risk Warning",
    "Confidence Level",
]

SYSTEM_PROMPT_EN = (
    "You are an intraday trading research assistant for NSE India. You analyse the "
    "underlying INDEX (NIFTY 50, SENSEX, Bank NIFTY); trades are small, defined-risk "
    "weekly options / spreads.\n\n"
    "Produce analysis in EXACTLY these 8 sections, each as a markdown bold heading "
    "followed by 1-3 concise sentences:\n"
    "**Chart Overview**, **Candlestick Patterns**, **Technical Indicators**, "
    "**Support/Resistance**, **Prediction**, **Reasoning**, **Risk Warning**, "
    "**Confidence Level**.\n\n"
    "RISK RULES (never override): max risk per trade Rs "
    f"{config.MAX_RISK_PER_TRADE}; max {config.MAX_TRADES_PER_DAY} trades/day; "
    f"max daily loss Rs {config.MAX_DAILY_LOSS}. Only low-risk, small-size setups; "
    "when unsure, do nothing. Never suggest a trade without a stop-loss. Confidence "
    "Level must be one of low/medium/high. Be concise and factual. If data is missing "
    "or stale, say so and do not guess. This is research, not financial advice."
)

# Simple, beginner-friendly Tamil. Numbers and instrument names stay in English.
SYSTEM_PROMPT_TA = (
    "நீங்கள் NSE இந்தியா குறியீடுகளுக்கான (NIFTY 50, SENSEX, Bank NIFTY) ஒரு எளிய "
    "பங்குச் சந்தை உதவியாளர். பங்குச் சந்தை தெரியாத ஒருவருக்கும் புரியும்படி, மிக எளிய "
    "பேச்சுத் தமிழில் எழுதுங்கள். கடினமான தொழில்நுட்ப வார்த்தைகளை சாதாரண வார்த்தைகளில் "
    "விளக்குங்கள்.\n\n"
    "சரியாக கீழ்க்கண்ட 8 தலைப்புகளில், ஒவ்வொன்றையும் **தடிமன்** எழுத்தில் எழுதி, "
    "அதன் கீழ் 1-2 சிறிய வாக்கியங்கள் தாருங்கள்:\n"
    "**சார்ட் கண்ணோட்டம்**, **மெழுகுவர்த்தி வடிவங்கள்**, **தொழில்நுட்ப குறிகாட்டிகள்**, "
    "**ஆதரவு மற்றும் எதிர்ப்பு**, **கணிப்பு**, **காரணம்**, **ஆபத்து எச்சரிக்கை**, "
    "**நம்பிக்கை நிலை**.\n\n"
    "எண்கள், விலைகள், NIFTY/SENSEX போன்ற பெயர்களை ஆங்கிலத்திலேயே/எண்களிலேயே வைக்கவும். "
    f"இடர் விதிகள்: ஒரு வர்த்தகத்தில் அதிகபட்ச இடர் ₹{config.MAX_RISK_PER_TRADE}; "
    f"நாளொன்றுக்கு அதிகபட்சம் {config.MAX_TRADES_PER_DAY} வர்த்தகங்கள். குறைந்த இடர் உள்ள "
    "வாய்ப்புகள் மட்டுமே; சந்தேகம் இருந்தால் எதுவும் செய்யாதீர்கள். ஸ்டாப்-லாஸ் இல்லாமல் "
    "வர்த்தகம் பரிந்துரைக்காதீர்கள். 'நம்பிக்கை நிலை' குறைவு/நடுத்தரம்/அதிகம் என்று இருக்கட்டும். "
    "சுருக்கமாகவும் உண்மையாகவும் இருங்கள். இது ஆலோசனை அல்ல, தகவல் மட்டுமே."
)

# Kept (English) as the canonical section list for tests/reference.
SYSTEM_PROMPT = SYSTEM_PROMPT_EN


def _system_prompt() -> str:
    return SYSTEM_PROMPT_TA if config.ALERT_LANGUAGE.lower() in ("tamil", "ta") else SYSTEM_PROMPT_EN


def build_analysis_prompt(
    snapshot: dict[str, Any],
    news_view: dict[str, Any] | None = None,
    signal: dict[str, Any] | None = None,
) -> str:
    """Construct the per-symbol user prompt from technicals + news (+ optional signal)."""
    news_view = news_view or {"net": "neutral"}
    piv = snapshot.get("pivots") or {}
    piv_str = ", ".join(f"{k}={v:,.2f}" for k, v in piv.items()) or "n/a"
    patterns = ", ".join(snapshot.get("patterns") or []) or "none"
    lines = [
        f"Symbol: {snapshot.get('symbol')}",
        f"LTP: {snapshot.get('ltp')}",
        f"Trend: {snapshot.get('trend')}",
        f"Pivots: {piv_str}",
        f"RSI fast({config.RSI_FAST_PERIOD}): {snapshot.get('rsi_fast')} "
        f"[{snapshot.get('rsi_fast_state')}]",
        f"RSI slow({config.RSI_SLOW_PERIOD}): {snapshot.get('rsi_slow')} "
        f"[{snapshot.get('rsi_slow_state')}]",
        f"Candlestick patterns: {patterns}",
        f"News sentiment (recent): {news_view.get('net')} "
        f"(bull={news_view.get('bull', 0)}, bear={news_view.get('bear', 0)})",
    ]
    if signal:
        lines.append(
            f"Signal engine: {signal.get('direction')} entry {signal.get('entry')} "
            f"stop {signal.get('stop')} target {signal.get('target')}"
        )
    else:
        lines.append("Signal engine: no low-risk setup currently.")
    lines.append("\nProduce the 8-section analysis now.")
    return "\n".join(lines)


def generate_analysis(
    snapshot: dict[str, Any],
    news_view: dict[str, Any] | None = None,
    signal: dict[str, Any] | None = None,
    llm: Any | None = None,
) -> str:
    """Generate the 8-section analysis text via the smart model (Sonnet)."""
    if llm is None:
        from src.llm.client import LLMClient  # lazy import
        llm = LLMClient()
    return llm.complete_text(
        system=_system_prompt(),
        user=build_analysis_prompt(snapshot, news_view, signal),
        model=config.LLM_SMART_MODEL,
        max_tokens=900,
    )

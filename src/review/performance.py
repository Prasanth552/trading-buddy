"""Performance stats + AI 'learn from mistakes' review.

The honest version of learning: we journal every closed trade with the
conditions it was taken under, compute win/loss stats and breakdowns, and ask
the LLM to spot what separated winners from losers and suggest conservative
rule tweaks — for the human to approve. No black-box auto-tuning on tiny data.

compute_stats() is pure and offline-testable; generate_review() calls the LLM.
"""
from __future__ import annotations

import json
from typing import Any

import config
from src.utils.logging import get_logger

log = get_logger("review")


def _ctx(row: dict[str, Any]) -> dict[str, Any]:
    c = row.get("context")
    if isinstance(c, str):
        try:
            return json.loads(c)
        except (ValueError, TypeError):
            return {}
    return c or {}


def compute_stats(rows: list[Any]) -> dict[str, Any]:
    """Compute win/loss stats + condition breakdowns from closed-trade rows."""
    trades = []
    for r in rows:
        d = dict(r)
        d["_ctx"] = _ctx(d)
        d["_win"] = (d.get("pnl") or 0) > 0
        trades.append(d)

    n = len(trades)
    wins = [t for t in trades if t["_win"]]
    losses = [t for t in trades if not t["_win"]]
    total = sum(t.get("pnl") or 0 for t in trades)

    def _bucket(keyfn) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for t in trades:
            b = out.setdefault(str(keyfn(t)), {"wins": 0, "losses": 0, "pnl": 0.0})
            b["wins"] += int(t["_win"])
            b["losses"] += int(not t["_win"])
            b["pnl"] = round(b["pnl"] + (t.get("pnl") or 0), 2)
        return out

    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 3) if n else 0.0,
        "total_pnl": round(total, 2),
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0.0,
        "expectancy": round(total / n, 2) if n else 0.0,
        "by_news": _bucket(lambda t: t["_ctx"].get("news_net", "?")),
        "by_rsi_fast": _bucket(lambda t: t["_ctx"].get("rsi_fast_state", "?")),
        "by_pattern": _bucket(lambda t: "pattern" if t["_ctx"].get("patterns") else "no_pattern"),
        "by_symbol": _bucket(lambda t: t.get("symbol", "?")),
    }


def format_stats_text(s: dict[str, Any]) -> str:
    """Compact English summary of the stats (used in the LLM prompt + dashboard)."""
    if s["n"] == 0:
        return "No closed trades yet."
    lines = [
        f"Closed trades: {s['n']} | Wins: {s['wins']} | Losses: {s['losses']} "
        f"| Win rate: {s['win_rate']*100:.0f}%",
        f"Total P&L: ₹{s['total_pnl']} | Avg win: ₹{s['avg_win']} "
        f"| Avg loss: ₹{s['avg_loss']} | Expectancy/trade: ₹{s['expectancy']}",
    ]
    for label, key in [("By news", "by_news"), ("By RSI(fast)", "by_rsi_fast"),
                       ("By pattern", "by_pattern"), ("By symbol", "by_symbol")]:
        parts = [f"{k}: {v['wins']}W/{v['losses']}L (₹{v['pnl']})" for k, v in s[key].items()]
        lines.append(f"{label}: " + "; ".join(parts))
    return "\n".join(lines)


def generate_review(rows: list[Any], llm: Any | None = None) -> str:
    """LLM review: what separated winners from losers + suggested rule tweaks."""
    stats = compute_stats(rows)
    if stats["n"] < 1:
        return ("இதுவரை முடிந்த வர்த்தகங்கள் இல்லை — மதிப்பாய்வுக்கு தரவு இல்லை."
                if config.ALERT_LANGUAGE.lower() in ("tamil", "ta")
                else "No closed trades yet — nothing to review.")

    if llm is None:
        from src.llm.client import LLMClient
        llm = LLMClient()

    tamil = config.ALERT_LANGUAGE.lower() in ("tamil", "ta")
    system = (
        "You are a conservative trading-strategy reviewer for an NSE index-option "
        "paper-trading bot. You are given closed trades with the conditions they were "
        "taken under and their outcomes. Identify what separated winners from losers "
        "and suggest 2-3 SPECIFIC, conservative rule changes (e.g. RSI thresholds, "
        "the news filter, pivot proximity, avoiding certain setups). Be explicit that "
        "the sample is small and these are tentative. Do not invent data. "
        + ("Respond in simple Tamil; keep technical terms like RSI/NIFTY/SENSEX in "
           "English. Keep it short (under ~180 words)." if tamil else
           "Respond in concise English (under ~180 words).")
    )
    user = (
        "Performance summary:\n" + format_stats_text(stats) +
        "\n\nWrite: (1) a one-line headline, (2) what's working, (3) what's losing money, "
        "(4) 2-3 concrete rule tweaks to try next. Frame tweaks as suggestions to approve."
    )
    try:
        return llm.complete_text(system=system, user=user,
                                 model=config.LLM_SMART_MODEL, max_tokens=600)
    except Exception as exc:  # noqa: BLE001
        log.error("Review generation failed: %s", exc)
        return f"Review unavailable: {exc}"

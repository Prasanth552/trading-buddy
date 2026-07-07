"""User-facing Telegram alert text — language-aware (config.ALERT_LANGUAGE).

Tamil-first for a non-technical reader; instrument names (NIFTY/SENSEX) and all
numbers stay as-is. Console/logs are NOT routed through here — they stay English.
"""
from __future__ import annotations

from typing import Any

import config


def _ta() -> bool:
    return config.ALERT_LANGUAGE.lower() in ("tamil", "ta")


def started(mode: str) -> str:
    if _ta():
        return (f"🤖 *டிரேடிங் பட்டி தொடங்கியது* ({mode} முறை)\n"
                "இது பயிற்சி மட்டுமே — உண்மையான பணம் இல்லை. சந்தை திறந்திருக்கும் "
                "நேரத்தில் தானாகவே கவனித்து, முக்கியத் தகவல்களை இங்கே அனுப்பும்.")
    return f"🤖 Trading Buddy started (MODE={mode})."


def signal_alert(sig: dict[str, Any]) -> str:
    long = sig.get("direction") == "long"
    if _ta():
        head = ("🟢 வாங்கும் வாய்ப்பு (விலை மேலே போகலாம்)" if long
                else "🔴 விற்கும் வாய்ப்பு (விலை கீழே போகலாம்)")
        return (f"*வர்த்தக சமிக்ஞை* — {head}\n"
                f"*{sig.get('symbol')}*\n"
                f"நுழைவு விலை: `{sig.get('entry')}`\n"
                f"ஸ்டாப் (நஷ்டம் தடுக்க): `{sig.get('stop')}`\n"
                f"இலக்கு (லாப எதிர்பார்ப்பு): `{sig.get('target')}`\n"
                f"அளவு: {sig.get('qty')} யூனிட் · அதிகபட்ச இடர்: ₹{sig.get('max_risk')}")
    arrow = "🟢 LONG" if long else "🔴 SHORT"
    return (f"*TRADE ALERT* {arrow}\n*{sig.get('symbol')}*\n"
            f"Entry: `{sig.get('entry')}`\nStop:  `{sig.get('stop')}`\n"
            f"Target: `{sig.get('target')}`\n"
            f"Qty: {sig.get('qty')} lot(s) | Max risk: ₹{sig.get('max_risk')}")


def order_placed(symbol: str, direction: str, qty: int, entry: float,
                 stop: float, risk: float, mode: str) -> str:
    emoji = "🟢" if direction == "long" else "🔴"
    manual = not getattr(config, "STOP_LOSS_ENABLED", True)
    if _ta():
        stop_line = ("⚠️ ஸ்டாப் இல்லை — நீங்களே வெளியேற வேண்டும் (dashboard Close)"
                     if manual else
                     f"ஸ்டாப் (நஷ்டம் தடுக்க): {stop} · இடர்: ₹{risk:,.0f}")
        return (f"{emoji} *{mode} ஆர்டர்* {symbol}\n"
                f"{qty} யூனிட் ~{entry} விலையில் வாங்கப்பட்டது.\n"
                f"{stop_line}\n"
                "(பயிற்சி வர்த்தகம் — உண்மையான பணம் இல்லை)")
    stop_line = ("⚠️ NO auto stop — exit manually via dashboard Close"
                 if manual else f"stop {stop} (risk ₹{risk:,.0f})")
    return (f"{emoji} *{mode} ORDER* {symbol}\n"
            f"BUY {qty} @ ~{entry}, {stop_line}")


def exit_msg(symbol: str, reason: str, exit_price: float, pnl: float) -> str:
    # A win is a target hit, or any other exit (trail/square-off/manual) in profit.
    profit = reason in ("target", "profit") or (
        reason in ("manual", "trail", "squareoff", "flip") and pnl >= 0)
    if _ta():
        if profit:
            head = ("🎯 *லாபத்தில் வெளியேறியது*" if reason != "manual"
                    else "✅ *நீங்கள் கைமுறையாக வெளியேறினீர்கள் (லாபம்)*")
            return (f"{head} {symbol}\n"
                    f"விலை {exit_price} ஐ அடைந்தது. லாபம்: ₹{pnl:,.2f} 🎉")
        head = {"manual": "↩️ *நீங்கள் கைமுறையாக வெளியேறினீர்கள்*",
                "zero": "🫥 *பிரீமியம் பூஜ்ஜியமானது — மூடப்பட்டது*"}.get(
                    reason, "🛑 *ஸ்டாப்பில் வெளியேறியது*")
        return (f"{head} {symbol}\n"
                f"விலை {exit_price} ஐ அடைந்தது. நஷ்டம்: ₹{pnl:,.2f}\n"
                "(திட்டமிட்ட சிறிய நஷ்டம் — பெரிய நஷ்டத்தைத் தடுக்க)")
    emoji = "🎯" if profit else "🛑"
    label = {"target": "TARGET", "profit": "PROFIT ₹", "stop": "STOP",
             "manual": "MANUAL", "trail": "TRAIL", "squareoff": "EOD",
             "flip": "FLIP", "zero": "ZERO"}.get(reason, reason.upper())
    return f"{emoji} *EXIT ({label})* {symbol} @ {exit_price} | P&L ₹{pnl:,.2f}"


def eod_summary(date: str, mode: str, sig_count: int, trades_count: int,
                max_trades: int, pnl: float, tripped: bool,
                orders: list[str] | None = None) -> str:
    if _ta():
        ks = "செயல்பட்டது" if tripped else "இயல்பு நிலையில்"
        lines = [
            f"🔔 *இன்றைய சுருக்கம் — {date}*",
            f"முறை: `{mode}`",
            f"கண்டறிந்த வாய்ப்புகள்: {sig_count}",
            f"வர்த்தகங்கள்: {trades_count}/{max_trades}",
            f"இன்றைய லாபம்/நஷ்டம்: ₹{pnl:.2f}",
            f"பாதுகாப்பு சுவிட்ச்: {ks}",
        ]
        if orders:
            lines.append("வர்த்தக விவரங்கள் (நுழைவு → வெளியேற்றம் | லாபம்/நஷ்டம்):")
            lines += orders
        return "\n".join(lines)
    ks = "TRIPPED" if tripped else "armed"
    lines = [
        f"🔔 *EOD Summary — {date}*", f"Mode: `{mode}`",
        f"Signals fired: {sig_count}", f"Trades: {trades_count}/{max_trades}",
        f"Realised P&L: ₹{pnl:.2f}", f"Kill switch: {ks}",
    ]
    if orders:
        lines.append("Trades (entry → exit | P&L):")
        lines += orders
    return "\n".join(lines)


def kill_switch() -> str:
    if _ta():
        return ("🛑 *பாதுகாப்பு சுவிட்ச் செயல்பட்டது* — இன்றைய நஷ்ட வரம்பை எட்டியது. "
                "பணத்தைப் பாதுகாக்க இன்றைக்கு வர்த்தகம் நிறுத்தப்பட்டது.")
    return "🛑 *KILL SWITCH TRIPPED* — daily loss limit reached. Trading halted for today."

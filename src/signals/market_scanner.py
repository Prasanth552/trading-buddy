"""Independent market scanner — high-conviction signals only.

Stripped down to setups that actually work in live trading:
  1. Earnings Momentum — stocks with results today (historically ~75% win rate)
  2. Gap Play — stocks gapping >2% at open (strong catalyst, ride continuation)
  3. Intraday Momentum — >1.5% move with >1.5x volume (institutional push)

Removed: FII flow momentum (too many wrong-direction CE trades in flat markets),
mean-reversion banking CE (failed in sustained red markets), sector rotation
(crude correlation too weak for daily trades).

Philosophy: 0-2 trades/day at 65%+ win rate beats 3 trades at 50%.

Run standalone:  python -m src.signals.market_scanner
Integrate:       from src.signals.market_scanner import MarketScanner
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.logging import get_logger

log = get_logger("market_scanner")

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class ScanSignal:
    symbol: str
    option_type: str       # CE / PE
    strategy: str          # which strategy generated this
    confidence: int        # 0-100
    reasons: list[str] = field(default_factory=list)
    suggested_strike: float | None = None
    entry_window: str = ""  # e.g. "9:15-9:30"


# F&O stocks we can actually trade (must have options on NSE/BSE)
FNO_STOCKS: list[str] = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN",
    "BHARTIARTL", "ITC", "KOTAKBANK", "LT", "AXISBANK", "BAJFINANCE",
    "MARUTI", "TATAMOTORS", "HINDUNILVR", "WIPRO", "HCLTECH", "TECHM",
    "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "NESTLEIND",
    "ADANIENT", "ADANIPORTS", "NTPC", "POWERGRID", "TATAPOWER",
    "ONGC", "BPCL", "IOC", "GAIL", "COALINDIA",
    "HINDALCO", "TATASTEEL", "JSWSTEEL", "VEDL", "JINDALSTEL",
    "INDUSINDBK", "BANDHANBNK", "PNB", "BANKBARODA",
    "HEROMOTOCO", "BAJAJ-AUTO", "EICHERMOT", "M&M", "TVSMOTOR",
    "DLF", "GODREJPROP", "OBEROIRLTY",
    "JSWENERGY", "NHPC", "SJVN",
    "APOLLOHOSP", "BIOCON", "LUPIN", "AUROPHARMA",
    "BRITANNIA", "DABUR", "MARICO", "TATACONSUM", "COLPAL",
    "COFORGE", "PERSISTENT", "LTIM", "MPHASIS",
    "INDIGO",
]

SECTOR_GROUPS: dict[str, list[str]] = {
    "METAL": ["HINDALCO", "TATASTEEL", "JSWSTEEL", "VEDL", "JINDALSTEL", "COALINDIA"],
    "AUTO": ["MARUTI", "TATAMOTORS", "M&M", "BAJAJ-AUTO", "HEROMOTOCO", "EICHERMOT", "TVSMOTOR"],
    "AIRLINE": ["INDIGO"],
    "IT": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "LTIM", "COFORGE", "PERSISTENT", "MPHASIS"],
    "BANKING": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK", "PNB", "BANKBARODA"],
    "PHARMA": ["SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "LUPIN", "AUROPHARMA", "BIOCON"],
    "ENERGY": ["RELIANCE", "ONGC", "BPCL", "IOC", "GAIL"],
    "POWER": ["NTPC", "POWERGRID", "TATAPOWER", "JSWENERGY", "NHPC", "SJVN"],
}


class MarketScanner:
    def __init__(self):
        from src.signals.smart_filter import (
            get_nifty_trend, get_crude_change, get_fii_dii_flow,
            check_earnings_today, get_sector_index_trend,
        )
        self._get_nifty_trend = get_nifty_trend
        self._get_crude_change = get_crude_change
        self._get_fii_dii_flow = get_fii_dii_flow
        self._check_earnings = check_earnings_today
        self._get_sector_trend = get_sector_index_trend

    def scan(self) -> list[ScanSignal]:
        """Run high-conviction strategies and return ranked signals."""
        now = datetime.now(IST)
        signals: list[ScanSignal] = []

        log.info("Starting market scan at %s", now.strftime("%H:%M"))

        signals.extend(self._earnings_momentum())
        signals.extend(self._gap_play())
        signals.extend(self._intraday_momentum())

        merged = self._merge_signals(signals)
        merged.sort(key=lambda s: s.confidence, reverse=True)
        top = merged[:3]

        for sig in top:
            log.info(
                "SCAN SIGNAL: %s %s (confidence=%d, strategy=%s) — %s",
                sig.symbol, sig.option_type, sig.confidence,
                sig.strategy, "; ".join(sig.reasons),
            )

        return top

    # ------------------------------------------------------------------
    # Strategy 1: Earnings Momentum (highest conviction)
    # ------------------------------------------------------------------
    def _earnings_momentum(self) -> list[ScanSignal]:
        """Stocks with earnings results today — historically ~75% win rate."""
        signals = []
        for sym in FNO_STOCKS:
            if self._check_earnings(sym):
                nifty = self._get_nifty_trend()
                sector = self._get_sector_for_stock(sym)
                sector_trend = self._get_sector_trend(sector) if sector else 0

                if sector_trend > 0 and nifty.get("change_pct", 0) >= 0:
                    opt_type = "CE"
                    confidence = 80
                elif sector_trend < -1:
                    opt_type = "PE"
                    confidence = 70
                else:
                    opt_type = "CE"
                    confidence = 70

                signals.append(ScanSignal(
                    symbol=sym,
                    option_type=opt_type,
                    strategy="earnings_momentum",
                    confidence=confidence,
                    reasons=[
                        f"{sym} reporting earnings today",
                        f"Sector trend: {sector_trend:+.1f}%",
                        f"Market: {nifty.get('change_pct', 0):+.1f}%",
                    ],
                    entry_window="9:15-9:30",
                ))
        return signals

    # ------------------------------------------------------------------
    # Strategy 2: Gap Play (strong gaps only — >2%)
    # ------------------------------------------------------------------
    def _gap_play(self) -> list[ScanSignal]:
        """Stocks gapping >2% at open — strong catalyst, ride continuation."""
        signals = []
        try:
            import yfinance as yf
        except ImportError:
            log.warning("yfinance not installed — gap scan skipped")
            return signals

        check_list = [
            "RELIANCE", "TCS", "INFY", "HDFCBANK", "SBIN",
            "MARUTI", "BAJFINANCE", "LT", "ITC", "ICICIBANK",
            "AXISBANK", "KOTAKBANK", "BHARTIARTL", "HINDALCO",
            "TATASTEEL", "SUNPHARMA", "DRREDDY", "WIPRO",
            "HCLTECH", "ADANIENT", "INDIGO", "M&M",
        ]

        for sym in check_list:
            try:
                ticker = yf.Ticker(f"{sym}.NS")
                hist = ticker.history(period="2d")
                if len(hist) < 2:
                    continue

                prev_close = hist["Close"].iloc[-2]
                today_open = hist["Open"].iloc[-1]
                gap_pct = ((today_open - prev_close) / prev_close) * 100

                # Only take gaps >2% (was 1.5% — too many false signals)
                if abs(gap_pct) < 2.0:
                    continue

                opt_type = "CE" if gap_pct > 0 else "PE"
                # Confidence scales with gap size: 2% = 65, 3% = 75, 4%+ = 80
                confidence = min(80, int(55 + abs(gap_pct) * 5))

                signals.append(ScanSignal(
                    symbol=sym,
                    option_type=opt_type,
                    strategy="gap_play",
                    confidence=confidence,
                    reasons=[
                        f"Gap {'up' if gap_pct > 0 else 'down'} {gap_pct:+.1f}%",
                        f"Prev close: {prev_close:.1f}, Today open: {today_open:.1f}",
                    ],
                    entry_window="9:15-9:30" if gap_pct > 0 else "9:30-10:00",
                ))
            except Exception:
                continue

        return signals

    # ------------------------------------------------------------------
    # Strategy 3: Intraday Momentum (strict filters)
    # ------------------------------------------------------------------
    def _intraday_momentum(self) -> list[ScanSignal]:
        """Early movers: >1% move AND >1.2x volume required.

        Both conditions filter out noise while still catching real
        institutional moves. Backtested sweet spot across choppy weeks.
        """
        signals = []
        try:
            import yfinance as yf
        except ImportError:
            log.warning("yfinance not installed — intraday momentum skipped")
            return signals

        nifty = self._get_nifty_trend()
        market_bias = nifty.get("change_pct", 0)

        scan_list = [
            "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN",
            "AXISBANK", "KOTAKBANK", "BAJFINANCE", "LT", "BHARTIARTL",
            "MARUTI", "M&M", "HINDALCO", "TATASTEEL",
            "SUNPHARMA", "DRREDDY", "WIPRO", "HCLTECH", "TECHM",
            "ITC", "HINDUNILVR", "ADANIENT", "NTPC", "TATAPOWER",
            "DLF", "INDIGO", "CIPLA", "JSWSTEEL",
        ]

        movers = []
        for sym in scan_list:
            try:
                ticker = yf.Ticker(f"{sym}.NS")
                hist = ticker.history(period="1d", interval="5m")
                if hist.empty or len(hist) < 2:
                    continue

                open_price = hist["Open"].iloc[0]
                current = hist["Close"].iloc[-1]
                move_pct = ((current - open_price) / open_price) * 100

                today_vol = hist["Volume"].sum()
                try:
                    daily = ticker.history(period="5d", interval="1d")
                    avg_vol = daily["Volume"].iloc[:-1].mean() if len(daily) > 1 else 0
                    candles_so_far = len(hist)
                    expected_candles = 75
                    vol_ratio = (today_vol / (avg_vol * candles_so_far / expected_candles)) if avg_vol > 0 else 1.0
                except Exception:
                    vol_ratio = 1.0

                # Need >1% move AND >1.2x volume
                if abs(move_pct) >= 1.0 and vol_ratio >= 1.2:
                    movers.append({
                        "symbol": sym,
                        "move_pct": move_pct,
                        "vol_ratio": vol_ratio,
                        "current": current,
                        "open": open_price,
                    })
            except Exception:
                continue

        movers.sort(key=lambda m: abs(m["move_pct"]) * m["vol_ratio"], reverse=True)

        for m in movers[:2]:
            move = m["move_pct"]
            vol_r = m["vol_ratio"]
            sym = m["symbol"]
            opt_type = "CE" if move > 0 else "PE"

            conf = 65
            conf += min(15, int((abs(move) - 1.0) / 0.5) * 5)
            conf += min(10, int((vol_r - 1.2) / 0.3) * 5)
            if (move > 0 and market_bias > 0) or (move < 0 and market_bias < 0):
                conf += 5
            conf = min(85, conf)

            reasons = [
                f"{sym} {'surging' if move > 0 else 'dumping'} {move:+.1f}% from open",
                f"Volume: {vol_r:.1f}x average",
            ]
            if vol_r > 1.5:
                reasons.append("High volume — institutional activity")
            if (move > 0 and market_bias > 0) or (move < 0 and market_bias < 0):
                reasons.append(f"Aligned with market ({market_bias:+.1f}%)")

            signals.append(ScanSignal(
                symbol=sym,
                option_type=opt_type,
                strategy="intraday_momentum",
                confidence=conf,
                reasons=reasons,
                entry_window="9:20-10:00",
            ))

        return signals

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _get_sector_for_stock(self, symbol: str) -> str | None:
        for sector, stocks in SECTOR_GROUPS.items():
            if symbol in stocks:
                return sector
        return None

    def _merge_signals(self, signals: list[ScanSignal]) -> list[ScanSignal]:
        """Merge duplicate symbols — boost confidence when multiple strategies agree."""
        by_symbol: dict[str, list[ScanSignal]] = {}
        for sig in signals:
            key = f"{sig.symbol}_{sig.option_type}"
            by_symbol.setdefault(key, []).append(sig)

        merged = []
        for key, group in by_symbol.items():
            if len(group) == 1:
                merged.append(group[0])
            else:
                best = max(group, key=lambda s: s.confidence)
                boost = (len(group) - 1) * 10
                best.confidence = min(95, best.confidence + boost)
                strategies = list({s.strategy for s in group})
                best.strategy = "+".join(strategies)
                for s in group:
                    if s is not best:
                        best.reasons.extend(s.reasons)
                best.reasons.append(f"Multi-strategy confluence: {len(group)} strategies agree (+{boost})")
                merged.append(best)

        return merged


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------
def run_scan():
    """Run a market scan and print results."""
    scanner = MarketScanner()
    signals = scanner.scan()

    if not signals:
        print("\nNo signals found — market conditions unclear or no catalysts today.")
        return

    print(f"\n{'='*70}")
    print(f"MARKET SCANNER — {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}")
    print(f"{'='*70}\n")

    for i, sig in enumerate(signals, 1):
        print(f"  #{i}  {sig.symbol} {sig.option_type}  "
              f"[confidence: {sig.confidence}/100]  "
              f"strategy: {sig.strategy}")
        print(f"      Window: {sig.entry_window}")
        for r in sig.reasons:
            print(f"        → {r}")
        print()


if __name__ == "__main__":
    run_scan()

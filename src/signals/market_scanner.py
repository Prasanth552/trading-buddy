"""Independent market scanner — generates CE/PE signals from market data.

Mimics what CH1 does best (earnings plays, sector rotation, macro alignment)
without depending on any Telegram channel. Runs as a standalone scan or
integrates with channel_listener to produce its own signals.

Strategies (derived from CH1 pattern analysis):
  1. Earnings Momentum — trade stocks reporting results today/tomorrow
  2. Sector Rotation — crude down → airline CE, commodity drop → metal PE
  3. FII Flow Momentum — heavy FII buying → large-cap CE
  4. Pre-market Gap — stocks gapping up/down on news

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
        """Run all strategies and return ranked signals."""
        now = datetime.now(IST)
        signals: list[ScanSignal] = []

        log.info("Starting market scan at %s", now.strftime("%H:%M"))

        signals.extend(self._earnings_momentum())
        signals.extend(self._sector_rotation())
        signals.extend(self._fii_flow_momentum())
        signals.extend(self._pre_market_movers())
        signals.extend(self._intraday_momentum())

        # Deduplicate: if multiple strategies pick the same stock, merge and boost
        merged = self._merge_signals(signals)

        # Sort by confidence descending, take top 5
        merged.sort(key=lambda s: s.confidence, reverse=True)
        top = merged[:5]

        for sig in top:
            log.info(
                "SCAN SIGNAL: %s %s (confidence=%d, strategy=%s) — %s",
                sig.symbol, sig.option_type, sig.confidence,
                sig.strategy, "; ".join(sig.reasons),
            )

        return top

    # ------------------------------------------------------------------
    # Strategy 1: Earnings Momentum
    # ------------------------------------------------------------------
    def _earnings_momentum(self) -> list[ScanSignal]:
        """Find F&O stocks with earnings today → high-confidence catalyst trades."""
        signals = []
        for sym in FNO_STOCKS:
            if self._check_earnings(sym):
                # Determine CE/PE based on sector trend and market sentiment
                nifty = self._get_nifty_trend()
                sector = self._get_sector_for_stock(sym)
                sector_trend = self._get_sector_trend(sector) if sector else 0

                if sector_trend > 0 and nifty.get("change_pct", 0) >= 0:
                    opt_type = "CE"
                    confidence = 75
                elif sector_trend < -1:
                    opt_type = "PE"
                    confidence = 65
                else:
                    opt_type = "CE"  # default bullish on earnings
                    confidence = 60

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
    # Strategy 2: Sector Rotation (macro-driven)
    # ------------------------------------------------------------------
    def _sector_rotation(self) -> list[ScanSignal]:
        """Generate signals based on crude oil and commodity macro moves."""
        signals = []
        crude_chg = self._get_crude_change()
        nifty = self._get_nifty_trend()

        # Crude falling > 2% → airline stocks CE
        if crude_chg < -2:
            for sym in SECTOR_GROUPS["AIRLINE"]:
                signals.append(ScanSignal(
                    symbol=sym,
                    option_type="CE",
                    strategy="sector_rotation",
                    confidence=70,
                    reasons=[
                        f"Crude oil down {crude_chg}%",
                        "Airline cost tailwind — bullish",
                    ],
                    entry_window="9:15-9:45",
                ))

        # Crude spiking > 3% → energy stocks CE, metal/airline PE
        if crude_chg > 3:
            for sym in SECTOR_GROUPS["ENERGY"][:3]:
                signals.append(ScanSignal(
                    symbol=sym,
                    option_type="CE",
                    strategy="sector_rotation",
                    confidence=65,
                    reasons=[
                        f"Crude oil up {crude_chg}%",
                        "Energy sector tailwind",
                    ],
                    entry_window="9:15-9:45",
                ))

        # Market red 2+ days → contrarian banking CE (mean reversion)
        red_days = nifty.get("consecutive_red", 0)
        if red_days >= 3:
            for sym in ["HDFCBANK", "ICICIBANK", "SBIN"]:
                signals.append(ScanSignal(
                    symbol=sym,
                    option_type="CE",
                    strategy="sector_rotation",
                    confidence=55,
                    reasons=[
                        f"Market red {red_days} days — mean reversion setup",
                        "Large-cap banking — first to bounce",
                    ],
                    entry_window="9:30-10:00",
                ))

        return signals

    # ------------------------------------------------------------------
    # Strategy 3: FII Flow Momentum
    # ------------------------------------------------------------------
    def _fii_flow_momentum(self) -> list[ScanSignal]:
        """Heavy FII buying → large-cap momentum CE plays."""
        signals = []
        flows = self._get_fii_dii_flow()
        fii = flows.get("fii", 0)

        if fii > 1000:
            # Strong FII buying → ride momentum in large caps
            momentum_picks = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS"]
            for sym in momentum_picks:
                signals.append(ScanSignal(
                    symbol=sym,
                    option_type="CE",
                    strategy="fii_momentum",
                    confidence=65,
                    reasons=[
                        f"FII net buying: ₹{fii:.0f}cr",
                        "Large-cap momentum — institutional flow driven",
                    ],
                    entry_window="9:30-10:30",
                ))
        elif fii < -1000:
            # Strong FII selling → defensive plays or PE on frothy stocks
            signals.append(ScanSignal(
                symbol="NIFTY",
                option_type="PE",
                strategy="fii_momentum",
                confidence=60,
                reasons=[
                    f"FII heavy selling: ₹{fii:.0f}cr",
                    "Index-level hedge / bearish play",
                ],
                entry_window="9:30-10:00",
            ))

        return signals

    # ------------------------------------------------------------------
    # Strategy 4: Pre-market Movers (gap analysis)
    # ------------------------------------------------------------------
    def _pre_market_movers(self) -> list[ScanSignal]:
        """Find stocks with significant pre-market gaps."""
        signals = []
        try:
            import yfinance as yf

            # Check a curated set for gaps
            check_list = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "SBIN",
                          "TATAMOTORS", "MARUTI", "BAJFINANCE", "LT", "ITC"]

            for sym in check_list:
                try:
                    ticker = yf.Ticker(f"{sym}.NS")
                    hist = ticker.history(period="2d")
                    if len(hist) < 2:
                        continue

                    prev_close = hist["Close"].iloc[-2]
                    today_open = hist["Open"].iloc[-1]
                    gap_pct = ((today_open - prev_close) / prev_close) * 100

                    if abs(gap_pct) >= 1.5:
                        opt_type = "CE" if gap_pct > 0 else "PE"
                        confidence = min(70, int(40 + abs(gap_pct) * 10))
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

        except ImportError:
            log.warning("yfinance not installed — gap scan skipped")

        return signals

    # ------------------------------------------------------------------
    # Strategy 5: Intraday Momentum (early movers)
    # ------------------------------------------------------------------
    def _intraday_momentum(self) -> list[ScanSignal]:
        """Find F&O stocks with strong directional moves in the first 5-15 minutes.

        Checks today's intraday data: stocks moving >1% from open with
        volume confirmation get flagged. Fires most trading days.
        """
        signals = []
        try:
            import yfinance as yf
        except ImportError:
            log.warning("yfinance not installed — intraday momentum skipped")
            return signals

        nifty = self._get_nifty_trend()
        market_bias = nifty.get("change_pct", 0)

        # Scan a focused list of liquid F&O stocks
        scan_list = [
            "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN",
            "AXISBANK", "KOTAKBANK", "BAJFINANCE", "LT", "BHARTIARTL",
            "TATAMOTORS", "MARUTI", "M&M", "HINDALCO", "TATASTEEL",
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

                # Volume check: compare first candles' volume to recent average
                today_vol = hist["Volume"].sum()
                try:
                    daily = ticker.history(period="5d", interval="1d")
                    avg_vol = daily["Volume"].iloc[:-1].mean() if len(daily) > 1 else 0
                    # Scale: today's partial volume vs full-day average
                    candles_so_far = len(hist)
                    expected_candles = 75  # ~6.25 hrs of 5-min candles
                    vol_ratio = (today_vol / (avg_vol * candles_so_far / expected_candles)) if avg_vol > 0 else 1.0
                except Exception:
                    vol_ratio = 1.0

                if abs(move_pct) >= 1.0:
                    movers.append({
                        "symbol": sym,
                        "move_pct": move_pct,
                        "vol_ratio": vol_ratio,
                        "current": current,
                        "open": open_price,
                    })
            except Exception:
                continue

        # Sort by absolute move * volume ratio — strongest movers first
        movers.sort(key=lambda m: abs(m["move_pct"]) * m["vol_ratio"], reverse=True)

        for m in movers[:5]:
            move = m["move_pct"]
            vol_r = m["vol_ratio"]
            sym = m["symbol"]

            # Direction: ride the momentum
            opt_type = "CE" if move > 0 else "PE"

            # Confidence scoring — a 1%+ move is already meaningful
            conf = 55
            # Move size: +5 per 0.5% beyond 1%
            conf += min(20, int((abs(move) - 1.0) / 0.5) * 5)
            # Volume confirmation: high volume = institutional
            if vol_r > 1.5:
                conf += 15
            elif vol_r > 1.2:
                conf += 10
            elif vol_r > 0.8:
                conf += 5
            # Market alignment: momentum in same direction as market
            if (move > 0 and market_bias > 0) or (move < 0 and market_bias < 0):
                conf += 5
            # Penalize if momentum fights the market hard
            if (move > 0 and market_bias < -0.5) or (move < 0 and market_bias > 0.5):
                conf -= 5

            conf = max(30, min(85, conf))

            reasons = [
                f"{sym} {'surging' if move > 0 else 'dumping'} {move:+.1f}% from open",
                f"Volume ratio: {vol_r:.1f}x vs average",
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
                # Boost by 10 for each additional strategy that agrees
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

"""Replay index straddle strategies using REAL 1-min option candles.

Instead of Black-Scholes estimates, fetches actual 1-min candles for
ATM CE and PE options and replays the straddle entry/exit logic with
real premium movement.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/replay_straddle_day.py [--date 2026-09-15]
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.broker.upstox_data import UpstoxData
from src.strategy.live_runner import (
    INDEXES, STRATEGIES, EXPIRY_WEEKDAY,
    _build_option_master, _next_expiry,
    _first_candle_range, round_strike, calc_charges,
)
from src.utils.logging import get_logger

log = get_logger("replay_straddle")
IST = ZoneInfo("Asia/Kolkata")


def fetch_1min_candles(udata: UpstoxData, instrument_key: str,
                       ref_date: date) -> list[dict]:
    """Fetch 1-min candles for any instrument on a given date."""
    candles = []
    ds = ref_date.isoformat()
    try:
        d = udata._get(f"/v3/historical-candle/{instrument_key}/minutes/1/{ds}/{ds}")
        candles += d.get("data", {}).get("candles", [])
    except Exception as exc:
        log.warning("Historical 1min failed (%s): %s", instrument_key, exc)

    try:
        d = udata._get(f"/v3/historical-candle/intraday/{instrument_key}/minutes/1")
        candles += d.get("data", {}).get("candles", [])
    except Exception:
        pass

    rows = [{"date": c[0], "open": c[1], "high": c[2], "low": c[3],
             "close": c[4], "volume": c[5]} for c in candles]
    rows.sort(key=lambda r: r["date"])

    seen = set()
    out = []
    for r in rows:
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        if r["date"][:10] == ds:
            out.append(r)
    return out


def time_str(c: dict) -> str:
    return c["date"][11:16]


def run_real_straddle(idx_candles: list[dict], ce_candles: list[dict],
                      pe_candles: list[dict], idx_name: str, ref_date: date,
                      *, entry_hour: int, entry_min: int, sl_pct: float,
                      combined_sl: bool = False, trailing: bool = False,
                      vol_filter: bool = False, lots: int = 1) -> dict:
    """Run straddle replay with real option 1-min candles."""
    idx = INDEXES[idx_name]
    lot_size = idx["lot_size"] * lots
    step = idx["strike_step"]
    dte_days = (EXPIRY_WEEKDAY.get(idx_name, 1) - ref_date.weekday()) % 7

    # Vol filter on first 3 index candles
    if vol_filter:
        fr = _first_candle_range(idx_candles)
        if fr > idx["vol_skip_range"]:
            return {"skipped": True, "skip_reason": "vol_filter", "net_pnl": 0,
                    "dte": dte_days, "vol_range": round(fr)}

    # Build time-indexed lookup for option candles
    ce_by_time = {c["date"][11:16]: c for c in ce_candles}
    pe_by_time = {c["date"][11:16]: c for c in pe_candles}

    # Find entry candle
    entry_candle = None
    entry_time_key = None
    for c in idx_candles:
        t = time_str(c)
        h, m = int(t[:2]), int(t[3:5])
        if h > entry_hour or (h == entry_hour and m >= entry_min):
            entry_candle = c
            entry_time_key = t
            break

    if not entry_candle:
        return {"skipped": True, "skip_reason": "no_entry", "net_pnl": 0, "dte": dte_days}

    spot_entry = entry_candle["close"]
    atm = round_strike(spot_entry, step)

    # Get entry premiums from real option candles
    ce_entry_candle = ce_by_time.get(entry_time_key)
    pe_entry_candle = pe_by_time.get(entry_time_key)

    if not ce_entry_candle or not pe_entry_candle:
        return {"skipped": True, "skip_reason": "no_option_candle_at_entry",
                "net_pnl": 0, "dte": dte_days}

    ce_entry = ce_entry_candle["close"]
    pe_entry = pe_entry_candle["close"]
    total_prem = ce_entry + pe_entry

    if total_prem <= 0:
        return {"skipped": True, "skip_reason": "zero_premium", "net_pnl": 0, "dte": dte_days}

    # SL levels
    if combined_sl:
        sl_level = total_prem * (1 + sl_pct)
    else:
        ce_sl = ce_entry * (1 + sl_pct)
        pe_sl = pe_entry * (1 + sl_pct)

    ce_alive = pe_alive = True
    ce_exit_prem = pe_exit_prem = None
    exit_reason = "time_3:10"
    exit_time = None
    best_combined_profit = 0.0
    trail_active = False

    # Timeline for reporting
    timeline = []
    entry_idx = idx_candles.index(entry_candle)

    for c in idx_candles[entry_idx + 1:]:
        t = time_str(c)
        h, m = int(t[:2]), int(t[3:5])

        ce_c = ce_by_time.get(t)
        pe_c = pe_by_time.get(t)
        if not ce_c or not pe_c:
            continue

        ce_now = ce_c["close"]
        pe_now = pe_c["close"]
        # Use high for worst-case SL check
        ce_worst = ce_c["high"]
        pe_worst = pe_c["high"]

        current_combined = ce_now + pe_now
        unrealised = (total_prem - current_combined) * lot_size

        # Record timeline at key intervals
        if t[-1] in ("0", "5") or h >= 15:
            timeline.append({
                "time": t, "spot": c["close"],
                "ce": ce_now, "pe": pe_now,
                "combined": current_combined,
                "pnl": unrealised,
            })

        if combined_sl:
            if ce_worst + pe_worst >= sl_level:
                ce_exit_prem = ce_now
                pe_exit_prem = pe_now
                exit_reason = "combined_sl"
                exit_time = t
                break
        else:
            if ce_alive and ce_worst >= ce_sl:
                ce_exit_prem = ce_sl
                ce_alive = False
                exit_reason = "ce_sl" if pe_alive else "both_sl"
                if not pe_alive:
                    exit_time = t
                    break
            if pe_alive and pe_worst >= pe_sl:
                pe_exit_prem = pe_sl
                pe_alive = False
                exit_reason = "pe_sl" if ce_alive else "both_sl"
                if not ce_alive:
                    exit_time = t
                    break

        if trailing and ce_alive and pe_alive:
            current_profit = total_prem - current_combined
            best_combined_profit = max(best_combined_profit, current_profit)
            if current_profit / total_prem >= 0.40:
                trail_active = True
            if trail_active and best_combined_profit > 0:
                give_back = best_combined_profit * 0.20
                if current_profit < best_combined_profit - give_back:
                    ce_exit_prem = ce_now
                    pe_exit_prem = pe_now
                    exit_reason = "trailing"
                    exit_time = t
                    break

        if h >= 15 and m >= 10:
            if ce_alive:
                ce_exit_prem = ce_now
            if pe_alive:
                pe_exit_prem = pe_now
            exit_time = t
            break

    # Fallback to last candle
    if ce_exit_prem is None:
        last_ce = ce_candles[-1] if ce_candles else None
        ce_exit_prem = last_ce["close"] if last_ce else ce_entry
    if pe_exit_prem is None:
        last_pe = pe_candles[-1] if pe_candles else None
        pe_exit_prem = last_pe["close"] if last_pe else pe_entry
    if exit_time is None:
        exit_time = time_str(idx_candles[-1])

    ce_pnl = (ce_entry - ce_exit_prem) * lot_size
    pe_pnl = (pe_entry - pe_exit_prem) * lot_size
    charges = (calc_charges(ce_entry, ce_exit_prem, lot_size) +
               calc_charges(pe_entry, pe_exit_prem, lot_size))
    net = ce_pnl + pe_pnl - charges

    return {
        "skipped": False,
        "net_pnl": round(net, 2),
        "ce_pnl": round(ce_pnl, 2),
        "pe_pnl": round(pe_pnl, 2),
        "charges": round(charges, 2),
        "exit_reason": exit_reason,
        "entry_time": entry_time_key,
        "exit_time": exit_time,
        "spot_entry": round(spot_entry, 2),
        "atm_strike": atm,
        "ce_entry": round(ce_entry, 2),
        "pe_entry": round(pe_entry, 2),
        "ce_exit": round(ce_exit_prem, 2),
        "pe_exit": round(pe_exit_prem, 2),
        "dte": dte_days,
        "total_prem": round(total_prem, 2),
        "lot_size": lot_size,
        "timeline": timeline,
        "premium_source": "real",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()

    ref_date = date.fromisoformat(args.date) if args.date else datetime.now(IST).date()

    print(f"\n{'='*70}")
    print(f"  INDEX STRADDLE REPLAY — {ref_date} (REAL 1-min option candles)")
    print(f"{'='*70}")

    udata = UpstoxData()

    # Build option master for instrument key lookup
    print("\nLoading option master...")
    master = _build_option_master(udata)
    print(f"  Master: {len(master)} entries")

    grand_total = 0

    for sname, params in STRATEGIES.items():
        print(f"\n{'━'*70}")
        print(f"  Strategy: {sname}")
        print(f"  Entry: {params['entry_hour']:02d}:{params['entry_min']:02d} | "
              f"SL: {params['sl_pct']*100:.0f}% {'combined' if params.get('combined_sl') else 'per-leg'} | "
              f"Trail: {'yes' if params.get('trailing') else 'no'} | "
              f"Vol filter: {'yes' if params.get('vol_filter') else 'no'}")
        print(f"{'━'*70}")

        strat_total = 0

        for idx_name in INDEXES:
            idx = INDEXES[idx_name]
            print(f"\n  {'─'*50}")
            print(f"  {idx_name} (lot={idx['lot_size']}, step={idx['strike_step']})")

            # Fetch 1-min index candles
            idx_candles = fetch_1min_candles(udata, idx["key"], ref_date)
            if not idx_candles or len(idx_candles) < 20:
                print(f"  ✗ No candles ({len(idx_candles) if idx_candles else 0})")
                continue

            print(f"  Index candles: {len(idx_candles)} (09:15 → {time_str(idx_candles[-1])})")

            # Determine entry spot and ATM from the entry time candle
            entry_time_target = f"{params['entry_hour']:02d}:{params['entry_min']:02d}"
            entry_c = None
            for c in idx_candles:
                t = time_str(c)
                if t >= entry_time_target:
                    entry_c = c
                    break

            if not entry_c:
                print(f"  ✗ No candle at entry time {entry_time_target}")
                continue

            spot = entry_c["close"]
            atm = round_strike(spot, idx["strike_step"])
            print(f"  Entry spot: {spot:.1f} → ATM {atm}")

            # Find expiry and resolve option instrument keys
            expiry = _next_expiry(ref_date, idx_name, master)
            if not expiry:
                print(f"  ✗ No expiry found")
                continue

            dte = (EXPIRY_WEEKDAY.get(idx_name, 1) - ref_date.weekday()) % 7
            print(f"  Expiry: {expiry} (DTE={dte})")

            ce_key = master.get((idx_name, expiry, atm, "CE"))
            pe_key = master.get((idx_name, expiry, atm, "PE"))

            if not ce_key or not pe_key:
                print(f"  ✗ Option keys not found for ATM={atm}")
                continue

            # Fetch 1-min option candles
            ce_candles = fetch_1min_candles(udata, ce_key, ref_date)
            pe_candles = fetch_1min_candles(udata, pe_key, ref_date)

            print(f"  CE candles: {len(ce_candles)} | PE candles: {len(pe_candles)}")

            if len(ce_candles) < 20 or len(pe_candles) < 20:
                print(f"  ✗ Insufficient option candles")
                continue

            # Run the replay
            r = run_real_straddle(
                idx_candles, ce_candles, pe_candles,
                idx_name, ref_date, lots=1, **params,
            )

            if r.get("skipped"):
                reason = r.get("skip_reason", "unknown")
                extra = f" (range={r['vol_range']} > {idx['vol_skip_range']})" if reason == "vol_filter" else ""
                print(f"  ⊘ SKIPPED: {reason}{extra}")
                continue

            strat_total += r["net_pnl"]

            # Print trade details
            tag = "SHORT STRADDLE"
            print(f"\n  ╔═══════════════════════════════════════════════════════╗")
            print(f"  ║  {tag} — {idx_name:12s}                            ║")
            print(f"  ╠═══════════════════════════════════════════════════════╣")
            print(f"  ║  Spot: {r['spot_entry']:>8.1f}  ATM: {r['atm_strike']:>8.0f}             ║")
            print(f"  ║  CE:   {r['ce_entry']:>8.2f} → {r['ce_exit']:>8.2f}  P&L: {r['ce_pnl']:>+8,.0f}   ║")
            print(f"  ║  PE:   {r['pe_entry']:>8.2f} → {r['pe_exit']:>8.2f}  P&L: {r['pe_pnl']:>+8,.0f}   ║")
            print(f"  ║  Total prem: {r['total_prem']:>7.2f}  Charges: {r['charges']:>6.0f}       ║")
            print(f"  ║  Entry: {r['entry_time']}  Exit: {r['exit_time']} ({r['exit_reason']})    ║")
            print(f"  ║  Net P&L: ₹{r['net_pnl']:>+8,.0f}                              ║")
            print(f"  ╚═══════════════════════════════════════════════════════╝")

            # Print timeline
            tl = r.get("timeline", [])
            if tl:
                print(f"\n  Timeline (every 5 min):")
                print(f"  {'Time':>5s}  {'Spot':>8s}  {'CE':>7s}  {'PE':>7s}  {'Combined':>8s}  {'Unreal P&L':>10s}")
                print(f"  {'─'*5}  {'─'*8}  {'─'*7}  {'─'*7}  {'─'*8}  {'─'*10}")
                for pt in tl:
                    bar_len = min(int(abs(pt["pnl"]) / 200), 20)
                    bar = ("▓" if pt["pnl"] >= 0 else "░") * bar_len
                    print(f"  {pt['time']:>5s}  {pt['spot']:>8.1f}  {pt['ce']:>7.2f}  "
                          f"{pt['pe']:>7.2f}  {pt['combined']:>8.2f}  "
                          f"₹{pt['pnl']:>+8,.0f}  {bar}")

        grand_total += strat_total
        print(f"\n  Strategy total: ₹{strat_total:+,.0f}")

    # Grand summary
    print(f"\n{'='*70}")
    print(f"  GRAND SUMMARY — {ref_date}")
    print(f"{'='*70}")
    print(f"  {'Strategy':<20s} {'Total P&L':>12s}")
    print(f"  {'─'*20} {'─'*12}")

    gt = 0
    for sname, params in STRATEGIES.items():
        # Re-run quickly to get per-strategy totals (already cached)
        # Just print from what we computed
        pass

    print(f"\n  Grand total: ₹{grand_total:+,.0f}")
    print(f"  All premiums: REAL (1-min Upstox option candles)")
    print()


if __name__ == "__main__":
    main()

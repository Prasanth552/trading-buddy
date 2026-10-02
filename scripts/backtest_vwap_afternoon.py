"""Backtest VWAP Mean Reversion + Afternoon Momentum strategies.

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/backtest_vwap_afternoon.py --month 2026-09
    PYTHONPATH=. .venv/bin/python3 scripts/backtest_vwap_afternoon.py --date 2026-10-01
"""
from __future__ import annotations
import argparse, hashlib, json, math, re, time as _t
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from src.broker.upstox_data import UpstoxData, load_cached_token, _expiry_to_date

IST = ZoneInfo("Asia/Kolkata")
BLOCKLIST = {"GODREJCP", "GRASIM"}
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "NIFTY BANK", "NIFTY 50"}

SLIPPAGE_PCT = 0.005
MIN_PREMIUM = 0.50
SL_PCT = 0.30
MAX_SL_RS = 5000
FLOOR_STEPS = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000]
CAPITAL = 150000
BROKERAGE_PER_ORDER = 20
STT_PCT = 0.000625
LOT_MULT = 2
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "candle_cache"

# VWAP Mean Reversion config
VWAP_SD_ENTRY = 2.0          # enter when price hits 2 SD from VWAP
VWAP_MIN_CANDLES = 15        # minimum candles before VWAP is reliable (~10:00 AM)
VWAP_VOL_DECLINE_RATIO = 0.8 # extension candle volume < 80% of avg prior 3
VWAP_LOSS_CAP = 10000
VWAP_PROFIT_CAP = 25000

# Afternoon Momentum config
AFT_RANGE_START = "11:30"
AFT_RANGE_END = "13:30"
AFT_VOL_MULT = 2.0           # breakout candle volume >= 2x lunchtime avg
AFT_HARD_EXIT = "15:10"
AFT_LOSS_CAP = 10000
AFT_PROFIT_CAP = 25000


def _cache_key(inst_key, from_dt, to_dt, interval):
    raw = f"{inst_key}|{from_dt}|{to_dt}|{interval}"
    return hashlib.md5(raw.encode()).hexdigest()


def _cached_fetch(ud, inst_key, from_dt, to_dt, interval):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = _cache_key(inst_key, from_dt, to_dt, interval)
    path = CACHE_DIR / f"{key}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    candles = ud.historical_data(inst_key, from_dt, to_dt, interval)
    _t.sleep(0.05)
    if candles is not None:
        with open(path, "w") as f:
            json.dump(candles, f)
    return candles


def _candle_time(cn):
    t = str(cn.get("date", cn.get("timestamp", "")))
    return t[11:16] if len(t) > 16 else t[:5]


def _simulate_trade(ocandles, entry_idx, entry, lot, sl_price, hard_exit_time=None):
    peak_pnl = 0.0
    active_floor = 0

    for i, cn in enumerate(ocandles[entry_idx + 1:], start=entry_idx + 1):
        high, low, close = cn["high"], cn["low"], cn["close"]
        t_short = _candle_time(cn)

        if hard_exit_time and t_short >= hard_exit_time:
            ep = round(close * (1 - SLIPPAGE_PCT), 2)
            return ep, "TIME", t_short, peak_pnl

        pnl_high = (high - entry) * lot
        pnl_close = (close - entry) * lot

        if pnl_high > peak_pnl:
            peak_pnl = pnl_high

        if low <= sl_price:
            ep = round(sl_price * (1 - SLIPPAGE_PCT), 2)
            return ep, "SL", t_short, peak_pnl

        for fl in FLOOR_STEPS:
            if pnl_close >= fl and fl > active_floor:
                active_floor = fl
        if active_floor > 0 and pnl_close <= active_floor:
            ep = round(close * (1 - SLIPPAGE_PCT), 2)
            return ep, f"FLOOR ₹{active_floor}", t_short, peak_pnl

    last = ocandles[-1]
    ep = round(last["close"] * (1 - SLIPPAGE_PCT), 2)
    return ep, "EOD", _candle_time(last), peak_pnl


def _load_master_data(ud, master):
    eq_keys = {}
    for inst in master:
        if inst.get("segment") == "NSE_EQ":
            tsym = (inst.get("trading_symbol") or "").upper()
            if tsym:
                eq_keys[tsym] = inst.get("instrument_key")

    universe = set()
    opt_master = {}
    lot_sizes = {}
    for inst in master:
        if inst.get("segment") != "NSE_FO":
            continue
        itype = (inst.get("instrument_type") or "").upper()
        if itype not in ("CE", "PE"):
            continue
        tsym = (inst.get("trading_symbol") or "").upper()
        base = re.match(r'^([A-Z&]+)', tsym)
        if not base:
            continue
        sym_name = base.group(1)
        if sym_name in INDEX_NAMES:
            continue
        universe.add(sym_name)
        strike_val = float(inst.get("strike_price", 0))
        ed = _expiry_to_date(inst.get("expiry"))
        if ed and strike_val > 0:
            opt_master[(sym_name, ed, strike_val, itype)] = inst.get("instrument_key")
            ls = int(inst.get("lot_size") or 0)
            if ls > 0:
                lot_sizes[sym_name] = ls

    return eq_keys, sorted(universe), opt_master, lot_sizes


def _resolve_option(sym, spot, opt_type, ref_date, opt_master, lot_sizes):
    sym_opts = {k: v for k, v in opt_master.items() if k[0] == sym and k[3] == opt_type}
    if not sym_opts:
        return None, None, None
    expiries = sorted({k[1] for k in sym_opts if k[1] >= ref_date})
    if not expiries:
        return None, None, None
    strikes = sorted({k[2] for k in sym_opts if k[1] == expiries[0]})
    strike = min(strikes, key=lambda s: abs(s - spot))
    opt_key = opt_master.get((sym, expiries[0], strike, opt_type))
    lot = lot_sizes.get(sym, 1) * LOT_MULT
    return opt_key, strike, lot


# ---------------------------------------------------------------------------
# VWAP Mean Reversion Strategy
# ---------------------------------------------------------------------------
def _compute_vwap_bands(candles):
    """Compute running VWAP and standard deviation bands from 5-min candles."""
    cum_vol = 0.0
    cum_tp_vol = 0.0
    cum_tp2_vol = 0.0
    results = []

    for cn in candles:
        tp = (cn["high"] + cn["low"] + cn["close"]) / 3
        vol = cn.get("volume", 0) or 0
        if vol <= 0:
            vol = 1
        cum_vol += vol
        cum_tp_vol += tp * vol
        cum_tp2_vol += tp * tp * vol

        vwap = cum_tp_vol / cum_vol
        variance = max(0, (cum_tp2_vol / cum_vol) - vwap * vwap)
        sd = math.sqrt(variance)

        results.append({
            "vwap": vwap,
            "sd": sd,
            "upper_2sd": vwap + VWAP_SD_ENTRY * sd,
            "lower_2sd": vwap - VWAP_SD_ENTRY * sd,
            "upper_1sd": vwap + sd,
            "lower_1sd": vwap - sd,
            "volume": vol,
        })
    return results


def scan_vwap_mean_reversion(ud, ref_date, eq_keys, universe, opt_master, lot_sizes, verbose=True):
    print(f"\n{'='*90}")
    print(f"  VWAP Mean Reversion — {ref_date}")
    print(f"{'='*90}")

    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    candidates = []
    scanned = 0

    for sym in universe:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if not inst_key:
            continue

        try:
            candles = _cached_fetch(ud, inst_key, from_dt, to_dt, "5minute")
        except Exception:
            continue
        scanned += 1
        if not candles or len(candles) < VWAP_MIN_CANDLES + 3:
            continue

        bands = _compute_vwap_bands(candles)

        for i in range(VWAP_MIN_CANDLES, len(candles)):
            cn = candles[i]
            b = bands[i]
            t_short = _candle_time(cn)

            if t_short < "10:00" or t_short > "14:00":
                continue

            avg_vol_3 = sum(bands[i-j]["volume"] for j in range(1, 4)) / 3
            cur_vol = b["volume"]

            if b["sd"] < 0.01:
                continue

            # Upper extension — price above upper 2SD band, volume declining
            if cn["close"] > b["upper_2sd"] and cur_vol < avg_vol_3 * VWAP_VOL_DECLINE_RATIO:
                # Exhaustion signal: small body or long upper wick
                body = abs(cn["close"] - cn["open"])
                total_range = cn["high"] - cn["low"]
                if total_range > 0 and (body / total_range < 0.5 or cn["high"] - max(cn["close"], cn["open"]) > body):
                    candidates.append({
                        "symbol": sym, "direction": "bearish", "opt_type": "PE",
                        "breakout_price": cn["close"], "entry_after": t_short,
                        "vwap": b["vwap"], "sd_dist": (cn["close"] - b["vwap"]) / b["sd"],
                    })
                    break

            # Lower extension — price below lower 2SD band, volume declining
            if cn["close"] < b["lower_2sd"] and cur_vol < avg_vol_3 * VWAP_VOL_DECLINE_RATIO:
                body = abs(cn["close"] - cn["open"])
                total_range = cn["high"] - cn["low"]
                if total_range > 0 and (body / total_range < 0.5 or min(cn["close"], cn["open"]) - cn["low"] > body):
                    candidates.append({
                        "symbol": sym, "direction": "bullish", "opt_type": "CE",
                        "breakout_price": cn["close"], "entry_after": t_short,
                        "vwap": b["vwap"], "sd_dist": (b["vwap"] - cn["close"]) / b["sd"],
                    })
                    break

    candidates.sort(key=lambda x: x.get("sd_dist", 0), reverse=True)
    print(f"  Scanned: {scanned} | VWAP reversals: {len(candidates)}")

    return _exec_trades(candidates, ref_date, ud, opt_master, lot_sizes,
                        VWAP_LOSS_CAP, VWAP_PROFIT_CAP, "VWAP-MR", verbose)


# ---------------------------------------------------------------------------
# Afternoon Momentum Strategy
# ---------------------------------------------------------------------------
def scan_afternoon_momentum(ud, ref_date, eq_keys, universe, opt_master, lot_sizes, verbose=True):
    print(f"\n{'='*90}")
    print(f"  Afternoon Momentum — {ref_date}")
    print(f"{'='*90}")

    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    candidates = []
    scanned = 0

    for sym in universe:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if not inst_key:
            continue

        try:
            candles = _cached_fetch(ud, inst_key, from_dt, to_dt, "5minute")
        except Exception:
            continue
        scanned += 1
        if not candles or len(candles) < 30:
            continue

        # Build lunchtime range (11:30 - 13:30)
        lunch_high = 0
        lunch_low = float("inf")
        lunch_vols = []
        lunch_found = False

        for cn in candles:
            t = _candle_time(cn)
            if t < AFT_RANGE_START:
                continue
            if t >= AFT_RANGE_END:
                break
            lunch_found = True
            if cn["high"] > lunch_high:
                lunch_high = cn["high"]
            if cn["low"] < lunch_low:
                lunch_low = cn["low"]
            lunch_vols.append(cn.get("volume", 0) or 1)

        if not lunch_found or lunch_high <= lunch_low or not lunch_vols:
            continue

        avg_lunch_vol = sum(lunch_vols) / len(lunch_vols)
        if avg_lunch_vol <= 0:
            avg_lunch_vol = 1

        # Scan for breakout after 13:30
        for cn in candles:
            t = _candle_time(cn)
            if t < AFT_RANGE_END:
                continue
            if t >= AFT_HARD_EXIT:
                break

            vol = cn.get("volume", 0) or 0

            if cn["close"] > lunch_high and vol >= avg_lunch_vol * AFT_VOL_MULT:
                candidates.append({
                    "symbol": sym, "direction": "bullish", "opt_type": "CE",
                    "breakout_price": cn["close"], "entry_after": t,
                    "range_pct": (lunch_high - lunch_low) / lunch_low * 100,
                    "vol_ratio": vol / avg_lunch_vol,
                })
                break
            elif cn["close"] < lunch_low and vol >= avg_lunch_vol * AFT_VOL_MULT:
                candidates.append({
                    "symbol": sym, "direction": "bearish", "opt_type": "PE",
                    "breakout_price": cn["close"], "entry_after": t,
                    "range_pct": (lunch_high - lunch_low) / lunch_low * 100,
                    "vol_ratio": vol / avg_lunch_vol,
                })
                break

    candidates.sort(key=lambda x: x.get("vol_ratio", 0), reverse=True)
    print(f"  Scanned: {scanned} | Afternoon breakouts: {len(candidates)}")

    return _exec_trades(candidates, ref_date, ud, opt_master, lot_sizes,
                        AFT_LOSS_CAP, AFT_PROFIT_CAP, "AFT-MOM", verbose,
                        hard_exit_time=AFT_HARD_EXIT)


# ---------------------------------------------------------------------------
# Trade execution (shared)
# ---------------------------------------------------------------------------
def _exec_trades(candidates, ref_date, ud, opt_master, lot_sizes,
                 loss_cap, profit_cap, label, verbose, hard_exit_time=None):
    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    full_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    prepared = []
    for c in candidates:
        sym = c["symbol"]
        opt_type = c.get("opt_type", "PE")
        spot = c["breakout_price"]

        opt_key, strike, lot = _resolve_option(sym, spot, opt_type, ref_date, opt_master, lot_sizes)
        if not opt_key:
            continue

        try:
            ocandles = _cached_fetch(ud, opt_key, from_dt, full_to, "1minute")
        except Exception:
            continue
        if not ocandles or len(ocandles) < 5:
            continue

        bt = c.get("entry_after", "09:20")
        entry_candle = None
        entry_idx = 0
        for i, cn in enumerate(ocandles):
            ct_short = _candle_time(cn)
            if ct_short >= bt:
                entry_candle = cn
                entry_idx = i
                break
        if not entry_candle:
            continue

        raw_entry = entry_candle["close"]
        if raw_entry <= 0 or raw_entry < MIN_PREMIUM:
            continue

        entry = round(raw_entry * (1 + SLIPPAGE_PCT), 2)
        sl_pct_price = entry * (1 - SL_PCT)
        sl_cap_price = entry - (MAX_SL_RS / lot)
        sl_price = round(max(sl_pct_price, sl_cap_price), 2)

        exit_price, exit_reason, exit_time, peak_pnl = _simulate_trade(
            ocandles, entry_idx, entry, lot, sl_price, hard_exit_time
        )

        pnl = (exit_price - entry) * lot
        charges = (BROKERAGE_PER_ORDER * 2) + (exit_price * lot * STT_PCT)
        pnl -= charges

        prepared.append({
            "candidate": c, "sym": sym, "opt_type": opt_type, "strike": strike,
            "lot": lot, "entry": entry, "margin": entry * lot,
            "entry_time": bt, "exit_time": exit_time,
            "exit_price": exit_price, "exit_reason": exit_reason,
            "pnl": pnl, "peak": peak_pnl,
        })

    prepared.sort(key=lambda x: x["entry_time"])

    avail = CAPITAL
    active = []
    realized_pnl = 0.0
    results = []
    traded_indices = set()
    cap_stopped = False

    if verbose:
        print(f"\n  {'Symbol':<14s} {'Str':>6s} {'D':>1s} {'Entry':>7s} {'Exit':>7s}"
              f" {'P&L':>9s} {'Peak':>8s} {'Reason':<12s} {'ETime':>5s} {'XTime':>5s}")
        print(f"  {'-'*95}")

    def _do_place(idx):
        nonlocal avail
        t = prepared[idx]
        avail -= t["margin"]
        active.append((t["exit_time"], t["entry_time"], t["margin"], t["pnl"], idx))
        traded_indices.add(idx)
        results.append({"sym": t["sym"], "pnl": t["pnl"], "reason": t["exit_reason"], "peak": t["peak"]})
        if verbose:
            c = t["candidate"]
            d_tag = "▲" if c.get("direction", "bearish") == "bullish" else "▼"
            print(f"  {d_tag} {t['sym']:<12s} {t['strike']:>6.0f}{t['opt_type']} {t['entry']:>7.1f} → {t['exit_price']:>6.1f}"
                  f"  ₹{t['pnl']:>+8,.0f}  ₹{t['peak']:>+7,.0f} {t['exit_reason']:<12s} {t['entry_time']:>5s} {t['exit_time']:>5s}")

    pending = []
    for idx in range(len(prepared)):
        t = prepared[idx]
        if t["margin"] > avail:
            pending.append(idx)
            continue
        _do_place(idx)

    while pending and active:
        active.sort(key=lambda x: x[0])
        ext, _et, margin, pnl, tidx = active.pop(0)
        avail += margin + pnl
        realized_pnl += pnl

        new_active = []
        for a in active:
            if a[0] <= ext:
                avail += a[2] + a[3]
                realized_pnl += a[3]
            else:
                new_active.append(a)
        active = new_active

        if realized_pnl >= profit_cap or realized_pnl <= -loss_cap:
            cap_stopped = True
            break

        if avail < 5000:
            continue

        retryable = [i for i in pending if prepared[i]["entry_time"] <= ext]
        later = [i for i in pending if prepared[i]["entry_time"] > ext]

        placed_now = 0
        still_pending = []
        for idx in retryable:
            if idx in traded_indices:
                continue
            t = prepared[idx]
            if t["margin"] > avail:
                still_pending.append(idx)
                continue
            _do_place(idx)
            placed_now += 1

        pending = still_pending + later
        if placed_now == 0 and not active:
            break

    skipped = len(pending)
    if results:
        day_pnl = sum(r["pnl"] for r in results)
        wins = sum(1 for r in results if r["pnl"] > 0)
        losses = len(results) - wins
        extra = ""
        if cap_stopped:
            extra = f" | CAP HIT (realized ₹{realized_pnl:>+,.0f})"
        print(f"  {label} TOTAL: {len(results)} trades | {wins}W/{losses}L | Skipped(cap): {skipped}{extra} | ₹{day_pnl:>+,.0f}")
    else:
        print(f"  {label}: No trades")

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _trading_days(year, month):
    """Return weekdays in given month (holidays will just have no data)."""
    days = []
    d = date(year, month, 1)
    while d.month == month:
        if d.weekday() < 5 and d <= date.today():
            days.append(d)
        d += timedelta(days=1)
    return days


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Single date YYYY-MM-DD")
    parser.add_argument("--month", help="Full month YYYY-MM")
    args = parser.parse_args()

    if args.date:
        dates = [date.fromisoformat(args.date)]
    elif args.month:
        y, m = args.month.split("-")
        dates = _trading_days(int(y), int(m))
    else:
        print("Provide --date or --month")
        return

    token = load_cached_token()
    if not token:
        print("ERROR: No valid Upstox token. Run auto-login first.")
        return
    ud = UpstoxData(access_token=token)
    master = ud._load_master()
    eq_keys, universe, opt_master, lot_sizes = _load_master_data(ud, master)

    print(f"\n  Backtesting VWAP Mean Reversion + Afternoon Momentum")
    print(f"  Capital: ₹{CAPITAL:,} per strategy | Lots: {LOT_MULT} | SL: {SL_PCT*100:.0f}%")
    print(f"  F&O universe: {len(universe)} stocks | Dates: {len(dates)} days")
    print(f"  VWAP: entry at {VWAP_SD_ENTRY}SD, after 10:00, vol decline filter")
    print(f"  Afternoon: lunchtime range 11:30-13:30, breakout 13:30+, 2x vol, exit by 15:10")

    all_vwap = []
    all_aft = []

    for ref_date in dates:
        print(f"\n{'#'*90}")
        print(f"  {ref_date} ({ref_date.strftime('%A')})")
        print(f"{'#'*90}")

        vwap_results = scan_vwap_mean_reversion(ud, ref_date, eq_keys, universe, opt_master, lot_sizes)
        aft_results = scan_afternoon_momentum(ud, ref_date, eq_keys, universe, opt_master, lot_sizes)

        all_vwap.extend(vwap_results)
        all_aft.extend(aft_results)

    # Summary
    print(f"\n{'='*90}")
    print(f"  MONTHLY SUMMARY")
    print(f"{'='*90}")

    grand = 0
    for label, results in [("VWAP-MR", all_vwap), ("AFT-MOM", all_aft)]:
        if not results:
            print(f"  {label:8s}: No trades")
            continue
        pnl = sum(r["pnl"] for r in results)
        wins = sum(1 for r in results if r["pnl"] > 0)
        losses = len(results) - wins
        wr = wins / len(results) * 100
        avg_win = sum(r["pnl"] for r in results if r["pnl"] > 0) / max(wins, 1)
        avg_loss = sum(r["pnl"] for r in results if r["pnl"] <= 0) / max(losses, 1)
        grand += pnl
        print(f"  {label:8s}: {len(results):3d} trades | {wins}W/{losses}L ({wr:.0f}%) | "
              f"Avg W: ₹{avg_win:>+,.0f} | Avg L: ₹{avg_loss:>+,.0f} | Total: ₹{pnl:>+,.0f}")
    print(f"  {'TOTAL':8s}: ₹{grand:>+,.0f}")
    print()


if __name__ == "__main__":
    main()

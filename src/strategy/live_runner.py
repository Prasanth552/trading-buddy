"""Live short-straddle strategy runner.

Runs three validated strategies (kitchen_sink, vf_920_sl30, entry_945_sl30)
against real-time index candle data and records results to SQLite.

Usage (standalone):
    .venv/bin/python3 -m src.strategy.live_runner

Integrated into channel_app via the /api/strategy/* endpoints.
"""
from __future__ import annotations

import math
import os
import pickle
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config
from src.broker.upstox_data import UpstoxData
from src.storage import db

IST = ZoneInfo("Asia/Kolkata")

INDEXES = {
    "NIFTY": {
        "key": "NSE_INDEX|Nifty 50",
        "lot_size": 75,
        "strike_step": 50,
        "iv_annual": 0.13,
        "vol_skip_range": 120,
    },
    "BANKNIFTY": {
        "key": "NSE_INDEX|Nifty Bank",
        "lot_size": 30,
        "strike_step": 100,
        "iv_annual": 0.17,
        "vol_skip_range": 250,
    },
    "SENSEX": {
        "key": "BSE_INDEX|SENSEX",
        "lot_size": 20,
        "strike_step": 100,
        "iv_annual": 0.13,
        "vol_skip_range": 400,
    },
}

EXPIRY_WEEKDAY = {"NIFTY": 1, "BANKNIFTY": 2, "SENSEX": 4}

STRATEGIES = {
    "kitchen_sink": dict(
        entry_hour=9, entry_min=30, sl_pct=0.35,
        combined_sl=True, trailing=True, vol_filter=True,
    ),
    "vf_920_sl30": dict(
        entry_hour=9, entry_min=20, sl_pct=0.30,
        combined_sl=False, trailing=False, vol_filter=True,
    ),
    "entry_945_sl30": dict(
        entry_hour=9, entry_min=45, sl_pct=0.30,
        combined_sl=False, trailing=False, vol_filter=False,
    ),
}

CACHE_DIR = os.path.join(config.DATA_DIR, "ml_cache")

STRATEGY_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    idx         TEXT NOT NULL,
    lots        INTEGER DEFAULT 1,
    entry_time  TEXT,
    exit_time   TEXT,
    exit_reason TEXT,
    spot_entry  REAL,
    atm_strike  REAL,
    ce_entry    REAL,
    pe_entry    REAL,
    ce_exit     REAL,
    pe_exit     REAL,
    ce_pnl      REAL,
    pe_pnl      REAL,
    charges     REAL,
    net_pnl     REAL,
    skipped     INTEGER DEFAULT 0,
    skip_reason TEXT,
    dte         INTEGER,
    UNIQUE(date, strategy, idx)
);
CREATE INDEX IF NOT EXISTS idx_sr_date ON strategy_results(date);
CREATE INDEX IF NOT EXISTS idx_sr_strat ON strategy_results(strategy);
"""


def init_strategy_db():
    with db.get_conn() as conn:
        conn.executescript(STRATEGY_SCHEMA)


# ---------------------------------------------------------------------------
# Black-Scholes helpers
# ---------------------------------------------------------------------------
def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs_call(S, K, T, sigma, r=0.07):
    if T <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def _bs_put(S, K, T, sigma, r=0.07):
    if T <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def est_prem(spot, strike, opt_type, T_frac, iv):
    T = max(T_frac, 1e-6)
    return _bs_call(spot, strike, T, iv) if opt_type == "CE" else _bs_put(spot, strike, T, iv)


def round_strike(spot, step):
    return round(spot / step) * step


def calc_charges(entry_p, exit_p, qty):
    buy_t = entry_p * qty
    sell_t = exit_p * qty
    tot_t = buy_t + sell_t
    brok = 40.0
    stt = sell_t * 0.001
    exch = tot_t * 0.000495
    sebi = tot_t * 0.000001
    stamp = buy_t * 0.00003
    gst = (brok + exch) * 0.18
    return brok + stt + exch + sebi + stamp + gst


# ---------------------------------------------------------------------------
# Candle helpers
# ---------------------------------------------------------------------------
def _candle_hm(c):
    ts = c["date"]
    return int(ts[11:13]), int(ts[14:16])


def _candle_time_str(c):
    return c["date"][11:16]


def _candle_minutes(c):
    h, m = _candle_hm(c)
    return (h - 9) * 60 + (m - 15)


def _days_to_expiry(ref_date, idx_name):
    exp_wd = EXPIRY_WEEKDAY.get(idx_name, 3)
    return (exp_wd - ref_date.weekday()) % 7


def _dte_fraction(ref_date, idx_name, minutes_into_day=0):
    dte = _days_to_expiry(ref_date, idx_name)
    day_fraction = max(0, (375 - minutes_into_day) / 375)
    return (dte + day_fraction) / 365.0


def _first_candle_range(candles):
    high, low, count = -float("inf"), float("inf"), 0
    for c in candles:
        h, m = _candle_hm(c)
        if h == 9 and m < 15:
            continue
        if count >= 3:
            break
        high = max(high, c["high"])
        low = min(low, c["low"])
        count += 1
    return high - low if high != -float("inf") else 0


def fetch_candles(uclient, idx_name, ref_date, interval="5minute"):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, f"candles_{idx_name}_{ref_date}_{interval}.pkl")
    if os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            return pickle.load(f)
    idx = INDEXES[idx_name]
    from_dt = datetime(ref_date.year, ref_date.month, ref_date.day, 9, 0, tzinfo=IST)
    to_dt = datetime(ref_date.year, ref_date.month, ref_date.day, 16, 0, tzinfo=IST)
    candles = uclient.historical_data(idx["key"], from_dt, to_dt, interval)
    if candles and len(candles) > 5:
        with open(cache_file, "wb") as f:
            pickle.dump(candles, f)
    return candles


# ---------------------------------------------------------------------------
# Core: run a single strategy on a single index for one day
# ---------------------------------------------------------------------------
def run_straddle(candles, idx_name, ref_date, *,
                 entry_hour, entry_min, sl_pct,
                 combined_sl=False, trailing=False, vol_filter=False,
                 lots=1):
    """Run short straddle and return result dict (no printing)."""
    idx = INDEXES[idx_name]
    iv = idx["iv_annual"]
    lot_size = idx["lot_size"] * lots
    step = idx["strike_step"]
    dte = _days_to_expiry(ref_date, idx_name)

    fr = _first_candle_range(candles)
    if vol_filter and fr > idx["vol_skip_range"]:
        return {"skipped": True, "skip_reason": "vol_filter", "net_pnl": 0,
                "dte": dte, "vol_range": round(fr)}

    entry_candle = None
    for c in candles:
        h, m = _candle_hm(c)
        if h > entry_hour or (h == entry_hour and m >= entry_min):
            entry_candle = c
            break
    if not entry_candle:
        return {"skipped": True, "skip_reason": "no_entry", "net_pnl": 0, "dte": dte}

    spot_entry = entry_candle["close"]
    atm = round_strike(spot_entry, step)
    mins_entry = _candle_minutes(entry_candle)
    T_entry = _dte_fraction(ref_date, idx_name, mins_entry)

    ce_entry = est_prem(spot_entry, atm, "CE", T_entry, iv)
    pe_entry = est_prem(spot_entry, atm, "PE", T_entry, iv)
    total_prem = ce_entry + pe_entry

    if combined_sl:
        sl_level = total_prem * (1 + sl_pct)
    else:
        ce_sl = ce_entry * (1 + sl_pct)
        pe_sl = pe_entry * (1 + sl_pct)

    ce_alive = pe_alive = True
    ce_exit_prem = pe_exit_prem = None
    exit_reason = "time_3:10"
    best_combined_profit = 0.0
    trail_active = False
    exit_time = None

    e_idx = candles.index(entry_candle)
    for c in candles[e_idx + 1:]:
        h, m = _candle_hm(c)
        mins = _candle_minutes(c)
        T = _dte_fraction(ref_date, idx_name, mins)
        spot = c["close"]

        ce_now = est_prem(spot, atm, "CE", T, iv)
        pe_now = est_prem(spot, atm, "PE", T, iv)
        ce_worst = est_prem(c["high"], atm, "CE", T, iv)
        pe_worst = est_prem(c["low"], atm, "PE", T, iv)

        if combined_sl:
            if ce_worst + pe_worst >= sl_level:
                ce_exit_prem, pe_exit_prem = ce_now, pe_now
                exit_reason = "combined_sl"
                exit_time = _candle_time_str(c)
                break
        else:
            if ce_alive and ce_worst >= ce_sl:
                ce_exit_prem = ce_sl
                ce_alive = False
            if pe_alive and pe_worst >= pe_sl:
                pe_exit_prem = pe_sl
                pe_alive = False

        if trailing and ce_alive and pe_alive:
            current_profit = total_prem - (ce_now + pe_now)
            best_combined_profit = max(best_combined_profit, current_profit)
            if current_profit / total_prem >= 0.40:
                trail_active = True
            if trail_active and best_combined_profit > 0:
                give_back = best_combined_profit * 0.20
                if current_profit < best_combined_profit - give_back:
                    ce_exit_prem, pe_exit_prem = ce_now, pe_now
                    exit_reason = "trailing"
                    exit_time = _candle_time_str(c)
                    break

        if h >= 15 and m >= 10:
            if ce_alive:
                ce_exit_prem = ce_now
            if pe_alive:
                pe_exit_prem = pe_now
            exit_time = _candle_time_str(c)
            break

    last = candles[-1]
    T_last = _dte_fraction(ref_date, idx_name, _candle_minutes(last))
    if ce_exit_prem is None:
        ce_exit_prem = est_prem(last["close"], atm, "CE", T_last, iv)
    if pe_exit_prem is None:
        pe_exit_prem = est_prem(last["close"], atm, "PE", T_last, iv)
    if exit_time is None:
        exit_time = _candle_time_str(last)

    ce_pnl = (ce_entry - ce_exit_prem) * lot_size
    pe_pnl = (pe_entry - pe_exit_prem) * lot_size
    charges = calc_charges(ce_entry, ce_exit_prem, lot_size) + \
              calc_charges(pe_entry, pe_exit_prem, lot_size)
    net = ce_pnl + pe_pnl - charges

    return {
        "skipped": False,
        "net_pnl": round(net, 2),
        "ce_pnl": round(ce_pnl, 2),
        "pe_pnl": round(pe_pnl, 2),
        "charges": round(charges, 2),
        "exit_reason": exit_reason,
        "entry_time": _candle_time_str(entry_candle),
        "exit_time": exit_time,
        "spot_entry": round(spot_entry, 2),
        "atm_strike": atm,
        "ce_entry": round(ce_entry, 2),
        "pe_entry": round(pe_entry, 2),
        "ce_exit": round(ce_exit_prem, 2),
        "pe_exit": round(pe_exit_prem, 2),
        "dte": dte,
    }


# ---------------------------------------------------------------------------
# Run all strategies for a single day, save to DB
# ---------------------------------------------------------------------------
def run_day(ref_date: date, lots: int = 1, *, force: bool = False) -> dict:
    """Run all 3 strategies across all indexes for one day.

    Returns: {strategy_name: {"day_pnl": float, "indexes": {idx: result}}}
    """
    init_strategy_db()

    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT strategy, idx FROM strategy_results WHERE date=?",
            (ref_date.isoformat(),)
        ).fetchall()
    if existing and not force:
        return _load_day_from_db(ref_date)

    uclient = UpstoxData()
    candle_cache = {}
    for idx_name in INDEXES:
        candle_cache[idx_name] = fetch_candles(uclient, idx_name, ref_date)

    results = {}
    for sname, params in STRATEGIES.items():
        day_pnl = 0.0
        idx_results = {}
        for idx_name in INDEXES:
            candles = candle_cache[idx_name]
            if not candles or len(candles) < 20:
                r = {"skipped": True, "skip_reason": "no_data", "net_pnl": 0, "dte": None}
            else:
                r = run_straddle(candles, idx_name, ref_date, lots=lots, **params)
            idx_results[idx_name] = r
            day_pnl += r["net_pnl"]

            with db.get_conn() as conn:
                conn.execute("""INSERT OR REPLACE INTO strategy_results
                    (date, strategy, idx, lots, entry_time, exit_time, exit_reason,
                     spot_entry, atm_strike, ce_entry, pe_entry, ce_exit, pe_exit,
                     ce_pnl, pe_pnl, charges, net_pnl, skipped, skip_reason, dte)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ref_date.isoformat(), sname, idx_name, lots,
                     r.get("entry_time"), r.get("exit_time"), r.get("exit_reason"),
                     r.get("spot_entry"), r.get("atm_strike"),
                     r.get("ce_entry"), r.get("pe_entry"),
                     r.get("ce_exit"), r.get("pe_exit"),
                     r.get("ce_pnl"), r.get("pe_pnl"),
                     r.get("charges"), r["net_pnl"],
                     1 if r.get("skipped") else 0, r.get("skip_reason"), r.get("dte")))

        results[sname] = {"day_pnl": round(day_pnl, 2), "indexes": idx_results}
    return results


def _load_day_from_db(ref_date: date) -> dict:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_results WHERE date=?",
            (ref_date.isoformat(),)
        ).fetchall()
    results = {}
    for r in rows:
        r = dict(r)
        sname = r["strategy"]
        if sname not in results:
            results[sname] = {"day_pnl": 0.0, "indexes": {}}
        results[sname]["indexes"][r["idx"]] = r
        results[sname]["day_pnl"] += r["net_pnl"] or 0
    for s in results:
        results[s]["day_pnl"] = round(results[s]["day_pnl"], 2)
    return results


# ---------------------------------------------------------------------------
# Backfill + query helpers for dashboard
# ---------------------------------------------------------------------------
def backfill(from_date: date, to_date: date, lots: int = 1):
    """Run strategies for a range of dates (skips weekends, reuses cache)."""
    d = from_date
    while d <= to_date:
        if d.weekday() < 5:
            print(f"  Running {d}...", end=" ", flush=True)
            try:
                res = run_day(d, lots)
                pnls = {s: res[s]["day_pnl"] for s in res}
                print(f"kitchen_sink={pnls.get('kitchen_sink', 0):+,.0f}")
            except Exception as e:
                print(f"ERROR: {e}")
        d += timedelta(days=1)


def get_history(days: int = 30) -> list[dict]:
    """Get daily strategy results for the last N calendar days."""
    init_strategy_db()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT date, strategy, idx, net_pnl, skipped, skip_reason,
                   entry_time, exit_time, exit_reason, dte,
                   ce_entry, pe_entry, ce_exit, pe_exit, charges, lots
            FROM strategy_results
            WHERE date >= ?
            ORDER BY date, strategy, idx
        """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def get_daily_summary(days: int = 30) -> dict:
    """Aggregate daily P&L per strategy for dashboard display."""
    rows = get_history(days)
    strats = {}
    for r in rows:
        s = r["strategy"]
        d = r["date"]
        if s not in strats:
            strats[s] = {}
        if d not in strats[s]:
            strats[s][d] = 0.0
        strats[s][d] += r["net_pnl"] or 0

    result = {}
    for s, daily in strats.items():
        days_list = sorted(daily.items())
        pnls = [round(p, 2) for _, p in days_list]
        dates = [d for d, _ in days_list]
        total = round(sum(pnls), 2)
        green = sum(1 for p in pnls if p > 0)
        traded = len(pnls)
        cum = []
        running = 0
        for d, p in days_list:
            running += p
            cum.append({"date": d, "pnl": round(p, 2), "cumulative": round(running, 2)})

        result[s] = {
            "dates": dates,
            "pnls": pnls,
            "total": total,
            "green": green,
            "traded": traded,
            "win_rate": round(green / traded * 100, 1) if traded else 0,
            "avg_day": round(total / traded, 2) if traded else 0,
            "max_day": max(pnls) if pnls else 0,
            "min_day": min(pnls) if pnls else 0,
            "cumulative": cum,
        }
    return result


def get_today_detail() -> dict:
    """Get per-index breakdown for today."""
    today = date.today().isoformat()
    init_strategy_db()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_results WHERE date=? ORDER BY strategy, idx",
            (today,)
        ).fetchall()
    result = {}
    for r in rows:
        r = dict(r)
        s = r["strategy"]
        if s not in result:
            result[s] = {"day_pnl": 0.0, "indexes": {}}
        result[s]["indexes"][r["idx"]] = r
        result[s]["day_pnl"] += r["net_pnl"] or 0
    for s in result:
        result[s]["day_pnl"] = round(result[s]["day_pnl"], 2)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    p = argparse.ArgumentParser(description="Live strategy runner")
    p.add_argument("--date", help="Run for specific date (YYYY-MM-DD)")
    p.add_argument("--backfill-from", help="Backfill from date")
    p.add_argument("--backfill-to", help="Backfill to date")
    p.add_argument("--lots", type=int, default=1)
    p.add_argument("--force", action="store_true")
    a = p.parse_args()

    if a.backfill_from:
        fd = date.fromisoformat(a.backfill_from)
        td = date.fromisoformat(a.backfill_to) if a.backfill_to else date.today()
        print(f"Backfilling {fd} to {td}...")
        backfill(fd, td, a.lots)
    else:
        d = date.fromisoformat(a.date) if a.date else date.today()
        print(f"Running strategies for {d}...")
        res = run_day(d, a.lots, force=a.force)
        for s, data in res.items():
            tag = "GREEN" if data["day_pnl"] > 0 else "RED"
            print(f"  {s}: {data['day_pnl']:+,.0f} [{tag}]")

"""Backtest all 5 strategies: OEH + ORB + PDHL + ORF + AFT.

Time-stepped simulation that mirrors the live bot's rescan loop:
- Each strategy rescans every 5 min within its active window
- Capital freed by closed trades is immediately available for new entries
- Per-strategy daily loss/profit caps tracked in real time

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/backtest_all5.py --month 2026-09
    PYTHONPATH=. .venv/bin/python3 scripts/backtest_all5.py --date 2026-10-01
"""
from __future__ import annotations
import argparse, hashlib, json, math, re, time as _t
from concurrent.futures import ThreadPoolExecutor, as_completed
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
CAPITAL = 300000
BROKERAGE_PER_ORDER = 20
STT_PCT = 0.000625
LOT_MULT = 2
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "candle_cache"

# Realism constraints
SYM_COOLDOWN_MIN = 15       # minutes before re-entering same symbol after exit
MAX_CONCURRENT = 8          # max open positions per strategy at any time
MAX_ENTRIES_PER_SCAN = 3    # max new trades placed per scan tick (API/order throughput)

# OEH config — capital-only gating, no daily caps
OEH_TOLERANCE = 0.05
OEH_MIN_DROP_PCT = 0.3
OEH_LOSS_CAP = 999999
OEH_PROFIT_CAP = 999999
OEH_SCAN_START = "09:20"
OEH_SCAN_END = "15:20"

# OEL config — mirror of OEH but bullish (Open=Low, buy CE)
OEL_TOLERANCE = 0.05
OEL_MIN_RISE_PCT = 0.3
OEL_LOSS_CAP = 999999
OEL_PROFIT_CAP = 999999
OEL_SCAN_START = "09:20"
OEL_SCAN_END = "15:20"

# ORB config
ORB_MIN_RANGE_PCT = 0.3
ORB_MAX_RANGE_PCT = 3.0
ORB_LOSS_CAP = 20000
ORB_PROFIT_CAP = 25000
ORB_SCAN_START = "09:25"
ORB_SCAN_END = "14:30"

# PDHL config
PDHL_LOSS_CAP = 10000
PDHL_PROFIT_CAP = 25000
PDHL_SCAN_START = "09:20"
PDHL_SCAN_END = "14:30"

# ORF config
ORF_MIN_RANGE_PCT = 0.3
ORF_MAX_RANGE_PCT = 3.0
ORF_BREAKOUT_MIN_PCT = 0.3
ORF_FADE_WINDOW = 10
ORF_TIME_EXIT = "11:30"
ORF_LOSS_CAP = 10000
ORF_PROFIT_CAP = 25000
ORF_SCAN_START = "09:30"
ORF_SCAN_END = "11:30"

# AFT config
AFT_RANGE_START = "11:30"
AFT_RANGE_END = "13:30"
AFT_VOL_MULT = 2.0
AFT_HARD_EXIT = "15:10"
AFT_MIN_RANGE_PCT = 0.3
AFT_LOSS_CAP = 10000
AFT_PROFIT_CAP = 25000
AFT_SCAN_START = "13:35"
AFT_SCAN_END = "15:10"

# ---------------------------------------------------------------------------
# Caching + parallel fetch
# ---------------------------------------------------------------------------
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
    if candles is not None:
        with open(path, "w") as f:
            json.dump(candles, f)
    return candles


def _parallel_fetch(ud, fetch_jobs, max_workers=8):
    results = {}
    uncached = []

    for sym, inst_key, from_dt, to_dt, interval in fetch_jobs:
        key = _cache_key(inst_key, from_dt, to_dt, interval)
        path = CACHE_DIR / f"{key}.json"
        if path.exists():
            with open(path) as f:
                results[sym] = json.load(f)
        else:
            uncached.append((sym, inst_key, from_dt, to_dt, interval))

    if not uncached:
        return results

    def _fetch_one(job):
        sym, inst_key, from_dt, to_dt, interval = job
        try:
            candles = ud.historical_data(inst_key, from_dt, to_dt, interval)
            if candles is not None:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                key = _cache_key(inst_key, from_dt, to_dt, interval)
                path = CACHE_DIR / f"{key}.json"
                with open(path, "w") as f:
                    json.dump(candles, f)
            return sym, candles
        except Exception:
            return sym, None

    batch_size = max_workers
    for i in range(0, len(uncached), batch_size):
        batch = uncached[i:i + batch_size]
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_fetch_one, j): j for j in batch}
            for fut in as_completed(futures):
                sym, candles = fut.result()
                if candles:
                    results[sym] = candles
        if i + batch_size < len(uncached):
            _t.sleep(0.15)

    return results


def _candle_time(cn):
    t = str(cn.get("date", cn.get("timestamp", "")))
    return t[11:16] if len(t) > 16 else t[:5]


# ---------------------------------------------------------------------------
# Charges
# ---------------------------------------------------------------------------
def _calc_charges(entry_price, exit_price, qty):
    buy_turnover = entry_price * qty
    sell_turnover = exit_price * qty
    total_turnover = buy_turnover + sell_turnover
    brokerage = 20.0 * 2
    stt = sell_turnover * 0.001
    exchange_txn = total_turnover * 0.000495
    sebi = total_turnover * 0.000001
    stamp_duty = buy_turnover * 0.00003
    gst = (brokerage + exchange_txn) * 0.18
    return round(brokerage + stt + exchange_txn + sebi + stamp_duty + gst, 2)


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------
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


_opt_index: dict[tuple, list[tuple]] = {}

def _build_opt_index(opt_master):
    global _opt_index
    _opt_index.clear()
    for k, v in opt_master.items():
        idx_key = (k[0], k[3])  # (sym, opt_type)
        _opt_index.setdefault(idx_key, []).append((k[1], k[2], v))  # (expiry, strike, inst_key)

def _resolve_option(sym, spot, opt_type, ref_date, opt_master, lot_sizes):
    entries = _opt_index.get((sym, opt_type))
    if not entries:
        return None, None, None
    expiries = sorted({e for e, s, v in entries if e >= ref_date})
    if not expiries:
        return None, None, None
    nearest_exp = expiries[0]
    strikes = [s for e, s, v in entries if e == nearest_exp]
    strike = min(strikes, key=lambda s: abs(s - spot))
    opt_key = opt_master.get((sym, nearest_exp, strike, opt_type))
    lot = lot_sizes.get(sym, 1) * LOT_MULT
    return opt_key, strike, lot


def _prev_trading_day(d):
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _prefetch_equity_candles(ud, eq_keys, universe, from_dt, to_dt, interval):
    jobs = []
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if inst_key:
            jobs.append((sym, inst_key, from_dt, to_dt, interval))
    return _parallel_fetch(ud, jobs)


def _prefetch_option_candles(ud, candles_5m, universe, opt_master, lot_sizes, ref_date, from_dt, to_dt):
    """Prefetch 1-min option candles for ATM CE+PE of every stock upfront in parallel."""
    jobs = []
    resolved = {}
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        eq = candles_5m.get(sym)
        if not eq:
            continue
        spot = eq[0]["open"]
        if spot <= 0:
            continue
        for ot in ("CE", "PE"):
            opt_key, strike, lot = _resolve_option(sym, spot, ot, ref_date, opt_master, lot_sizes)
            if opt_key:
                cache_key = f"{sym}_{strike}_{ot}"
                resolved[cache_key] = (opt_key, strike, lot)
                jobs.append((cache_key, opt_key, from_dt, to_dt, "1minute"))
    result = _parallel_fetch(ud, jobs, max_workers=12)
    opt_cache = {}
    for cache_key, candles in result.items():
        if candles and len(candles) >= 5:
            cmap = {_candle_time(cn): cn for cn in candles}
            opt_cache[cache_key] = cmap
        else:
            opt_cache[cache_key] = {}
    return opt_cache, resolved


# ---------------------------------------------------------------------------
# Time-stepped trade simulation engine
# ---------------------------------------------------------------------------
# Instead of simulating a trade from start to end in one call, we advance
# one minute at a time across ALL active positions simultaneously.

def _tick_hhmm(hh, mm):
    return f"{hh:02d}:{mm:02d}"


def _hhmm_to_tuple(s):
    return int(s[:2]), int(s[3:5])


def _advance_1min(hh, mm):
    mm += 1
    if mm >= 60:
        mm = 0
        hh += 1
    return hh, mm


class Position:
    __slots__ = ("sym", "opt_type", "strike", "lot", "entry", "sl_price",
                 "margin", "entry_time", "hard_exit_time", "peak_net",
                 "stepped_floor", "opt_candle_map", "strategy", "candidate")

    def __init__(self, sym, opt_type, strike, lot, entry, sl_price,
                 margin, entry_time, hard_exit_time, opt_candle_map, strategy, candidate):
        self.sym = sym
        self.opt_type = opt_type
        self.strike = strike
        self.lot = lot
        self.entry = entry
        self.sl_price = sl_price
        self.margin = margin
        self.entry_time = entry_time
        self.hard_exit_time = hard_exit_time
        self.peak_net = 0.0
        self.stepped_floor = 0
        self.opt_candle_map = opt_candle_map
        self.strategy = strategy
        self.candidate = candidate

    def _net_pnl_at(self, price):
        gross = (price - self.entry) * self.lot
        return gross - _calc_charges(self.entry, price, self.lot)

    def _update_floor(self, net_pnl_at_peak):
        if net_pnl_at_peak > self.peak_net:
            self.peak_net = net_pnl_at_peak
        for fl in FLOOR_STEPS:
            if self.peak_net >= fl:
                self.stepped_floor = fl

    def tick(self, current_time):
        """Check this position at current_time using full OHLC for realistic simulation.

        Within each candle we know Open/High/Low/Close. We determine the likely
        intra-candle order of price action:
        - If open is closer to high → price went up first then down (high→low)
        - If open is closer to low  → price went down first then up (low→high)

        This lets us check SL/floor/max-loss at the candle's extreme that came
        FIRST, avoiding impossible scenarios like "peak updated then SL hit" when
        the SL was actually breached before the peak.
        """
        cn = self.opt_candle_map.get(current_time)

        # hard exit check even if no candle at this exact minute
        if self.hard_exit_time and current_time >= self.hard_exit_time:
            if cn:
                ltp = cn["close"]
            else:
                earlier = [t for t in self.opt_candle_map if t < current_time]
                if earlier:
                    ltp = self.opt_candle_map[max(earlier)]["close"]
                else:
                    ltp = self.entry
            return ltp, "TIME", self._net_pnl_at(ltp)

        if cn is None:
            return None

        opn, high, low, close = cn["open"], cn["high"], cn["low"], cn["close"]

        # Determine intra-candle order: did price go up first or down first?
        high_first = (opn - low) >= (high - opn)  # open closer to high → went up first

        if high_first:
            # Sequence: open → high → low → close

            # 1. Price rises to high — update peak & floor
            self._update_floor(self._net_pnl_at(high))

            # 2. Price drops to low — check max loss cap first (PnL-based)
            net_at_low = self._net_pnl_at(low)
            if net_at_low <= -MAX_SL_RS:
                # max loss hit — exit at the price that gives exactly -MAX_SL_RS
                exit_p = self.entry - (MAX_SL_RS + _calc_charges(self.entry, self.entry, self.lot)) / self.lot
                exit_p = max(exit_p, low)  # can't get better than low
                return round(exit_p, 2), "MAX_LOSS", self._net_pnl_at(exit_p)

            # 3. SL check at low
            if low <= self.sl_price:
                return self.sl_price, "SL", self._net_pnl_at(self.sl_price)

            # 4. Floor check — peak was set at high, now price dropped
            if self.stepped_floor > 0 and net_at_low <= self.stepped_floor:
                # Exit at the floor level price (interpolate)
                return low, f"FLOOR ₹{self.stepped_floor}", net_at_low

        else:
            # Sequence: open → low → high → close

            # 1. Price drops to low first — check max loss cap
            net_at_low = self._net_pnl_at(low)
            if net_at_low <= -MAX_SL_RS:
                exit_p = self.entry - (MAX_SL_RS + _calc_charges(self.entry, self.entry, self.lot)) / self.lot
                exit_p = max(exit_p, low)
                return round(exit_p, 2), "MAX_LOSS", self._net_pnl_at(exit_p)

            # 2. SL check at low
            if low <= self.sl_price:
                return self.sl_price, "SL", self._net_pnl_at(self.sl_price)

            # 3. Floor check at low (using existing peak from previous candles)
            if self.stepped_floor > 0 and net_at_low <= self.stepped_floor:
                return low, f"FLOOR ₹{self.stepped_floor}", net_at_low

            # 4. Price rises to high — update peak & floor
            self._update_floor(self._net_pnl_at(high))

        # 5. End of candle — check floor at close (peak may have been set this candle)
        net_at_close = self._net_pnl_at(close)
        if self.stepped_floor > 0 and net_at_close <= self.stepped_floor:
            return close, f"FLOOR ₹{self.stepped_floor}", net_at_close

        return None

    def eod_close(self):
        """Force close at last available candle."""
        last_time = max(self.opt_candle_map.keys()) if self.opt_candle_map else "15:29"
        cn = self.opt_candle_map.get(last_time)
        if cn:
            ltp = cn["close"]
        else:
            ltp = self.entry
        pnl = (ltp - self.entry) * self.lot - _calc_charges(self.entry, ltp, self.lot)
        return ltp, "EOD", pnl


# ---------------------------------------------------------------------------
# Scanners — return candidates visible at current scan time
# ---------------------------------------------------------------------------
# These are called repeatedly at each rescan tick. They use candles available
# up to the current time and return NEW candidates not already in active/done sets.

def scan_oeh_at_tick(candles_5m, universe, tick_time):
    """OEH: at each tick, check the 5-min candle that just closed.
    The candle closing at tick_time covers [tick_time-5min, tick_time).
    We check if open > close by >= OEH_MIN_DROP_PCT and high didn't exceed open+tolerance."""
    candidates = []
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        candles = candles_5m.get(sym)
        if not candles:
            continue
        # find the candle whose time matches tick_time (the just-closed candle)
        for cn in candles:
            ct = _candle_time(cn)
            if ct == tick_time:
                op = cn["open"]
                if op <= 0:
                    break
                mh = cn["high"]
                if mh > op + OEH_TOLERANCE:
                    break
                ep = cn["close"]
                dp = (op - ep) / op * 100
                if dp < OEH_MIN_DROP_PCT:
                    break
                candidates.append({
                    "symbol": sym, "direction": "bearish", "opt_type": "PE",
                    "breakout_price": ep, "entry_after": tick_time, "drop_pct": dp,
                })
                break
    candidates.sort(key=lambda x: x["drop_pct"], reverse=True)
    return candidates


def scan_oel_at_tick(candles_5m, universe, tick_time):
    """OEL: mirror of OEH — open=low (bullish). Buy CE when low didn't breach open-tolerance
    and close rose >= OEL_MIN_RISE_PCT above open."""
    candidates = []
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        candles = candles_5m.get(sym)
        if not candles:
            continue
        for cn in candles:
            ct = _candle_time(cn)
            if ct == tick_time:
                op = cn["open"]
                if op <= 0:
                    break
                ml = cn["low"]
                if ml < op - OEL_TOLERANCE:
                    break
                ep = cn["close"]
                rp = (ep - op) / op * 100
                if rp < OEL_MIN_RISE_PCT:
                    break
                candidates.append({
                    "symbol": sym, "direction": "bullish", "opt_type": "CE",
                    "breakout_price": ep, "entry_after": tick_time, "rise_pct": rp,
                })
                break
    candidates.sort(key=lambda x: x["rise_pct"], reverse=True)
    return candidates


def scan_orb_at_tick(candles_5m, universe, tick_time):
    """ORB: check if any symbol has a new breakout in the candle at tick_time."""
    candidates = []
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        candles = candles_5m.get(sym)
        if not candles or len(candles) < 2:
            continue
        # opening range = first 5-min candle
        rh = candles[0]["high"]
        rl = candles[0]["low"]
        ro = candles[0]["open"]
        if ro <= 0:
            continue
        rp = (rh - rl) / ro * 100
        if rp < ORB_MIN_RANGE_PCT or rp > ORB_MAX_RANGE_PCT:
            continue
        # check the candle at tick_time for breakout
        for cn in candles[1:]:
            ct = _candle_time(cn)
            if ct != tick_time:
                continue
            if cn["high"] > rh:
                candidates.append({
                    "symbol": sym, "direction": "bullish", "opt_type": "CE",
                    "breakout_price": cn["close"], "entry_after": tick_time, "range_pct": rp,
                })
            elif cn["low"] < rl:
                candidates.append({
                    "symbol": sym, "direction": "bearish", "opt_type": "PE",
                    "breakout_price": cn["close"], "entry_after": tick_time, "range_pct": rp,
                })
            break
    candidates.sort(key=lambda x: x.get("range_pct", 0), reverse=True)
    return candidates


def scan_pdhl_at_tick(candles_5m, prev_day_hl, universe, tick_time):
    """PDHL: at each tick, check if the current candle breaches prev day high/low."""
    candidates = []
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        candles = candles_5m.get(sym)
        if not candles:
            continue
        hl = prev_day_hl.get(sym)
        if not hl:
            continue
        pdh, pdl = hl
        for cn in candles:
            ct = _candle_time(cn)
            if ct != tick_time:
                continue
            if cn["high"] > pdh:
                candidates.append({
                    "symbol": sym, "direction": "bullish", "opt_type": "CE",
                    "breakout_price": cn["close"], "entry_after": tick_time,
                })
            elif cn["low"] < pdl:
                candidates.append({
                    "symbol": sym, "direction": "bearish", "opt_type": "PE",
                    "breakout_price": cn["close"], "entry_after": tick_time,
                })
            break
    return candidates


def scan_orf_at_tick(candles_1m, universe, tick_time):
    """ORF: at each tick, check for new faded breakouts visible by now."""
    candidates = []
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        candles = candles_1m.get(sym)
        if not candles or len(candles) < 20:
            continue

        or_candles = [c for c in candles if _candle_time(c) < "09:20"]
        if len(or_candles) < 3:
            continue

        rh = max(c["high"] for c in or_candles)
        rl = min(c["low"] for c in or_candles)
        ro = or_candles[0]["open"]
        if ro <= 0 or rh <= rl:
            continue
        rp = (rh - rl) / ro * 100
        if rp < ORF_MIN_RANGE_PCT or rp > ORF_MAX_RANGE_PCT:
            continue

        breakout_min_up = rh * (1 + ORF_BREAKOUT_MIN_PCT / 100)
        breakout_min_dn = rl * (1 - ORF_BREAKOUT_MIN_PCT / 100)

        post_or = [c for c in candles if "09:20" <= _candle_time(c) <= tick_time]

        breakout_type = None
        breakout_idx = None

        for i, cn in enumerate(post_or):
            if cn["high"] >= breakout_min_up:
                breakout_type = "up"
                breakout_idx = i
                break
            elif cn["low"] <= breakout_min_dn:
                breakout_type = "down"
                breakout_idx = i
                break

        if breakout_type is None:
            continue

        for j in range(breakout_idx + 1, min(breakout_idx + 1 + ORF_FADE_WINDOW, len(post_or))):
            cn = post_or[j]
            fade_time = _candle_time(cn)
            # only count fades that happened at the current tick (so we don't re-discover old fades)
            if fade_time != tick_time:
                continue
            if breakout_type == "up" and cn["close"] < rh:
                candidates.append({
                    "symbol": sym, "direction": "bearish", "opt_type": "PE",
                    "breakout_price": cn["close"], "entry_after": tick_time, "range_pct": rp,
                })
                break
            elif breakout_type == "down" and cn["close"] > rl:
                candidates.append({
                    "symbol": sym, "direction": "bullish", "opt_type": "CE",
                    "breakout_price": cn["close"], "entry_after": tick_time, "range_pct": rp,
                })
                break

    candidates.sort(key=lambda x: x.get("entry_after", ""))
    return candidates


def scan_aft_at_tick(candles_5m, universe, tick_time):
    """AFT: at each tick after 13:30, check for lunch range breakout with volume."""
    candidates = []
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        candles = candles_5m.get(sym)
        if not candles or len(candles) < 30:
            continue

        lunch_high = 0
        lunch_low = float("inf")
        lunch_vols = []

        for cn in candles:
            t = _candle_time(cn)
            if t < AFT_RANGE_START:
                continue
            if t >= AFT_RANGE_END:
                break
            lunch_high = max(lunch_high, cn["high"])
            lunch_low = min(lunch_low, cn["low"])
            lunch_vols.append(cn.get("volume", 0) or 1)

        if not lunch_vols or lunch_high <= lunch_low:
            continue
        lunch_range_pct = (lunch_high - lunch_low) / lunch_low * 100
        if lunch_range_pct < AFT_MIN_RANGE_PCT:
            continue

        avg_lunch_vol = sum(lunch_vols) / len(lunch_vols)
        if avg_lunch_vol <= 0:
            avg_lunch_vol = 1

        for cn in candles:
            t = _candle_time(cn)
            if t != tick_time:
                continue
            vol = cn.get("volume", 0) or 0
            if cn["close"] > lunch_high and vol >= avg_lunch_vol * AFT_VOL_MULT:
                candidates.append({
                    "symbol": sym, "direction": "bullish", "opt_type": "CE",
                    "breakout_price": cn["close"], "entry_after": tick_time,
                    "vol_ratio": vol / avg_lunch_vol,
                })
            elif cn["close"] < lunch_low and vol >= avg_lunch_vol * AFT_VOL_MULT:
                candidates.append({
                    "symbol": sym, "direction": "bearish", "opt_type": "PE",
                    "breakout_price": cn["close"], "entry_after": tick_time,
                    "vol_ratio": vol / avg_lunch_vol,
                })
            break

    candidates.sort(key=lambda x: x.get("vol_ratio", 0), reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Time-stepped simulation for one strategy
# ---------------------------------------------------------------------------
def _run_strategy_sim(strategy_name, ref_date, ud, opt_master, lot_sizes,
                      candles_eq, scan_fn, loss_cap, profit_cap,
                      scan_start, scan_end, scan_interval_min,
                      hard_exit_time, verbose, candles_1m=None,
                      prefetched_opt_cache=None, prefetched_resolved=None):
    """Run a single strategy through the day using time-stepped simulation.

    scan_fn(tick_time) -> list of candidates at that tick.
    """
    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    full_to = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    active_positions: list[Position] = []
    avail_capital = CAPITAL
    realized_pnl = 0.0
    results = []
    active_syms: set[tuple[str, str]] = set()
    sym_cooldown: dict[tuple[str, str], str] = {}
    cap_stopped = False

    if verbose:
        print(f"\n  {'Symbol':<14s} {'Str':>6s} {'D':>1s} {'Entry':>7s} {'Exit':>7s}"
              f" {'P&L':>9s} {'Peak':>8s} {'Reason':<12s} {'ETime':>5s} {'XTime':>5s}")
        print(f"  {'-'*95}")

    # Use prefetched cache if available, fall back to lazy fetch
    _opt_candle_cache: dict[str, dict[str, dict]] = dict(prefetched_opt_cache) if prefetched_opt_cache else {}
    _opt_resolve_cache: dict[tuple, tuple] = {}
    # Seed resolve cache from prefetched resolved data
    if prefetched_resolved:
        for cache_key, (opt_key, strike, lot) in prefetched_resolved.items():
            parts = cache_key.rsplit("_", 2)
            if len(parts) == 3:
                sym, strike_str, ot = parts
                try:
                    _opt_resolve_cache[(sym, float(strike_str), ot)] = (opt_key, strike, lot)
                except ValueError:
                    pass

    def _get_opt_candles(sym, spot, opt_type):
        """Resolve option + fetch/cache 1-min candles, return (strike, lot, candle_map) or None."""
        cache_key_resolve = (sym, round(spot, 1), opt_type)
        if cache_key_resolve in _opt_resolve_cache:
            opt_key, strike, lot = _opt_resolve_cache[cache_key_resolve]
        else:
            opt_key, strike, lot = _resolve_option(sym, spot, opt_type, ref_date, opt_master, lot_sizes)
            _opt_resolve_cache[cache_key_resolve] = (opt_key, strike, lot)

        if not opt_key:
            return None

        candle_cache_key = f"{sym}_{strike}_{opt_type}"
        if candle_cache_key not in _opt_candle_cache:
            ocandles = _cached_fetch(ud, opt_key, from_dt, full_to, "1minute")
            if not ocandles or len(ocandles) < 5:
                _opt_candle_cache[candle_cache_key] = {}
                return None
            cmap = {}
            for cn in ocandles:
                cmap[_candle_time(cn)] = cn
            _opt_candle_cache[candle_cache_key] = cmap

        cmap = _opt_candle_cache[candle_cache_key]
        if not cmap:
            return None

        # re-lookup strike/lot from cache
        opt_key, strike, lot = _opt_resolve_cache[cache_key_resolve]
        return strike, lot, cmap

    def _try_enter(candidate):
        nonlocal avail_capital
        sym = candidate["symbol"]
        opt_type = candidate.get("opt_type", "PE")
        spot = candidate["breakout_price"]
        entry_time = candidate.get("entry_after", "09:20")

        result = _get_opt_candles(sym, spot, opt_type)
        if result is None:
            return False
        strike, lot, cmap = result

        # get entry price from the candle at entry_time
        entry_cn = cmap.get(entry_time)
        if not entry_cn:
            return False
        entry_price = entry_cn["close"]
        if entry_price <= 0 or entry_price < MIN_PREMIUM:
            return False

        margin = entry_price * lot
        if margin > avail_capital:
            return False

        sl_pct_price = entry_price * (1 - SL_PCT)
        sl_cap_price = entry_price - (MAX_SL_RS / lot)
        sl_price = round(max(sl_pct_price, sl_cap_price), 2)

        pos = Position(
            sym=sym, opt_type=opt_type, strike=strike, lot=lot,
            entry=entry_price, sl_price=sl_price, margin=margin,
            entry_time=entry_time, hard_exit_time=hard_exit_time,
            opt_candle_map=cmap, strategy=strategy_name, candidate=candidate,
        )
        active_positions.append(pos)
        active_syms.add((sym, opt_type))
        avail_capital -= margin
        return True

    def _close_position(pos, exit_price, exit_reason, pnl, exit_time):
        nonlocal avail_capital, realized_pnl
        avail_capital += pos.margin
        realized_pnl += pnl
        active_syms.discard((pos.sym, pos.opt_type))
        # set cooldown: can't re-enter this symbol for SYM_COOLDOWN_MIN minutes
        eh, em = _hhmm_to_tuple(exit_time)
        em += SYM_COOLDOWN_MIN
        while em >= 60:
            em -= 60
            eh += 1
        sym_cooldown[(pos.sym, pos.opt_type)] = _tick_hhmm(eh, em)

        results.append({
            "sym": pos.sym, "pnl": pnl, "reason": exit_reason,
            "peak": pos.peak_net, "date": ref_date,
            "entry": pos.entry, "exit_price": exit_price,
            "entry_time": pos.entry_time, "exit_time": exit_time,
            "strike": pos.strike, "opt_type": pos.opt_type,
            "lot": pos.lot, "direction": pos.candidate.get("direction", "bearish"),
        })

        if verbose:
            d_tag = "▲" if pos.candidate.get("direction", "bearish") == "bullish" else "▼"
            print(f"  {d_tag} {pos.sym:<12s} {pos.strike:>6.0f}{pos.opt_type} {pos.entry:>7.1f} → {exit_price:>6.1f}"
                  f"  ₹{pnl:>+8,.0f}  ₹{pos.peak_net:>+7,.0f} {exit_reason:<12s} {pos.entry_time:>5s} {exit_time:>5s}")

    # Build scan ticks
    sh, sm = _hhmm_to_tuple(scan_start)
    eh, em = _hhmm_to_tuple(scan_end)
    scan_ticks = set()
    h, m = sh, sm
    while _tick_hhmm(h, m) <= scan_end:
        scan_ticks.add(_tick_hhmm(h, m))
        m += scan_interval_min
        while m >= 60:
            m -= 60
            h += 1
        if h > eh or (h == eh and m > em):
            break

    # Main time loop: 1-min ticks from 09:16 to 15:29
    for hh in range(9, 16):
        mm_start = 16 if hh == 9 else 0
        mm_end = 30 if hh == 15 else 60
        for mm in range(mm_start, mm_end):
            current_time = _tick_hhmm(hh, mm)

            # 1. Tick all active positions — check for exits
            closed_this_tick = []
            for pos in active_positions:
                result = pos.tick(current_time)
                if result:
                    exit_price, exit_reason, pnl = result
                    closed_this_tick.append((pos, exit_price, exit_reason, pnl, current_time))

            for pos, exit_price, exit_reason, pnl, etime in closed_this_tick:
                active_positions.remove(pos)
                _close_position(pos, exit_price, exit_reason, pnl, etime)

                if realized_pnl >= profit_cap or realized_pnl <= -loss_cap:
                    cap_stopped = True

            # If all positions closed, no point continuing
            if cap_stopped and not active_positions:
                break

            # 2. At scan ticks, run scanner and try to enter new trades
            if current_time in scan_ticks:
                new_candidates = scan_fn(current_time)
                entries_this_tick = 0
                for cand in new_candidates:
                    if cap_stopped:
                        break
                    if entries_this_tick >= MAX_ENTRIES_PER_SCAN:
                        break
                    if len(active_positions) >= MAX_CONCURRENT:
                        break
                    sym = cand["symbol"]
                    ot = cand.get("opt_type", "PE")
                    if (sym, ot) in active_syms:
                        continue
                    cooldown_until = sym_cooldown.get((sym, ot))
                    if cooldown_until and current_time < cooldown_until:
                        continue
                    if avail_capital < 5000:
                        continue
                    if _try_enter(cand):
                        entries_this_tick += 1
                        if realized_pnl >= profit_cap or realized_pnl <= -loss_cap:
                            cap_stopped = True

        if cap_stopped and not active_positions:
            break

    # EOD: close remaining positions
    for pos in list(active_positions):
        exit_price, exit_reason, pnl = pos.eod_close()
        active_positions.remove(pos)
        _close_position(pos, exit_price, exit_reason, pnl, "15:29")

    if results:
        day_pnl = sum(r["pnl"] for r in results)
        wins = sum(1 for r in results if r["pnl"] > 0)
        losses = len(results) - wins
        extra = ""
        if cap_stopped:
            extra = f" | CAP HIT (₹{realized_pnl:>+,.0f})"
        print(f"  {strategy_name} TOTAL: {len(results)} trades | {wins}W/{losses}L{extra} | ₹{day_pnl:>+,.0f}")
    else:
        print(f"  {strategy_name}: No trades")

    return results


# ---------------------------------------------------------------------------
# Fetch prev day high/low for PDHL
# ---------------------------------------------------------------------------
def _fetch_prev_day_hl(ud, eq_keys, universe, ref_date):
    prev_date = _prev_trading_day(ref_date)
    prev_from = datetime.combine(prev_date, datetime.min.time()).replace(hour=9, minute=15)
    prev_to = datetime.combine(prev_date, datetime.min.time()).replace(hour=15, minute=30)

    prev_jobs = []
    for sym in universe:
        if sym in BLOCKLIST:
            continue
        inst_key = eq_keys.get(sym)
        if inst_key:
            prev_jobs.append((sym, inst_key, prev_from, prev_to, "day"))
    prev_candles = _parallel_fetch(ud, prev_jobs)

    prev_day_hl = {}
    for sym, candles in prev_candles.items():
        if candles:
            pdh = max(c["high"] for c in candles)
            pdl = min(c["low"] for c in candles)
            if pdh > 0 and pdl > 0:
                prev_day_hl[sym] = (pdh, pdl)
    return prev_day_hl


# ---------------------------------------------------------------------------
# Run one day
# ---------------------------------------------------------------------------
def run_day(ud, ref_date, eq_keys, universe, opt_master, lot_sizes, verbose=True, enabled=None):
    print(f"\n{'#'*90}")
    print(f"  {ref_date} ({ref_date.strftime('%A')})")
    print(f"{'#'*90}")

    from_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(ref_date, datetime.min.time()).replace(hour=15, minute=30)

    # Prefetch equity candles
    t0 = _t.time()
    candles_5m = _prefetch_equity_candles(ud, eq_keys, universe, from_dt, to_dt, "5minute")
    fetch_time = _t.time() - t0
    print(f"  Fetched {len(candles_5m)} stocks in {fetch_time:.1f}s")

    day_results = {}
    _en = lambda s: enabled is None or s in enabled

    # Prefetch ALL option candles for the day (ATM CE+PE for every stock)
    t1 = _t.time()
    opt_cache, opt_resolved = _prefetch_option_candles(
        ud, candles_5m, universe, opt_master, lot_sizes, ref_date, from_dt, to_dt)
    print(f"  Prefetched {len(opt_cache)} option series in {_t.time()-t1:.1f}s")

    # Fetch prev day data for PDHL
    prev_day_hl = _fetch_prev_day_hl(ud, eq_keys, universe, ref_date) if _en("PDHL") else {}

    # Build jobs for enabled strategies — run them in parallel
    _strat_jobs = []

    if _en("OEH"):
        _strat_jobs.append(("OEH", dict(
            scan_fn=lambda tick: scan_oeh_at_tick(candles_5m, universe, tick),
            loss_cap=OEH_LOSS_CAP, profit_cap=OEH_PROFIT_CAP,
            scan_start=OEH_SCAN_START, scan_end=OEH_SCAN_END,
            scan_interval_min=5, hard_exit_time=None,
        )))
    if _en("OEL"):
        _strat_jobs.append(("OEL", dict(
            scan_fn=lambda tick: scan_oel_at_tick(candles_5m, universe, tick),
            loss_cap=OEL_LOSS_CAP, profit_cap=OEL_PROFIT_CAP,
            scan_start=OEL_SCAN_START, scan_end=OEL_SCAN_END,
            scan_interval_min=5, hard_exit_time=None,
        )))
    if _en("ORB"):
        _strat_jobs.append(("ORB", dict(
            scan_fn=lambda tick: scan_orb_at_tick(candles_5m, universe, tick),
            loss_cap=ORB_LOSS_CAP, profit_cap=ORB_PROFIT_CAP,
            scan_start=ORB_SCAN_START, scan_end=ORB_SCAN_END,
            scan_interval_min=5, hard_exit_time=None,
        )))

    # Run strategies in parallel (each has its own capital pool + candle cache)
    if _strat_jobs:
        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
        import io as _io, sys as _sys

        def _run_one(name_kwargs):
            name, kwargs = name_kwargs
            buf = _io.StringIO()
            old_stdout = _sys.stdout
            _sys.stdout = buf
            print(f"\n  --- {name} ---")
            res = _run_strategy_sim(
                name, ref_date, ud, opt_master, lot_sizes, candles_5m,
                verbose=verbose,
                prefetched_opt_cache=opt_cache, prefetched_resolved=opt_resolved,
                **kwargs,
            )
            _sys.stdout = old_stdout
            return name, res, buf.getvalue()

        with _TPE(max_workers=len(_strat_jobs)) as _ex:
            futs = {_ex.submit(_run_one, j): j[0] for j in _strat_jobs}
            for fut in _ac(futs):
                name, res, output = fut.result()
                print(output, end="")
                day_results[name] = res

    for label in ["OEH", "OEL", "ORB"]:
        if label not in day_results:
            day_results[label] = []

    # 3. PDHL
    if not _en("PDHL"):
        day_results["PDHL"] = []
    else:
        print(f"\n  --- PDHL ---")
        day_results["PDHL"] = _run_strategy_sim(
            "PDHL", ref_date, ud, opt_master, lot_sizes, candles_5m,
            scan_fn=lambda tick: scan_pdhl_at_tick(candles_5m, prev_day_hl, universe, tick),
            loss_cap=PDHL_LOSS_CAP, profit_cap=PDHL_PROFIT_CAP,
            scan_start=PDHL_SCAN_START, scan_end=PDHL_SCAN_END,
            scan_interval_min=5, hard_exit_time=None, verbose=verbose,
            prefetched_opt_cache=opt_cache, prefetched_resolved=opt_resolved,
        )

    # 4. ORF
    if not _en("ORF"):
        day_results["ORF"] = []
    else:
        candles_1m = _prefetch_equity_candles(ud, eq_keys, universe, from_dt,
                                              datetime.combine(ref_date, datetime.min.time()).replace(hour=11, minute=35),
                                              "1minute")
        print(f"\n  --- ORF ---")
        day_results["ORF"] = _run_strategy_sim(
            "ORF", ref_date, ud, opt_master, lot_sizes, candles_1m,
            scan_fn=lambda tick: scan_orf_at_tick(candles_1m, universe, tick),
            loss_cap=ORF_LOSS_CAP, profit_cap=ORF_PROFIT_CAP,
            scan_start=ORF_SCAN_START, scan_end=ORF_SCAN_END,
            scan_interval_min=5, hard_exit_time=ORF_TIME_EXIT, verbose=verbose,
            prefetched_opt_cache=opt_cache, prefetched_resolved=opt_resolved,
        )

    # 5. AFT
    if not _en("AFT"):
        day_results["AFT"] = []
    else:
        print(f"\n  --- AFT ---")
        day_results["AFT"] = _run_strategy_sim(
            "AFT", ref_date, ud, opt_master, lot_sizes, candles_5m,
            scan_fn=lambda tick: scan_aft_at_tick(candles_5m, universe, tick),
            loss_cap=AFT_LOSS_CAP, profit_cap=AFT_PROFIT_CAP,
            scan_start=AFT_SCAN_START, scan_end=AFT_SCAN_END,
            scan_interval_min=5, hard_exit_time=AFT_HARD_EXIT, verbose=verbose,
            prefetched_opt_cache=opt_cache, prefetched_resolved=opt_resolved,
        )

    # Day summary
    print(f"\n  {'─'*60}")
    grand = 0
    for label in ["OEH", "OEL", "ORB", "PDHL", "ORF", "AFT"]:
        r = day_results[label]
        if not r:
            print(f"  {label:5s}: --")
            continue
        pnl = sum(x["pnl"] for x in r)
        w = sum(1 for x in r if x["pnl"] > 0)
        l = len(r) - w
        grand += pnl
        print(f"  {label:5s}: {len(r):3d}t {w}W/{l}L ₹{pnl:>+,.0f}")
    print(f"  {'DAY':5s}: ₹{grand:>+,.0f}")

    return day_results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _trading_days(year, month):
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
    parser.add_argument("--from-date", help="Start date YYYY-MM-DD (use with --to-date)")
    parser.add_argument("--to-date", help="End date YYYY-MM-DD (use with --from-date)")
    parser.add_argument("--month", help="Full month YYYY-MM")
    parser.add_argument("--quiet", action="store_true", help="Only show summaries")
    parser.add_argument("--report", type=str, help="Write report to file")
    parser.add_argument("--strategies", type=str, help="Comma-separated strategies to run (e.g. OEH,OEL)")
    args = parser.parse_args()
    enabled_strategies = set(args.strategies.upper().split(",")) if args.strategies else None

    if args.from_date and args.to_date:
        start = date.fromisoformat(args.from_date)
        end = date.fromisoformat(args.to_date)
        dates = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                dates.append(d)
            d += timedelta(days=1)
    elif args.date:
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
    _build_opt_index(opt_master)

    verbose = not args.quiet

    print(f"\n  ALL 5 STRATEGIES BACKTEST (time-stepped simulation)")
    print(f"  Capital: ₹{CAPITAL:,}/strategy | Lots: {LOT_MULT} | SL: {SL_PCT*100:.0f}% / ₹{MAX_SL_RS:,}")
    print(f"  Max concurrent: {MAX_CONCURRENT} | Cooldown: {SYM_COOLDOWN_MIN}min | Max/scan: {MAX_ENTRIES_PER_SCAN}")
    print(f"  Universe: {len(universe)} stocks | Days: {len(dates)}")

    all_results = {"OEH": [], "OEL": [], "ORB": [], "PDHL": [], "ORF": [], "AFT": []}

    for ref_date in dates:
        day_results = run_day(ud, ref_date, eq_keys, universe, opt_master, lot_sizes, verbose, enabled=enabled_strategies)
        for k in all_results:
            all_results[k].extend(day_results.get(k, []))

    # Summary
    print(f"\n{'='*90}")
    print(f"  SUMMARY — {len(dates)} trading days")
    print(f"{'='*90}")

    grand = 0
    summary_lines = []
    for label in ["OEH", "OEL", "ORB", "PDHL", "ORF", "AFT"]:
        r = all_results[label]
        if not r:
            line = f"  {label:8s}: No trades"
        else:
            pnl = sum(x["pnl"] for x in r)
            wins = sum(1 for x in r if x["pnl"] > 0)
            losses = len(r) - wins
            wr = wins / len(r) * 100
            avg_w = sum(x["pnl"] for x in r if x["pnl"] > 0) / max(wins, 1)
            avg_l = sum(x["pnl"] for x in r if x["pnl"] <= 0) / max(losses, 1)
            grand += pnl
            line = (f"  {label:8s}: {len(r):4d} trades | {wins}W/{losses}L ({wr:.0f}%) | "
                    f"Avg W: ₹{avg_w:>+,.0f} | Avg L: ₹{avg_l:>+,.0f} | ₹{pnl:>+,.0f}")
        print(line)
        summary_lines.append(line)
    total_line = f"  {'TOTAL':8s}: ₹{grand:>+,.0f}"
    print(total_line)
    summary_lines.append(total_line)
    print()

    if args.report:
        report_path = Path(args.report)
        with open(report_path, "w") as f:
            f.write(f"BACKTEST REPORT — {len(dates)} trading days\n")
            f.write(f"Capital: ₹{CAPITAL:,}/strategy | Lots: {LOT_MULT} | SL: {SL_PCT*100:.0f}% / ₹{MAX_SL_RS:,}\n")
            f.write(f"{'='*90}\n\n")
            for ref_date in dates:
                day_res = {}
                for label in ["OEH", "OEL", "ORB", "PDHL", "ORF", "AFT"]:
                    day_trades = [x for x in all_results[label] if x.get("date") == ref_date]
                    day_res[label] = day_trades
                f.write(f"  {ref_date} ({ref_date.strftime('%A')})\n")
                f.write(f"  {'-'*60}\n")
                day_grand = 0
                for label in ["OEH", "OEL", "ORB", "PDHL", "ORF", "AFT"]:
                    r = day_res[label]
                    if not r:
                        f.write(f"  {label:5s}: --\n")
                        continue
                    pnl = sum(x["pnl"] for x in r)
                    w = sum(1 for x in r if x["pnl"] > 0)
                    l = len(r) - w
                    day_grand += pnl
                    f.write(f"  {label:5s}: {len(r):3d}t {w}W/{l}L ₹{pnl:>+,.0f}\n")
                f.write(f"  {'DAY':5s}: ₹{day_grand:>+,.0f}\n\n")
            f.write(f"{'='*90}\n")
            f.write(f"OVERALL SUMMARY\n")
            f.write(f"{'='*90}\n")
            for line in summary_lines:
                f.write(line + "\n")
        print(f"  Report saved to {report_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
OEH (Open=High) backtest: compare CURRENT params vs IMPROVED params.

Current: entry at 09:25, no VWAP filter, SL=30%, max 3 entries/sym, floor ₹500 step
Improved: entry at 09:45, VWAP filter (price < VWAP), SL=20%, max 1 entry/sym,
          floor ₹1000 start ₹1500 step, max 2 rescans total

Usage:
    PYTHONPATH=. .venv/bin/python3 -u scripts/backtest_oeh_compare.py \
        --from 2026-10-01 --to 2026-10-16
"""

import asyncio, sys, os, argparse, time as _time
from datetime import datetime, timedelta, time as dtime, date as dt_date
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo(config.TIMEZONE)

CAPITAL = 300000
SLIPPAGE_PCT = 0.005
OEH_TOLERANCE = 0.05
OEH_MIN_DROP_PCT = 0.3

# ── Two parameter sets to compare ──
CONFIGS = {
    "CURRENT": {
        "scan_after": dtime(9, 25),     # entry after first 5-min candle
        "vwap_filter": False,
        "sl_pct": 0.30,
        "max_sl": 5000,
        "max_entries_per_sym": 3,
        "sl_cooldown": True,
        "floor_levels": [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000],
        "time_cutoff": dtime(13, 0),
        "max_rescans": 99,              # effectively unlimited
    },
    "IMPROVED": {
        "scan_after": dtime(9, 45),     # wait 30 min for confirmation
        "vwap_filter": True,            # only enter if price < VWAP (bearish)
        "sl_pct": 0.20,                 # tighter SL
        "max_sl": 3000,                 # tighter cap
        "max_entries_per_sym": 1,       # no re-entries
        "sl_cooldown": True,
        "floor_levels": [1000, 2500, 4000, 5500, 7000, 8500, 10000],  # higher start, ₹1500 step
        "time_cutoff": dtime(13, 0),
        "max_rescans": 2,               # max 2 total rescan rounds
    },
}


def calc_charges(entry_price, exit_price, qty):
    buy_tv = entry_price * qty
    sell_tv = exit_price * qty
    total_tv = buy_tv + sell_tv
    brokerage = 40.0
    stt = sell_tv * 0.001
    exchange_txn = total_tv * 0.000495
    sebi = total_tv * 0.000001
    stamp = buy_tv * 0.00003
    gst = (brokerage + exchange_txn) * 0.18
    return brokerage + stt + exchange_txn + sebi + stamp + gst


def get_stepped_floor(net_pnl, floor_levels):
    locked = 0
    for lvl in floor_levels:
        if net_pnl >= lvl:
            locked = lvl
        else:
            break
    return locked


def parse_ts(c):
    ts = c.get("ts") or c.get("date") or c.get("timestamp")
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if hasattr(ts, "astimezone"):
        ts = ts.astimezone(IST)
    return ts.replace(tzinfo=None) if ts.tzinfo else ts


def build_5min(candles_1m):
    bars = []
    bucket = None
    for c in candles_1m:
        ct = parse_ts(c)
        bs = ct.replace(minute=(ct.minute // 5) * 5, second=0, microsecond=0)
        if bucket is None or bucket["time"] != bs:
            if bucket:
                bars.append(bucket)
            bucket = {"time": bs, "open": c["open"], "high": c["high"],
                      "low": c["low"], "close": c["close"], "volume": c.get("volume", 0) or 0}
        else:
            bucket["high"] = max(bucket["high"], c["high"])
            bucket["low"] = min(bucket["low"], c["low"])
            bucket["close"] = c["close"]
            bucket["volume"] += c.get("volume", 0) or 0
    if bucket:
        bars.append(bucket)
    return bars


def calc_vwap_at(candles_1m, up_to_time):
    cv, ctv = 0, 0
    for c in candles_1m:
        ct = parse_ts(c)
        if ct.time() > up_to_time:
            break
        tp = (c["high"] + c["low"] + c["close"]) / 3
        v = c.get("volume", 0) or 1
        cv += v
        ctv += tp * v
    return ctv / cv if cv > 0 else None


async def _fetch_one(ud, sym, key, from_dt, to_dt):
    try:
        c = await asyncio.wait_for(
            asyncio.to_thread(ud.historical_data, key, from_dt, to_dt, "1minute"),
            timeout=15
        )
        if c and len(c) > 5:
            return (sym, c)
    except Exception:
        pass
    return None


async def _fetch_batch(ud, pairs, from_dt, to_dt, concurrency=15, label=""):
    sem = asyncio.Semaphore(concurrency)
    done = 0
    total = len(pairs)
    async def _wrapped(sym, key):
        nonlocal done
        async with sem:
            r = await _fetch_one(ud, sym, key, from_dt, to_dt)
            done += 1
            if done % 50 == 0 or done == total:
                sys.stdout.write(f"  [{label}] {done}/{total}\n")
                sys.stdout.flush()
            return r
    tasks = [_wrapped(sym, key) for sym, key in pairs]
    results = await asyncio.gather(*tasks)
    return {sym: c for r in results if r for sym, c in [r]}


def find_oeh_candidates(equity_candles, cfg, DATE):
    """Find OEH candidates: open=high stocks with drop from open."""
    scan_time = cfg["scan_after"]
    candidates = []

    for sym, candles_1m in equity_candles.items():
        if not candles_1m or len(candles_1m) < 5:
            continue

        # Build 5-min bars up to scan time
        bars5 = build_5min(candles_1m)
        if not bars5:
            continue

        open_price = bars5[0]["open"]
        if open_price <= 0:
            continue

        # Check open=high up to scan_time (all 5-min bars so far)
        max_high = 0
        latest_close = open_price
        for b in bars5:
            if b["time"].time() > scan_time:
                break
            max_high = max(max_high, b["high"])
            latest_close = b["close"]

        if max_high > open_price + OEH_TOLERANCE:
            continue

        drop_pct = (open_price - latest_close) / open_price * 100
        if drop_pct < OEH_MIN_DROP_PCT:
            continue

        # VWAP filter: only enter if current price < VWAP (bearish)
        if cfg["vwap_filter"]:
            vwap = calc_vwap_at(candles_1m, scan_time)
            if vwap is None or latest_close >= vwap:
                continue

        candidates.append({
            "symbol": sym,
            "open": open_price,
            "entry_close": latest_close,
            "drop_pct": drop_pct,
            "entry_time": scan_time,
        })

    candidates.sort(key=lambda x: x["drop_pct"], reverse=True)
    return candidates


def sim_oeh_trade(candles, entry_time, entry_price, qty, sl_price, floor_levels, DATE):
    """Simulate one OEH trade through 1-min option candles."""
    ep_slip = entry_price * (1 + SLIPPAGE_PCT)
    peak_net = 0
    floor_val = 0
    first = True

    after = []
    for c in candles:
        ct = parse_ts(c)
        if ct >= datetime.combine(DATE, entry_time):
            after.append(c)

    if not after:
        return None

    for c in after:
        low = c["low"]
        high = c["high"]
        close = c["close"]

        # SL hit — exit at candle close with slippage
        if low <= sl_price:
            xp = close * (1 - SLIPPAGE_PCT)
            gross = (xp - ep_slip) * qty
            charges = calc_charges(ep_slip, xp, qty)
            return {"pnl": round(gross - charges), "xp": xp, "reason": "SL",
                    "xt": parse_ts(c), "qty": qty}

        # Floor exit
        if not first and floor_val > 0:
            close_gross = (close - ep_slip) * qty
            close_charges = calc_charges(ep_slip, close, qty)
            close_net = close_gross - close_charges
            if close_net <= floor_val:
                xp = close * (1 - SLIPPAGE_PCT)
                gross = (xp - ep_slip) * qty
                charges = calc_charges(ep_slip, xp, qty)
                return {"pnl": round(gross - charges), "xp": xp, "reason": "FLOOR",
                        "xt": parse_ts(c), "qty": qty}

        # Peak uses NET pnl
        high_gross = (high - ep_slip) * qty
        high_charges = calc_charges(ep_slip, high, qty)
        high_net = high_gross - high_charges
        peak_net = max(peak_net, high_net)
        nf = get_stepped_floor(peak_net, floor_levels)
        if nf > floor_val:
            floor_val = nf
        first = False

    # EOD
    last = after[-1]
    xp = last["close"] * (1 - SLIPPAGE_PCT)
    gross = (xp - ep_slip) * qty
    charges = calc_charges(ep_slip, xp, qty)
    return {"pnl": round(gross - charges), "xp": xp, "reason": "EOD",
            "xt": parse_ts(last), "qty": qty}


async def run_one_day(ud, DATE, universe, eq_keys, lot_sizes, option_keys, strike_steps):
    from_dt = datetime.combine(DATE, dtime(9, 15))
    to_dt = datetime.combine(DATE, dtime(15, 30))

    # Fetch equity 1-min candles
    syms_with_keys = [(s, eq_keys[s]) for s in universe if s in eq_keys]
    equity_candles = await _fetch_batch(ud, syms_with_keys, from_dt, to_dt, concurrency=15, label="EQ")

    if len(equity_candles) < 50:
        return None  # holiday

    def find_atm(sym, ltp, otype):
        step = strike_steps.get(sym, 50)
        atm = round(ltp / step) * step
        strikes = option_keys.get(sym, {})
        if not strikes:
            return None
        for off in [0, step, -step, 2*step, -2*step]:
            s = atm + off
            if s in strikes and otype in strikes[s]:
                return s
        valid = [s for s in strikes if otype in strikes[s]]
        return min(valid, key=lambda s: abs(s - ltp)) if valid else None

    results = {}

    for cfg_name, cfg in CONFIGS.items():
        candidates = find_oeh_candidates(equity_candles, cfg, DATE)
        sys.stdout.write(f"  [{cfg_name}] {len(candidates)} OEH candidates\n")
        sys.stdout.flush()

        # Collect needed PE options
        needed = set()
        for c in candidates[:30]:  # limit to top 30 by drop%
            sym = c["symbol"]
            strike = find_atm(sym, c["entry_close"], "PE")
            if strike is not None:
                needed.add((sym, strike))

        # Fetch option candles
        opt_pairs = []
        opt_map = {}
        for sym, strike in needed:
            info = option_keys.get(sym, {}).get(strike, {}).get("PE")
            if info:
                label = f"{sym}_{strike}_PE"
                opt_pairs.append((label, info["key"]))
                opt_map[label] = (sym, strike)

        sys.stdout.write(f"  [{cfg_name}] fetching {len(opt_pairs)} PE options\n")
        sys.stdout.flush()
        raw = await _fetch_batch(ud, opt_pairs, from_dt, to_dt, concurrency=15, label=f"{cfg_name}-OPT")
        opt_candles = {}
        for label, candles in raw.items():
            key = opt_map.get(label)
            if key:
                opt_candles[key] = candles

        # Simulate trades with capital tracking
        trades = []
        cap = CAPITAL
        sym_entry_count = defaultdict(int)
        sl_blocked = set()
        open_pos = []
        rescan_count = 0

        for c in candidates:
            sym = c["symbol"]
            entry_time = c["entry_time"]
            edt = datetime.combine(DATE, entry_time)

            # Free capital from closed positions
            still = []
            for p in open_pos:
                if p["xt"] <= edt:
                    cap += p["xp"] * p["qty"]
                else:
                    still.append(p)
            open_pos = still

            if sym_entry_count[sym] >= cfg["max_entries_per_sym"]:
                continue
            if cfg["sl_cooldown"] and sym in sl_blocked:
                continue

            strike = find_atm(sym, c["entry_close"], "PE")
            if strike is None or (sym, strike) not in opt_candles:
                continue

            candles = opt_candles[(sym, strike)]
            # Get entry premium at scan time
            ep = None
            for bar in candles:
                ct = parse_ts(bar)
                if ct <= edt:
                    ep = bar["close"]
                else:
                    break
            if not ep or ep < 2:
                continue

            lot = lot_sizes.get(sym, 1) * 2
            cost = ep * lot
            if cost > cap:
                continue

            sl_pct_price = ep * (1 - cfg["sl_pct"])
            sl_cap_price = ep - (cfg["max_sl"] / lot)
            sl_price = max(sl_pct_price, sl_cap_price)

            t = sim_oeh_trade(candles, entry_time, ep, lot, sl_price, cfg["floor_levels"], DATE)
            if t is None:
                continue

            trades.append({**t, "sym": sym, "strike": strike, "ep": ep * (1 + SLIPPAGE_PCT)})
            cap -= cost
            sym_entry_count[sym] += 1
            open_pos.append({"xt": t["xt"], "xp": t["xp"], "qty": t["qty"]})
            if t["reason"] == "SL":
                sl_blocked.add(sym)

        total_pnl = sum(t["pnl"] for t in trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        losses = len(trades) - wins
        wr = wins / max(1, len(trades)) * 100
        sl_count = sum(1 for t in trades if t["reason"] == "SL")
        floor_count = sum(1 for t in trades if t["reason"] == "FLOOR")
        eod_count = sum(1 for t in trades if t["reason"] == "EOD")
        avg_win = sum(t["pnl"] for t in trades if t["pnl"] > 0) / max(1, wins)
        avg_loss = sum(t["pnl"] for t in trades if t["pnl"] < 0) / max(1, losses)

        sys.stdout.write(
            f"  [{cfg_name}] {len(trades)} trades | ₹{total_pnl:>+8,} | "
            f"W/L {wins}/{losses} ({wr:.0f}%) | SL:{sl_count} Floor:{floor_count} EOD:{eod_count} | "
            f"AvgW:{avg_win:+,.0f} AvgL:{avg_loss:+,.0f}\n"
        )
        sys.stdout.flush()

        results[cfg_name] = {
            "trades": trades, "pnl": total_pnl, "wins": wins, "losses": losses,
            "sl": sl_count, "floor": floor_count, "eod": eod_count,
            "avg_win": avg_win, "avg_loss": avg_loss,
        }

    return results


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", required=True)
    parser.add_argument("--to", dest="to_date", required=True)
    args = parser.parse_args()

    d_from = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    d_to = datetime.strptime(args.to_date, "%Y-%m-%d").date()

    token = load_cached_token()
    if not token:
        print("ERROR: No token")
        return

    ud = UpstoxData(access_token=token)
    master = ud._load_master()
    print(f"Loaded {len(master)} instruments", flush=True)

    import re as _re
    eq_keys, lot_sizes, fo_symbols = {}, {}, set()
    option_keys = defaultdict(lambda: defaultdict(dict))
    sym_strikes = defaultdict(set)

    for inst in master:
        seg = inst.get("segment")
        if seg == "NSE_EQ":
            tsym = (inst.get("trading_symbol") or "").upper()
            if tsym:
                eq_keys[tsym] = inst.get("instrument_key")
        elif seg == "NSE_FO":
            _base = _re.match(r'^([A-Z&]+)', (inst.get("trading_symbol") or "").upper())
            if _base:
                fo_symbols.add(_base.group(1))
                _ls = int(inst.get("lot_size") or 0)
                if _ls > 0:
                    lot_sizes[_base.group(1)] = _ls
            ot = inst.get("instrument_type", "").upper()
            if ot in ("CE", "PE"):
                strike = float(inst.get("strike_price") or 0)
                if strike <= 0:
                    continue
                asset = (inst.get("asset_symbol") or inst.get("name") or "").upper()
                if not asset and _base:
                    asset = _base.group(1)
                if not asset:
                    continue
                ikey = inst.get("instrument_key")
                expiry = inst.get("expiry")
                sym_strikes[asset].add(strike)
                ex = option_keys[asset][strike].get(ot)
                if ex is None or (expiry and ex.get("expiry") and expiry < ex["expiry"]):
                    option_keys[asset][strike][ot] = {"key": ikey, "expiry": expiry}

    universe = sorted(fo_symbols & set(eq_keys.keys()))
    strike_steps = {}
    for sym, strikes in sym_strikes.items():
        ss = sorted(strikes)
        if len(ss) >= 2:
            strike_steps[sym] = min(ss[j+1] - ss[j] for j in range(min(20, len(ss)-1)))
        else:
            strike_steps[sym] = 50

    print(f"Universe: {len(universe)} stocks", flush=True)

    # ── Day loop ──
    cfg_names = list(CONFIGS.keys())
    grand = {n: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0,
                 "sl": 0, "floor": 0, "eod": 0, "days_pos": 0, "days_neg": 0}
             for n in cfg_names}
    daily_rows = []
    trading_days = 0

    d = d_from
    while d <= d_to:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue

        t0 = _time.time()
        sys.stdout.write(f"\n{'='*60}\n  {d.strftime('%a %d %b %Y')} ...\n")
        sys.stdout.flush()

        res = await run_one_day(ud, d, universe, eq_keys, lot_sizes, option_keys, strike_steps)
        elapsed = _time.time() - t0
        if res is None:
            sys.stdout.write(f"  HOLIDAY ({elapsed:.0f}s)\n")
            sys.stdout.flush()
            d += timedelta(days=1)
            continue

        trading_days += 1
        sys.stdout.write(f"  done ({elapsed:.0f}s)\n")
        sys.stdout.flush()

        row = {"date": d}
        for n in cfg_names:
            r = res[n]
            row[n] = r["pnl"]
            row[f"{n}_trades"] = len(r["trades"])
            grand[n]["pnl"] += r["pnl"]
            grand[n]["trades"] += len(r["trades"])
            grand[n]["wins"] += r["wins"]
            grand[n]["losses"] += r["losses"]
            grand[n]["sl"] += r["sl"]
            grand[n]["floor"] += r["floor"]
            grand[n]["eod"] += r["eod"]
            if r["pnl"] > 0:
                grand[n]["days_pos"] += 1
            elif r["pnl"] < 0:
                grand[n]["days_neg"] += 1
        daily_rows.append(row)
        d += timedelta(days=1)

    # ══════════════════════════════════════════════════════════════
    #  GRAND SUMMARY
    # ══════════════════════════════════════════════════════════════
    print(f"\n{'='*90}")
    print(f"  OEH BACKTEST COMPARISON — {d_from} to {d_to} ({trading_days} trading days)")
    print(f"{'='*90}")

    for n in cfg_names:
        g = grand[n]
        wr = g["wins"] / max(1, g["trades"]) * 100
        avg = g["pnl"] / max(1, trading_days)
        aw = (g["pnl"] + abs(sum(r.get(n, 0) for r in daily_rows if r.get(n, 0) < 0))) / max(1, g["wins"]) if g["wins"] else 0
        print(f"\n  ── {n} ──")
        c = CONFIGS[n]
        print(f"  Params: entry={c['scan_after']}, VWAP={'YES' if c['vwap_filter'] else 'NO'}, "
              f"SL={c['sl_pct']:.0%}, MaxSL=₹{c['max_sl']}, MaxEntry/sym={c['max_entries_per_sym']}")
        print(f"  Floors: {c['floor_levels'][:5]}...")
        print(f"  Trades: {g['trades']} | Net PnL: ₹{g['pnl']:+,}")
        print(f"  Wins: {g['wins']} | Losses: {g['losses']} | WR: {wr:.0f}%")
        print(f"  SL: {g['sl']} | Floor: {g['floor']} | EOD: {g['eod']}")
        print(f"  Days+: {g['days_pos']} | Days-: {g['days_neg']} | Avg/Day: ₹{avg:+,.0f}")

    # Side-by-side daily comparison
    print(f"\n{'='*90}")
    print(f"  DAILY COMPARISON")
    print(f"{'='*90}")
    hdr = f"{'Date':<14}"
    for n in cfg_names:
        hdr += f" {'PnL-'+n:>12} {'Tr':>4}"
    print(hdr)
    print("-" * (14 + len(cfg_names) * 17))
    for r in daily_rows:
        line = f"{r['date'].strftime('%d %b'):>6} {r['date'].strftime('%a'):<6}"
        for n in cfg_names:
            line += f" {r.get(n, 0):>+12,} {r.get(n+'_trades', 0):>4}"
        print(line)
    print("-" * (14 + len(cfg_names) * 17))
    totals = f"{'TOTAL':<14}"
    for n in cfg_names:
        totals += f" {grand[n]['pnl']:>+12,} {grand[n]['trades']:>4}"
    print(totals)

    # Winner
    better = max(cfg_names, key=lambda n: grand[n]["pnl"])
    worse = min(cfg_names, key=lambda n: grand[n]["pnl"])
    diff = grand[better]["pnl"] - grand[worse]["pnl"]
    print(f"\n✅ {better} beats {worse} by ₹{diff:+,}")


if __name__ == "__main__":
    asyncio.run(main())

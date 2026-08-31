#!/usr/bin/env python3
"""Our own signal scanner — generates BUY PE signals based on patterns extracted from CH2.

Strategy (derived from CH2 pattern analysis):
  1. PE-only — CE has 46% win rate vs PE 70%, deep OTM CE wins 34%
  2. Deep ITM puts — operator picks puts 500-800 pts ITM; deep ITM = 71% win rate
  3. Down-gap days — spot down >0.1% from open → PE signals win 70%+
  4. Morning priority — 09:15-11:00 has best win rate (63% vs 57%)
  5. Trending days — high-volume signal days (30+) average +₹41K
  6. Skip 12-13 dead zone

Scanner logic:
  Every 5 min candle close (9:20, 9:25, ... 15:25):
    - Check if spot is down from open (gap < -0.1%)
    - Check short-term momentum (last 3 candles trending down or continuing)
    - Find ATM strike, go 400-800 pts ITM for PE → pick a strike
    - Entry = option LTP at that candle close
    - SL = 50% of entry (wide, matching operator pattern — avg SL dist 56%)
    - TGT = entry × 1.25 (25% move up, matching winners' avg)
    - Manage with ₹6K hard SL cap + ₹2K profit floor (from optimizer)

This script GENERATES signals, does NOT execute them.
For backtesting, use backtest_own_scanner.py.

Usage:
  .venv/bin/python3 scripts/own_scanner.py --days 30 --end 2026-08-31
"""
import sys, os, re, json, time as _time, argparse
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

parser = argparse.ArgumentParser()
parser.add_argument("--days", type=int, default=30)
parser.add_argument("--end", default=None)
parser.add_argument("--lots", type=int, default=3)
parser.add_argument("--max-loss", type=float, default=6000, help="Per-trade hard SL cap")
parser.add_argument("--floor", type=float, default=2000, help="Profit floor")
parser.add_argument("--itm-min", type=int, default=300, help="Min ITM depth for PE strike (pts)")
parser.add_argument("--itm-max", type=int, default=900, help="Max ITM depth for PE strike (pts)")
parser.add_argument("--gap-threshold", type=float, default=-0.10, help="Min gap from open %% to trigger (negative = down)")
parser.add_argument("--skip-hours", default="12,13", help="Hours to skip (comma-separated)")
parser.add_argument("--cooldown", type=int, default=30, help="Min minutes between signals on same index")
parser.add_argument("--indexes", default="NIFTY,BANKNIFTY", help="Indexes to scan")
parser.add_argument("--max-signals-per-day", type=int, default=8, help="Cap signals per day")
parser.add_argument("--momentum-candles", type=int, default=3, help="Candles to check for momentum")
args = parser.parse_args()

end_date_str = args.end or datetime.now(IST).strftime("%Y-%m-%d")
end_date = date(*[int(x) for x in end_date_str.split("-")])
start_date = end_date - timedelta(days=args.days - 1)
SKIP_HOURS = set(int(h) for h in args.skip_hours.split(",") if h.strip())
INDEXES = [s.strip() for s in args.indexes.split(",")]

try:
    import config
    from src.broker.upstox_data import UpstoxData, load_cached_token
    from src.broker.upstox_client import _expiry_to_date
except ImportError as e:
    print(f"ERROR: {e}"); sys.exit(1)

token = load_cached_token()
if not token:
    print("ERROR: No Upstox token"); sys.exit(1)
uclient = UpstoxData()
master = uclient._load_master()

SPOT_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
}

LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 40, "MIDCPNIFTY": 50}

STRIKE_STEPS = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25}


def fetch_spot_candles(index_sym, ref_date):
    spot_key = SPOT_KEYS.get(index_sym)
    if not spot_key:
        return None
    y, m, d = ref_date.year, ref_date.month, ref_date.day
    from_dt = datetime(y, m, d, 9, 15, 0, tzinfo=IST)
    to_dt = datetime(y, m, d, 15, 30, 0, tzinfo=IST)
    try:
        candles = uclient.historical_data(spot_key, from_dt, to_dt, "5minute")
        _time.sleep(0.3)
        return candles
    except Exception:
        _time.sleep(0.5)
        return None


def find_pe_strike(index_sym, spot_price, itm_min, itm_max):
    """Find a PE strike that's itm_min to itm_max points ITM.
    For PE, ITM means strike > spot. So strike = spot + offset.
    Pick the midpoint of the range, rounded to strike step."""
    step = STRIKE_STEPS.get(index_sym, 50)
    target_depth = (itm_min + itm_max) // 2
    raw_strike = spot_price + target_depth
    strike = round(raw_strike / step) * step
    return strike


def find_option_instrument(index_sym, strike, opt_type, ref_date):
    """Find the nearest-expiry option matching index/strike/type."""
    candidates = []
    for inst in master:
        seg = inst.get("segment", "")
        if seg not in ("NSE_FO", "BSE_FO"):
            continue
        if inst.get("asset_symbol", "").upper() != index_sym:
            continue
        if inst.get("instrument_type") != opt_type:
            continue
        if abs(float(inst.get("strike_price", -1)) - strike) > 0.01:
            continue
        exp = _expiry_to_date(inst.get("expiry"))
        if exp is None or exp < ref_date:
            continue
        candidates.append((exp, inst))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1].get("instrument_key"), candidates[0][0]


def fetch_option_candles(inst_key, ref_date):
    y, m, d = ref_date.year, ref_date.month, ref_date.day
    from_dt = datetime(y, m, d, 9, 15, 0, tzinfo=IST)
    to_dt = datetime(y, m, d, 15, 30, 0, tzinfo=IST)
    for interval in ("5minute", "15minute"):
        try:
            candles = uclient.historical_data(inst_key, from_dt, to_dt, interval)
            _time.sleep(0.3)
            if candles:
                return candles
        except Exception:
            _time.sleep(0.5)
    return None


def generate_signals_for_day(index_sym, spot_candles, ref_date):
    """Scan spot candles and generate PE signals when conditions are met."""
    if not spot_candles or len(spot_candles) < 2:
        return []

    signals = []
    spot_open = spot_candles[0]["open"]
    last_signal_time = 0

    for i, candle in enumerate(spot_candles):
        candle_time = candle["date"][11:16]
        hour = int(candle_time.split(":")[0])
        minute = int(candle_time.split(":")[1])
        candle_epoch = hour * 60 + minute

        # Skip outside market hours
        if hour < 9 or (hour == 9 and minute < 20):
            continue
        if hour >= 15 and minute >= 25:
            continue
        if hour in SKIP_HOURS:
            continue

        # Cooldown check
        if candle_epoch - last_signal_time < args.cooldown:
            continue

        spot_now = candle["close"]
        gap_pct = ((spot_now - spot_open) / spot_open) * 100

        # CONDITION 1: Gap from open must be below threshold (down day)
        if gap_pct > args.gap_threshold:
            continue

        # CONDITION 2: Short-term momentum — last N candles trending down
        if i < args.momentum_candles:
            continue
        recent = spot_candles[i - args.momentum_candles:i + 1]
        closes = [c["close"] for c in recent]
        down_moves = sum(1 for j in range(1, len(closes)) if closes[j] < closes[j-1])
        # Need majority of recent candles to be down
        if down_moves < len(closes) // 2:
            continue

        # CONDITION 3: Not just a tiny dip — spot must have moved meaningfully
        candle_range = candle["high"] - candle["low"]
        avg_range = sum(c["high"] - c["low"] for c in spot_candles[:i+1]) / (i + 1)
        # Skip if current candle is abnormally small (no conviction)
        if candle_range < avg_range * 0.3:
            continue

        # Generate signal
        strike = find_pe_strike(index_sym, spot_now, args.itm_min, args.itm_max)
        lot_size = LOT_SIZES.get(index_sym, 75)

        signals.append({
            "time": candle_time,
            "hour": hour,
            "minute": minute,
            "index": index_sym,
            "spot": spot_now,
            "spot_open": spot_open,
            "gap_pct": gap_pct,
            "strike": strike,
            "option_type": "PE",
            "momentum": down_moves,
            "candle_range": candle_range,
            "avg_range": avg_range,
        })
        last_signal_time = candle_epoch

        if len(signals) >= args.max_signals_per_day:
            break

    return signals


def simulate_signal(sig, ref_date):
    """Simulate a generated signal — find the option, fetch candles, walk them."""
    inst_key, exp = find_option_instrument(sig["index"], sig["strike"], "PE", ref_date)
    if not inst_key:
        return None

    opt_candles = fetch_option_candles(inst_key, ref_date)
    if not opt_candles:
        return None

    filtered = [c for c in opt_candles if c["date"][11:16] >= sig["time"]]
    if not filtered:
        return None

    entry = filtered[0]["open"]
    if entry <= 0:
        return None

    lot_size = LOT_SIZES.get(sig["index"], 75)
    qty = lot_size * args.lots

    # SL at 50% of entry (matching operator's avg SL distance of 56%)
    sl_price = entry * 0.50
    # TGT at entry + 25%
    tgt_price = entry * 1.25

    peak_pnl = 0
    floor_armed = False
    cur_sl = sl_price

    for c in filtered:
        low_pnl = (c["low"] - entry) * qty
        # Hard SL cap
        if args.max_loss > 0 and low_pnl <= -args.max_loss:
            exit_price = entry - (args.max_loss / qty)
            return {"entry": entry, "exit": exit_price, "qty": qty,
                    "pnl": -args.max_loss, "result": "MAX_SL",
                    "sl": sl_price, "tgt": tgt_price}
        # SL hit
        if c["low"] <= cur_sl:
            pnl = (cur_sl - entry) * qty
            return {"entry": entry, "exit": cur_sl, "qty": qty,
                    "pnl": pnl, "result": "SL",
                    "sl": sl_price, "tgt": tgt_price}
        # TGT hit
        if c["high"] >= tgt_price:
            pnl = (tgt_price - entry) * qty
            return {"entry": entry, "exit": tgt_price, "qty": qty,
                    "pnl": pnl, "result": "TGT",
                    "sl": sl_price, "tgt": tgt_price}
        # Profit floor
        candle_peak = (c["high"] - entry) * qty
        peak_pnl = max(peak_pnl, candle_peak)
        if peak_pnl >= args.floor:
            floor_armed = True
        if floor_armed:
            cur_pnl = (c["low"] - entry) * qty
            if cur_pnl <= args.floor:
                floor_price = entry + (args.floor / qty)
                pnl = args.floor
                return {"entry": entry, "exit": floor_price, "qty": qty,
                        "pnl": pnl, "result": "FLOOR",
                        "sl": sl_price, "tgt": tgt_price}

    # EOD
    exit_price = filtered[-1]["close"]
    pnl = (exit_price - entry) * qty
    return {"entry": entry, "exit": exit_price, "qty": qty,
            "pnl": pnl, "result": "EOD",
            "sl": sl_price, "tgt": tgt_price}


def main():
    print(f"{'='*100}")
    print(f"  OWN SCANNER BACKTEST — {start_date} to {end_date}")
    print(f"  Indexes: {', '.join(INDEXES)} | {args.lots}L | ₹{args.max_loss:,.0f} SL cap | ₹{args.floor:,.0f} floor")
    print(f"  ITM depth: {args.itm_min}-{args.itm_max} pts | Gap threshold: {args.gap_threshold}%")
    print(f"  Skip hours: {SKIP_HOURS} | Cooldown: {args.cooldown} min | Max signals/day: {args.max_signals_per_day}")
    print(f"{'='*100}\n")

    all_signals = []
    all_trades = []
    day_results = []

    current = start_date
    trading_days = 0

    while current <= end_date:
        if current.weekday() >= 5:
            current += timedelta(days=1)
            continue

        day_signals = []
        for index_sym in INDEXES:
            spot_candles = fetch_spot_candles(index_sym, current)
            if not spot_candles:
                continue
            sigs = generate_signals_for_day(index_sym, spot_candles, current)
            day_signals.extend(sigs)

        if not day_signals:
            sys.stdout.write(f"\r  {current} — no signals")
            sys.stdout.flush()
            current += timedelta(days=1)
            continue

        trading_days += 1
        day_signals.sort(key=lambda s: s["time"])
        day_pnl = 0
        day_wins = 0
        day_losses = 0
        day_trades = []

        for sig in day_signals:
            trade = simulate_signal(sig, current)
            if trade is None:
                continue

            trade["date"] = str(current)
            trade["time"] = sig["time"]
            trade["index"] = sig["index"]
            trade["strike"] = sig["strike"]
            trade["spot"] = sig["spot"]
            trade["gap_pct"] = sig["gap_pct"]
            trade["momentum"] = sig["momentum"]

            all_trades.append(trade)
            day_trades.append(trade)

            if trade["pnl"] >= 0:
                day_wins += 1
            else:
                day_losses += 1
            day_pnl += trade["pnl"]

        all_signals.extend(day_signals)

        wd = current.strftime("%a")
        icon = "+" if day_pnl >= 0 else "-"
        print(f"\r  {current} ({wd})  signals={len(day_signals):>2}  trades={len(day_trades):>2}  "
              f"{day_wins}W/{day_losses}L  P&L: ₹{day_pnl:>+10,.0f}  [{icon}]")

        day_results.append({
            "date": str(current), "signals": len(day_signals),
            "trades": len(day_trades), "wins": day_wins,
            "losses": day_losses, "pnl": day_pnl,
        })

        current += timedelta(days=1)

    # ================================================================
    # Aggregate
    # ================================================================
    print(f"\n{'='*100}")
    print(f"  AGGREGATE RESULTS")
    print(f"{'='*100}")

    wins = sum(1 for t in all_trades if t["pnl"] >= 0)
    losses = sum(1 for t in all_trades if t["pnl"] < 0)
    total_pnl = sum(t["pnl"] for t in all_trades)
    win_pnls = [t["pnl"] for t in all_trades if t["pnl"] >= 0]
    loss_pnls = [t["pnl"] for t in all_trades if t["pnl"] < 0]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0
    max_win = max(win_pnls) if win_pnls else 0
    max_loss = min(loss_pnls) if loss_pnls else 0
    win_rate = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

    green_days = sum(1 for d in day_results if d["pnl"] >= 0 and d["trades"] > 0)
    red_days = sum(1 for d in day_results if d["pnl"] < 0)

    print(f"  Period:        {start_date} → {end_date}")
    print(f"  Trading days:  {trading_days} (with signals)")
    print(f"  Total signals: {len(all_signals)}  |  Total trades: {len(all_trades)}")
    print(f"  Win rate:      {win_rate:.1f}% ({wins}W / {losses}L)")
    print(f"  Total P&L:     ₹{total_pnl:+,.0f}")
    print(f"  Avg daily:     ₹{total_pnl / max(trading_days, 1):+,.0f}")
    print(f"  Avg win:       ₹{avg_win:+,.0f}")
    print(f"  Avg loss:      ₹{avg_loss:+,.0f}")
    print(f"  Max win:       ₹{max_win:+,.0f}")
    print(f"  Max loss:      ₹{max_loss:+,.0f}")
    if avg_loss != 0:
        print(f"  Risk/Reward:   {abs(avg_win/avg_loss):.2f}x")
    print(f"  Green days:    {green_days}  |  Red days: {red_days}")

    # Equity curve
    cumulative = 0
    peak = 0
    max_dd = 0
    for d in day_results:
        cumulative += d["pnl"]
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)
    print(f"  Max drawdown:  ₹{max_dd:,.0f}")
    print(f"  Final equity:  ₹{cumulative:+,.0f}")
    if max_dd > 0:
        calmar = (total_pnl / max_dd) if max_dd > 0 else 0
        print(f"  Calmar ratio:  {calmar:.2f}")

    # By index
    print(f"\n  --- By Index ---")
    for idx in INDEXES:
        idx_trades = [t for t in all_trades if t["index"] == idx]
        if not idx_trades:
            continue
        w = sum(1 for t in idx_trades if t["pnl"] >= 0)
        l = sum(1 for t in idx_trades if t["pnl"] < 0)
        pnl = sum(t["pnl"] for t in idx_trades)
        wr = w / (w + l) * 100 if (w + l) > 0 else 0
        print(f"  {idx:<12} {w}W/{l}L ({wr:.0f}%) = ₹{pnl:+,.0f}")

    # By hour
    print(f"\n  --- By Hour ---")
    hour_stats = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
    for t in all_trades:
        hr = int(t["time"].split(":")[0])
        if t["pnl"] >= 0:
            hour_stats[hr]["w"] += 1
        else:
            hour_stats[hr]["l"] += 1
        hour_stats[hr]["pnl"] += t["pnl"]
    print(f"  {'Hour':<6} {'Trades':>6} {'Win%':>6} {'P&L':>12}")
    print(f"  {'─'*34}")
    for hr in sorted(hour_stats.keys()):
        s = hour_stats[hr]
        total = s["w"] + s["l"]
        wr = s["w"] / total * 100
        print(f"  {hr:02d}:xx  {total:>6} {wr:>5.0f}% ₹{s['pnl']:>+10,.0f}")

    # By exit type
    print(f"\n  --- By Exit Type ---")
    exit_stats = defaultdict(lambda: {"count": 0, "pnl": 0})
    for t in all_trades:
        exit_stats[t["result"]]["count"] += 1
        exit_stats[t["result"]]["pnl"] += t["pnl"]
    print(f"  {'Exit':<10} {'Count':>6} {'Avg P&L':>10} {'Total':>12}")
    print(f"  {'─'*42}")
    for res in sorted(exit_stats.keys()):
        s = exit_stats[res]
        avg = s["pnl"] / s["count"]
        print(f"  {res:<10} {s['count']:>6} ₹{avg:>+8,.0f} ₹{s['pnl']:>+10,.0f}")

    # By gap bucket
    print(f"\n  --- By Gap From Open ---")
    gap_stats = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
    for t in all_trades:
        g = t["gap_pct"]
        if g < -1.0: bk = "down >1%"
        elif g < -0.5: bk = "down 0.5-1%"
        elif g < -0.1: bk = "down 0.1-0.5%"
        else: bk = "flat/up"
        if t["pnl"] >= 0:
            gap_stats[bk]["w"] += 1
        else:
            gap_stats[bk]["l"] += 1
        gap_stats[bk]["pnl"] += t["pnl"]
    print(f"  {'Gap':<16} {'Trades':>6} {'Win%':>6} {'P&L':>12}")
    print(f"  {'─'*44}")
    for bk in ["down >1%", "down 0.5-1%", "down 0.1-0.5%", "flat/up"]:
        if bk in gap_stats:
            s = gap_stats[bk]
            total = s["w"] + s["l"]
            wr = s["w"] / total * 100
            print(f"  {bk:<16} {total:>6} {wr:>5.0f}% ₹{s['pnl']:>+10,.0f}")

    # Comparison note
    print(f"\n{'='*100}")
    print(f"  COMPARISON — CH2 Baseline vs Our Scanner")
    print(f"{'='*100}")
    print(f"  CH2 (all signals):     581 trades, 60% WR, ₹+2.78L, ₹1.98L max DD")
    print(f"  CH2F (PE+skip12-13):   optimizer says ₹+7.09L, 75% WR, ₹26K max DD")
    print(f"  Our scanner:           {len(all_trades)} trades, {win_rate:.0f}% WR, ₹{total_pnl:+,.0f}, ₹{max_dd:,.0f} max DD")
    print(f"{'='*100}")

    # Save
    _data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    out_file = os.path.join(_data_dir, f"own_scanner_{start_date}_{end_date}.json")
    with open(out_file, "w") as f:
        json.dump({
            "period": f"{start_date} to {end_date}",
            "params": {
                "lots": args.lots, "max_loss": args.max_loss, "floor": args.floor,
                "itm_min": args.itm_min, "itm_max": args.itm_max,
                "gap_threshold": args.gap_threshold, "skip_hours": list(SKIP_HOURS),
                "cooldown": args.cooldown, "indexes": INDEXES,
                "max_signals_per_day": args.max_signals_per_day,
                "momentum_candles": args.momentum_candles,
            },
            "summary": {
                "total_signals": len(all_signals), "total_trades": len(all_trades),
                "wins": wins, "losses": losses, "win_rate": win_rate,
                "total_pnl": total_pnl, "max_drawdown": max_dd,
                "avg_daily": total_pnl / max(trading_days, 1),
            },
            "trades": all_trades,
            "daily": day_results,
        }, f, indent=2)
    print(f"\nResults saved: {out_file}")


main()

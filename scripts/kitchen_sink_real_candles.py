"""kitchen_sink — REAL option candle backtest.

Fetches actual 1-min option candles from Upstox for the ATM straddle,
simulates kitchen_sink logic (combined 35% SL, trailing, time exit 3:10).
Compares real premiums vs B-S estimates.

Only works for days whose weekly expiry is still in the Upstox master
(typically current + next week).

Usage: PYTHONPATH=. .venv/bin/python3 scripts/kitchen_sink_real_candles.py [days]
"""
import os, sys, time as _time, math
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()

import config
from src.broker.upstox_data import UpstoxData, load_cached_token
from src.broker.upstox_client import UpstoxClient, _expiry_to_date
from src.strategy.live_runner import (
    INDEXES, EXPIRY_WEEKDAY, est_prem, round_strike,
    _first_candle_range, fetch_candles, _candle_hm, _candle_time_str,
    _dte_fraction, _candle_minutes,
)

IST = ZoneInfo("Asia/Kolkata")
LOTS = 1
SL_PCT = 0.35
VOL_FILTER = True

token = load_cached_token()
ud = UpstoxData(access_token=token)
uc = UpstoxClient()
master = uc.load_instruments()
print(f"  Loaded {len(master)} instruments\n")

# Build index option expiry + instrument map
idx_options = {}  # (idx_name, expiry, strike, opt_type) -> instrument_key
idx_expiries = defaultdict(set)  # idx_name -> set of expiry dates

IDX_SEGMENTS = {
    "NIFTY": ("NSE_FO", "NIFTY"),
    "BANKNIFTY": ("NSE_FO", "BANKNIFTY"),
    "SENSEX": ("BSE_FO", "SENSEX"),
}

for inst in master:
    seg = inst.get("segment", "")
    name = inst.get("name", "")
    itype = inst.get("instrument_type", "")
    if itype not in ("CE", "PE"):
        continue

    for idx_name, (expected_seg, expected_name) in IDX_SEGMENTS.items():
        if seg == expected_seg and name == expected_name:
            strike = float(inst.get("strike_price", 0))
            ed = _expiry_to_date(inst.get("expiry"))
            if ed and strike > 0:
                idx_expiries[idx_name].add(ed)
                key = (idx_name, ed, strike, itype)
                idx_options[key] = inst.get("instrument_key")
            break

for idx_name in INDEXES:
    exps = sorted(idx_expiries.get(idx_name, set()))
    print(f"  {idx_name}: {len(exps)} expiries available: {exps[:5]}")
print()


def _weekly_expiry(ref_date, idx_name):
    """Find the weekly expiry for this trading day (nearest future expiry)."""
    available = sorted(e for e in idx_expiries.get(idx_name, set()) if e >= ref_date)
    if available:
        return available[0]
    return None


def _find_option_key(idx_name, expiry, strike, opt_type):
    return idx_options.get((idx_name, expiry, strike, opt_type))


def _fetch_option_candles(inst_key, day):
    """Fetch 1-min candles for the full trading day."""
    from_dt = datetime(day.year, day.month, day.day, 9, 14, tzinfo=IST)
    to_dt = datetime(day.year, day.month, day.day, 15, 35, tzinfo=IST)
    try:
        candles = ud.historical_data(inst_key, from_dt, to_dt, "1minute")
        _time.sleep(0.4)
        return candles
    except Exception as e:
        print(f"      Candle fetch error: {e}")
        return None


def _candle_at_time(candles, hour, minute):
    """Get candle at or just after a specific time."""
    for c in candles:
        ct = c.get("date") or ""
        if isinstance(ct, str):
            try:
                cdt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(IST)
            except:
                continue
        else:
            cdt = ct.astimezone(IST) if hasattr(ct, 'astimezone') else ct
        if cdt.hour > hour or (cdt.hour == hour and cdt.minute >= minute):
            return c, cdt
    return None, None


def _get_premium_at(candles, hour, minute):
    """Get option premium (close price) at a specific time."""
    c, _ = _candle_at_time(candles, hour, minute)
    return c["close"] if c else None


def charges_straddle(ce_entry, ce_exit, pe_entry, pe_exit, lot_size, lots=1):
    """Real F&O charges for a short straddle (sell at entry, buy back at exit)."""
    qty = lot_size * lots

    # CE leg: sell at entry, buy at exit
    ce_sell_to = ce_entry * qty
    ce_buy_to = ce_exit * qty
    # PE leg: sell at entry, buy at exit
    pe_sell_to = pe_entry * qty
    pe_buy_to = pe_exit * qty

    total_sell = ce_sell_to + pe_sell_to
    total_buy = ce_buy_to + pe_buy_to
    total_turnover = total_sell + total_buy

    brokerage = 40 * 4  # 4 orders (sell CE, sell PE, buy CE, buy PE)
    stt = total_sell * 0.000625  # STT on sell side only for options
    txn = total_turnover * 0.00053
    gst = (brokerage + txn) * 0.18
    sebi = total_turnover * 0.000001
    stamp = total_buy * 0.00003

    return round(brokerage + stt + txn + gst + sebi + stamp, 2)


# ── Generate trading days ──
days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 5
end_date = date.today()
start_date = end_date - timedelta(days=days_back + 5)
trading_days = []
d = start_date
while d <= end_date:
    if d.weekday() < 5:
        trading_days.append(d)
    d += timedelta(days=1)
trading_days = trading_days[-days_back:] if len(trading_days) >= days_back else trading_days

print(f"{'='*140}")
print(f"  KITCHEN SINK — REAL OPTION CANDLES, {LOTS} lot(s), {len(trading_days)} trading days")
print(f"  Dates: {trading_days[0]} to {trading_days[-1]}")
print(f"  Entry: 9:30 ATM straddle | SL: 35% combined | Trailing: yes | Vol filter: yes | Exit: 3:10")
print(f"{'='*140}\n")

all_trades = []

for day in trading_days:
    print(f"  --- {day} ---")

    for idx_name, idx_cfg in INDEXES.items():
        lot_size = idx_cfg["lot_size"]
        step = idx_cfg["strike_step"]
        iv = idx_cfg["iv_annual"]

        # Get weekly expiry
        expiry = _weekly_expiry(day, idx_name)
        if not expiry:
            print(f"    {idx_name}: no expiry available, skip")
            continue

        dte = (expiry - day).days

        # Fetch index spot candles for vol filter + ATM determination
        spot_candles = fetch_candles(ud, idx_name, day, "1minute")
        if not spot_candles or len(spot_candles) < 10:
            print(f"    {idx_name}: no spot candles, skip")
            continue

        # Vol filter: check first 3 candles range (using 5-min equivalent)
        spot_5min = fetch_candles(ud, idx_name, day, "5minute")
        if spot_5min:
            fr = _first_candle_range(spot_5min)
            if VOL_FILTER and fr > idx_cfg["vol_skip_range"]:
                print(f"    {idx_name}: vol filter skip (range={fr:.0f} > {idx_cfg['vol_skip_range']})")
                continue

        # Get spot at 9:30
        spot_930, _ = _candle_at_time(spot_candles, 9, 30)
        if not spot_930:
            print(f"    {idx_name}: no 9:30 candle, skip")
            continue

        spot_entry = spot_930["close"]
        atm = round_strike(spot_entry, step)

        # Find option instruments
        ce_key = _find_option_key(idx_name, expiry, atm, "CE")
        pe_key = _find_option_key(idx_name, expiry, atm, "PE")

        if not ce_key or not pe_key:
            print(f"    {idx_name}: CE/PE {atm} instruments not found for expiry {expiry}, skip")
            continue

        # Fetch real option candles
        ce_candles = _fetch_option_candles(ce_key, day)
        pe_candles = _fetch_option_candles(pe_key, day)

        if not ce_candles or not pe_candles:
            print(f"    {idx_name}: no option candles, skip")
            continue

        # Entry at 9:30
        ce_entry_prem = _get_premium_at(ce_candles, 9, 30)
        pe_entry_prem = _get_premium_at(pe_candles, 9, 30)

        if not ce_entry_prem or not pe_entry_prem:
            print(f"    {idx_name}: no 9:30 option premiums, skip")
            continue

        total_prem = ce_entry_prem + pe_entry_prem
        sl_level = total_prem * (1 + SL_PCT)

        # B-S estimate for comparison
        mins_entry = 15  # 9:30 = 15 mins into session
        T_entry = (dte + max(0, (375 - mins_entry) / 375)) / 365.0
        bs_ce = est_prem(spot_entry, atm, "CE", T_entry, iv)
        bs_pe = est_prem(spot_entry, atm, "PE", T_entry, iv)

        # Simulate through the day with real candles
        ce_exit_prem = None
        pe_exit_prem = None
        exit_reason = "time_3:10"
        exit_time = None
        best_profit = 0.0
        trail_active = False

        # Walk through 1-min candles after 9:30
        for ce_c in ce_candles:
            ct = ce_c.get("date") or ""
            if isinstance(ct, str):
                try:
                    cdt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(IST)
                except:
                    continue
            else:
                cdt = ct.astimezone(IST)

            if cdt.hour < 9 or (cdt.hour == 9 and cdt.minute <= 30):
                continue

            # Find matching PE candle at same time
            pe_c_match = None
            for pc in pe_candles:
                pct = pc.get("date") or ""
                if isinstance(pct, str):
                    try:
                        pcdt = datetime.fromisoformat(pct.replace("Z", "+00:00")).astimezone(IST)
                    except:
                        continue
                else:
                    pcdt = pct.astimezone(IST)
                if pcdt.hour == cdt.hour and pcdt.minute == cdt.minute:
                    pe_c_match = pc
                    break

            if not pe_c_match:
                continue

            ce_now = ce_c["close"]
            pe_now = pe_c_match["close"]
            combined_now = ce_now + pe_now

            # Check worst case within this candle (use highs)
            ce_worst = ce_c["high"]
            pe_worst = pe_c_match["high"]

            # Combined SL check
            if ce_worst + pe_worst >= sl_level:
                ce_exit_prem = ce_now
                pe_exit_prem = pe_now
                exit_reason = "combined_sl"
                exit_time = f"{cdt.hour:02d}:{cdt.minute:02d}"
                break

            # Trailing stop
            current_profit = total_prem - combined_now
            best_profit = max(best_profit, current_profit)
            if current_profit / total_prem >= 0.40:
                trail_active = True
            if trail_active and best_profit > 0:
                give_back = best_profit * 0.20
                if current_profit < best_profit - give_back:
                    ce_exit_prem = ce_now
                    pe_exit_prem = pe_now
                    exit_reason = "trailing"
                    exit_time = f"{cdt.hour:02d}:{cdt.minute:02d}"
                    break

            # Time exit at 3:10
            if cdt.hour >= 15 and cdt.minute >= 10:
                ce_exit_prem = ce_now
                pe_exit_prem = pe_now
                exit_time = f"{cdt.hour:02d}:{cdt.minute:02d}"
                break

        # Fallback: last candle
        if ce_exit_prem is None:
            ce_exit_prem = ce_candles[-1]["close"]
        if pe_exit_prem is None:
            pe_exit_prem = pe_candles[-1]["close"]
        if exit_time is None:
            ct = ce_candles[-1].get("date", "")
            exit_time = ct[11:16] if len(ct) > 16 else "15:30"

        qty = lot_size * LOTS
        ce_pnl = (ce_entry_prem - ce_exit_prem) * qty
        pe_pnl = (pe_entry_prem - pe_exit_prem) * qty
        gross = ce_pnl + pe_pnl
        charges = charges_straddle(ce_entry_prem, ce_exit_prem,
                                   pe_entry_prem, pe_exit_prem, lot_size, LOTS)
        net = gross - charges

        # B-S comparison
        bs_total = bs_ce + bs_pe
        prem_diff_pct = ((total_prem - bs_total) / bs_total * 100) if bs_total > 0 else 0

        trade = {
            "date": day.isoformat(),
            "idx": idx_name,
            "expiry": expiry.isoformat(),
            "dte": dte,
            "spot": spot_entry,
            "atm": atm,
            "ce_entry": ce_entry_prem,
            "pe_entry": pe_entry_prem,
            "total_prem": total_prem,
            "ce_exit": ce_exit_prem,
            "pe_exit": pe_exit_prem,
            "ce_pnl": ce_pnl,
            "pe_pnl": pe_pnl,
            "gross": gross,
            "charges": charges,
            "net": net,
            "exit_reason": exit_reason,
            "exit_time": exit_time,
            "bs_ce": bs_ce,
            "bs_pe": bs_pe,
            "bs_total": bs_total,
            "prem_diff_pct": prem_diff_pct,
        }
        all_trades.append(trade)

        icon = "+" if net > 0 else ""
        print(f"    {idx_name:>10}  ATM {atm:.0f}  CE={ce_entry_prem:.1f} PE={pe_entry_prem:.1f} "
              f"(total={total_prem:.1f}, B-S={bs_total:.1f}, {prem_diff_pct:+.0f}%)  "
              f"→  {icon}₹{net:,.0f}  [{exit_reason} @{exit_time}]")

# ── Detailed Table ──
print(f"\n{'='*150}")
print(f"  {'Date':>12} {'Index':>10} {'Spot':>8} {'ATM':>8} {'DTE':>4} "
      f"{'CE Ent':>8} {'PE Ent':>8} {'Total':>8} {'B-S':>8} {'Diff%':>6} "
      f"{'CE Exit':>8} {'PE Exit':>8} {'CE P&L':>10} {'PE P&L':>10} "
      f"{'Charges':>8} {'Net P&L':>10} {'Exit':>12}")
print(f"  {'─'*145}")

total_net = 0
total_charges = 0
total_gross = 0
wins = losses = 0
idx_stats = defaultdict(lambda: {"net": 0, "wins": 0, "losses": 0, "count": 0, "charges": 0})

for t in all_trades:
    icon = "✅" if t["net"] > 0 else "❌" if t["net"] < 0 else "⏸️"
    print(f"  {t['date']:>12} {t['idx']:>10} {t['spot']:>8.0f} {t['atm']:>8.0f} {t['dte']:>4} "
          f"{t['ce_entry']:>8.1f} {t['pe_entry']:>8.1f} {t['total_prem']:>8.1f} {t['bs_total']:>8.1f} "
          f"{t['prem_diff_pct']:>+5.0f}% "
          f"{t['ce_exit']:>8.1f} {t['pe_exit']:>8.1f} "
          f"{t['ce_pnl']:>+10,.0f} {t['pe_pnl']:>+10,.0f} "
          f"{t['charges']:>8,.0f} {t['net']:>+10,.0f} "
          f"{t['exit_reason']:>12} {icon}")

    total_net += t["net"]
    total_charges += t["charges"]
    total_gross += t["gross"]
    if t["net"] > 0: wins += 1
    elif t["net"] < 0: losses += 1
    idx_stats[t["idx"]]["net"] += t["net"]
    idx_stats[t["idx"]]["charges"] += t["charges"]
    idx_stats[t["idx"]]["count"] += 1
    if t["net"] > 0: idx_stats[t["idx"]]["wins"] += 1
    elif t["net"] < 0: idx_stats[t["idx"]]["losses"] += 1

# ── Summary ──
print(f"\n{'='*150}")
print(f"  SUMMARY — kitchen_sink REAL CANDLES, {LOTS} lot(s)")
print(f"  {'─'*60}")
print(f"  {'Trades:':30} {len(all_trades)}")
if wins + losses > 0:
    print(f"  {'Win / Loss:':30} {wins}W / {losses}L ({wins/(wins+losses)*100:.0f}% win)")
print(f"  {'':30}")
print(f"  {'Total Gross P&L:':30} ₹{total_gross:>+12,.0f}")
print(f"  {'Total Charges:':30} ₹{total_charges:>12,.0f}")
print(f"  {'Total Net P&L:':30} ₹{total_net:>+12,.0f}")
if all_trades:
    print(f"  {'':30}")
    print(f"  {'Avg net P&L per trade:':30} ₹{total_net/len(all_trades):>12,.0f}")
    print(f"  {'Avg charges per trade:':30} ₹{total_charges/len(all_trades):>12,.0f}")

    # Premium comparison
    avg_diff = sum(t["prem_diff_pct"] for t in all_trades) / len(all_trades)
    print(f"  {'':30}")
    print(f"  {'Avg real vs B-S premium diff:':30} {avg_diff:>+11.1f}%")
    print(f"  (positive = real premiums higher than B-S estimate)")

# Per-index
print(f"\n  PER-INDEX:")
print(f"  {'Index':>10} {'Trades':>8} {'W/L':>8} {'Net P&L':>12} {'Charges':>10}")
print(f"  {'─'*52}")
for idx_name in INDEXES:
    s = idx_stats[idx_name]
    if s["count"] == 0:
        continue
    print(f"  {idx_name:>10} {s['count']:>8} {s['wins']}W/{s['losses']}L "
          f"{s['net']:>+12,.0f} {s['charges']:>10,.0f}")

# Per-day
day_nets = defaultdict(float)
for t in all_trades:
    day_nets[t["date"]] += t["net"]

print(f"\n  PER-DAY:")
print(f"  {'Date':>12} {'Net P&L':>12}")
print(f"  {'─'*28}")
for d in sorted(day_nets):
    icon = "🟢" if day_nets[d] > 0 else "🔴"
    print(f"  {d:>12} {day_nets[d]:>+12,.0f} {icon}")

print(f"\n{'='*150}")
print(f"  BACKTEST COMPLETE")
print(f"{'='*150}")

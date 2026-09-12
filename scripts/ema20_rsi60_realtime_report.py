"""ema20_rsi60 — realistic 2-lot report for last N trading days.

For each signal detected in stock_strategy_results, resolves real option
instruments, fetches actual 1-min candles for both legs, and simulates
the spread with real premiums and real charges.

Usage: .venv/bin/python3 scripts/ema20_rsi60_realtime_report.py [days]
"""
import os, sys, time
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()

import config
from src.storage.db import get_conn
from src.broker.upstox_data import UpstoxData, load_cached_token
from src.broker.upstox_client import UpstoxClient, pick_upstox_option
from src.strategy.stock_runner import (
    STOCKS, STRATEGIES, _monthly_expiry_for, round_strike,
    init_stock_strategy_db, calc_charges as sim_calc_charges,
)

IST = ZoneInfo("Asia/Kolkata")
LOTS = 1
STRATEGY = "ema20_rsi60"
PARAMS = STRATEGIES[STRATEGY]

token = load_cached_token()
udata = UpstoxData(access_token=token)
uclient = UpstoxClient()
master = uclient.load_instruments()

print(f"  Loaded {len(master)} instruments from Upstox master")
print()


def real_charges(sell_prem, buy_prem, lot_size, lots, exit_sell=None, exit_buy=None):
    """Calculate actual F&O charges for a credit spread round-trip."""
    qty = lot_size * lots

    entry_sell_to = sell_prem * qty
    entry_buy_to = buy_prem * qty
    brokerage_entry = 40.0  # ₹20/leg × 2 legs

    charges = brokerage_entry
    charges += entry_sell_to * 0.001   # STT on sell
    charges += (entry_sell_to + entry_buy_to) * 0.000495  # exchange txn
    charges += (entry_sell_to + entry_buy_to) * 0.000001  # SEBI
    charges += entry_buy_to * 0.00003  # stamp duty on buy
    charges += (brokerage_entry + (entry_sell_to + entry_buy_to) * 0.000495) * 0.18  # GST

    if exit_sell is not None and exit_buy is not None:
        exit_sell_to = exit_sell * qty
        exit_buy_to = exit_buy * qty
        brokerage_exit = 40.0
        charges += brokerage_exit
        charges += exit_buy_to * 0.001  # STT on sell (we're buying back sell leg = selling buy leg)
        charges += (exit_sell_to + exit_buy_to) * 0.000495
        charges += (exit_sell_to + exit_buy_to) * 0.000001
        charges += exit_sell_to * 0.00003
        charges += (brokerage_exit + (exit_sell_to + exit_buy_to) * 0.000495) * 0.18

    return round(charges, 2)


def _find_nearest_expiry(stock, opt_type, target_expiry):
    """Find the nearest actual expiry in the master for a stock option."""
    from src.broker.upstox_client import _expiry_to_date
    name_upper = stock.upper()
    expiries = set()
    for inst in master:
        if inst.get("segment") != "NSE_FO":
            continue
        tsym = (inst.get("trading_symbol") or "").upper()
        if not tsym.startswith(name_upper + " "):
            continue
        if inst.get("instrument_type") != opt_type:
            continue
        ed = _expiry_to_date(inst.get("expiry"))
        if ed:
            expiries.add(ed)
    if not expiries:
        return target_expiry
    # Find closest expiry to our target (within 7 days)
    best = min(expiries, key=lambda e: abs((e - target_expiry).days))
    if abs((best - target_expiry).days) <= 7:
        return best
    return target_expiry


def resolve_option(stock, expiry, strike, opt_type):
    """Find instrument in master, with expiry fuzzy-matching for stocks."""
    spec = config.UPSTOX_OPTION_SEGMENTS.get(f"NSE:{stock}")
    if not spec:
        return None
    # Try exact match first
    result = pick_upstox_option(master, spec["name"], expiry, strike,
                                opt_type, spec["segment"])
    if result:
        return result
    # Try with nearest actual expiry from master
    real_expiry = _find_nearest_expiry(stock, opt_type, expiry)
    if real_expiry != expiry:
        result = pick_upstox_option(master, spec["name"], real_expiry, strike,
                                    opt_type, spec["segment"])
    return result


def get_entry_candles(instrument_key, trade_date):
    """Get 1-min candles for entry day (15:00-15:30 window = typical entry)."""
    from_dt = datetime(trade_date.year, trade_date.month, trade_date.day,
                       9, 15, tzinfo=IST)
    to_dt = datetime(trade_date.year, trade_date.month, trade_date.day,
                     15, 30, tzinfo=IST)
    try:
        candles = udata.historical_data(instrument_key, from_dt, to_dt, "1minute")
        time.sleep(0.5)
        return candles
    except Exception as e:
        print(f"    Candle fetch error: {e}")
        return None


def get_day_close_candle(instrument_key, trade_date):
    """Get closing candle (15:25-15:30) for a date."""
    from_dt = datetime(trade_date.year, trade_date.month, trade_date.day,
                       15, 20, tzinfo=IST)
    to_dt = datetime(trade_date.year, trade_date.month, trade_date.day,
                     15, 30, tzinfo=IST)
    try:
        candles = udata.historical_data(instrument_key, from_dt, to_dt, "1minute")
        time.sleep(0.5)
        if candles:
            return candles[-1]
    except Exception:
        pass
    return None


def get_entry_premium(instrument_key, trade_date):
    """Get the premium around 15:30 (strategy runs at 15:35)."""
    candles = get_entry_candles(instrument_key, trade_date)
    if not candles:
        return None
    # Use the last candle (closest to 15:30)
    for c in reversed(candles):
        ct = c.get("date") or c.get("timestamp") or ""
        if isinstance(ct, str):
            try:
                cdt = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(IST)
            except:
                continue
        else:
            cdt = ct.astimezone(IST) if hasattr(ct, 'astimezone') else ct
        if cdt.hour >= 15 and cdt.minute >= 25:
            return c["close"]
    # Fallback: last candle
    return candles[-1]["close"] if candles else None


# ── Fetch trades from DB ──
days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 4
today = date.today()
cutoff = today - timedelta(days=days_back + 3)  # extra buffer for weekends

init_stock_strategy_db()

with get_conn() as conn:
    rows = conn.execute(
        "SELECT * FROM stock_strategy_results "
        "WHERE strategy=? AND skipped=0 AND date >= ? "
        "ORDER BY date, stock",
        (STRATEGY, cutoff.isoformat())
    ).fetchall()

# Filter to last N trading days
trade_dates = sorted(set(dict(r)["date"] for r in rows))
last_n_dates = trade_dates[-days_back:] if len(trade_dates) >= days_back else trade_dates
rows = [r for r in rows if dict(r)["date"] in last_n_dates]

CAPITAL = 100_000  # ₹1 lakh capital limit

print(f"\n{'#'*120}")
print(f"  ema20_rsi60 REAL-PREMIUM REPORT — {LOTS} lot(s) × last {len(last_n_dates)} trading days (capital: ₹{CAPITAL:,.0f})")
print(f"  Dates: {last_n_dates[0] if last_n_dates else '?'} to {last_n_dates[-1] if last_n_dates else '?'}")
print(f"{'#'*120}")
print(f"  Found {len(rows)} signals\n")

results = []
daily_margin_used = {}  # track margin used per day

for r in rows:
    r = dict(r)
    stock = r["stock"]
    entry_date_str = r["entry_date"] or r["date"]
    entry_date = date.fromisoformat(entry_date_str)
    exit_date_str = r["exit_date"]
    exit_date = date.fromisoformat(exit_date_str) if exit_date_str else None
    direction = r["direction"]
    sell_strike = r["sell_strike"]
    buy_strike = r["buy_strike"]
    sim_credit = r["net_credit"]
    sim_pnl = r["net_pnl"] or 0
    exit_reason = r["exit_reason"]
    expiry_str = r.get("expiry_date")
    expiry = date.fromisoformat(expiry_str) if expiry_str else _monthly_expiry_for(entry_date)

    stk = STOCKS.get(stock)
    if not stk:
        print(f"  {stock}: not in STOCKS dict, skipping")
        continue

    lot_size = stk["lot_size"]
    opt_type = "PE" if direction == "bullish" else "CE"

    # Find real expiry from master (NSE moved stock F&O from Thu to Tue)
    real_expiry = _find_nearest_expiry(stock, opt_type, expiry)
    exp_note = f" → {real_expiry}" if real_expiry != expiry else ""

    print(f"  {stock:12} {direction:8} {opt_type} {sell_strike:.0f}/{buy_strike:.0f} "
          f"(entry={entry_date_str}, expiry={expiry}{exp_note})...", end=" ", flush=True)

    # Resolve real instruments
    sell_inst = resolve_option(stock, real_expiry, sell_strike, opt_type)
    buy_inst = resolve_option(stock, real_expiry, buy_strike, opt_type)

    if not sell_inst or not buy_inst:
        # Expired instrument — fall back to sim P&L from DB
        stk_info = STOCKS.get(stock)
        lot_size = stk_info["lot_size"] if stk_info else 1
        qty = lot_size * LOTS
        strike_width = abs(sell_strike - buy_strike)
        margin_est = (strike_width * qty) - (sim_credit * qty) if sim_credit else strike_width * qty

        day_margin_so_far = daily_margin_used.get(entry_date_str, 0)
        if day_margin_so_far + margin_est > CAPITAL:
            print(f"SKIP (capital limit)")
            continue
        daily_margin_used[entry_date_str] = day_margin_so_far + margin_est

        net_pnl_sim = (sim_pnl or 0) * LOTS
        gross_sim = net_pnl_sim + (sim_calc_charges(sim_credit, abs(sim_credit - (r.get("exit_spread_val") or 0)), lot_size, LOTS) if sim_credit else 0)
        ch = gross_sim - net_pnl_sim if gross_sim != net_pnl_sim else 0
        result = {
            "stock": stock, "direction": direction, "opt_type": opt_type,
            "sell_strike": sell_strike, "buy_strike": buy_strike,
            "entry_date": entry_date_str,
            "exit_date": exit_date.isoformat() if exit_date else "OPEN",
            "exit_reason": (exit_reason or "OPEN") + " [sim]",
            "lot_size": lot_size, "qty": qty,
            "sell_entry": sim_credit, "buy_entry": 0,
            "real_credit": sim_credit,
            "sim_credit": sim_credit,
            "sell_exit": None, "buy_exit": None,
            "exit_spread": r.get("exit_spread_val"),
            "gross_pnl": net_pnl_sim,
            "charges": abs(ch),
            "net_pnl": net_pnl_sim,
            "sim_pnl": sim_pnl,
            "margin": margin_est,
            "source": "sim",
        }
        results.append(result)
        icon = "+" if net_pnl_sim > 0 else ""
        print(f"[SIM] P&L: {icon}₹{net_pnl_sim:,.0f} (B-S, instrument expired) | {exit_reason}")
        continue

    sell_key = sell_inst["instrument_key"]
    buy_key = buy_inst["instrument_key"]

    # Get real entry premiums
    sell_entry_prem = get_entry_premium(sell_key, entry_date)
    buy_entry_prem = get_entry_premium(buy_key, entry_date)

    if sell_entry_prem is None or buy_entry_prem is None:
        print(f"SKIP (no entry candles: sell={sell_entry_prem}, buy={buy_entry_prem})")
        continue

    real_credit = sell_entry_prem - buy_entry_prem
    qty = lot_size * LOTS

    # Get exit premiums
    real_exit_sell = None
    real_exit_buy = None
    real_exit_spread = None

    if exit_date and exit_reason:
        if exit_reason == "expiry":
            # At expiry, options settle at intrinsic value
            # For simplicity, use 0 spread (credit kept) or fetch last candle
            exit_candle_sell = get_day_close_candle(sell_key, exit_date)
            exit_candle_buy = get_day_close_candle(buy_key, exit_date)
            if exit_candle_sell and exit_candle_buy:
                real_exit_sell = exit_candle_sell["close"]
                real_exit_buy = exit_candle_buy["close"]
            else:
                # Expiry OTM = both legs worthless
                real_exit_sell = 0.0
                real_exit_buy = 0.0
        else:
            # profit_target, stop_loss, dte_exit
            exit_candle_sell = get_day_close_candle(sell_key, exit_date)
            exit_candle_buy = get_day_close_candle(buy_key, exit_date)
            if exit_candle_sell and exit_candle_buy:
                real_exit_sell = exit_candle_sell["close"]
                real_exit_buy = exit_candle_buy["close"]

        if real_exit_sell is not None and real_exit_buy is not None:
            real_exit_spread = real_exit_sell - real_exit_buy
    else:
        # Still open — get today's LTP
        try:
            sell_ltp_data = udata.ltp([sell_key])
            time.sleep(0.3)
            buy_ltp_data = udata.ltp([buy_key])
            time.sleep(0.3)
            real_exit_sell = float(sell_ltp_data.get(sell_key, {}).get("last_price", 0))
            real_exit_buy = float(buy_ltp_data.get(buy_key, {}).get("last_price", 0))
            real_exit_spread = real_exit_sell - real_exit_buy
            exit_reason = "OPEN (current LTP)"
        except Exception as e:
            print(f"LTP error: {e}")
            real_exit_sell = sell_entry_prem
            real_exit_buy = buy_entry_prem

    # Calculate P&L
    if real_exit_spread is not None:
        gross_pnl = (real_credit - real_exit_spread) * qty
    else:
        gross_pnl = 0

    charges = real_charges(
        sell_entry_prem, buy_entry_prem, lot_size, LOTS,
        real_exit_sell if real_exit_sell is not None else 0,
        real_exit_buy if real_exit_buy is not None else 0,
    )
    net_pnl = gross_pnl - charges

    # Margin required: max loss = (strike_width * qty) - net_credit * qty
    strike_width = abs(sell_strike - buy_strike)
    max_loss = (strike_width * qty) - (real_credit * qty)
    margin_est = max_loss  # approximate margin for credit spread

    # Capital limit: skip if adding this trade exceeds CAPITAL for the day
    day_margin_so_far = daily_margin_used.get(entry_date_str, 0)
    if day_margin_so_far + margin_est > CAPITAL:
        print(f"SKIP (capital limit: ₹{day_margin_so_far:,.0f} + ₹{margin_est:,.0f} > ₹{CAPITAL:,.0f})")
        continue
    daily_margin_used[entry_date_str] = day_margin_so_far + margin_est

    result = {
        "stock": stock, "direction": direction, "opt_type": opt_type,
        "sell_strike": sell_strike, "buy_strike": buy_strike,
        "entry_date": entry_date_str,
        "exit_date": exit_date.isoformat() if exit_date else "OPEN",
        "exit_reason": exit_reason or "OPEN",
        "lot_size": lot_size, "qty": qty,
        "sell_entry": sell_entry_prem, "buy_entry": buy_entry_prem,
        "real_credit": real_credit,
        "sim_credit": sim_credit,
        "sell_exit": real_exit_sell, "buy_exit": real_exit_buy,
        "exit_spread": real_exit_spread,
        "gross_pnl": gross_pnl,
        "charges": charges,
        "net_pnl": net_pnl,
        "sim_pnl": sim_pnl,
        "margin": margin_est,
        "source": "real",
    }
    results.append(result)

    icon = "+" if net_pnl > 0 else ""
    print(f"Credit: {real_credit:.2f} (sim: {sim_credit:.2f}) | "
          f"P&L: {icon}₹{net_pnl:,.0f} (sim: ₹{sim_pnl:+,.0f}) | "
          f"Charges: ₹{charges:,.0f} | {exit_reason}")

# ── Detailed Table ──
print(f"\n{'='*145}")
print(f"  {'':1} {'Stock':12} {'Dir':6} {'Spread':14} {'Entry':>12} {'Exit':>12} "
      f"{'Credit':>8} {'ExitSpd':>8} {'Gross':>10} {'Charges':>8} {'Net P&L':>10} "
      f"{'Margin':>10} {'Exit Reason':>18}")
print(f"  {'─'*140}")
print(f"  R = Real candles, S = Sim (B-S, instrument expired)\n")

total_gross = 0
total_charges = 0
total_net = 0
total_sim = 0
total_margin = 0
wins = losses = 0

for r in results:
    icon = "✅" if r["net_pnl"] > 0 else "❌" if r["net_pnl"] < 0 else "⏳"
    src = "S" if r.get("source") == "sim" else "R"
    spread = f"{r['sell_strike']:.0f}/{r['buy_strike']:.0f} {r['opt_type']}"
    entry_str = f"{r['sell_entry']:.2f}-{r['buy_entry']:.2f}" if r['sell_entry'] is not None else "—"
    exit_str = f"{r['sell_exit']:.2f}-{r['buy_exit']:.2f}" if r['sell_exit'] is not None else "—"

    print(f"  {src} {r['stock']:12} {r['direction']:6} {spread:14} {entry_str:>12} {exit_str:>12} "
          f"{r['real_credit'] or 0:>8.2f} {r['exit_spread'] or 0:>8.2f} "
          f"{r['gross_pnl']:>+10,.0f} {r['charges']:>8,.0f} {r['net_pnl']:>+10,.0f} "
          f"{r['margin']:>10,.0f} {r['exit_reason']:>18} {icon}")

    total_gross += r["gross_pnl"]
    total_charges += r["charges"]
    total_net += r["net_pnl"]
    total_sim += r["sim_pnl"]
    total_margin += r["margin"]
    if r["net_pnl"] > 0: wins += 1
    elif r["net_pnl"] < 0: losses += 1

# ── Summary ──
print(f"\n{'='*140}")
print(f"  SUMMARY — ema20_rsi60, {LOTS} lots, {len(last_n_dates)} trading days")
print(f"  {'─'*60}")
print(f"  {'Trades:':30} {len(results)}")
print(f"  {'Win / Loss:':30} {wins}W / {losses}L ({wins/(wins+losses)*100:.0f}% win)" if wins+losses else "")
print(f"  {'':30}")
print(f"  {'Total Gross P&L:':30} ₹{total_gross:>+12,.0f}")
print(f"  {'Total Charges:':30} ₹{total_charges:>12,.0f}")
print(f"  {'Total Net P&L:':30} ₹{total_net:>+12,.0f}")
print(f"  {'':30}")
print(f"  {'Simulated P&L (B-S):':30} ₹{total_sim:>+12,.0f}")
print(f"  {'Real vs Sim delta:':30} ₹{total_net - total_sim:>+12,.0f}")
print(f"  {'':30}")
print(f"  {'Total margin required:':30} ₹{total_margin:>12,.0f}")
print(f"  {'Avg margin per trade:':30} ₹{total_margin/len(results) if results else 0:>12,.0f}")
print(f"  {'ROI on margin:':30} {total_net/total_margin*100 if total_margin else 0:>11.1f}%")
print(f"  {'':30}")
print(f"  {'Avg net P&L per trade:':30} ₹{total_net/len(results) if results else 0:>12,.0f}")
print(f"  {'Avg charges per trade:':30} ₹{total_charges/len(results) if results else 0:>12,.0f}")

real_trades = [r for r in results if r.get("source") == "real"]
sim_trades = [r for r in results if r.get("source") == "sim"]
if real_trades and sim_trades:
    print(f"  {'':30}")
    print(f"  {'── Source breakdown ──':30}")
    r_net = sum(r["net_pnl"] for r in real_trades)
    s_net = sum(r["net_pnl"] for r in sim_trades)
    print(f"  {'Real candle trades:':30} {len(real_trades):>4}   ₹{r_net:>+10,.0f}")
    print(f"  {'Sim (B-S) trades:':30} {len(sim_trades):>4}   ₹{s_net:>+10,.0f}")

# ── Per-day breakdown ──
days = {}
for r in results:
    d = r["entry_date"]
    if d not in days:
        days[d] = {"net": 0, "count": 0, "charges": 0, "margin": 0}
    days[d]["net"] += r["net_pnl"]
    days[d]["count"] += 1
    days[d]["charges"] += r["charges"]
    days[d]["margin"] += r["margin"]

print(f"\n  PER-DAY:")
print(f"  {'Date':>12} {'Trades':>8} {'Net P&L':>12} {'Charges':>10} {'Margin':>12}")
print(f"  {'─'*60}")
for d in sorted(days):
    dd = days[d]
    icon = "🟢" if dd["net"] > 0 else "🔴"
    print(f"  {d:>12} {dd['count']:>8} {dd['net']:>+12,.0f} {dd['charges']:>10,.0f} {dd['margin']:>12,.0f} {icon}")

# ── Weekly breakdown ──
from collections import defaultdict
weeks = defaultdict(lambda: {"net": 0, "count": 0, "charges": 0, "margin": 0, "wins": 0, "losses": 0})
for r in results:
    d = date.fromisoformat(r["entry_date"])
    iso = d.isocalendar()
    wk = f"{iso.year}-W{iso.week:02d}"
    weeks[wk]["net"] += r["net_pnl"]
    weeks[wk]["count"] += 1
    weeks[wk]["charges"] += r["charges"]
    weeks[wk]["margin"] += r["margin"]
    if r["net_pnl"] > 0: weeks[wk]["wins"] += 1
    elif r["net_pnl"] < 0: weeks[wk]["losses"] += 1

print(f"\n  WEEKLY:")
print(f"  {'Week':>10} {'Trades':>8} {'W/L':>8} {'Net P&L':>12} {'Charges':>10} {'Peak Margin':>12}")
print(f"  {'─'*66}")
for wk in sorted(weeks):
    w = weeks[wk]
    icon = "🟢" if w["net"] > 0 else "🔴"
    print(f"  {wk:>10} {w['count']:>8} {w['wins']}W/{w['losses']}L {w['net']:>+12,.0f} {w['charges']:>10,.0f} {w['margin']:>12,.0f} {icon}")

print(f"\n{'#'*140}")
print(f"  REPORT COMPLETE")
print(f"{'#'*140}")

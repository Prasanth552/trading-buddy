"""Backtest OEH trades from the past week with the NEW exit rules.

Compares OLD logic (as actually traded) vs NEW logic:
  1. ₹5,000 max loss cap (instead of ₹8,000)
  2. Partial profit booking: sell 1 lot at 1.5x floor, keep 1 lot with SL=entry, TGT=2x
  3. Stepping trailing floor: ₹1,500 increments on net P&L

Uses 1-minute option candles (or B-S estimation from stock candles) to replay
each trade tick-by-tick with both old and new exit logic.
"""
import os, sys, time, math
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()

import config
from src.storage.db import get_conn
from src.broker.upstox_data import UpstoxData, load_cached_token

IST = ZoneInfo("Asia/Kolkata")
FLOOR_MULT = 1.5
TARGET_MULT = 2.0
OLD_MAX_LOSS = 8000
NEW_MAX_LOSS = 5000
FLOOR_STEP = 1500

token = load_cached_token()
ud = UpstoxData(access_token=token)
master = ud._load_master()


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_put(S, K, T, sigma):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_call(S, K, T, sigma):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * _norm_cdf(-d2)


STOCK_IV = {
    "SHREECEM": 0.30, "ITC": 0.22, "AMBUJACEM": 0.32, "VEDL": 0.40,
    "INFY": 0.28, "TCS": 0.25, "ONGC": 0.35, "HDFCBANK": 0.25,
    "RELIANCE": 0.28, "SBIN": 0.32, "TATAMOTORS": 0.38, "BAJFINANCE": 0.30,
    "LT": 0.28, "TATASTEEL": 0.38, "NESTLEIND": 0.25, "ASIANPAINT": 0.28,
    "SUNPHARMA": 0.28, "BHEL": 0.45, "DABUR": 0.25, "COALINDIA": 0.32,
    "NTPC": 0.28, "MARUTI": 0.28, "BRITANNIA": 0.25, "ULTRACEMCO": 0.28,
    "HCLTECH": 0.28, "JSWSTEEL": 0.35, "WIPRO": 0.28, "HINDALCO": 0.35,
    "ADANIPORTS": 0.35, "APOLLOHOSP": 0.30, "BEL": 0.35, "CIPLA": 0.28,
    "DRREDDY": 0.25, "EICHERMOT": 0.28, "GRASIM": 0.30, "HAL": 0.35,
    "HEROMOTOCO": 0.28, "ICICIBANK": 0.25, "INDUSINDBK": 0.35, "KOTAKBANK": 0.25,
    "M&M": 0.30, "POWERGRID": 0.25, "TATACONSUM": 0.28, "TECHM": 0.30,
    "TITAN": 0.28, "TRENT": 0.35, "PNB": 0.40, "CANFINHOME": 0.35,
    "COFORGE": 0.30, "CROMPTON": 0.30, "DEEPAKNTR": 0.30, "DIXON": 0.35,
    "FEDERALBNK": 0.32, "GODREJCP": 0.25, "IDFCFIRSTB": 0.38,
    "IRCTC": 0.35, "JSWENERGY": 0.35, "JUBLFOOD": 0.28, "LICI": 0.30,
    "MOTHERSON": 0.32, "MUTHOOTFIN": 0.28, "NAUKRI": 0.30, "PAGEIND": 0.28,
    "PERSISTENT": 0.35, "PIIND": 0.28, "POLYCAB": 0.30, "SAIL": 0.40,
    "SBICARD": 0.28, "SBILIFE": 0.25, "SIEMENS": 0.28, "TATAELXSI": 0.35,
    "ZOMATO": 0.40,
}


def find_option_key(stock, strike, opt_type="PE"):
    strike_int = int(strike)
    for inst in master:
        if inst.get("segment") != "NSE_FO":
            continue
        tsym = (inst.get("trading_symbol") or "").upper()
        if stock.upper() not in tsym:
            continue
        itype = (inst.get("instrument_type") or "").upper()
        if opt_type == "PE" and itype not in ("PE", "PUT"):
            continue
        if opt_type == "CE" and itype not in ("CE", "CALL"):
            continue
        if str(strike_int) not in tsym:
            continue
        exp = inst.get("expiry") or ""
        exp_str = str(exp)
        if "2026-09" in exp_str or "2026-10" in exp_str or "25SEP" in tsym or "25OCT" in tsym:
            return inst.get("instrument_key"), tsym
    return None, None


def find_eq_key(name):
    for inst in master:
        if inst.get("segment") == "NSE_EQ":
            tsym = (inst.get("trading_symbol") or "").upper()
            if tsym == name.upper():
                return inst.get("instrument_key")
    return None


def _monthly_expiry(dt):
    import calendar
    y, m = dt.year, dt.month
    if dt.day > 25:
        m += 1
        if m > 12:
            m = 1; y += 1
    last_day = calendar.monthrange(y, m)[1]
    d = datetime(y, m, last_day).date()
    while d.weekday() != 3:
        d -= timedelta(days=1)
    return d


def calc_charges(entry_price, exit_price, qty):
    buy_to = entry_price * qty
    sell_to = exit_price * qty
    total_to = buy_to + sell_to
    brokerage = 40.0
    stt = sell_to * 0.001
    exchange_txn = total_to * 0.000495
    sebi = total_to * 0.000001
    stamp = buy_to * 0.00003
    gst = (brokerage + exchange_txn) * 0.18
    return brokerage + stt + exchange_txn + sebi + stamp + gst


def get_candles(stock_name, strike, opt_type, trade_date, entry_dt):
    """Fetch 1-min candles. Returns (candle_list, is_bs_estimated)."""
    from_dt = datetime.combine(trade_date, datetime.min.time()).replace(hour=9, minute=15)
    to_dt = datetime.combine(trade_date, datetime.min.time()).replace(hour=15, minute=30)

    opt_key, opt_tsym = find_option_key(stock_name, strike, opt_type)
    if opt_key:
        try:
            candles = ud.historical_data(opt_key, from_dt, to_dt, "1minute")
            time.sleep(0.6)
            if candles:
                return candles, False
        except Exception:
            pass

    eq_key = find_eq_key(stock_name)
    if not eq_key:
        return None, True
    try:
        candles = ud.historical_data(eq_key, from_dt, to_dt, "1minute")
        time.sleep(0.4)
        return candles, True
    except Exception:
        return None, True


def simulate_trade(tid, ts, sym, qty, entry, sl_price, target_price, old_exit, old_pnl, old_status):
    """Replay a trade with OLD vs NEW exit logic using 1-min candles."""
    parts = sym.strip().split()
    stock_name = parts[0]
    strike = float(parts[1])
    opt_type = "CE" if len(parts) > 2 and "CE" in parts[-1].upper() else "PE"

    entry_dt = datetime.fromisoformat(ts)
    trade_date = entry_dt.date()
    entry_mins = entry_dt.hour * 60 + entry_dt.minute

    floor_price = round(entry * FLOOR_MULT, 2)
    full_tgt = round(entry * TARGET_MULT, 2)
    lot_key = stock_name.upper()
    one_lot = config.LOT_SIZES.get(lot_key, qty // 2)
    is_two_lot = qty >= one_lot * 2

    candles, use_bs = get_candles(stock_name, strike, opt_type, trade_date, entry_dt)
    if not candles:
        return None

    iv = STOCK_IV.get(stock_name, 0.30)
    expiry = _monthly_expiry(trade_date)
    dte = (expiry - trade_date).days
    bs_fn = bs_put if opt_type == "PE" else bs_call

    # ── State for OLD logic simulation ──
    old_sim = {
        "active": True, "exit_price": None, "exit_reason": None, "exit_time": None,
        "qty": qty, "peak_net": 0,
    }
    # ── State for NEW logic simulation ──
    new_sim = {
        "active": True, "exit_price": None, "exit_reason": None, "exit_time": None,
        "qty": qty, "peak_net": 0,
        "partial_done": False, "partial_pnl": 0,
        "remain_qty": qty, "remain_sl": sl_price, "remain_tgt": target_price,
    }

    for c in candles:
        ct = c.get("date") or c.get("timestamp") or c.get("ts") or ""
        if isinstance(ct, str):
            try:
                cdt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            except:
                continue
        else:
            cdt = ct
        try:
            cdt_ist = cdt.astimezone(IST)
        except:
            cdt_ist = cdt
        cmins = cdt_ist.hour * 60 + cdt_ist.minute
        if cmins < entry_mins:
            continue

        timestr = f"{cdt_ist.hour}:{cdt_ist.minute:02d}"

        if use_bs:
            spot_high = c["high"]
            spot_low = c["low"]
            spot_close = c["close"]
            mins_in_day = cmins - (9 * 60 + 15)
            T = max((dte - mins_in_day / (6.25 * 60)) / 365, 0.0001)
            pe_high = bs_fn(spot_low, strike, T, iv)
            pe_low = bs_fn(spot_high, strike, T, iv)
            pe_close = bs_fn(spot_close, strike, T, iv)
        else:
            pe_high = c["high"]
            pe_low = c["low"]
            pe_close = c["close"]

        # ── OLD logic: original ATR SL + 2x target, ₹8K cap, ₹1,500 floor step ──
        if old_sim["active"]:
            o_gross = (pe_close - entry) * old_sim["qty"]
            o_charges = calc_charges(entry, pe_close, old_sim["qty"])
            o_net = o_gross - o_charges
            old_sim["peak_net"] = max(old_sim["peak_net"], o_net)

            if pe_high >= target_price:
                old_sim["exit_price"] = target_price
                old_sim["exit_reason"] = "target_hit"
                old_sim["exit_time"] = timestr
                old_sim["active"] = False
            elif pe_low <= sl_price:
                old_sim["exit_price"] = sl_price
                old_sim["exit_reason"] = "sl_hit"
                old_sim["exit_time"] = timestr
                old_sim["active"] = False
            elif o_net <= -OLD_MAX_LOSS:
                old_sim["exit_price"] = pe_close
                old_sim["exit_reason"] = "max_loss_8k"
                old_sim["exit_time"] = timestr
                old_sim["active"] = False
            else:
                peak = old_sim["peak_net"]
                if peak >= FLOOR_STEP:
                    stepped = int(peak // FLOOR_STEP) * FLOOR_STEP
                    if o_net <= stepped:
                        old_sim["exit_price"] = pe_close
                        old_sim["exit_reason"] = f"floor_{stepped}"
                        old_sim["exit_time"] = timestr
                        old_sim["active"] = False

        # ── NEW logic: ₹5K SL cap, partial exit at floor, stepping floor ──
        if new_sim["active"]:
            rq = new_sim["remain_qty"]
            rsl = new_sim["remain_sl"]
            rtgt = new_sim["remain_tgt"]
            n_gross = (pe_close - entry) * rq
            n_charges = calc_charges(entry, pe_close, rq)
            n_net = n_gross - n_charges + new_sim["partial_pnl"]
            new_sim["peak_net"] = max(new_sim["peak_net"], n_net)

            if not new_sim["partial_done"] and is_two_lot and pe_high >= floor_price:
                # Partial exit: sell 1 lot at floor price
                p_gross = (floor_price - entry) * one_lot
                p_charges = calc_charges(entry, floor_price, one_lot)
                new_sim["partial_pnl"] = p_gross - p_charges
                new_sim["partial_done"] = True
                new_sim["remain_qty"] = rq - one_lot
                new_sim["remain_sl"] = entry  # SL moves to cost
                new_sim["remain_tgt"] = full_tgt  # TGT becomes 2x
                rq = new_sim["remain_qty"]
                rsl = new_sim["remain_sl"]
                rtgt = new_sim["remain_tgt"]
                # Recalculate net for remaining lot
                n_gross = (pe_close - entry) * rq
                n_charges = calc_charges(entry, pe_close, rq)
                n_net = n_gross - n_charges + new_sim["partial_pnl"]
                new_sim["peak_net"] = max(new_sim["peak_net"], n_net)

            if pe_high >= rtgt:
                new_sim["exit_price"] = rtgt
                new_sim["exit_reason"] = "target_hit" if new_sim["partial_done"] else "full_target"
                new_sim["exit_time"] = timestr
                new_sim["active"] = False
            elif pe_low <= rsl:
                new_sim["exit_price"] = rsl
                if new_sim["partial_done"]:
                    new_sim["exit_reason"] = "runner_sl_at_cost"
                else:
                    new_sim["exit_reason"] = "sl_hit"
                new_sim["exit_time"] = timestr
                new_sim["active"] = False
            else:
                # ₹5K max loss cap (only matters if partial not yet done)
                cur_net = n_net
                if cur_net <= -NEW_MAX_LOSS:
                    new_sim["exit_price"] = pe_close
                    new_sim["exit_reason"] = "max_loss_5k"
                    new_sim["exit_time"] = timestr
                    new_sim["active"] = False
                else:
                    # Stepping floor
                    peak = new_sim["peak_net"]
                    if peak >= FLOOR_STEP:
                        stepped = int(peak // FLOOR_STEP) * FLOOR_STEP
                        if cur_net <= stepped:
                            new_sim["exit_price"] = pe_close
                            new_sim["exit_reason"] = f"floor_{stepped}"
                            new_sim["exit_time"] = timestr
                            new_sim["active"] = False

        if not old_sim["active"] and not new_sim["active"]:
            break

    # Calculate final P&L
    def _pnl(sim, partial_pnl=0):
        if not sim["exit_price"]:
            return 0
        gross = (sim["exit_price"] - entry) * sim.get("remain_qty", sim["qty"])
        ch = calc_charges(entry, sim["exit_price"], sim.get("remain_qty", sim["qty"]))
        return gross - ch + partial_pnl

    old_final_pnl = _pnl(old_sim)
    if old_sim["active"]:
        old_final_pnl = old_pnl or 0

    new_final_pnl = _pnl(new_sim, new_sim["partial_pnl"])
    if new_sim["active"] and old_status in ("CLOSED", "CLOSED_SL", "CLOSED_TGT"):
        # B-S estimation couldn't replicate actual exits — use DB P&L as fallback
        new_final_pnl = old_pnl or 0
        new_sim["exit_reason"] = f"{old_status}_est"
        bs_fallback = True
    else:
        bs_fallback = False

    return {
        "tid": tid, "sym": sym, "date": trade_date, "entry": entry,
        "qty": qty, "sl": sl_price, "tgt": target_price,
        "old_pnl": old_final_pnl,
        "old_reason": old_sim["exit_reason"] or old_status,
        "old_time": old_sim["exit_time"] or "—",
        "new_pnl": new_final_pnl,
        "new_reason": new_sim["exit_reason"] or "open",
        "new_time": new_sim["exit_time"] or "—",
        "partial_done": new_sim["partial_done"],
        "partial_pnl": new_sim["partial_pnl"],
        "use_bs": use_bs,
        "old_active": old_sim["active"],
        "new_active": new_sim["active"],
        "bs_fallback": bs_fallback,
    }


# ── Main ──
days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 7
cutoff = (datetime.now(IST) - timedelta(days=days_back)).strftime("%Y-%m-%d")

with get_conn() as conn:
    rows = conn.execute(
        "SELECT id, ts, symbol, qty, price, exit_price, pnl, stop_price, "
        "target_price, status, peak_price "
        "FROM trades WHERE channel='oeh' AND date(ts) >= ? ORDER BY ts",
        (cutoff,)
    ).fetchall()

print(f"\n{'#'*120}")
print(f"  OEH BACKTEST — NEW RULES vs OLD (past {days_back} days, since {cutoff})")
print(f"  Rules: ₹5K SL cap | Partial profit (sell 1 lot @1.5x, runner to 2x) | ₹1,500 stepping floor")
print(f"{'#'*120}")
print(f"  Found {len(rows)} OEH trades\n")

results = []
for r in rows:
    tid, ts, sym, qty, entry, exit_price, pnl, sl_price, target_price, status, peak = r
    if not sl_price or not entry:
        continue
    if not target_price:
        target_price = round(entry * FLOOR_MULT, 2)

    print(f"  Simulating trade #{tid}: {sym} (entry={entry:.2f}, qty={qty})...", end=" ", flush=True)
    result = simulate_trade(tid, ts, sym, qty, entry, sl_price, target_price, exit_price, pnl, status)
    if result:
        results.append(result)
        delta = result["new_pnl"] - result["old_pnl"]
        icon = "+" if delta > 0 else ""
        bs_tag = " [B-S]" if result["use_bs"] else ""
        fb_tag = " [FALLBACK]" if result.get("bs_fallback") else ""
        print(f"OLD: ₹{result['old_pnl']:+,.0f} ({result['old_reason']}) → "
              f"NEW: ₹{result['new_pnl']:+,.0f} ({result['new_reason']}) "
              f"Δ: {icon}₹{delta:,.0f}{bs_tag}{fb_tag}")
    else:
        print("SKIP (no candle data)")

# ── Summary table ──
print(f"\n{'='*120}")
print(f"  {'ID':>5} {'Date':>10} {'Symbol':25} {'Qty':>5} {'Entry':>7} "
      f"{'OLD P&L':>10} {'OLD Reason':>16} {'NEW P&L':>10} {'NEW Reason':>20} {'Delta':>10} {'Partial':>8}")
print(f"  {'─'*115}")

old_total = 0
new_total = 0
partial_count = 0
fallback_count = 0
new_wins = 0
new_losses = 0
old_wins = 0
old_losses = 0
sl_saved = 0
for r in results:
    delta = r["new_pnl"] - r["old_pnl"]
    icon = "🟢" if delta > 0 else "🔴" if delta < 0 else "⚪"
    partial_tag = f"₹{r['partial_pnl']:+,.0f}" if r["partial_done"] else "—"
    fb_tag = " *" if r.get("bs_fallback") else ""
    print(f"  {r['tid']:>5} {r['date'].strftime('%m/%d'):>10} {r['sym']:25} {r['qty']:>5} {r['entry']:>7.2f} "
          f"{r['old_pnl']:>+10,.0f} {r['old_reason']:>16} "
          f"{r['new_pnl']:>+10,.0f} {r['new_reason']:>20} "
          f"{delta:>+10,.0f} {icon} {partial_tag:>8}{fb_tag}")
    old_total += r["old_pnl"]
    new_total += r["new_pnl"]
    if r["partial_done"]:
        partial_count += 1
    if r.get("bs_fallback"):
        fallback_count += 1
    if r["new_pnl"] > 0: new_wins += 1
    elif r["new_pnl"] < 0: new_losses += 1
    if r["old_pnl"] > 0: old_wins += 1
    elif r["old_pnl"] < 0: old_losses += 1
    if "max_loss_5k" in (r["new_reason"] or ""):
        old_loss = r["old_pnl"] if r["old_pnl"] < 0 else 0
        sl_saved += (r["new_pnl"] - old_loss)

# ── Grand summary ──
print(f"\n  (* = B-S fallback: sim couldn't replicate, used actual DB P&L)")
print(f"\n{'='*120}")
print(f"  SUMMARY")
print(f"  {'─'*50}")
print(f"  {'Trades analyzed:':35} {len(results)}")
print(f"  {'B-S fallback (same P&L):':35} {fallback_count} trades")
print(f"  {'Partial profit booked:':35} {partial_count} trades")
print(f"  {'':35}")
print(f"  {'OLD win/loss:':35} {old_wins}W / {old_losses}L")
print(f"  {'NEW win/loss:':35} {new_wins}W / {new_losses}L")
print(f"  {'':35}")
print(f"  {'OLD strategy P&L:':35} ₹{old_total:>+10,.0f}")
print(f"  {'NEW strategy P&L:':35} ₹{new_total:>+10,.0f}")
print(f"  {'─'*50}")
delta_total = new_total - old_total
pct = (delta_total / abs(old_total) * 100) if old_total else 0
icon = "🟢" if delta_total > 0 else "🔴"
print(f"  {icon} {'Improvement:':33} ₹{delta_total:>+10,.0f} ({pct:+.0f}%)")
if sl_saved:
    print(f"  {'₹5K SL cap saved:':35} ₹{sl_saved:>+10,.0f}")
print(f"{'='*120}")

# ── Per-day breakdown ──
days = {}
for r in results:
    d = r["date"]
    if d not in days:
        days[d] = {"old": 0, "new": 0, "count": 0}
    days[d]["old"] += r["old_pnl"]
    days[d]["new"] += r["new_pnl"]
    days[d]["count"] += 1

if days:
    print(f"\n  PER-DAY BREAKDOWN:")
    print(f"  {'Date':>12} {'Trades':>8} {'OLD P&L':>12} {'NEW P&L':>12} {'Delta':>12}")
    print(f"  {'─'*60}")
    for d in sorted(days):
        dd = days[d]
        delta = dd["new"] - dd["old"]
        icon = "🟢" if delta > 0 else "🔴" if delta < 0 else "⚪"
        print(f"  {d.strftime('%Y-%m-%d'):>12} {dd['count']:>8} "
              f"{dd['old']:>+12,.0f} {dd['new']:>+12,.0f} {delta:>+12,.0f} {icon}")
    print(f"  {'─'*60}")
    print(f"  {'TOTAL':>12} {len(results):>8} {old_total:>+12,.0f} {new_total:>+12,.0f} {new_total-old_total:>+12,.0f}")

print(f"\n{'#'*120}")
print(f"  BACKTEST COMPLETE")
print(f"{'#'*120}")

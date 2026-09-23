"""Debug why stock strategy had 0 trades on a given date."""
from datetime import date, timedelta
from src.strategy.stock_runner import (
    STOCKS, STRATEGIES, UpstoxData, fetch_daily_candles,
    _find_signal_for_date, _days_to_expiry, _monthly_expiry_for,
    calc_ema, calc_rsi, _has_active_trade, init_stock_strategy_db,
)
from src.storage import db

ref = date(2026, 9, 23)
dte = _days_to_expiry(ref)
exp = _monthly_expiry_for(ref)
print(f"Date: {ref} | DTE: {dte} | Expiry: {exp}\n")

for sname, params in STRATEGIES.items():
    mn, mx = params["entry_dte_range"]
    ok = mn <= dte <= mx
    print(f"  {sname}: range={mn}-{mx} -> {'IN' if ok else 'OUT'}")

print()
uclient = UpstoxData()
buf_start = ref - timedelta(days=60)

init_stock_strategy_db()

for sname_strat, params in STRATEGIES.items():
    mn, mx = params["entry_dte_range"]
    if not (mn <= dte <= mx):
        continue
    print(f"\n=== {sname_strat} (DTE {dte} in range {mn}-{mx}) ===")
    for stock in STOCKS:
        daily = fetch_daily_candles(uclient, stock, buf_start, ref + timedelta(days=35))
        sig = _find_signal_for_date(daily, ref, stock, params)
        if sig:
            with db.get_conn() as conn:
                active = _has_active_trade(conn, sname_strat, stock, sig["expiry"])
            status = "ACTIVE_POS" if active else "READY"
            print(f"  SIGNAL: {stock} {sig['direction']} RSI={sig['rsi']} EMA={sig['ema']} spot={sig['spot']} -> {status}")
        else:
            closes = [c["close"] for c in daily if c["date"][:10] <= ref.isoformat()]
            if len(closes) >= 20:
                ema = calc_ema(closes, 20)
                rsi = calc_rsi(closes, 14)
                spot = closes[-1]
                above = "above" if spot > ema else "below"
                print(f"  {stock}: spot={spot:.1f} {above} ema={ema:.1f} rsi={rsi:.1f}")

"""Check CH2 Nov 12 results against actual 1-min candles."""
import requests, json
from datetime import datetime

token_data = json.load(open("data/.upstox_session.json"))
access_token = token_data["access_token"]
headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

trades = [
    {"name": "NIFTY 25400 CE", "key": "NSE_FO|45406", "entry_time": "2026-11-12 09:33", "entry": 188.09, "qty": 130},
    {"name": "SENSEX 82300 PE", "key": "BSE_FO|822240", "entry_time": "2026-11-12 11:17", "entry": 355.52, "qty": 40},
    {"name": "SENSEX 82000 PE", "key": "BSE_FO|821942", "entry_time": "2026-11-12 12:18", "entry": 313.86, "qty": 40},
    {"name": "SENSEX 81700 CE", "key": "BSE_FO|1165492", "entry_time": "2026-11-12 13:03", "entry": 303.46, "qty": 40},
    {"name": "SENSEX 82100 CE", "key": "BSE_FO|822015", "entry_time": "2026-11-12 14:03", "entry": 76.08, "qty": 40},
    {"name": "SENSEX 81900 CE", "key": "BSE_FO|821729", "entry_time": "2026-11-12 15:03", "entry": 145.83, "qty": 40},
]

total = 0
for t in trades:
    url = f"https://api.upstox.com/v2/historical-candle/intraday/{t['key']}/1minute"
    resp = requests.get(url, headers=headers, timeout=15)
    data = resp.json()
    candles = data.get("data", {}).get("candles", [])
    entry_dt = datetime.strptime(t["entry_time"], "%Y-%m-%d %H:%M")
    candles.sort(key=lambda c: c[0])

    relevant = []
    for c in candles:
        ct = datetime.strptime(c[0][:19], "%Y-%m-%dT%H:%M:%S")
        if ct >= entry_dt:
            relevant.append({"ts": ct, "o": c[1], "h": c[2], "l": c[3], "c": c[4]})

    if not relevant:
        print(f"{t['name']}: NO CANDLES")
        continue

    entry_price = t["entry"]
    qty = t["qty"]
    floor_levels = [1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000]
    max_loss = 2000
    peak_pnl = 0
    current_floor = 0
    exit_price = None
    exit_reason = None

    for candle in relevant:
        high_pnl = (candle["h"] - entry_price) * qty
        if high_pnl > peak_pnl:
            peak_pnl = high_pnl
            for lvl in floor_levels:
                if peak_pnl >= lvl and lvl > current_floor:
                    current_floor = lvl

        low_pnl = (candle["l"] - entry_price) * qty
        if low_pnl <= -max_loss:
            exit_price = entry_price - (max_loss / qty)
            exit_reason = f"LOSS CAP -Rs{max_loss}"
            break
        if current_floor > 0 and low_pnl < current_floor:
            exit_price = entry_price + (current_floor / qty)
            exit_reason = f"FLOOR Rs{current_floor}"
            break

    if exit_price is None:
        last_c = relevant[-1]["c"]
        exit_price = last_c
        exit_reason = "EOD"
    final_pnl = (exit_price - entry_price) * qty
    total += final_pnl

    peak_high = max(c["h"] for c in relevant)
    lowest_low = min(c["l"] for c in relevant)
    print(f"--- {t['name']} (entry Rs{entry_price}, {t['entry_time']}) ---")
    print(f"  Candles: {len(relevant)} | Peak high: {peak_high} | Low: {lowest_low}")
    print(f"  Peak PNL: Rs{peak_pnl:.0f} | Floor: Rs{current_floor} | Exit: Rs{exit_price:.2f} ({exit_reason})")
    print(f"  PNL: Rs{final_pnl:.0f}")
    print()

print(f"=== TOTAL: Rs{total:.0f} ===")

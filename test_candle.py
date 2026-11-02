#!/usr/bin/env python3
import json, gzip, requests, urllib.parse

url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
r = requests.get(url)
master = json.loads(gzip.decompress(r.content))
print(f"Total instruments: {len(master)}")

found = None
for inst in master:
    if not isinstance(inst, dict):
        continue
    asset = (inst.get("asset_symbol") or "").upper()
    itype = inst.get("instrument_type", "")
    seg = inst.get("segment", "")
    if asset == "M&M" and itype == "PE" and seg == "NSE_FO":
        sp = inst.get("strike_price")
        if sp is not None and abs(float(sp) - 3000) < 1:
            found = inst
            break

if not found:
    # Try relaxed search
    for inst in master:
        if not isinstance(inst, dict):
            continue
        asset = (inst.get("asset_symbol") or "").upper()
        if "M&M" in asset or "M_M" in asset:
            itype = inst.get("instrument_type", "")
            if itype == "PE":
                sp = inst.get("strike_price")
                if sp is not None and abs(float(sp) - 3000) < 1:
                    found = inst
                    print(f"Found via relaxed: asset={asset}")
                    break

if found:
    ikey = found["instrument_key"]
    exp_raw = found.get("expiry", "")
    print(f"Found: M&M 3000 PE exp_raw={exp_raw} type={type(exp_raw)} key={ikey}")
    print(f"Full inst: {json.dumps({k:str(v) for k,v in found.items()})}")

    enc_key = urllib.parse.quote(ikey, safe="")
    candle_url = f"https://api.upstox.com/v2/historical-candle/{enc_key}/1minute/2026-10-01/2026-10-01"
    cr = requests.get(candle_url, headers={"Accept": "application/json"})
    cd = cr.json()
    candles = cd.get("data", {}).get("candles", [])
    print(f"Candles: {len(candles)}")
    if candles:
        print(f"Sample: {candles[-1]}")
else:
    print("M&M 3000 PE not found")
    # Show what M&M options exist
    mm_options = [i for i in master if isinstance(i, dict) and (i.get("asset_symbol") or "").upper() == "M&M" and i.get("segment") == "NSE_FO"]
    print(f"M&M NSE_FO options: {len(mm_options)}")
    if mm_options:
        strikes = set(float(i.get("strike_price", 0)) for i in mm_options[:20])
        print(f"Sample strikes: {sorted(strikes)[:10]}")
        print(f"Sample: {mm_options[0]}")

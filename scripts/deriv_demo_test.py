"""Deriv API Demo Test — PAT + App ID (REST/WebSocket hybrid).

Run on VM:
    cd ~/Trading-Buddy && .venv/bin/python scripts/deriv_demo_test.py
"""
import asyncio
import json
import sys

try:
    import websockets
except ImportError:
    print("Install: pip install websockets")
    sys.exit(1)

PAT_TOKEN = "pat_04ee674de985b74c6cd04abe3dfa25ec50be32e8759c306b24209bc4e7c56851"
APP_ID = "34BQhpQsZoNW0za6aNV7F"

# Cloudflare Worker proxy — deploy scripts/deriv_ws_proxy/ first
# Replace with your actual worker URL after deploying
PROXY_HOST = "deriv-ws-proxy.tbprasanth.workers.dev"

ENDPOINTS = [
    # Proxy first (bypasses Cloudflare bot detection from India)
    f"wss://{PROXY_HOST}/websockets/v3?app_id={APP_ID}",
    # Direct endpoints as fallback
    f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
    f"wss://ws.binaryws.com/websockets/v3?app_id={APP_ID}",
]

async def send_recv(ws, msg):
    await ws.send(json.dumps(msg))
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=15))


async def ws_connect(uri):
    """Connect with or without extra_headers depending on websockets version."""
    try:
        return await websockets.connect(uri, additional_headers={
            "Origin": "https://app.deriv.com",
        })
    except TypeError:
        try:
            return await websockets.connect(uri, extra_headers={
                "Origin": "https://app.deriv.com",
            })
        except TypeError:
            return await websockets.connect(uri)


async def main():
    ws = None
    for uri in ENDPOINTS:
        try:
            print(f"Trying {uri[:50]}...", end=" ", flush=True)
            ws = await asyncio.wait_for(ws_connect(uri), timeout=10)
            print("Connected!")
            break
        except Exception as e:
            print(f"Failed: {e}")
            ws = None

    if not ws:
        print("\nTrying with default app_id 1089...")
        for uri in [
            "wss://ws.derivws.com/websockets/v3?app_id=1089",
            "wss://ws.binaryws.com/websockets/v3?app_id=1089",
        ]:
            try:
                print(f"  {uri[:50]}...", end=" ", flush=True)
                ws = await asyncio.wait_for(ws_connect(uri), timeout=10)
                print("Connected!")
                break
            except Exception as e:
                print(f"Failed: {e}")
                ws = None

    if not ws:
        print("\n\nAll connection methods failed.")
        print("Possible issues:")
        print("  1. Deriv may be blocked in your region/IP")
        print("  2. App ID format might need to be numeric")
        print("  3. Try: curl -v https://ws.derivws.com 2>&1 | head -20")
        print("  4. Try accessing deriv.com from your browser on the VM")
        return

    try:
        # 1. Authorize
        print("\n" + "=" * 80)
        print("  STEP 1: Authorize")
        print("=" * 80)
        resp = await send_recv(ws, {"authorize": PAT_TOKEN})
        if "error" in resp:
            print(f"  AUTH ERROR: {resp['error']}")
            # Try without PAT prefix
            print("  Trying token without pat_ prefix...")
            token_short = PAT_TOKEN.replace("pat_", "")
            resp = await send_recv(ws, {"authorize": token_short})
            if "error" in resp:
                print(f"  Still error: {resp['error']}")
                print("\n  You may need an old-style API token:")
                print("  deriv.com → Settings → Security → API Token")
                return

        auth = resp.get("authorize", {})
        print(f"  Account:  {auth.get('loginid')}")
        print(f"  Balance:  {auth.get('balance')} {auth.get('currency')}")
        print(f"  Virtual:  {auth.get('is_virtual', '?')}")

        # 2. Get forex symbols
        print("\n" + "=" * 80)
        print("  STEP 2: Available Forex Pairs")
        print("=" * 80)
        resp = await send_recv(ws, {"active_symbols": "brief", "product_type": "basic"})
        if "error" in resp:
            print(f"  Error: {resp['error']}")
        else:
            symbols = resp.get("active_symbols", [])
            forex = [s for s in symbols if s.get("market") == "forex"]
            print(f"  Total forex pairs: {len(forex)}")

            our_pairs = {}
            for s in forex:
                sym = s["symbol"].lower()
                name = s.get("display_name", "").lower()
                if "eurgbp" in sym or ("eur" in name and "gbp" in name):
                    our_pairs["EUR/GBP"] = s
                if "cadchf" in sym or ("cad" in name and "chf" in name):
                    our_pairs["CAD/CHF"] = s

            if our_pairs:
                for label, s in our_pairs.items():
                    print(f"  ✓ {label}: symbol={s['symbol']}, spot={s.get('spot','?')}")
            else:
                print("  EUR/GBP / CAD/CHF not found. All forex:")
                for s in sorted(forex, key=lambda x: x.get("display_name", "")):
                    print(f"    {s['symbol']:<15s} {s.get('display_name','')}")

            # 3. Contracts
            print("\n" + "=" * 80)
            print("  STEP 3: Contracts")
            print("=" * 80)
            test_syms = list(our_pairs.values()) if our_pairs else []
            for fallback in ["frxEURGBP", "frxCADCHF", "frxEURUSD", "R_50", "R_100", "1HZ100V"]:
                if not any(s["symbol"] == fallback for s in test_syms):
                    test_syms.append({"symbol": fallback, "display_name": fallback})

            for s in test_syms:
                sym = s["symbol"]
                try:
                    resp = await send_recv(ws, {"contracts_for": sym, "product_type": "basic"})
                    if "error" in resp:
                        continue
                    avail = resp.get("contracts_for", {}).get("available", [])
                    call_put = [c for c in avail if c.get("contract_type") in ("CALL", "PUT")]
                    if call_put or avail:
                        print(f"\n  {sym} ({s.get('display_name', '')}):")
                        for c in avail[:15]:
                            ct = c.get("contract_type", "")
                            cat = c.get("contract_category_display", "")
                            dur_min = c.get("min_contract_duration", "")
                            dur_max = c.get("max_contract_duration", "")
                            print(f"    {ct:<12s} | {cat:<25s} | {dur_min} - {dur_max}")
                except Exception:
                    pass

            # 4. Demo trade
            if our_pairs:
                print("\n" + "=" * 80)
                print("  STEP 4: Demo Trade")
                print("=" * 80)
                sym = list(our_pairs.values())[0]["symbol"]
                for dur, unit in [(1, "m"), (5, "t"), (2, "m"), (5, "m")]:
                    print(f"  Trying {sym} CALL {dur}{unit} $1...", end=" ", flush=True)
                    resp = await send_recv(ws, {
                        "proposal": 1, "amount": "1", "basis": "stake",
                        "contract_type": "CALL", "currency": "USD",
                        "duration": dur, "duration_unit": unit, "symbol": sym,
                    })
                    if "error" in resp:
                        print(f"Error: {resp['error'].get('message','')}")
                        continue
                    p = resp["proposal"]
                    print(f"OK! payout={p.get('payout')} ask={p.get('ask_price')} spot={p.get('spot')}")

                    print(f"  Buying demo trade...")
                    buy = await send_recv(ws, {"buy": p["id"], "price": float(p["ask_price"]) + 1})
                    if "error" in buy:
                        print(f"  Buy error: {buy['error'].get('message','')}")
                    else:
                        b = buy.get("buy", {})
                        print(f"  ✓ TRADE PLACED! contract_id={b.get('contract_id')} buy_price={b.get('buy_price')} payout={b.get('payout')}")
                    break

        print("\n" + "=" * 80)
        print("  DONE")
        print("=" * 80)

    finally:
        await ws.close()


asyncio.run(main())

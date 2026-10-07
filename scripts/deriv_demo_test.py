"""Deriv API Demo Test — check pairs, contracts, and place a test trade.

Run on VM:
    cd ~/Trading-Buddy && .venv/bin/pip install websockets
    cd ~/Trading-Buddy && .venv/bin/python scripts/deriv_demo_test.py
"""
import asyncio
import json
import sys

try:
    import websockets
except ImportError:
    print("Install websockets: pip install websockets")
    sys.exit(1)

TOKEN = "pat_04ee674de985b74c6cd04abe3dfa25ec50be32e8759c306b24209bc4e7c56851"
ENDPOINTS = [
    "wss://ws.derivws.com/websockets/v3?app_id=1089",
    "wss://ws.binaryws.com/websockets/v3?app_id=1089",
    "wss://green.derivws.com/websockets/v3?app_id=1089",
    "wss://blue.derivws.com/websockets/v3?app_id=1089",
]

async def send_recv(ws, msg):
    await ws.send(json.dumps(msg))
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=15))


async def main():
    # Try endpoints until one works
    ws = None
    for uri in ENDPOINTS:
        try:
            print(f"Trying {uri[:40]}...", end=" ", flush=True)
            ws = await asyncio.wait_for(websockets.connect(uri), timeout=10)
            print("Connected!")
            break
        except Exception as e:
            print(f"Failed: {e}")
            ws = None

    if not ws:
        print("\nAll endpoints failed. Check network/firewall.")
        return

    try:
        # 1. Authorize
        print("\n" + "=" * 80)
        print("  STEP 1: Authorize")
        print("=" * 80)
        resp = await send_recv(ws, {"authorize": TOKEN})
        if "error" in resp:
            print(f"  AUTH ERROR: {resp['error']}")
            print(f"  Full response: {json.dumps(resp, indent=2)[:500]}")
            print("\n  Token might be wrong. Get it from: api.deriv.com → Manage Tokens")
            return
        auth = resp["authorize"]
        print(f"  Account:  {auth['loginid']}")
        print(f"  Balance:  {auth['balance']} {auth['currency']}")
        print(f"  Virtual:  {auth.get('is_virtual', '?')}")
        print(f"  Email:    {auth.get('email', '?')}")

        # 2. Get forex symbols
        print("\n" + "=" * 80)
        print("  STEP 2: Available Forex Pairs")
        print("=" * 80)
        resp = await send_recv(ws, {"active_symbols": "brief", "product_type": "basic"})
        symbols = resp.get("active_symbols", [])
        forex = [s for s in symbols if s.get("market") == "forex"]
        print(f"  Total forex pairs: {len(forex)}")

        # Find our pairs
        our_pairs = {}
        for s in forex:
            sym = s["symbol"].lower()
            name = s.get("display_name", "").lower()
            if "eurgbp" in sym or ("eur" in name and "gbp" in name):
                our_pairs["EUR/GBP"] = s
            if "cadchf" in sym or ("cad" in name and "chf" in name):
                our_pairs["CAD/CHF"] = s

        if our_pairs:
            print(f"\n  OUR PAIRS FOUND:")
            for label, s in our_pairs.items():
                print(f"    ✓ {label}: symbol={s['symbol']}, pip={s.get('pip','?')}, spot={s.get('spot','?')}")
        else:
            print(f"\n  EUR/GBP and CAD/CHF NOT found. All forex pairs:")
            for s in sorted(forex, key=lambda x: x.get("display_name", "")):
                print(f"    {s['symbol']:<15s} {s.get('display_name',''):<20s} {s.get('submarket_display_name','')}")

        # 3. Check contracts for each pair
        print("\n" + "=" * 80)
        print("  STEP 3: Available Contracts")
        print("=" * 80)

        check_symbols = list(our_pairs.values()) if our_pairs else []
        # Also check synthetic indices as fallback
        for test in ["frxEURGBP", "frxCADCHF", "frxEURUSD", "R_10", "R_50", "R_100", "1HZ10V", "1HZ100V"]:
            if not any(s["symbol"] == test for s in check_symbols):
                check_symbols.append({"symbol": test, "display_name": test})

        for s in check_symbols:
            sym = s["symbol"]
            try:
                resp = await send_recv(ws, {"contracts_for": sym, "product_type": "basic"})
                if "error" in resp:
                    continue
                avail = resp.get("contracts_for", {}).get("available", [])
                rise_fall = [c for c in avail if c.get("contract_type") in ("CALL", "PUT", "CALLE", "PUTE", "DIGITDIFF", "DIGITOVER", "DIGITUNDER")]

                if rise_fall:
                    print(f"\n  {sym} ({s.get('display_name', '')}):")
                    for c in avail:
                        ct = c.get("contract_type", "")
                        cat = c.get("contract_category_display", "")
                        dur_min = c.get("min_contract_duration", "")
                        dur_max = c.get("max_contract_duration", "")
                        sub = c.get("submarket", "")
                        print(f"    {ct:<12s} | {cat:<25s} | duration: {dur_min} - {dur_max}")
            except Exception as e:
                pass

        # 4. Try a demo trade (Rise/Fall, smallest amount, 1 min)
        if our_pairs:
            print("\n" + "=" * 80)
            print("  STEP 4: Demo Trade Test")
            print("=" * 80)
            test_pair = list(our_pairs.values())[0]
            sym = test_pair["symbol"]
            print(f"  Getting price proposal for {sym} CALL 1min $1...")

            resp = await send_recv(ws, {
                "proposal": 1,
                "amount": "1",
                "basis": "stake",
                "contract_type": "CALL",
                "currency": "USD",
                "duration": 1,
                "duration_unit": "m",
                "symbol": sym,
            })

            if "error" in resp:
                print(f"  Proposal error: {resp['error'].get('message', resp['error'])}")
                # Try with different duration
                print(f"  Trying 5 ticks instead...")
                resp = await send_recv(ws, {
                    "proposal": 1,
                    "amount": "1",
                    "basis": "stake",
                    "contract_type": "CALL",
                    "currency": "USD",
                    "duration": 5,
                    "duration_unit": "t",
                    "symbol": sym,
                })
                if "error" in resp:
                    print(f"  Still error: {resp['error'].get('message', resp['error'])}")
                else:
                    p = resp["proposal"]
                    print(f"  ✓ Proposal OK!")
                    print(f"    Payout: {p.get('payout', '?')}")
                    print(f"    Ask price: {p.get('ask_price', '?')}")
                    print(f"    Spot: {p.get('spot', '?')}")
                    print(f"    ID: {p.get('id', '?')}")
            else:
                p = resp["proposal"]
                print(f"  ✓ Proposal OK!")
                print(f"    Payout: {p.get('payout', '?')}")
                print(f"    Ask price (stake): {p.get('ask_price', '?')}")
                print(f"    Spot: {p.get('spot', '?')}")
                print(f"    Proposal ID: {p.get('id', '?')}")

                # Buy it!
                print(f"\n  Buying demo trade...")
                buy_resp = await send_recv(ws, {
                    "buy": p["id"],
                    "price": float(p["ask_price"]) + 1,
                })
                if "error" in buy_resp:
                    print(f"  Buy error: {buy_resp['error'].get('message', buy_resp['error'])}")
                else:
                    b = buy_resp.get("buy", {})
                    print(f"  ✓ TRADE PLACED!")
                    print(f"    Contract ID: {b.get('contract_id', '?')}")
                    print(f"    Buy price: {b.get('buy_price', '?')}")
                    print(f"    Payout: {b.get('payout', '?')}")
                    print(f"    Start time: {b.get('start_time', '?')}")

        print("\n" + "=" * 80)
        print("  DONE — Copy the output above and share with me!")
        print("=" * 80)

    finally:
        await ws.close()


asyncio.run(main())

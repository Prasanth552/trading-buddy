"""Deriv API via headless browser — bypasses Cloudflare bot detection.

Install on VM:
    pip install playwright
    playwright install chromium

Run:
    python scripts/deriv_browser_test.py
"""
import asyncio
import json
import sys

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Install: pip install playwright && playwright install chromium")
    sys.exit(1)

PAT_TOKEN = "pat_04ee674de985b74c6cd04abe3dfa25ec50be32e8759c306b24209bc4e7c56851"
APP_ID = "34BQhpQsZoNW0za6aNV7F"
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        # Hide webdriver flag
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        # Navigate to deriv first to get proper cookies/origin
        print("Opening browser and loading Deriv...", flush=True)
        try:
            resp = await page.goto("https://deriv.com", wait_until="domcontentloaded", timeout=30000)
            print(f"  Page loaded. Status: {resp.status if resp else 'no response'}", flush=True)
            print(f"  URL: {page.url}", flush=True)
        except Exception as e:
            print(f"  Page load failed: {e}", flush=True)
            print("  Trying app.deriv.com instead...", flush=True)
            try:
                resp = await page.goto("https://app.deriv.com", wait_until="domcontentloaded", timeout=30000)
                print(f"  app.deriv.com loaded. Status: {resp.status if resp else 'no response'}", flush=True)
            except Exception as e2:
                print(f"  Also failed: {e2}", flush=True)

        # Create WebSocket connection from within the browser
        print(f"\nConnecting WebSocket via browser...", flush=True)
        result = await page.evaluate(f"""async () => {{
            const WS_URL = "{WS_URL}";
            const PAT_TOKEN = "{PAT_TOKEN}";

            function sendRecv(ws, msg) {{
                return new Promise((resolve, reject) => {{
                    const handler = (e) => {{
                        ws.removeEventListener('message', handler);
                        resolve(JSON.parse(e.data));
                    }};
                    ws.addEventListener('message', handler);
                    ws.send(JSON.stringify(msg));
                    setTimeout(() => reject(new Error('timeout')), 15000);
                }});
            }}

            try {{
                // Connect
                // Try multiple endpoints
                const endpoints = [
                    WS_URL,
                    WS_URL.replace('ws.derivws.com', 'ws.binaryws.com'),
                    WS_URL.replace('ws.derivws.com', 'green.derivws.com'),
                ];
                let ws = null;
                let lastErr = '';
                for (const url of endpoints) {{
                    try {{
                        ws = await new Promise((resolve, reject) => {{
                            const s = new WebSocket(url);
                            s.onopen = () => resolve(s);
                            s.onerror = (e) => reject(new Error('ws_error at ' + url));
                            s.onclose = (e) => reject(new Error('ws_closed code=' + e.code + ' reason=' + e.reason + ' at ' + url));
                            setTimeout(() => reject(new Error('ws_timeout at ' + url)), 10000);
                        }});
                        break;
                    }} catch (e) {{
                        lastErr = e.message;
                        ws = null;
                    }}
                }}
                if (!ws) return {{error: lastErr, tried: endpoints.length}};

                const results = {{}};
                results.connected = true;

                // Step 1: Authorize
                let auth = await sendRecv(ws, {{authorize: PAT_TOKEN}});
                if (auth.error) {{
                    // Try without pat_ prefix
                    auth = await sendRecv(ws, {{authorize: PAT_TOKEN.replace('pat_', '')}});
                }}
                if (auth.error) {{
                    results.auth_error = auth.error;
                    // Continue without auth for public endpoints
                }} else {{
                    const a = auth.authorize;
                    results.auth = {{
                        loginid: a.loginid,
                        balance: a.balance,
                        currency: a.currency,
                        is_virtual: a.is_virtual
                    }};
                }}

                // Step 2: Active symbols
                const syms = await sendRecv(ws, {{active_symbols: "brief", product_type: "basic"}});
                if (!syms.error) {{
                    const forex = syms.active_symbols.filter(s => s.market === 'forex');
                    results.total_forex = forex.length;
                    results.our_pairs = {{}};
                    for (const s of forex) {{
                        if (s.symbol.toLowerCase().includes('eurgbp'))
                            results.our_pairs['EUR/GBP'] = {{symbol: s.symbol, spot: s.spot, display: s.display_name}};
                        if (s.symbol.toLowerCase().includes('cadchf'))
                            results.our_pairs['CAD/CHF'] = {{symbol: s.symbol, spot: s.spot, display: s.display_name}};
                    }}

                    // Step 3: Contracts for our pairs
                    results.contracts = {{}};
                    for (const [label, info] of Object.entries(results.our_pairs)) {{
                        const c = await sendRecv(ws, {{contracts_for: info.symbol, product_type: "basic"}});
                        if (!c.error) {{
                            const avail = c.contracts_for.available || [];
                            results.contracts[label] = avail.map(x => ({{
                                type: x.contract_type,
                                category: x.contract_category_display,
                                min_dur: x.min_contract_duration,
                                max_dur: x.max_contract_duration
                            }}));
                        }}
                    }}

                    // Step 4: Demo trade (only if authed + virtual)
                    if (results.auth && results.auth.is_virtual) {{
                        const sym = Object.values(results.our_pairs)[0]?.symbol;
                        if (sym) {{
                            for (const [dur, unit] of [[1,'m'],[5,'t'],[2,'m'],[5,'m']]) {{
                                const prop = await sendRecv(ws, {{
                                    proposal: 1, amount: "1", basis: "stake",
                                    contract_type: "CALL", currency: "USD",
                                    duration: dur, duration_unit: unit, symbol: sym
                                }});
                                if (!prop.error) {{
                                    results.proposal = {{
                                        payout: prop.proposal.payout,
                                        ask_price: prop.proposal.ask_price,
                                        spot: prop.proposal.spot,
                                        duration: dur + unit
                                    }};
                                    // Buy it
                                    const buy = await sendRecv(ws, {{
                                        buy: prop.proposal.id,
                                        price: parseFloat(prop.proposal.ask_price) + 1
                                    }});
                                    if (!buy.error) {{
                                        results.trade = {{
                                            contract_id: buy.buy.contract_id,
                                            buy_price: buy.buy.buy_price,
                                            payout: buy.buy.payout
                                        }};
                                    }} else {{
                                        results.trade_error = buy.error;
                                    }}
                                    break;
                                }}
                            }}
                        }}
                    }}
                }}

                ws.close();
                return results;
            }} catch (e) {{
                return {{error: e.message}};
            }}
        }}""")

        await browser.close()

    # Print results
    print("\n" + "=" * 80)
    print("  RESULTS")
    print("=" * 80)

    if isinstance(result, dict) and result.get("error"):
        print(f"  ERROR: {result['error']}")
        return

    if result.get("connected"):
        print("  ✓ WebSocket connected!")

    if result.get("auth"):
        a = result["auth"]
        print(f"\n  Account:  {a['loginid']}")
        print(f"  Balance:  {a['balance']} {a['currency']}")
        print(f"  Virtual:  {a['is_virtual']}")
    elif result.get("auth_error"):
        print(f"\n  Auth error: {result['auth_error']}")
        print("  You may need an old-style API token from:")
        print("  deriv.com → Settings → Security & safety → API token")

    if result.get("total_forex"):
        print(f"\n  Total forex pairs: {result['total_forex']}")

    if result.get("our_pairs"):
        for label, info in result["our_pairs"].items():
            print(f"  ✓ {label}: {info['symbol']} spot={info['spot']}")

    if result.get("contracts"):
        for pair, contracts in result["contracts"].items():
            print(f"\n  {pair} contracts:")
            for c in contracts[:10]:
                print(f"    {c['type']:<12s} | {c['category']:<25s} | {c['min_dur']} - {c['max_dur']}")

    if result.get("proposal"):
        p = result["proposal"]
        print(f"\n  Proposal: {p['duration']} CALL, payout={p['payout']}, ask={p['ask_price']}, spot={p['spot']}")

    if result.get("trade"):
        t = result["trade"]
        print(f"  ✓ DEMO TRADE PLACED! contract_id={t['contract_id']} buy={t['buy_price']} payout={t['payout']}")
    elif result.get("trade_error"):
        print(f"  Trade error: {result['trade_error']}")

    print("\n" + "=" * 80)
    print("  DONE")
    print("=" * 80)


asyncio.run(main())

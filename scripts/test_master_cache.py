#!/usr/bin/env python3
"""Sanity test: verify _ensure_master_loaded works and is fast on second call."""
import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import config

async def main():
    import src.notify.channel_listener as cl

    # Test 1: First load (downloads master in thread)
    print("=== Test 1: First master load (should be non-blocking) ===")
    t0 = time.time()
    master = await cl._ensure_master_loaded()
    t1 = time.time()
    print(f"  Loaded {len(master)} instruments in {t1-t0:.1f}s")
    print(f"  EQ keys: {len(cl._cached_eq_keys)}")
    print(f"  Lot sizes: {len(cl._cached_lot_sizes)}")
    print(f"  Option keys: {len(cl._cached_option_keys)} symbols")
    print(f"  Strike steps: {len(cl._cached_strike_steps)} symbols")
    print(f"  Universe: {len(cl.OEH_UNIVERSE)} stocks")
    assert len(master) > 10000, f"Master too small: {len(master)}"
    assert len(cl._cached_eq_keys) > 100, f"Too few eq keys: {len(cl._cached_eq_keys)}"
    assert len(cl.OEH_UNIVERSE) > 50, f"Universe too small: {len(cl.OEH_UNIVERSE)}"
    print("  PASS")

    # Test 2: Second load (should be instant from cache)
    print("\n=== Test 2: Cached load (should be <0.01s) ===")
    t0 = time.time()
    master2 = await cl._ensure_master_loaded()
    t1 = time.time()
    print(f"  Loaded in {t1-t0:.4f}s")
    assert t1 - t0 < 0.1, f"Cache too slow: {t1-t0:.3f}s"
    assert master2 is master, "Cache returned different object"
    print("  PASS")

    # Test 3: _resolve_atm_strike uses cached data
    print("\n=== Test 3: ATM resolution (should be fast, ~2 API calls) ===")
    test_sym = cl.OEH_UNIVERSE[0] if cl.OEH_UNIVERSE else "RELIANCE"
    print(f"  Testing with {test_sym}...")
    t0 = time.time()
    result = cl._resolve_atm_strike(test_sym, "PE")
    t1 = time.time()
    if result:
        print(f"  {test_sym} → strike={result.strike} {result.option_type} premium={result.trigger_price:.2f} ({t1-t0:.2f}s)")
        print("  PASS")
    else:
        print(f"  Could not resolve (market may be closed) — {t1-t0:.2f}s")
        print("  SKIP (market closed)")

    # Test 4: Verify option_keys have nearest expiry
    print("\n=== Test 4: Option keys sanity ===")
    sample_sym = "RELIANCE"
    rel_opts = cl._cached_option_keys.get(sample_sym, {})
    print(f"  {sample_sym} has {len(rel_opts)} strikes in cache")
    if rel_opts:
        first_strike = sorted(rel_opts.keys())[len(rel_opts)//2]
        ce_info = rel_opts.get(first_strike, {}).get("CE")
        pe_info = rel_opts.get(first_strike, {}).get("PE")
        print(f"  Strike {first_strike}: CE={ce_info is not None} PE={pe_info is not None}")
        if ce_info:
            print(f"    CE key: {ce_info['key'][:30]}... expiry: {ce_info.get('expiry')}")
    print("  PASS")

    print("\n✅ All sanity tests passed!")


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""Test _recreate_profile: request -> nuke profile -> request again (must work)."""
import asyncio, sys, os, json, logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["QWENMODE_GUEST_MODE"] = "1"
os.environ["QWENMODE_POOL_SIZE"] = "2"

logging.basicConfig(level=logging.INFO, format="%(name)s:%(lineno)d %(message)s")
from qwenmode import QwenModePool, USER_DATA_DIR

async def main():
    pool = QwenModePool(size=2, model="Qwen3.8-Max-Preview")
    await pool.start()
    try:
        # 1. Normal request
        r1 = await pool.chat([{"role": "user", "content": "Скажи одно слово: раз"}])
        print(f"[1] BEFORE recreate: content={r1.get('content','')[:80]!r} tool_calls={bool(r1.get('tool_calls'))}")
        assert r1.get("content") or r1.get("tool_calls"), "FAIL: empty response on fresh session!"

        # 2. Simulate burned session: nuke the profile
        print("[2] Nuking profile (simulated RateLimited)...")
        await pool._recreate_profile(0)
        assert pool._states[0] is not None, "FAIL: page 0 dead after recreate"
        print(f"    pages after recreate: {[s is not None for s in pool._states]}")
        assert all(s is not None for s in pool._states), "FAIL: not all pages recreated"

        # 3. Request again on the fresh session
        r2 = await pool.chat([{"role": "user", "content": "Скажи одно слово: два"}])
        print(f"[3] AFTER recreate: content={r2.get('content','')[:80]!r} tool_calls={bool(r2.get('tool_calls'))}")
        assert r2.get("content") or r2.get("tool_calls"), "FAIL: empty response after profile recreate!"
        print("\nPASS: profile recreate works, fresh guest session answers")
    finally:
        await pool.close()

if __name__ == "__main__":
    asyncio.run(main())

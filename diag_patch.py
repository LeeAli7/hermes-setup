#!/usr/bin/env python3
"""Confirm detectIncognito returns true; test monkey-patch workaround."""
import asyncio, os, json
os.environ.setdefault("QWENMODE_GUEST_MODE", "1")
from playwright.async_api import async_playwright

URL = "https://chat.qwen.ai"
_LAUNCH_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage", "--disable-gpu",
]

INIT_PATCH = """
// Force IDBTransaction.durability to 'relaxed' so the incognito detector's
// 'strict' check fails (o=false -> not incognito).
(() => {
    const origTx = IDBDatabase.prototype.transaction;
    IDBDatabase.prototype.transaction = function(...args) {
        const tx = origTx.apply(this, args);
        try {
            Object.defineProperty(tx, 'durability', { value: 'relaxed', configurable: true });
        } catch (e) {}
        return tx;
    };
})();
"""

async def run_case(label, use_patch):
    print(f"\n########## {label} ##########")
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir=f"/tmp/qwenmode_patch_{label.replace(' ','_')}", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="en-US", timezone_id="Europe/Helsinki",
    )
    if use_patch:
        await ctx.add_init_script(INIT_PATCH)
    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(4)

    di = await page.evaluate("async () => { try { return await window.detectIncognito(); } catch(e) { return {err: String(e)}; } }")
    print("detectIncognito:", json.dumps(di, ensure_ascii=False)[:200])

    # Also check durability of a fresh transaction
    dur = await page.evaluate("""async () => {
        const db = await new Promise((res, rej) => {
            const r = indexedDB.open('__durability_check__', 1);
            r.onupgradeneeded = () => r.result.createObjectStore('s');
            r.onsuccess = () => res(r.result);
            r.onerror = () => rej(r.error);
        });
        const tx = db.transaction('s', 'readwrite', {durability: 'strict'});
        const d = tx.durability;
        db.close();
        indexedDB.deleteDatabase('__durability_check__');
        return d;
    }""")
    print("tx durability (requested strict):", dur)

    await ctx.close()
    await p.stop()

async def main():
    await run_case("nopatch", False)
    await run_case("patched", True)

asyncio.run(main())

#!/usr/bin/env python3
"""Dump what Qwen stores in a fresh profile (cookies, localStorage, IDB)."""
import asyncio, os, json
os.environ.setdefault("QWENMODE_GUEST_MODE", "1")
from playwright.async_api import async_playwright

URL = "https://chat.qwen.ai"
_LAUNCH_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-web-security", "--no-first-run", "--no-default-browser-check",
    "--disable-dev-shm-usage", "--disable-gpu",
]

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_dump1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="ru-RU", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(8)

    cookies = await ctx.cookies()
    print("=== COOKIES after load:")
    for c in cookies:
        print(f"  {c['name']} = {c['value'][:40]} (domain={c['domain']})")

    ls = await page.evaluate("""() => {
        const out = {};
        for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            out[k] = (localStorage.getItem(k)||'').slice(0,80);
        }
        return out;
    }""")
    print("=== localStorage:", json.dumps(ls, ensure_ascii=False)[:800])

    idb = await page.evaluate("""async () => {
        const names = await indexedDB.databases ? await indexedDB.databases() : [];
        return names;
    }""")
    print("=== IndexedDB:", json.dumps(idb)[:300])

    await ctx.close()
    await p.stop()

asyncio.run(main())

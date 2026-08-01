#!/usr/bin/env python3
"""Test navigator.storage.getDirectory() in headless — the incognito detector."""
import asyncio, os, json
os.environ.setdefault("QWENMODE_GUEST_MODE", "1")
from playwright.async_api import async_playwright

URL = "https://chat.qwen.ai"
_LAUNCH_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage", "--disable-gpu",
]

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_opfs1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="en-US", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)

    result = await page.evaluate("""async () => {
        const out = {};
        out.hasGetDirectory = typeof navigator.storage?.getDirectory === 'function';
        try {
            const dir = await navigator.storage.getDirectory();
            out.getDirectory = 'OK: ' + (dir && dir.name ? dir.name : 'dir');
        } catch (e) {
            out.getDirectory = 'ERROR: ' + (e instanceof Error ? e.message : String(e));
        }
        // replicate detector's a()
        try {
            const t = parseInt("-1");
            t.toFixed(t);
            out.a = 'no-error';
        } catch (n) {
            out.a = 'errlen=' + n.message.length;
        }
        return out;
    }""")
    print(json.dumps(result, ensure_ascii=False, indent=1))
    await ctx.close()
    await p.stop()

asyncio.run(main())

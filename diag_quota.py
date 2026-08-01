#!/usr/bin/env python3
"""Check navigator.storage.estimate() quota in headless Chromium."""
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
        user_data_dir="/tmp/qwenmode_quota1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="en-US", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)

    result = await page.evaluate("""async () => {
        let est = null, fsQuota = null, webkitFS = null;
        try { est = await navigator.storage.estimate(); } catch(e) { est = {error: String(e)}; }
        try {
            await new Promise((res, rej) => {
                navigator.webkitTemporaryStorage.queryUsageAndQuota(
                    (u, q) => { fsQuota = q; webkitFS = {usage: u, quota: q}; res(); },
                    (e) => { webkitFS = {error: String(e)}; res(); }
                );
            });
        } catch(e) { webkitFS = {error: String(e)}; }
        return {
            estimate: est,
            webkitFS: webkitFS,
            threshold_1GB: 1073741824,
            isPrivateHack: (() => {
                // common incognito detection: FileSystem quota === 0 or undefined
                try {
                    let q = 0;
                    const fs = navigator.webkitTemporaryStorage;
                    return 'webkitTemporaryStorage exists: ' + !!fs;
                } catch(e) { return 'err ' + e; }
            })()
        };
    }""")
    print(json.dumps(result, ensure_ascii=False, indent=1)[:1500])
    await ctx.close()
    await p.stop()

asyncio.run(main())

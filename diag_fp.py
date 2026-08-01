#!/usr/bin/env python3
"""Check automation fingerprints visible to Qwen."""
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
        user_data_dir="/tmp/qwenmode_fp1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="en-US", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)

    fp = await page.evaluate("""() => {
        return {
            webdriver: navigator.webdriver,
            languages: navigator.languages,
            platform: navigator.platform,
            userAgent: navigator.userAgent.slice(0, 80),
            hardwareConcurrency: navigator.hardwareConcurrency,
            deviceMemory: navigator.deviceMemory,
            plugins: navigator.plugins.length,
            maxTouchPoints: navigator.maxTouchPoints,
            chrome: !!window.chrome,
            permissions: typeof navigator.permissions,
            webgl: (() => { try { const c = document.createElement('canvas'); return !!(c.getContext('webgl') || c.getContext('experimental-webgl')); } catch(e) { return false; } })(),
        };
    }""")
    print(json.dumps(fp, indent=1))
    await ctx.close()
    await p.stop()

asyncio.run(main())

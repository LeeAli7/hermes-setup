#!/usr/bin/env python3
"""Check /api/v2/configs/setting-config — anonymous access flag."""
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
        user_data_dir="/tmp/qwenmode_cfg1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="en-US", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    bodies = {}
    async def on_resp(resp):
        if "configs" in resp.url:
            try:
                bodies[resp.url] = (await resp.text())[:4000]
            except Exception:
                pass
    page.on("response", on_resp)
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(6)
    for u, b in bodies.items():
        print(f"=== {u}")
        print(b[:2500])
        print()
    await ctx.close()
    await p.stop()

asyncio.run(main())

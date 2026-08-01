#!/usr/bin/env python3
"""Probe /api/v2/chat/completions via Playwright APIRequestContext (same cookies)."""
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
        user_data_dir="/tmp/qwenmode_api2", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="ru-RU", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(5)

    # Playwright: page.request shares cookie storage with the page's context
    r = await page.request.post(
        "https://chat.qwen.ai/api/v2/chat/completions",
        data={
            "model": "Qwen3.8-Max-Preview",
            "messages": [{"role": "user", "content": "Say hello in exactly 5 words"}],
            "stream": False,
        },
        headers={"Content-Type": "application/json"},
    )
    print("status:", r.status)
    body = await r.text()
    print("body:", body[:800])
    await ctx.close()
    await p.stop()

asyncio.run(main())

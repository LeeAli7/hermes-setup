#!/usr/bin/env python3
"""Watch network when clicking 'Stay logged out' — any consent POST?"""
import asyncio, os, json
os.environ.setdefault("QWENMODE_GUEST_MODE", "1")
from playwright.async_api import async_playwright

URL = "https://chat.qwen.ai"
_LAUNCH_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage", "--disable-gpu",
]
PROMPT = "Say hello in exactly 5 words"

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_consentnet1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="en-US", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(4)
    try:
        g = page.locator('.guidance-pc-close-btn').first
        if await g.count() > 0:
            await g.click(timeout=2000, force=True)
            await asyncio.sleep(0.8)
    except Exception:
        pass

    net = []
    page.on("request", lambda r: net.append(("REQ", r.method, r.url[:130])))
    page.on("response", lambda r: net.append(("RESP", r.status, r.url[:130])))

    ta = page.locator('textarea').first
    await ta.click(timeout=3000, force=True)
    await page.keyboard.type(PROMPT, delay=25)
    await asyncio.sleep(0.8)
    sb = page.locator('button.send-button').first
    await sb.click(timeout=3000, force=True)
    await asyncio.sleep(2)

    print("=== network during send (non-aplus):")
    for m, s, u in net[-15:]:
        if "aplus" not in u and "tdum" not in u:
            print("  ", m, s, u)

    net.clear()
    stay = page.locator('button:has-text("Stay logged out")').first
    if await stay.count() > 0:
        await stay.click(timeout=3000, force=True)
        print("clicked Stay logged out")
        await asyncio.sleep(3)
        print("=== network during Stay logged out (non-aplus):")
        for m, s, u in net[-15:]:
            if "aplus" not in u and "tdum" not in u:
                print("  ", m, s, u)
    else:
        print("no Stay logged out button found")

    await ctx.close()
    await p.stop()

asyncio.run(main())

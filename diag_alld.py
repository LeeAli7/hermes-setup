#!/usr/bin/env python3
"""Capture ALL requests during send click — is anything sent at all?"""
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
        user_data_dir="/tmp/qwenmode_alld1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="ru-RU", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(3)
    try:
        g = page.locator('.guidance-pc-close-btn').first
        if await g.count() > 0:
            await g.click(timeout=2000, force=True)
            await asyncio.sleep(0.8)
    except Exception:
        pass

    net = []
    page.on("request", lambda r: net.append(("REQ", r.method, r.url[:150])))
    page.on("response", lambda r: net.append(("RESP", r.status, r.url[:150])))

    ta = page.locator('textarea').first
    await ta.click(timeout=3000, force=True)
    await page.keyboard.type(PROMPT, delay=20)
    await asyncio.sleep(0.5)
    print("=== typing done, network so far:")
    for n in net[-5:]:
        print("  ", n)

    sb = page.locator('button.send-button').first
    await sb.click(timeout=3000, force=True)
    print("=== clicked send")
    await asyncio.sleep(3)

    print("=== ALL network after send:")
    for n in net:
        if "qwen" in n[2] or "aplus" in n[2] or "alibaba" in n[2] or "aliyun" in n[2]:
            print("  ", n)
    await ctx.close()
    await p.stop()

asyncio.run(main())

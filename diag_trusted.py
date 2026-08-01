#!/usr/bin/env python3
"""Try trusted keyboard typing + Enter, watch ALL requests."""
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
PROMPT = "Say hello in exactly 5 words"

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_trusted1", headless=True,
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

    # All network
    net = []
    async def on_req(req):
        if not req.url.startswith("data:"):
            net.append(("REQ", req.method, req.url[:130]))
    async def on_resp(resp):
        if not resp.url.startswith("data:"):
            net.append(("RESP", resp.status, resp.url[:130]))
    page.on("request", on_req)
    page.on("response", on_resp)

    # Trusted typing: click textarea, then keyboard.type
    ta = page.locator('textarea').first
    await ta.click(timeout=3000, force=True)
    await asyncio.sleep(0.3)
    await page.keyboard.type(PROMPT, delay=30)
    await asyncio.sleep(1)
    val = await page.evaluate("() => document.querySelector('textarea')?.value || ''")
    print("ta value after keyboard.type:", repr(val))

    # Press Enter (trusted key event)
    await page.keyboard.press("Enter")
    print("pressed Enter")
    await asyncio.sleep(4)

    # Dismiss modal if any
    try:
        stay = page.locator('button:has-text("Оставаться вышедшим")').first
        if await stay.count() > 0 and await stay.is_visible():
            await stay.click(timeout=2000, force=True)
            print("dismissed modal after Enter")
            await asyncio.sleep(2)
    except Exception:
        pass

    await asyncio.sleep(12)
    print("=== NET (chat/api only):")
    for r in net:
        if "chat" in r[2] or "api" in r[2] or "completion" in r[2]:
            print("  ", r)
    print("=== DOM:", (await page.evaluate("() => document.body?.innerText || ''"))[:300].replace("\n"," | "))
    await ctx.close()
    await p.stop()

asyncio.run(main())

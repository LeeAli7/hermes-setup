#!/usr/bin/env python3
"""Full send-path test: select 3.8-Max, type, click send, capture SSE."""
import asyncio, os, json, re
os.environ.setdefault("QWENMODE_GUEST_MODE", "1")
from playwright.async_api import async_playwright

URL = "https://chat.qwen.ai"
MODEL = "Qwen3.8-Max-Preview"
USER_DATA_DIR = "/tmp/qwenmode_diag_profile3"

_LAUNCH_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-web-security", "--no-first-run", "--no-default-browser-check",
    "--disable-dev-shm-usage", "--disable-gpu",
]

SSE_PATTERN = "/api/v2/chat/completions"

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir=USER_DATA_DIR, headless=True, args=_LAUNCH_ARGS,
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="ru-RU", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(3)

    # Close guidance
    try:
        g = page.locator('.guidance-pc-close-btn').first
        if await g.count() > 0:
            await g.click(timeout=2000, force=True)
            await asyncio.sleep(0.8)
    except Exception as e:
        print("guidance:", e)

    # Select model
    sel = '.index-module__model-selector___rdCim'
    trig = page.locator(sel).first
    await trig.click(timeout=4000, force=True)
    await asyncio.sleep(1.0)
    item = page.locator(f'.index-module__model-item-name___X8Hec:has-text("{MODEL}")').first
    print("item count:", await item.count())
    await item.click(timeout=4000, force=True)
    await asyncio.sleep(1.0)
    cur = (await trig.inner_text()).strip()
    print("trigger text now:", cur)

    # Capture ALL network requests (not just SSE)
    reqs = []
    async def on_req(req):
        if "api" in req.url or "chat" in req.url:
            reqs.append(("REQ", req.method, req.url[:120]))
    async def on_resp(resp):
        if "api" in resp.url or "chat" in resp.url:
            reqs.append(("RESP", resp.status, resp.url[:120]))
    page.on("request", on_req)
    page.on("response", on_resp)

    # Capture SSE
    bodies = []
    async def capture(resp):
        if SSE_PATTERN in resp.url:
            try:
                raw = await resp.text()
                bodies.append(raw)
                print(f"SSE HIT: {len(raw)} bytes, head: {raw[:150]!r}")
            except Exception as e:
                print("SSE read err:", e)
    page.on("response", capture)

    # Type prompt
    ta = page.locator('textarea').first
    await ta.fill("Say hello in exactly 5 words")
    await asyncio.sleep(0.5)
    print("ta value len:", len(await ta.input_value()))

    # Dismiss auth if any, then send via the send button (Enter is ignored by React)
    try:
        send_btn = page.locator('button.send-button, button[aria-label="Send"]').first
        if await send_btn.count() > 0:
            await send_btn.click(timeout=3000, force=True)
            print("clicked send-button #1")
        else:
            await page.keyboard.press("Enter")
            print("no send-button, pressed Enter")
    except Exception as e:
        print("send err:", e)
    await asyncio.sleep(2)

    # Dismiss auth modal if appeared
    try:
        stay = page.locator('button:has-text("Оставаться вышедшим")').first
        if await stay.count() > 0 and await stay.is_visible():
            await stay.click(timeout=2000, force=True)
            print("dismissed auth modal")
            await asyncio.sleep(1)
    except Exception:
        pass

    # Send again after dismissing modal
    try:
        send_btn = page.locator('button.send-button, button[aria-label="Send"]').first
        if await send_btn.count() > 0:
            await send_btn.click(timeout=3000, force=True)
            print("clicked send-button #2")
        else:
            await page.keyboard.press("Enter")
            print("no send-button #2, pressed Enter")
    except Exception as e:
        print("send #2 err:", e)
    await asyncio.sleep(2)

    await asyncio.sleep(15)
    print("=== NETWORK:")
    for r in reqs:
        print("  ", r)
    print("=== SSE bodies:", len(bodies))
    for b in bodies:
        print("--- body head:", b[:300])

    print("=== DOM AFTER ===")
    print((await page.evaluate("() => document.body?.innerText || ''"))[:600])
    await ctx.close()
    await p.stop()

asyncio.run(main())

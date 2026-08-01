#!/usr/bin/env python3
"""No-dismiss test: does SSE arrive if we just wait after clicking send?"""
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
SSE_PATTERN = "/api/v2/chat/completions"
PROMPT = "Say hello in exactly 5 words"

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_nodismiss1", headless=True,
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
    sse = []
    async def on_req(req):
        if SSE_PATTERN in req.url:
            net.append(("REQ", req.method))
            print(">>> REQ", req.method)
    async def on_resp(resp):
        if SSE_PATTERN in resp.url:
            try:
                raw = await resp.text()
                sse.append(raw)
                print(f">>> SSE HIT ({len(raw)}b): {raw[:100]!r}")
            except Exception as e:
                print(">>> SSE err:", e)
    page.on("request", on_req)
    page.on("response", on_resp)

    ta = page.locator('textarea').first
    await ta.click(timeout=3000, force=True)
    await page.keyboard.type(PROMPT, delay=20)
    await asyncio.sleep(0.5)

    sb = page.locator('button.send-button').first
    await sb.click(timeout=3000, force=True)
    print("clicked send, waiting 30s without dismissing anything...")
    for i in range(6):
        await asyncio.sleep(5)
        modal = await page.evaluate("() => (document.body?.innerText||'').includes('Оставаться вышедшим')")
        print(f"  t+{5*(i+1)}s: modal={modal} sse={len(sse)}")

    print("=== FINAL")
    print("reqs:", net, "sse:", len(sse))
    print("DOM:", (await page.evaluate("() => document.body?.innerText || ''"))[:250].replace("\n"," | "))
    await ctx.close()
    await p.stop()

asyncio.run(main())

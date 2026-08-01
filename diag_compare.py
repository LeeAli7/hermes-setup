#!/usr/bin/env python3
"""Compare send behavior: default model vs 3.8-Max. Fresh profile each."""
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

async def run_case(label, select_model):
    print(f"\n########## {label} ##########")
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir=f"/tmp/qwenmode_compare_{label}", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
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
    except Exception:
        pass

    # Check current model + optionally switch
    trig = page.locator('.index-module__model-selector___rdCim').first
    cur_model = (await trig.inner_text()).strip() if await trig.count() > 0 else "?"
    print("default model:", cur_model)
    if select_model and select_model != cur_model:
        await trig.click(timeout=4000, force=True)
        await asyncio.sleep(1.0)
        item = page.locator(f'.index-module__model-item-name___X8Hec:has-text("{select_model}")').first
        print("item count:", await item.count())
        await item.click(timeout=4000, force=True)
        await asyncio.sleep(1.0)
        print("model now:", (await trig.inner_text()).strip())

    # Network capture
    reqs = []
    async def on_req(req):
        if SSE_PATTERN in req.url:
            reqs.append(("REQ", req.method))
    async def on_resp(resp):
        if SSE_PATTERN in resp.url:
            reqs.append(("RESP", resp.status))
    page.on("request", on_req)
    page.on("response", on_resp)

    # Type
    ta = page.locator('textarea').first
    await ta.fill("Say hello in exactly 5 words")
    await asyncio.sleep(0.5)

    # Send loop with modal dismissal, up to 3 attempts
    sent_ok = False
    for attempt in range(3):
        # Dismiss auth modal FIRST if visible
        try:
            stay = page.locator('button:has-text("Оставаться вышедшим")').first
            if await stay.count() > 0 and await stay.is_visible():
                await stay.click(timeout=2000, force=True)
                print(f"attempt {attempt}: dismissed auth modal first")
                await asyncio.sleep(1)
        except Exception:
            pass

        sb = page.locator('button.send-button, button[aria-label="Send"]').first
        if await sb.count() > 0:
            await sb.click(timeout=3000, force=True)
        else:
            await page.keyboard.press("Enter")
        await asyncio.sleep(3)

        if reqs:
            sent_ok = True
            break

    await asyncio.sleep(12)
    print("SSE requests:", reqs)
    print("DOM:", (await page.evaluate("() => document.body?.innerText || ''"))[:350].replace("\n", " | "))
    await ctx.close()
    await p.stop()

asyncio.run(run_case("default", None))
asyncio.run(run_case("max38", "Qwen3.8-Max-Preview"))

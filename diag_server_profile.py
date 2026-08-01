#!/usr/bin/env python3
"""Test send through running server page: dismiss modal, check detectIncognito state."""
import asyncio, os, json, urllib.request
os.environ.setdefault("QWENMODE_GUEST_MODE", "1")
from playwright.async_api import async_playwright

# Attach to the SAME persistent profile the server uses
USER_DATA_DIR = "/tmp/qwenmode_profile"
_LAUNCH_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage", "--disable-gpu",
]
SSE_PATTERN = "/api/v2/chat/completions"
PROMPT = "Reply with just the word OK"

async def main():
    p = await async_playwright().start()
    # launch_persistent_context with same profile = same cookies/session
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir=USER_DATA_DIR, headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="en-US", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    await page.goto("https://chat.qwen.ai", wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(4)

    di = await page.evaluate("async () => { try { return await window.detectIncognito(); } catch(e) { return {err: String(e)}; } }")
    print("detectIncognito NOW:", json.dumps(di, ensure_ascii=False)[:150])

    # Is the welcome modal present?
    modal = await page.evaluate("() => (document.body?.innerText||'').includes('Stay logged out') || (document.body?.innerText||'').includes('Оставаться вышедшим')")
    print("modal present:", modal)
    if modal:
        for txt in ["Stay logged out", "Оставаться вышедшим"]:
            try:
                stay = page.locator(f'button:has-text("{txt}")').first
                if await stay.count() > 0 and await stay.is_visible():
                    await stay.click(timeout=2000, force=True)
                    print(f"dismissed via {txt}")
                    await asyncio.sleep(1)
                    break
            except Exception:
                pass

    # Now send
    sse = []
    async def on_resp(resp):
        if SSE_PATTERN in resp.url:
            try:
                raw = await resp.text()
                sse.append(raw)
                print(f"  >>> SSE HIT ({len(raw)}b): {raw[:80]!r}")
            except Exception:
                pass
    page.on("response", on_resp)

    ta = page.locator('textarea').first
    if await ta.count() == 0:
        print("NO TEXTAREA")
        await ctx.close(); await p.stop(); return
    await ta.click(timeout=3000, force=True)
    await page.keyboard.type(PROMPT, delay=25)
    await asyncio.sleep(0.5)
    sb = page.locator('button.send-button').first
    await sb.click(timeout=3000, force=True)
    print("clicked send")
    await asyncio.sleep(4)
    modal2 = await page.evaluate("() => (document.body?.innerText||'').includes('Stay logged out') || (document.body?.innerText||'').includes('Оставаться вышедшим')")
    print("modal after send:", modal2)
    await asyncio.sleep(12)
    print("SSE:", len(sse))
    print("DOM:", (await page.evaluate("() => document.body?.innerText || ''"))[-300:].replace("\n"," | "))
    await ctx.close()
    await p.stop()

asyncio.run(main())

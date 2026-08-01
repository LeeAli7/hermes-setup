#!/usr/bin/env python3
"""Precise: does 'Оставаться вышедшим' actually close the modal? Then does send work?"""
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

async def state(page, label):
    s = await page.evaluate("""() => {
        const all = document.body?.innerText || '';
        return {
            modal: all.includes('Оставаться вышедшим'),
            sendVisible: !!document.querySelector('button.send-button') && !!(document.querySelector('button.send-button').offsetWidth||document.querySelector('button.send-button').offsetHeight),
            taVal: document.querySelector('textarea')?.value?.slice(0,30) || ''
        };
    }""")
    print(f"[{label}] modal={s['modal']} sendVisible={s['sendVisible']} ta='{s['taVal']}'")
    return s

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_precise1", headless=True,
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

    sse = []
    async def on_resp(resp):
        if SSE_PATTERN in resp.url:
            try:
                raw = await resp.text()
                sse.append(raw)
                print(f"  >>> SSE HIT ({len(raw)}b): {raw[:80]!r}")
            except Exception as e:
                print("  >>> SSE err:", e)
    page.on("response", on_resp)

    ta = page.locator('textarea').first
    await ta.click(timeout=3000, force=True)
    await page.keyboard.type(PROMPT, delay=20)
    await asyncio.sleep(0.5)
    await state(page, "typed")

    # Click send once
    sb = page.locator('button.send-button').first
    await sb.click(timeout=3000, force=True)
    await asyncio.sleep(2)
    await state(page, "after send#1")

    # Click "Оставаться вышедшим" — and verify modal actually closes
    stay = page.locator('button:has-text("Оставаться вышедшим")').first
    print("stay count:", await stay.count())
    if await stay.count() > 0:
        await stay.click(timeout=3000, force=True)
        await asyncio.sleep(2)
        await state(page, "after dismiss click")

    # Now send again WITHOUT force (real click, may fail if covered)
    sb = page.locator('button.send-button').first
    print("send count after dismiss:", await sb.count())
    try:
        await sb.click(timeout=3000)  # NOT force
        print("send#2 clicked (no force)")
    except Exception as e:
        print("send#2 failed:", str(e)[:100])
    await asyncio.sleep(5)

    await state(page, "after send#2")
    await asyncio.sleep(10)
    print("=== SSE:", len(sse))
    print("DOM:", (await page.evaluate("() => document.body?.innerText || ''"))[:300].replace("\n"," | "))
    await ctx.close()
    await p.stop()

asyncio.run(main())

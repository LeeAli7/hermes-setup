#!/usr/bin/env python3
"""Careful: does 'Оставаться вышедшим' set a consent cookie? Send after full dismiss."""
import asyncio, os, json
os.environ.setdefault("QWENMODE_GUEST_MODE", "1")
from playwright.async_api import async_playwright

URL = "https://chat.qwen.ai"
_LAUNCH_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage", "--disable-gpu",
]
SSE_PATTERN = "/api/v2/chat/completions"
PROMPT = "Say hello in exactly 5 words"

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_consent1", headless=True,
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
                print(f"  >>> SSE HIT ({len(raw)}b): {raw[:100]!r}")
            except Exception as e:
                print("  >>> SSE err:", e)
    page.on("response", on_resp)

    def cookies_now():
        return asyncio.get_event_loop().run_until_complete(ctx.cookies()) if False else None

    ta = page.locator('textarea').first
    await ta.click(timeout=3000, force=True)
    await page.keyboard.type(PROMPT, delay=20)
    await asyncio.sleep(0.5)

    # Send #1
    sb = page.locator('button.send-button').first
    await sb.click(timeout=3000, force=True)
    print("send #1")
    await asyncio.sleep(2.5)
    modal = await page.evaluate("() => (document.body?.innerText||'').includes('Оставаться вышедшим')")
    print("modal after send#1:", modal)

    # Find ALL buttons in the modal
    modal_btns = await page.evaluate("""() => {
        const all = document.body?.innerText || '';
        if (!all.includes('Оставаться вышедшим')) return [];
        return [...document.querySelectorAll('button')]
            .filter(b => b.offsetWidth || b.offsetHeight)
            .map(b => ({txt: (b.textContent||'').trim().slice(0,40), cls: (b.className||'').toString().slice(0,50)}))
            .filter(b => b.txt);
    }""")
    print("modal buttons:", json.dumps(modal_btns, ensure_ascii=False)[:600])

    # Dismiss via text match
    stay = page.locator('button:has-text("Оставаться вышедшим")').first
    await stay.click(timeout=3000, force=True)
    print("dismissed")
    await asyncio.sleep(3)
    modal = await page.evaluate("() => (document.body?.innerText||'').includes('Оставаться вышедшим')")
    print("modal after dismiss:", modal)

    # Cookies after dismiss — any new consent cookie?
    cookies = await ctx.cookies()
    print("cookie names:", [c['name'] for c in cookies])

    # Send #2 WITHOUT force
    try:
        sb = page.locator('button.send-button').first
        await sb.click(timeout=3000)  # no force
        print("send #2 (no force)")
    except Exception as e:
        print("send #2 failed:", str(e)[:120])
    await asyncio.sleep(2.5)
    modal = await page.evaluate("() => (document.body?.innerText||'').includes('Оставаться вышедшим')")
    print("modal after send#2:", modal)

    await asyncio.sleep(12)
    print("=== SSE:", len(sse))
    print("DOM:", (await page.evaluate("() => document.body?.innerText || ''"))[:250].replace("\n"," | "))
    await ctx.close()
    await p.stop()

asyncio.run(main())

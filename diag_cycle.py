#!/usr/bin/env python3
"""Full cycle: send -> dismiss modal -> RE-TYPE if cleared -> send -> capture."""
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

async def type_text(page, text):
    await page.evaluate("""(t) => {
        const ta = document.querySelector('textarea');
        if (!ta) return;
        ta.focus();
        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
        setter.call(ta, t);
        ta.dispatchEvent(new Event('input', {bubbles: true}));
        ta.dispatchEvent(new Event('change', {bubbles: true}));
        ta.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', bubbles: true}));
        setter.call(ta, t);
        ta.dispatchEvent(new Event('input', {bubbles: true}));
    }""", text)
    await asyncio.sleep(0.5)

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_cycle1", headless=True,
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

    sse_hits = []
    async def on_resp(resp):
        if SSE_PATTERN in resp.url:
            try:
                raw = await resp.text()
                sse_hits.append(raw)
                print(f"SSE HIT ({len(raw)}b): {raw[:120]!r}")
            except Exception as e:
                print("SSE err:", e)
    page.on("response", on_resp)

    await type_text(page, PROMPT)

    for attempt in range(4):
        # Click send
        sb = page.locator('button.send-button').first
        if await sb.count() > 0:
            await sb.click(timeout=3000, force=True)
            print(f"attempt {attempt}: clicked send")
        await asyncio.sleep(2.5)

        # If auth modal appeared, dismiss it
        try:
            stay = page.locator('button:has-text("Оставаться вышедшим")').first
            if await stay.count() > 0 and await stay.is_visible():
                await stay.click(timeout=2000, force=True)
                print(f"attempt {attempt}: dismissed modal")
                await asyncio.sleep(1.5)
        except Exception:
            pass

        # Check textarea — re-type if cleared
        val = await page.evaluate("() => document.querySelector('textarea')?.value || ''")
        print(f"attempt {attempt}: ta len={len(val.strip())}")
        if len(val.strip()) < len(PROMPT):
            await type_text(page, PROMPT)
            print(f"attempt {attempt}: re-typed")

        if sse_hits:
            print("GOT SSE! breaking")
            break

    await asyncio.sleep(10)
    print("=== FINAL ===")
    print("SSE bodies:", len(sse_hits))
    if sse_hits:
        print(sse_hits[-1][:400])
    print("DOM:", (await page.evaluate("() => document.body?.innerText || ''"))[:300].replace("\n"," | "))
    await ctx.close()
    await p.stop()

asyncio.run(main())

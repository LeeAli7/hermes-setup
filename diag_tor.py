#!/usr/bin/env python3
"""Test guest send through Tor SOCKS5 proxy."""
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
    "--proxy-server=socks5://127.0.0.1:9050",
]
SSE_PATTERN = "/api/v2/chat/completions"
PROMPT = "Say hello in exactly 5 words"

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_tor1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="ru-RU", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    # Check IP seen by the BROWSER (through the proxy)
    try:
        ip = await page.evaluate("""async () => {
            const r = await fetch('https://api.ipify.org?format=json', {cache:'no-store'});
            return await r.text();
        }""")
        print("IP via browser (through proxy):", ip)
    except Exception as e:
        print("browser IP check failed:", str(e)[:120])
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print("goto err:", str(e)[:100])
    await asyncio.sleep(5)
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
    if await ta.count() == 0:
        print("NO TEXTAREA — page:", (await page.evaluate("() => document.body?.innerText || ''"))[:200])
        await ctx.close(); await p.stop(); return
    await ta.click(timeout=3000, force=True)
    await page.keyboard.type(PROMPT, delay=20)
    await asyncio.sleep(0.5)

    for attempt in range(3):
        sb = page.locator('button.send-button').first
        if await sb.count() > 0:
            await sb.click(timeout=3000, force=True)
            print(f"attempt {attempt}: clicked send")
        await asyncio.sleep(2.5)
        try:
            stay = page.locator('button:has-text("Оставаться вышедшим")').first
            if await stay.count() > 0 and await stay.is_visible():
                await stay.click(timeout=2000, force=True)
                print(f"attempt {attempt}: dismissed modal")
                await asyncio.sleep(1.5)
        except Exception:
            pass
        val = await page.evaluate("() => document.querySelector('textarea')?.value || ''")
        if len(val.strip()) < len(PROMPT):
            await ta.click(timeout=2000, force=True)
            await page.keyboard.type(PROMPT, delay=20)
        if sse:
            break

    await asyncio.sleep(12)
    print("=== SSE:", len(sse))
    if sse:
        print(sse[-1][:500])
    print("DOM:", (await page.evaluate("() => document.body?.innerText || ''"))[:250].replace("\n"," | "))
    await ctx.close()
    await p.stop()

asyncio.run(main())

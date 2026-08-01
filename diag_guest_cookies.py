#!/usr/bin/env python3
"""Test: guest send WITH the user's 'familiar' guest cookies applied."""
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
COOKIE_FILE = "/home/ali/projects/hermes/cookies/guest1.json"

def load_cookies(path):
    with open(path) as f:
        raw = json.load(f)
    out = []
    for c in raw:
        cc = {}
        for k, v in c.items():
            if k == "expirationDate":
                cc["expires"] = int(v)
            elif k in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"):
                if k == "sameSite":
                    if v is None:
                        continue
                    ssl = str(v).lower()
                    if ssl in ("no_restriction", "none"):
                        cc["sameSite"] = "None"
                    elif ssl == "lax":
                        cc["sameSite"] = "Lax"
                    elif ssl == "strict":
                        cc["sameSite"] = "Strict"
                    else:
                        continue
                else:
                    cc[k] = v
        out.append(cc)
    return out

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_guestcookies1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="ru-RU", timezone_id="Europe/Helsinki",
    )
    try:
        cookies = load_cookies(COOKIE_FILE)
        await ctx.add_cookies(cookies)
        print("guest cookies added:", len(cookies))
    except Exception as e:
        print("cookie load err:", str(e)[:200])

    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(4)
    try:
        g = page.locator('.guidance-pc-close-btn').first
        if await g.count() > 0:
            await g.click(timeout=2000, force=True)
            await asyncio.sleep(0.8)
    except Exception:
        pass
    head = await page.evaluate("() => (document.body?.innerText || '').split('\\n').slice(0,10).join(' | ')")
    print("HEADER:", head[:200])

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

    ta = page.locator('textarea').first
    if await ta.count() == 0:
        print("NO TEXTAREA")
        await ctx.close(); await p.stop(); return
    await ta.click(timeout=3000, force=True)
    await page.keyboard.type(PROMPT, delay=20)
    await asyncio.sleep(0.5)
    sb = page.locator('button.send-button').first
    await sb.click(timeout=3000, force=True)
    print("clicked send")
    await asyncio.sleep(2.5)
    modal = await page.evaluate("() => (document.body?.innerText||'').includes('Оставаться вышедшим')")
    print("modal appeared:", modal)
    if modal:
        stay = page.locator('button:has-text("Оставаться вышедшим")').first
        await stay.click(timeout=2000, force=True)
        print("dismissed modal")
        await asyncio.sleep(1)
        sb = page.locator('button.send-button').first
        if await sb.count() > 0:
            await sb.click(timeout=3000, force=True)
            print("send again")
        await asyncio.sleep(2.5)

    await asyncio.sleep(15)
    print("=== SSE:", len(sse))
    if sse:
        print("LAST BODY:", sse[-1][:700])
    print("DOM:", (await page.evaluate("() => document.body?.innerText || ''"))[:300].replace("\n"," | "))
    await ctx.close()
    await p.stop()

asyncio.run(main())

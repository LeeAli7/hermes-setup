#!/usr/bin/env python3
"""Strongest combo: visible browser + user guest cookies + real click + long wait."""
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
        user_data_dir="/tmp/qwenmode_vis_cookie1", headless=False,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="ru-RU", timezone_id="Europe/Helsinki",
    )
    try:
        cookies = load_cookies(COOKIE_FILE)
        await ctx.add_cookies(cookies)
        print("cookies:", len(cookies))
    except Exception as e:
        print("cookie err:", str(e)[:150])

    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
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
                print(f"  >>> SSE HIT ({len(raw)}b): {raw[:100]!r}")
            except Exception as e:
                print("  >>> SSE err:", e)
    page.on("response", on_resp)

    ta = page.locator('textarea').first
    await ta.click(timeout=3000)  # real click
    await page.keyboard.type(PROMPT, delay=40)
    await asyncio.sleep(1)

    # real click on send (not force)
    sb = page.locator('button.send-button').first
    await sb.click(timeout=5000)
    print("real clicked send")
    await asyncio.sleep(3)

    modal = await page.evaluate("() => (document.body?.innerText||'').includes('Оставаться вышедшим')")
    print("modal:", modal)
    if modal:
        # Real click on stay-logged-out
        stay = page.locator('button:has-text("Оставаться вышедшим")').first
        await stay.click(timeout=3000)
        print("real clicked stay-logged-out")
        await asyncio.sleep(2)
        modal = await page.evaluate("() => (document.body?.innerText||'').includes('Оставаться вышедшим')")
        print("modal after dismiss:", modal)
        # Wait longer — maybe message auto-sends after consent
        print("waiting 25s for auto-send...")
        await asyncio.sleep(25)
        print("sse so far:", len(sse))

    await asyncio.sleep(10)
    print("=== SSE:", len(sse))
    if sse:
        print(sse[-1][:700])
    print("DOM:", (await page.evaluate("() => document.body?.innerText || ''"))[:300].replace("\n"," | "))
    await ctx.close()
    await p.stop()

asyncio.run(main())

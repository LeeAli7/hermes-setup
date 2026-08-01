#!/usr/bin/env python3
"""Check cookie-mode account send + probe chat API endpoint directly."""
import asyncio, os, json
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

def load_cookies(path):
    with open(path) as f:
        raw = json.load(f)
    out = []
    for c in raw:
        cc = {}
        for k, v in c.items():
            if k == "expirationDate":
                cc["expires"] = int(v)
            elif k in ("name","value","domain","path","expires","httpOnly","secure","sameSite"):
                if k == "sameSite" and isinstance(v, str):
                    v = v.title() if v.lower() in ("lax","strict","none") else "Lax"
                cc[k] = v
        out.append(cc)
    return out

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_cookietest2", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="ru-RU", timezone_id="Europe/Helsinki",
    )
    try:
        cookies = load_cookies("/home/ali/projects/hermes/cookies/cookies1.json")
        await ctx.add_cookies(cookies)
        print("cookies added:", len(cookies))
    except Exception as e:
        print("cookie load err:", str(e)[:150])

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

    # Check if logged in (avatar/menu instead of Войти)
    logged = await page.evaluate("""() => {
        const all = document.body?.innerText || '';
        return !all.includes('Войти') && !all.includes('Зарегистрироваться');
    }""")
    print("LOGGED IN (no auth buttons):", logged)

    if not logged:
        print("Account cookies NOT active — session is guest")
        await ctx.close(); await p.stop(); return

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
    await ta.click(timeout=3000, force=True)
    await page.keyboard.type(PROMPT, delay=20)
    await asyncio.sleep(0.5)
    sb = page.locator('button.send-button').first
    await sb.click(timeout=3000, force=True)
    print("clicked send")
    await asyncio.sleep(20)
    print("=== SSE:", len(sse))
    if sse:
        print(sse[-1][:800])
    else:
        print("DOM:", (await page.evaluate("() => document.body?.innerText || ''"))[:300].replace("\n"," | "))
    await ctx.close()
    await p.stop()

asyncio.run(main())

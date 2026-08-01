#!/usr/bin/env python3
"""Locale hypothesis: en-US vs ru-RU guest send."""
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

async def run_case(label, locale, with_cookies=False):
    print(f"\n########## {label} ##########")
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir=f"/tmp/qwenmode_loc_{label.replace(' ','_')}", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale=locale, timezone_id="Europe/Helsinki",
    )
    if with_cookies:
        with open("/home/ali/projects/hermes/cookies/guest1.json") as f:
            raw = json.load(f)
        out = []
        for c in raw:
            cc = {}
            for k, v in c.items():
                if k == "expirationDate":
                    cc["expires"] = int(v)
                elif k in ("name","value","domain","path","expires","httpOnly","secure","sameSite"):
                    if k == "sameSite":
                        if v is None: continue
                        ssl = str(v).lower()
                        if ssl in ("no_restriction","none"): cc["sameSite"] = "None"
                        elif ssl == "lax": cc["sameSite"] = "Lax"
                        elif ssl == "strict": cc["sameSite"] = "Strict"
                        else: continue
                    else: cc[k] = v
            out.append(cc)
        try:
            await ctx.add_cookies(out)
            print("cookies applied")
        except Exception as e:
            print("cookie err:", str(e)[:120])

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

    # Force qwen-locale cookie to en-US as well
    try:
        await ctx.add_cookies([{"name": "qwen-locale", "value": "en-US", "domain": "chat.qwen.ai", "path": "/"}])
    except Exception:
        pass

    ta = page.locator('textarea').first
    if await ta.count() == 0:
        print("NO TEXTAREA:", (await page.evaluate("() => document.body?.innerText||''"))[:150])
        await ctx.close(); await p.stop(); return
    await ta.click(timeout=3000, force=True)
    await page.keyboard.type(PROMPT, delay=25)
    await asyncio.sleep(0.8)
    sb = page.locator('button.send-button').first
    await sb.click(timeout=3000, force=True)
    print("clicked send")
    await asyncio.sleep(3)
    modal_en = await page.evaluate("""() => {
        const all = document.body?.innerText || '';
        return {stay: all.includes('Stay logged out') || all.includes('Оставаться вышедшим'),
                login: all.includes('Log in or sign up') || all.includes('Войдите или зарегистрируйтесь')};
    }""")
    print("modal after send:", modal_en)
    # try dismiss in both languages
    for txt in ["Stay logged out", "Оставаться вышедшим"]:
        try:
            stay = page.locator(f'button:has-text("{txt}")').first
            if await stay.count() > 0 and await stay.is_visible():
                await stay.click(timeout=2000, force=True)
                print(f"dismissed via '{txt}'")
                await asyncio.sleep(1.5)
                sb = page.locator('button.send-button').first
                if await sb.count() > 0:
                    await sb.click(timeout=3000, force=True)
                    print("send again")
                await asyncio.sleep(2.5)
                break
        except Exception:
            pass

    await asyncio.sleep(12)
    print("=== SSE:", len(sse))
    print("DOM:", (await page.evaluate("() => document.body?.innerText || ''"))[:250].replace("\n"," | "))
    await ctx.close()
    await p.stop()

async def main():
    await run_case("enUS", "en-US")
    await run_case("enUS_cookies", "en-US", with_cookies=True)

asyncio.run(main())

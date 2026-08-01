#!/usr/bin/env python3
"""Wait LONGER for welcome modal; check localStorage flags; dismiss BEFORE typing."""
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

async def check(page, label):
    s = await page.evaluate("""() => {
        const all = document.body?.innerText || '';
        const ls = {};
        for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            if (/guest|consent|chat|qwen|anon|stay|login|auth/i.test(k)) ls[k] = (localStorage.getItem(k)||'').slice(0,60);
        }
        return {
            modal: all.includes('Оставаться вышедшим'),
            welcome: all.includes('Добро пожаловать'),
            loginBtn: all.includes('Войти'),
            ls: ls
        };
    }""")
    print(f"[{label}] modal={s['modal']} welcome={s['welcome']}")
    if s['ls']:
        print(f"    LS flags: {json.dumps(s['ls'], ensure_ascii=False)[:400]}")

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_long1", headless=True,
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
    # Poll for modal up to 30s
    for i in range(15):
        await asyncio.sleep(2)
        modal = await page.evaluate("() => (document.body?.innerText||'').includes('Оставаться вышедшим')")
        if modal:
            print(f"modal appeared at t+{2*(i+1)}s")
            break
    await check(page, "after load")

    # Dismiss modal BEFORE typing if present
    try:
        stay = page.locator('button:has-text("Оставаться вышедшим")').first
        if await stay.count() > 0 and await stay.is_visible():
            await stay.click(timeout=2000, force=True)
            print("dismissed BEFORE typing")
            await asyncio.sleep(1)
    except Exception:
        pass
    await check(page, "after pre-dismiss")

    # Close guidance
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
    await ta.click(timeout=3000, force=True)
    await page.keyboard.type(PROMPT, delay=20)
    await asyncio.sleep(0.5)
    sb = page.locator('button.send-button').first
    await sb.click(timeout=3000, force=True)
    print("clicked send")
    await asyncio.sleep(3)
    modal = await page.evaluate("() => (document.body?.innerText||'').includes('Оставаться вышедшим')")
    print("modal after send:", modal)
    if modal:
        stay = page.locator('button:has-text("Оставаться вышедшим")').first
        await stay.click(timeout=2000, force=True)
        print("dismissed after send")
        await asyncio.sleep(1.5)
        sb = page.locator('button.send-button').first
        if await sb.count() > 0:
            await sb.click(timeout=3000, force=True)
            print("send #2")
    await asyncio.sleep(15)
    print("=== SSE:", len(sse))
    if sse:
        print("LAST:", sse[-1][:700])
    await check(page, "final")
    await ctx.close()
    await p.stop()

asyncio.run(main())

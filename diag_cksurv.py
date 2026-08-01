#!/usr/bin/env python3
"""Check: do user cookies survive page load? Does 'Stay logged out' write localStorage?"""
import asyncio, os, json
os.environ.setdefault("QWENMODE_GUEST_MODE", "1")
from playwright.async_api import async_playwright

URL = "https://chat.qwen.ai"
_LAUNCH_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage", "--disable-gpu",
]
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
        user_data_dir="/tmp/qwenmode_cksurv1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="en-US", timezone_id="Europe/Helsinki",
    )
    cookies = load_cookies(COOKIE_FILE)
    await ctx.add_cookies(cookies)
    print("applied:", len(cookies))

    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(4)

    after = await ctx.cookies()
    after_names = {c["name"]: c["value"][:25] for c in after}
    print("cookies after load:", json.dumps(after_names, indent=1)[:800])

    # Is _c_WBKFRo still there?
    wbk = [c for c in after if c["name"] == "_c_WBKFRo"]
    print("_c_WBKFRo present:", bool(wbk), wbk[0]["value"][:20] if wbk else "")

    # Trigger send -> modal -> stay logged out, then check localStorage diff
    ta = page.locator('textarea').first
    await ta.click(timeout=3000, force=True)
    await page.keyboard.type(PROMPT, delay=25)
    await asyncio.sleep(0.8)
    ls_before = await page.evaluate("() => Object.keys(localStorage)")
    sb = page.locator('button.send-button').first
    await sb.click(timeout=3000, force=True)
    await asyncio.sleep(2)
    modal = await page.evaluate("() => (document.body?.innerText||'').includes('Stay logged out')")
    print("modal:", modal)
    if modal:
        stay = page.locator('button:has-text("Stay logged out")').first
        await stay.click(timeout=3000, force=True)
        await asyncio.sleep(2)
        ls_after = await page.evaluate("() => Object.keys(localStorage)")
        new_keys = [k for k in ls_after if k not in ls_before]
        print("NEW localStorage keys after Stay logged out:", new_keys)
        # full ls dump
        all_ls = await page.evaluate("""() => {
            const o = {};
            for (let i=0;i<localStorage.length;i++){const k=localStorage.key(i);o[k]=(localStorage.getItem(k)||'').slice(0,60);}
            return o;
        }""")
        print("LS:", json.dumps(all_ls, ensure_ascii=False)[:900])
    await ctx.close()
    await p.stop()

asyncio.run(main())

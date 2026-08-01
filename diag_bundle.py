#!/usr/bin/env python3
"""Find what triggers the Welcome modal — grep frontend bundle."""
import asyncio, os, json, re
os.environ.setdefault("QWENMODE_GUEST_MODE", "1")
from playwright.async_api import async_playwright

URL = "https://chat.qwen.ai"
_LAUNCH_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage", "--disable-gpu",
]

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_bundle1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="en-US", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()

    # Capture JS bundles
    bundles = []
    async def on_resp(resp):
        ct = resp.headers.get("content-type", "")
        if "javascript" in ct and resp.url.startswith(URL):
            try:
                body = await resp.text()
                bundles.append((resp.url, body))
                print(f"captured bundle: {resp.url.split('/')[-1][:60]} ({len(body)}b)")
            except Exception:
                pass
    page.on("response", on_resp)
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(10)
    print("total captured:", len(bundles))
    if not bundles:
        # Try fetching known bundle paths
        try:
            for path in ["/assets/index.js", "/assets/index-B.js", "/static/js/main.js"]:
                r = await page.request.get(URL + path)
                print(path, r.status)
        except Exception as e:
            print("fetch err:", e)

    # Search for welcome modal trigger strings
    patterns = [
        "welcome", "Welcome", "welcome-modal", "loginModal", "login-modal",
        "isLogin", "needLogin", "guest", "showLogin", "Stay logged out",
        "stayLoggedOut", "welcome_modal", "chatWelcome", "firstMessage",
        "noAuth", "anonymous", "unauthorized", "unAuth",
    ]
    print(f"\n=== searching {len(bundles)} bundles ===")
    for url, body in bundles:
        for pat in patterns:
            for m in re.finditer(pat, body):
                s = max(0, m.start()-120)
                e = min(len(body), m.end()+120)
                snippet = body[s:e].replace("\n", " ")
                print(f"\n[{url.split('/')[-1][:40]} | {pat}] ...{snippet}...")
                break  # one hit per pattern per bundle
    await ctx.close()
    await p.stop()

asyncio.run(main())

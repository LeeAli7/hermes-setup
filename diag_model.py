#!/usr/bin/env python3
"""Diagnose model selection in headless persistent context (same as server)."""
import asyncio, os, json
os.environ.setdefault("QWENMODE_GUEST_MODE", "1")
from playwright.async_api import async_playwright

URL = "https://chat.qwen.ai"
MODEL = "Qwen3.8-Max-Preview"
USER_DATA_DIR = "/tmp/qwenmode_diag_profile"

_LAUNCH_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-web-security", "--no-first-run", "--no-default-browser-check",
    "--disable-dev-shm-usage", "--disable-gpu",
]

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir=USER_DATA_DIR, headless=True, args=_LAUNCH_ARGS,
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="ru-RU", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(3)

    # Dump header buttons
    btns = await page.evaluate("""() => {
        const btns = [...document.querySelectorAll('button')];
        return btns.map(b => ({
            txt: (b.textContent||'').trim().slice(0,40),
            cls: (b.className||'').toString().slice(0,70),
            vis: !!(b.offsetWidth||b.offsetHeight)
        })).filter(b => b.txt || b.cls.includes('model'));
    }""")
    print("=== BUTTONS ===")
    print(json.dumps(btns, ensure_ascii=False, indent=1)[:1500])

    # Find trigger-like elements
    trig = await page.evaluate("""() => {
        const sels = ['.ant-dropdown-trigger', '.index-module__model-selector___rdCim',
                      '[class*="model-selector"]', '[class*="model_selector"]'];
        const out = {};
        for (const s of sels) {
            const els = [...document.querySelectorAll(s)];
            out[s] = els.map(e => ({tag: e.tagName, txt: (e.textContent||'').trim().slice(0,40), vis: !!(e.offsetWidth||e.offsetHeight), cls: (e.className||'').toString().slice(0,60)}));
        }
        return out;
    }""")
    print("=== TRIGGERS ===")
    print(json.dumps(trig, ensure_ascii=False, indent=1)[:2000])

    # Try clicking each candidate trigger and see if dropdown items appear
    for sel in ['.ant-dropdown-trigger', '[class*="model-selector"]']:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.click(timeout=3000, force=True)
                await asyncio.sleep(1.2)
                items = await page.evaluate("""() => {
                    const names = [...document.querySelectorAll('[class*="model-item-name"]')];
                    return names.map(n => ({txt: (n.textContent||'').trim(), vis: !!(n.offsetWidth||n.offsetHeight), cls: (n.className||'').toString().slice(0,50)}));
                }""")
                print(f"=== AFTER CLICK {sel} ===")
                print(json.dumps(items, ensure_ascii=False, indent=1)[:1500])
                # close dropdown by pressing Escape
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
        except Exception as e:
            print(f"click {sel} failed: {e}")

    await ctx.close()
    await p.stop()

asyncio.run(main())

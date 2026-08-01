#!/usr/bin/env python3
"""Diagnose model selection v2: close guidance, click exact selector."""
import asyncio, os, json
os.environ.setdefault("QWENMODE_GUEST_MODE", "1")
from playwright.async_api import async_playwright

URL = "https://chat.qwen.ai"
MODEL = "Qwen3.8-Max-Preview"
USER_DATA_DIR = "/tmp/qwenmode_diag_profile2"

_LAUNCH_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-web-security", "--no-first-run", "--no-default-browser-check",
    "--disable-dev-shm-usage", "--disable-gpu",
]

async def dump_items(page, label):
    items = await page.evaluate("""() => {
        const names = [...document.querySelectorAll('[class*="model-item-name"]')];
        return names.map(n => ({txt: (n.textContent||'').trim(), vis: !!(n.offsetWidth||n.offsetHeight), cls: (n.className||'').toString().slice(0,50)}));
    }""")
    print(f"=== {label} ===")
    print(json.dumps(items, ensure_ascii=False, indent=1)[:1500])

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

    # Close guidance popup if present
    for sel in ['.guidance-pc-close-btn', 'button[aria-label="Закрыть"]', '[class*="guidance"] button']:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=2000, force=True)
                print(f"closed guidance via {sel}")
                await asyncio.sleep(0.8)
                break
        except Exception as e:
            print(f"guidance {sel}: {e}")

    await dump_items(page, "BEFORE CLICK")

    # Click exact model selector
    sel = '.index-module__model-selector___rdCim'
    loc = page.locator(sel).first
    print("count:", await loc.count())
    await loc.click(timeout=4000, force=True)
    await asyncio.sleep(1.2)
    await dump_items(page, "AFTER CLICK selector div")

    # Try clicking the text element too
    if not await page.locator('[class*="model-item-name"]').count():
        loc2 = page.locator('.index-module__model-selector-text___XvWe0').first
        await loc2.click(timeout=4000, force=True)
        await asyncio.sleep(1.2)
        await dump_items(page, "AFTER CLICK text div")

    await ctx.close()
    await p.stop()

asyncio.run(main())

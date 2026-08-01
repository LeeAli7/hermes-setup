#!/usr/bin/env python3
"""Deep-dive the Welcome modal HTML: buttons, checkboxes, data attrs."""
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

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_modal_html1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="en-US", timezone_id="Europe/Helsinki",
    )
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

    ta = page.locator('textarea').first
    await ta.click(timeout=3000, force=True)
    await page.keyboard.type(PROMPT, delay=25)
    await asyncio.sleep(0.8)
    sb = page.locator('button.send-button').first
    await sb.click(timeout=3000, force=True)
    await asyncio.sleep(2)

    # Dump the modal's HTML structure
    html = await page.evaluate("""() => {
        // Find the welcome/login modal container
        const all = document.querySelectorAll('div[class*="modal"], div[class*="Modal"], div[role="dialog"], div[class*="dialog"], div[class*="popup"], div[class*="Popup"]');
        for (const el of all) {
            const txt = el.textContent || '';
            if (txt.includes('Stay logged out') || txt.includes('Оставаться вышедшим') || txt.includes('Welcome') || txt.includes('Log in or sign up')) {
                return {
                    cls: el.className?.toString().slice(0, 120),
                    tag: el.tagName,
                    html: el.outerHTML.slice(0, 2500)
                };
            }
        }
        return {found: false, bodyClass: document.body.className?.toString().slice(0,100)};
    }""")
    print(json.dumps(html, ensure_ascii=False, indent=1)[:3000])
    await ctx.close()
    await p.stop()

asyncio.run(main())

#!/usr/bin/env python3
"""Probe /api/v2/chat/completions directly from page context (bypass UI gate)."""
import asyncio, os, json
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
        user_data_dir="/tmp/qwenmode_api1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="ru-RU", timezone_id="Europe/Helsinki",
    )
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

    result = await page.evaluate("""async () => {
        const body = {
            model: 'Qwen3.8-Max-Preview',
            messages: [{role: 'user', content: 'Say hello in exactly 5 words'}],
            stream: false
        };
        try {
            const r = await fetch('/api/v2/chat/completions', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            });
            const txt = await r.text();
            return {status: r.status, body: txt.slice(0, 600)};
        } catch (e) {
            return {error: String(e)};
        }
    }""")
    print(json.dumps(result, ensure_ascii=False, indent=1)[:1200])
    await ctx.close()
    await p.stop()

asyncio.run(main())

#!/usr/bin/env python3
"""Deep: dump buttons, check send disabled state, React-aware typing."""
import asyncio, os, json
os.environ.setdefault("QWENMODE_GUEST_MODE", "1")
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

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_deep1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="ru-RU", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(3)

    # Close guidance
    try:
        g = page.locator('.guidance-pc-close-btn').first
        if await g.count() > 0:
            await g.click(timeout=2000, force=True)
            await asyncio.sleep(0.8)
    except Exception:
        pass

    async def dump_buttons(label):
        btns = await page.evaluate("""() => {
            return [...document.querySelectorAll('button')].map(b => ({
                txt: (b.textContent||'').trim().slice(0,25),
                cls: (b.className||'').toString().slice(0,55),
                disabled: b.disabled,
                vis: !!(b.offsetWidth||b.offsetHeight),
                aria: b.getAttribute('aria-label')||''
            })).filter(b => b.vis);
        }""")
        print(f"=== BUTTONS {label} ===")
        print(json.dumps(btns, ensure_ascii=False)[:1200])

    await dump_buttons("EMPTY")

    # React-aware typing (native setter + events, like server _type_text)
    await page.evaluate("""(t) => {
        const ta = document.querySelector('textarea');
        if (!ta) return;
        ta.focus();
        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
        setter.call(ta, t);
        ta.dispatchEvent(new Event('input', {bubbles: true}));
        ta.dispatchEvent(new Event('change', {bubbles: true}));
        ta.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', bubbles: true}));
        setter.call(ta, t);
        ta.dispatchEvent(new Event('input', {bubbles: true}));
    }""", "Say hello in exactly 5 words")
    await asyncio.sleep(1)

    val = await page.evaluate("() => document.querySelector('textarea')?.value || ''")
    print("ta value:", repr(val))
    await dump_buttons("AFTER TYPE")

    # Click send
    sb = page.locator('button.send-button, button[aria-label="Send"]').first
    print("send count:", await sb.count())
    if await sb.count() > 0:
        print("send disabled:", await sb.is_disabled())
        await sb.click(timeout=3000, force=True)
        print("clicked")
    await asyncio.sleep(4)

    # What happened?
    print("=== AFTER SEND ===")
    print((await page.evaluate("() => document.body?.innerText || ''"))[:400].replace("\n"," | "))
    await ctx.close()
    await p.stop()

asyncio.run(main())

#!/usr/bin/env python3
"""Прямой тест гостевого режима Qwen через Playwright."""
import asyncio, json, sys, os, re
os.environ.setdefault("QWENMODE_GUEST_MODE", "1")
from playwright.async_api import async_playwright

URL = "https://chat.qwen.ai"
SSE_PATTERN = "/api/v2/chat/completions"

async def main():
    log("Starting browser...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
            locale="ru-RU",
        )
        page = await ctx.new_page()
        log("Navigating to chat.qwen.ai...")
        await page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        body = await page.evaluate("() => document.body?.innerText?.substring(0, 1000) || 'NO BODY'")
        log(f"Page loaded, text: {body[:300]}")

        # Check for auth modal
        has_auth = await page.evaluate("""() => {
            const btns = document.querySelectorAll('button');
            const texts = Array.from(btns).map(b => b.textContent?.trim());
            return texts;
        }""")
        log(f"Buttons: {has_auth}")

        # Try typing
        log("Typing message...")
        ta = page.locator("textarea").first
        if await ta.is_visible(timeout=3000):
            await ta.fill("Привет! Скажи 'тест' одним словом.")
            log("Text filled")
        else:
            log("NO TEXTAREA FOUND")
            await page.screenshot(path="/tmp/qwen_screenshot.png")
            log("Screenshot saved")
            text = await page.evaluate("() => document.body?.innerText?.substring(0, 3000) || 'none'")
            log(f"Full body text: {text}")
            await browser.close()
            return

        await asyncio.sleep(1)

        # Try to dismiss any dialog then click send
        dismissed = await page.evaluate("""() => {
            const all = document.body?.innerText || '';
            const signals = ['Оставаться вышедшим', 'Остаться не авторизованным',
                'Continue without', 'Продолжить без', 'No thanks'];
            const found = signals.some(s => all.includes(s));
            if (!found) return 'no_dialog';
            const els = document.querySelectorAll('button');
            for (const el of els) {
                const txt = el.textContent?.trim().toLowerCase() || '';
                if (txt.includes('остаться') || txt.includes('продолжить')
                    || txt.includes('continue without') || txt.includes('no thanks')) {
                    el.click();
                    return 'clicked_' + txt;
                }
            }
            return 'dialog_found_but_no_match';
        }""")
        log(f"Dismiss result: {dismissed}")

        await asyncio.sleep(1)

        # Setup SSE capture
        raw_response = {"body": None}
        def on_response(resp):
            if SSE_PATTERN in resp.url:
                log(f"SSE response captured! URL: {resp.url[:100]}")
                async def get_body():
                    try:
                        raw_response["body"] = await resp.text()
                    except Exception as e:
                        raw_response["body"] = f"<error: {e}>"
                asyncio.ensure_future(get_body())
        page.on("response", on_response)

        # Try clicking send button
        send_btn = page.locator("button:has-text('Отправить')").or_(
            page.locator("button:has-text('Send')")
        ).first

        for attempt in range(3):
            log(f"--- Send attempt {attempt + 1} ---")

            if await send_btn.is_visible(timeout=1000):
                log("Clicking Send button...")
                await send_btn.click()
            else:
                log("Send button not visible, trying Enter...")
                await page.keyboard.press("Enter")

            # Wait for SSE or response
            for s in range(25):
                await asyncio.sleep(1)
                if raw_response["body"] is not None:
                    log(f"GOT SSE after {s+1}s!")
                    break
                # Check for auth dialog
                has_dialog = await page.evaluate("""() => {
                    const all = document.body?.innerText || '';
                    const signals = ['Оставаться вышедшим', 'Остаться не авторизованным',
                        'Continue without', 'Продолжить без', 'No thanks', 'Not now', 'Skip'];
                    return signals.filter(s => all.includes(s));
                }""")
                if has_dialog and len(has_dialog) > 0:
                    log(f"Auth dialog detected: {has_dialog}")
                    # Dismiss it
                    await page.evaluate("""() => {
                        const els = document.querySelectorAll('button');
                        for (const el of els) {
                            const txt = el.textContent?.trim().toLowerCase() || '';
                            if (txt.includes('остаться') || txt.includes('продолжить')
                                || txt.includes('continue without') || txt.includes('no thanks')
                                || txt.includes('not now') || txt.includes('skip')) {
                                el.click();
                                return;
                            }
                        }
                    }""")
                    await asyncio.sleep(1)
                    # Re-type and retry
                    if await ta.is_visible(timeout=1000):
                        await ta.fill("Привет! Скажи 'тест' одним словом.")
                    break  # retry send

            if raw_response["body"] is not None:
                break

        # Log final state
        await page.screenshot(path="/tmp/qwen_test_final.png")
        log("Screenshot saved to /tmp/qwen_test_final.png")
        final_body = await page.evaluate("() => document.body?.innerText?.substring(0, 3000) || 'none'")
        log(f"Final body: {final_body[:500]}")

        if raw_response["body"]:
            log(f"SSE RAW ({len(raw_response['body'])} chars): {raw_response['body'][:500]}")
        else:
            log("NO SSE RESPONSE RECEIVED")
            # Try DOM extraction
            dom_text = await page.evaluate("""() => {
                const all = document.body?.innerText || '';
                const parts = all.split('\\n').filter(l => l.trim().length > 3);
                return parts.slice(-20).join('\\n');
            }""")
            log(f"DOM fallback: {dom_text[:500]}")

        await browser.close()
        log("Done!")

def log(msg):
    print(f"[{asyncio.get_event_loop().time():.1f}] {msg}", flush=True)

if __name__ == "__main__":
    asyncio.run(main())

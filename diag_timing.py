#!/usr/bin/env python3
"""Check modal timing: does 'Добро пожаловать' appear by itself after load?"""
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

async def check(page, label):
    info = await page.evaluate("""() => {
        const all = document.body?.innerText || '';
        return {
            hasWelcome: all.includes('Добро пожаловать') || all.includes('Welcome'),
            hasStay: all.includes('Оставаться вышедшим') || all.includes('Stay logged out'),
            hasLoginModal: all.includes('Войдите или зарегистрируйтесь') || all.includes('Log in or sign up'),
            head: all.split('\\n').slice(0,12).join(' | ')
        };
    }""")
    print(f"[{label}] welcome={info['hasWelcome']} stay={info['hasStay']} loginModal={info['hasLoginModal']}")
    print(f"    head: {info['head'][:200]}")

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_timing1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="ru-RU", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    for i in range(6):
        await asyncio.sleep(2)
        await check(page, f"t+{2*(i+1)}s")
    await ctx.close()
    await p.stop()

asyncio.run(main())

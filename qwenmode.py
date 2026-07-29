#!/usr/bin/env python3
"""
QwenMode — OpenAI-compatible API server for chat.qwen.ai
Fixed version with proper concurrency, error handling and streaming.
"""

import asyncio
import json
import sys
import time
import re
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Optional, Any
from playwright.async_api import async_playwright, Page, BrowserContext

# ─── Configuration ──────────────────────────────────────────────────────────
URL = os.getenv("QWENMODE_URL", "https://chat.qwen.ai")
POOL_SIZE = int(os.getenv("QWENMODE_POOL_SIZE", "1"))
SSE_TIMEOUT = int(os.getenv("QWENMODE_SSE_TIMEOUT", "90"))
MAX_ATTEMPTS = int(os.getenv("QWENMODE_MAX_ATTEMPTS", "2"))
CREATE_PAGE_DELAY = float(os.getenv("QWENMODE_CREATE_PAGE_DELAY", "3.0"))
USER_DATA_DIR = os.getenv("QWENMODE_USER_DATA_DIR", "/tmp/qwenmode_profile")
HEADLESS = os.getenv("QWENMODE_HEADLESS", "true").lower() == "true"
API_KEY = os.getenv("QWENMODE_API_KEY", "")
GUEST_MODE = os.getenv("QWENMODE_GUEST_MODE", "").lower() in ("1", "true", "yes")

log = logging.getLogger("qwenmode")

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-web-security",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

_PROXY = os.getenv("QWENMODE_PROXY", "").strip()
if _PROXY:
    _LAUNCH_ARGS.insert(1, f"--proxy-server={_PROXY}")
    log.info(f"Using proxy: {_PROXY}")

_SSE_URL_PATTERN = "/api/v2/chat/completions"

# ─── Page State ─────────────────────────────────────────────────────────────
@dataclass
class PageState:
    page: Page
    busy: bool = False
    resetting: bool = False
    last_used: float = field(default_factory=time.time)
    health_failures: int = 0


# ─── Helpers ────────────────────────────────────────────────────────────────
async def _dismiss_auth_dialog(page: Page) -> bool:
    """Dismiss 'Stay logged out' / 'Stay signed out' / 'Остаться не авторизованным' dialog."""
    try:
        dismissed = await asyncio.wait_for(page.evaluate("""() => {
            const all = document.body?.innerText || '';
            const signals = ['Stay logged out', 'Stay signed out',
                'Оставаться вышедшим', 'Остаться не авторизованным',
                'Остаться без', 'Продолжить без', 'Continue without',
                'No thanks', 'Not now', 'Skip', 'Maybe later'];
            const found = signals.some(s => all.includes(s));
            if (!found) return false;
            const els = document.querySelectorAll('button, div[role="button"]');
            for (const el of els) {
                const txt = el.textContent?.trim().toLowerCase() || '';
                if (txt.includes('остаться') || txt.includes('продолжить')
                    || txt.includes('continue without') || txt.includes('no thanks')
                    || txt.includes('not now') || txt.includes('skip')
                    || txt === 'stay logged out' || txt === 'stay signed out') {
                    el.click();
                    return true;
                }
            }
            return false;
        }"""), timeout=5)
        if dismissed:
            await asyncio.sleep(1)
        return bool(dismissed)
    except Exception:
        return False


async def _dismiss_modal(page: Page) -> bool:
    """Dismiss any overlay/popup/modal (cookie consent, login suggestion, upgrade, etc.)."""
    try:
        dismissed = await asyncio.wait_for(page.evaluate("""() => {
            const all = document.body?.innerText || '';
            // 1. Cookie consent banner
            const cookieBtns = document.querySelectorAll('button');
            for (const btn of cookieBtns) {
                const txt = btn.textContent?.trim().toLowerCase() || '';
                if (txt.includes('accept all cookies')) {
                    btn.click();
                    return true;
                }
            }
            // 2. Auth/login overlay
            const signals = ['log in', 'sign in', 'unlock', 'upgrade', 'more possibilities',
                'вход', 'войти', 'зарегистрироваться', 'больше возможностей',
                'get started', 'try qwen+', 'qwen+', 'premium',
                "you've reached", 'usage limit', 'daily limit'];
            const hasSignal = signals.some(s => all.toLowerCase().includes(s));
            if (!hasSignal) return false;

            // Try clicking X / close buttons
            const closeSelectors = [
                'button[class*="close"]', 'button[aria-label*="Close"]',
                'button[aria-label*="close"]', '[class*="modal"] button:first-child',
                '.dialog-close', '[class*="overlay"] button',
                'svg[class*="close"]', 'svg[x]',
                'button:has(svg[x])', 'button:has(svg[viewBox])',
                // Any button containing X-like text
                'button:not([class*="primary"]):not([class*="submit"])'
            ];
            for (const sel of closeSelectors) {
                const btns = document.querySelectorAll(sel);
                for (const btn of btns) {
                    const txt = btn.textContent?.trim().toLowerCase() || '';
                    const isIconBtn = !txt || txt.length < 3;
                    if (isIconBtn) {
                        (btn).click();
                        return true;
                    }
                }
            }
            // Last resort: click backdrop/overlay
            const overlays = document.querySelectorAll('[class*="overlay"], [class*="backdrop"], [class*="mask"]');
            for (const ov of overlays) {
                if (ov && ov.parentElement) {
                    ov.click();
                    return true;
                }
            }
            return false;
        }"""), timeout=5)
        if dismissed:
            await asyncio.sleep(1.5)
        return bool(dismissed)
    except Exception:
        return False


async def _create_page(ctx: BrowserContext, model: str) -> Page:
    """Create and initialize a new page."""
    page = await ctx.new_page()
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)
        await _dismiss_auth_dialog(page)
        await _dismiss_modal(page)
        await asyncio.sleep(2)
        # Wait for textarea to appear = page is ready
        for _ in range(10):
            ta = await page.query_selector('textarea')
            if ta:
                break
            await asyncio.sleep(0.5)
        return page
    except Exception:
        try:
            await page.close()
        except Exception:
            pass
        raise


async def _type_text(page: Page, text: str) -> None:
    """Type text into the chat textarea, triggering React events properly."""
    try:
        await page.evaluate("""(t) => {
            const ta = document.querySelector('textarea');
            if (!ta) return;
            // Focus first
            ta.focus();
            ta.dispatchEvent(new Event('focus', {bubbles: true}));
            // Set value via native setter (bypasses React controlled component)
            const nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLTextAreaElement.prototype, 'value'
            );
            if (nativeSetter && nativeSetter.set) {
                nativeSetter.set.call(ta, t);
            } else {
                ta.value = t;
            }
            // Dispatch all events React needs
            ta.dispatchEvent(new Event('input', {bubbles: true}));
            ta.dispatchEvent(new Event('change', {bubbles: true}));
            ta.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', bubbles: true}));
            // Trigger React's synthetic event by dispatching input with native value
            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLTextAreaElement.prototype, 'value'
            ).set;
            nativeInputValueSetter.call(ta, t);
            ta.dispatchEvent(new Event('input', {bubbles: true}));
        }""", text)
    except Exception:
        # Fallback: use Playwright fill()
        ta = await page.query_selector('textarea')
        if ta:
            await ta.fill(text)


async def _click_send(page: Page) -> None:
    """Send the message via Playwright click on Send button or Enter."""
    # Primary: button.send-button (class-based, works on all locales)
    for attempt in range(3):
        try:
            send_btn = page.locator('button.send-button').or_(
                page.locator('button[aria-label="Send"]')
            ).or_(
                page.locator('button[aria-label="Отправить"]')
            ).or_(
                page.locator('button:has-text("Отправить")')
            ).or_(
                page.locator('button:has-text("Send")')
            ).first
            if await send_btn.is_visible(timeout=800):
                await send_btn.click(force=True, timeout=5000)
                await asyncio.sleep(0.5)
                return
        except Exception:
            pass
        await asyncio.sleep(0.3)

    # Fallback: focus textarea and press Enter
    try:
        ta = page.locator('textarea').first
        if await ta.is_visible(timeout=500):
            await ta.click(force=True, timeout=3000)
            await asyncio.sleep(0.1)
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.5)
            return
    except Exception:
        pass

    # Last resort
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.5)


def _detect_captcha(body_text: str) -> bool:
    """Detect if page shows captcha."""
    bl = body_text.lower()
    return any(x in bl for x in ["security verification", "drag the slider", "captcha", "verify you"])


def _parse_qwen_sse(raw: str) -> dict:
    """Parse SSE stream from Qwen into text and reasoning."""
    if not raw:
        return {"text": "", "reasoning": ""}

    # Error response
    if raw.startswith('{"success":false') or raw.startswith('{"code"'):
        try:
            err = json.loads(raw)
            if err.get("success") is False:
                details = (err.get("data", {}).get("details", "")
                           or err.get("data", {}).get("template", "")
                           or err.get("message", ""))
                if details:
                    return {"text": f"[Qwen Error] {details}", "reasoning": ""}
                code = err.get("data", {}).get("code", "unknown")
                return {"text": f"[Qwen Error {code}]", "reasoning": ""}
        except Exception:
            pass

    reasoning_parts = []
    answer_parts = []

    for line in raw.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload.startswith('{"response.created"'):
            continue
        if payload == "[DONE]":
            break
        try:
            ev = json.loads(payload)
        except Exception:
            continue

        choices = ev.get("choices", [])
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        phase = delta.get("phase", "")
        content = delta.get("content", "") or ""

        if phase == "thinking_summary":
            extra = delta.get("extra", {})
            thought = extra.get("summary_thought", {})
            thought_content = thought.get("content", [])
            if thought_content:
                reasoning_parts.append(str(thought_content[0]))
        elif phase == "answer" and content:
            answer_parts.append(str(content))
        elif delta.get("status") == "finished":
            break

    text = "".join(answer_parts).strip()
    reasoning = "\n".join(reasoning_parts).strip()
    return {"text": text, "reasoning": reasoning}


async def _read_chat_from_dom(page: Page, prompt: str = "") -> str:
    """Extract chat response from DOM as fallback."""
    return await page.evaluate("""(prompt) => {
        const body = document.body?.innerText || '';
        if (!body) return '';

        let startIdx = 0;
        if (prompt) {
            const lastPrompt = body.lastIndexOf(prompt);
            if (lastPrompt !== -1) {
                startIdx = lastPrompt + prompt.length;
            }
        }

        const afterPrompt = body.substring(startIdx).trim();
        const markers = [
            'Generated by', 'Terms of', 'Privacy Policy', 'Contact us',
            'Log in', 'Sign up', 'Welcome', 'Stay logged out',
            'Get Started', 'Choose a style', 'Auto', 'Ready when you are',
            'How can I help', 'Create Image', 'Chat', 'Spaces', 'Explore',
            'Settings', 'Profile', 'Войти', 'Зарегистрироваться',
            'Добро пожаловать', 'Новый чат',
        ];

        let endIdx = afterPrompt.length;
        for (const m of markers) {
            const idx = afterPrompt.lastIndexOf(m);
            if (idx !== -1 && idx > endIdx - 500 && idx < endIdx) {
                endIdx = idx;
            }
        }

        let result = afterPrompt.substring(0, endIdx).trim();
        const lines = result.split('\n').filter(l => {
            const t = l.trim();
            if (t.length < 2) return false;
            if (t.startsWith('icon-')) return false;
            return true;
        });
        return lines.join('\n').trim();
    }""", prompt)


async def _wait_for_response(page: Page, prompt: str, model: str, timeout: int) -> tuple[dict, Page]:
    """Send prompt and wait for response via SSE or DOM fallback."""
    bodies: dict[str, Optional[str]] = {"raw": None}
    response_event = asyncio.Event()

    async def capture(resp) -> None:
        if _SSE_URL_PATTERN in resp.url and bodies["raw"] is None:
            try:
                raw = await asyncio.wait_for(resp.text(), timeout=timeout)
                bodies["raw"] = raw
                response_event.set()
            except Exception:
                pass

    page.on("response", capture)

    try:
        await _dismiss_auth_dialog(page)
        await _dismiss_modal(page)
        await _type_text(page, prompt)
        await asyncio.sleep(0.5)

        # Send loop — press Enter, dismiss login modal if it appears, retry
        for send_attempt in range(3):
            response_event.clear()
            await _click_send(page)

            # Wait short for SSE (high chance it comes in 3-5s if no modal)
            try:
                await asyncio.wait_for(response_event.wait(), timeout=3.0)
                break  # Got SSE!
            except asyncio.TimeoutError:
                pass

            # No SSE yet — check if a login modal appeared and dismiss it
            modal_dismissed = False
            try:
                ad = await _dismiss_auth_dialog(page)
                md = await _dismiss_modal(page)
                modal_dismissed = ad or md
            except Exception:
                pass

            if modal_dismissed:
                await asyncio.sleep(1)
                # Re-type if textarea was cleared by modal dismiss
                try:
                    current = await page.evaluate("() => document.querySelector('textarea')?.value || ''")
                    if len(current.strip()) < len(prompt.strip()):
                        await _type_text(page, prompt)
                except Exception:
                    await _type_text(page, prompt)
                continue  # Modal was blocking — retry

            # No modal, no SSE yet — wait the remainder
            try:
                await asyncio.wait_for(response_event.wait(), timeout=max(1, timeout - 3))
            except asyncio.TimeoutError:
                pass
            break

        if bodies["raw"] is not None:
            parsed = _parse_qwen_sse(bodies["raw"])
            # Dismiss post-response login prompt (guest mode)
            await _dismiss_auth_dialog(page)
            await _dismiss_modal(page)
            return parsed, page

        # No SSE — check for captcha
        try:
            bt = await asyncio.wait_for(
                page.evaluate("() => document.body.innerText"),
                timeout=5
            )
        except Exception:
            bt = ""

        if _detect_captcha(bt):
            return {"text": "[QwenMode] Captcha detected", "reasoning": ""}, page

        # DOM fallback
        try:
            text = await asyncio.wait_for(_read_chat_from_dom(page, prompt), timeout=5)
        except Exception:
            text = ""

        return {"text": text.strip(), "reasoning": ""}, page

    finally:
        try:
            page.remove_listener("response", capture)
        except Exception:
            pass


# ─── Prompt Building ────────────────────────────────────────────────────────
def _build_prompt(messages: list[dict]) -> str:
    """Convert OpenAI messages format to plain text for Qwen."""
    parts = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")

        if role == "system":
            text = str(content) if content else ""
            if len(text) > 800:
                text = text[:800] + "\n...[system truncated]"
            parts.append(f"[System]\n{text}")

        elif role == "user":
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"]
                text = "\n".join(texts)
            else:
                text = str(content)
            if len(text) > 2000:
                text = text[:2000] + "\n...[user message truncated]"
            parts.append(text)

        elif role == "assistant":
            tc = m.get("tool_calls")
            if tc:
                for t in tc:
                    fn = t.get("function", {})
                    parts.append(f'{{"tool": "{fn.get("name", "")}", "arguments": {fn.get("arguments", "{}")}}}')
            elif isinstance(content, list):
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                text = "\n".join(texts)
                if len(text) > 500:
                    text = text[:500] + "\n...[truncated]"
                parts.append(text)
            else:
                text = str(content)
                if len(text) > 500:
                    text = text[:500] + "\n...[truncated]"
                parts.append(text)

        elif role == "tool":
            text = str(content)
            if len(text) > 300:
                text = text[:300] + "\n...[tool result truncated]"
            parts.append(f"[Tool result]\n{text}")

    return "\n\n".join(parts).strip()


def _build_tool_prompt(last_content: str, tools: list[dict]) -> str:
    """Append tool descriptions to prompt."""
    desc_lines = ["Available tools:"]
    for t in tools:
        fn = t.get("function", {})
        name = fn.get("name", "")
        desc = fn.get("description", "")
        short = desc.split("\n")[0] if desc else ""
        if len(short) > 120:
            short = short[:120] + "..."
        desc_lines.append(f" {name}: {short}")
    desc_lines.extend([
        "",
        'Respond with ONLY: {"tool": "name", "arguments": {...}}',
        "NO other text. NO explanation.",
        "",
        last_content,
    ])
    return "\n".join(desc_lines)


# ─── Tool Call Parsing ──────────────────────────────────────────────────────
def _extract_json(text: str) -> tuple[Optional[str], Optional[dict]]:
    """Extract tool call JSON from text."""
    idx = 0
    while True:
        tool_pos = text.find('"tool"', idx)
        func_pos = text.find('"function"', idx)
        if tool_pos == -1 and func_pos == -1:
            break
        pos = tool_pos if tool_pos != -1 and (func_pos == -1 or tool_pos < func_pos) else func_pos
        idx = pos + 1
        brace = text.rfind("{", 0, pos)
        if brace == -1:
            continue
        i = brace
        depth = 0
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            if depth == 0:
                chunk = text[brace:i+1]
                try:
                    obj = json.loads(chunk)
                    if isinstance(obj, dict):
                        name = obj.get("tool") or obj.get("function")
                        if name:
                            return name, obj.get("arguments", {})
                except Exception:
                    pass
                break
            i += 1

    # Try code block
    m = re.search(r'`{3}(?:json)?\s*(\{.*\}|\[.*\])\s*`{3}', text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                name = obj.get("tool") or obj.get("function")
                if name:
                    return name, obj.get("arguments", {})
        except Exception:
            pass
    return None, None


def _parse_tool_call(text: str) -> tuple[Optional[str], Optional[dict]]:
    """Parse tool call from model response."""
    name, args = _extract_json(text)
    if name and args:
        param_map = {
            "file_path": "filePath", "filepath": "filePath", "path": "filePath",
            "old_str": "oldString", "oldstring": "oldString", "old": "oldString",
            "old_text": "oldString", "oldText": "oldString",
            "new_str": "newString", "newstring": "newString", "new": "newString",
            "new_text": "newString", "newText": "newString",
        }
        norm = {}
        for k, v in args.items():
            norm[param_map.get(k, k)] = v

        if name == "edit" and "edits" in norm and isinstance(norm["edits"], list) and len(norm["edits"]) > 0:
            ed = norm["edits"][0]
            if isinstance(ed, dict):
                if "oldString" not in norm:
                    norm["oldString"] = ed.get("oldText") or ed.get("old") or ed.get("old_str") or ""
                if "newString" not in norm:
                    norm["newString"] = ed.get("newText") or ed.get("new") or ed.get("new_str") or ""
                del norm["edits"]
        return name, norm

    # Natural language edit: "C:\path\file: change 'old' to 'new'"
    m = re.search(r'''([A-Z]:\\.+?):\s*(?:change|replace)\s+(?:the\s+)?(?:text\s+)?['"]?(.+?)['"]?\s+(?:to|with)\s+['"]?(.+?)['"]?''', text, re.IGNORECASE)
    if m:
        return "edit", {"filePath": m.group(1).strip(), "oldString": m.group(2), "newString": m.group(3)}

    return None, None


def _format_chat_result(content_text: str, reasoning: str) -> dict:
    """Format result into OpenAI-compatible response."""
    tool_name, tool_args = _parse_tool_call(content_text)

    if tool_name:
        tc_id = f"call_{uuid.uuid4().hex[:8]}"
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": tc_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(tool_args)},
            }],
        }

    # File path pattern: "C:\file.txt" or "/home/user/file"
    if re.match(r'^(?:[A-Z]:\\|/)[^\n]+', content_text.strip()):
        file_path = content_text.strip().rstrip(".")
        tc_id = f"call_{uuid.uuid4().hex[:8]}"
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": tc_id,
                "type": "function",
                "function": {"name": "read", "arguments": json.dumps({"filePath": file_path})},
            }],
        }

    resp = {"role": "assistant", "content": content_text}
    if reasoning:
        resp["reasoning_content"] = reasoning
    return resp


# ─── Pool ───────────────────────────────────────────────────────────────────
class QwenModePool:
    def __init__(self, size: int = POOL_SIZE, model: str = "Qwen3.8-Max-Preview", headless: bool = HEADLESS):
        self.size = size
        self.model = model
        self.headless = headless
        self.playwright = None
        self.ctx: Optional[BrowserContext] = None
        self._states: list[Optional[PageState]] = [None] * size
        self._lock = asyncio.Lock()
        self._available = asyncio.Semaphore(size)
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._shutdown = False

    async def start(self) -> None:
        self.playwright = await async_playwright().start()
        self.ctx = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=self.headless,
            args=_LAUNCH_ARGS,
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            locale="ru-RU",
            timezone_id="Europe/Helsinki",
        )

        # Load cookies if they exist (skip in guest mode)
        if not GUEST_MODE and os.path.exists("cookies.json"):
            try:
                import json
                with open("cookies.json", "r") as f:
                    raw = json.load(f)
                # Filter to only accepted Playwright fields
                _PLAYWRIGHT_COOKIE_FIELDS = {
                    "name", "value", "domain", "path", "expires",
                    "httpOnly", "secure", "sameSite", "url"
                }
                cookies = []
                for c in raw:
                    cc = {}
                    for k, v in c.items():
                        if k == "expirationDate":
                            cc["expires"] = int(v)
                        elif k in _PLAYWRIGHT_COOKIE_FIELDS:
                            cc[k] = v
                    # Normalize sameSite
                    if "sameSite" in cc:
                        ss = cc["sameSite"]
                        if ss is None:
                            del cc["sameSite"]
                        elif isinstance(ss, str):
                            ssl = ss.lower()
                            if ssl == "lax":
                                cc["sameSite"] = "Lax"
                            elif ssl == "strict":
                                cc["sameSite"] = "Strict"
                            elif ssl in ("none", "no_restriction"):
                                cc["sameSite"] = "None"
                            else:
                                del cc["sameSite"]
                    cookies.append(cc)
                await self.ctx.add_cookies(cookies)
                print(f"[QwenMode] Loaded {len(cookies)} cookies from cookies.json")
            except Exception as e:
                print(f"[QwenMode] Failed to load cookies: {e}")

        await self.ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'en']});
        """)

        for i in range(self.size):
            for attempt in range(5):
                try:
                    page = await _create_page(self.ctx, self.model)
                    if await self._check_health(page):
                        self._states[i] = PageState(page=page)
                        log.info(f"Page {i+1}/{self.size} ready")
                        break
                except Exception as e:
                    log.warning(f"Page {i} create failed (attempt {attempt+1}): {e}")
                    await asyncio.sleep(3)
            else:
                log.error(f"Page {i} UNAVAILABLE after 5 attempts")
                self._states[i] = None

            if i < self.size - 1:
                await asyncio.sleep(CREATE_PAGE_DELAY)

        log.info(f"Pool ready ({sum(1 for s in self._states if s is not None)}/{self.size} pages)")
        self._heartbeat_task = asyncio.create_task(self._heartbeat())

    async def _check_health(self, page: Page) -> bool:
        try:
            await _dismiss_auth_dialog(page)
            await _dismiss_modal(page)
            bt = await page.evaluate("() => (document.body?.innerText || '').substring(0, 300)")
            if _detect_captcha(bt) or not bt.strip():
                return False
            # Check textarea exists
            has_ta = await page.evaluate("() => !!document.querySelector('textarea')")
            return has_ta
        except Exception:
            return False

    async def _recreate_page(self, idx: int) -> None:
        old = self._states[idx]
        if old:
            try:
                await old.page.close()
            except Exception:
                pass
        try:
            page = await _create_page(self.ctx, self.model)
            self._states[idx] = PageState(page=page)
            log.info(f"Page {idx} recreated")
        except Exception as e:
            log.error(f"Page {idx} recreate failed: {e}")
            self._states[idx] = None

    async def _heartbeat(self) -> None:
        while not self._shutdown:
            try:
                await asyncio.wait_for(
                    asyncio.sleep(30),
                    timeout=35
                )
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

            for i in range(self.size):
                if self._shutdown:
                    break
                state = self._states[i]
                if state is None:
                    log.warning(f"Heartbeat: page {i} missing, recreating")
                    async with self._lock:
                        await self._recreate_page(i)
                    continue
                if state.busy or state.resetting:
                    continue
                if not await self._check_health(state.page):
                    log.warning(f"Heartbeat: page {i} unhealthy, recreating")
                    async with self._lock:
                        await self._recreate_page(i)

    async def execute(self, prompt: str) -> dict:
        await self._available.acquire()
        try:
            async with self._lock:
                # Find available page
                idx = -1
                for i, state in enumerate(self._states):
                    if state is not None and not state.busy and not state.resetting:
                        idx = i
                        state.busy = True
                        state.last_used = time.time()
                        break

                if idx == -1:
                    return {"text": "[QwenMode] No available pages", "reasoning": ""}

            state = self._states[idx]

            # Double-check health outside lock
            if not await self._check_health(state.page):
                async with self._lock:
                    await self._recreate_page(idx)
                    state = self._states[idx]
                    if state is None:
                        return {"text": "[QwenMode] Page unavailable after recreate", "reasoning": ""}
                    state.busy = True

            try:
                result = None
                for attempt in range(2):
                    res, new_page = await _wait_for_response(
                        state.page, prompt, self.model, SSE_TIMEOUT
                    )
                    text = res.get("text", "")
                    if text.startswith("[Qwen Error]"):
                        # Limit errors won't go away with retry
                        if "limit" in text.lower() or "usage" in text.lower():
                            log.warning(f"Page {idx} hit rate limit: {text[:100]}")
                            result = (res, new_page)
                            break
                        if attempt == 0:
                            log.info(f"Page {idx} got Qwen error: {text[:200]}, refreshing and retry")
                            try:
                                await state.page.goto(URL, wait_until="domcontentloaded", timeout=30000)
                                await asyncio.sleep(2)
                                await _dismiss_auth_dialog(state.page)
                                await _dismiss_modal(state.page)
                                for _ in range(15):
                                    ta = await state.page.query_selector('textarea')
                                    if ta:
                                        break
                                    await asyncio.sleep(0.5)
                            except Exception as e:
                                log.warning(f"Page {idx} refresh failed on retry: {e}")
                            continue
                    result = (res, new_page)
                    break

                if result is None:
                    result = (res, new_page)  # Use last attempt anyway
                result, new_page = result
                async with self._lock:
                    if new_page != state.page:
                        try:
                            await state.page.close()
                        except Exception:
                            pass
                        self._states[idx] = PageState(page=new_page)
                    else:
                        state.resetting = True
                        state.busy = False

                if new_page == state.page:
                    # Reset page after inactivity, keep for multi-turn bursts
                    RESET_IDLE = 30.0
                    try:
                        cur_url = state.page.url
                        idle = time.time() - state.last_used
                        needs_reset = False

                        if idle > RESET_IDLE:
                            needs_reset = True
                        elif "/c/" not in str(cur_url):
                            ta = await state.page.query_selector('textarea')
                            if ta is None:
                                needs_reset = True

                        if needs_reset:
                            await state.page.goto(URL, wait_until="domcontentloaded", timeout=30000)
                            for _ in range(20):
                                ta = await state.page.query_selector('textarea')
                                if ta:
                                    break
                                await asyncio.sleep(0.3)
                            await asyncio.sleep(1)
                            await _dismiss_auth_dialog(state.page)
                            await _dismiss_modal(state.page)
                    except Exception as e:
                        log.warning(f"Page {idx} reset check failed: {e}")
                    finally:
                        state.resetting = False
                        state.last_used = time.time()
                return result

            except Exception as e:
                log.error(f"Page {idx} error: {e}")
                async with self._lock:
                    state.busy = False
                    await self._recreate_page(idx)
                raise

        finally:
            self._available.release()

    async def chat(self, messages: list[dict], tools: Optional[list] = None) -> dict:
        last_content = _build_prompt(messages)
        if not last_content:
            return {"role": "assistant", "content": "[QwenMode] No message"}

        if tools:
            prompt = _build_tool_prompt(last_content, tools)
        else:
            prompt = last_content

        result = await self.execute(prompt)
        return _format_chat_result(result.get("text", ""), result.get("reasoning", ""))

    async def close(self) -> None:
        self._shutdown = True
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        for state in self._states:
            if state:
                try:
                    await state.page.close()
                except Exception:
                    pass

        if self.ctx:
            await self.ctx.close()
        if self.playwright:
            await self.playwright.stop()


# ─── API Server ─────────────────────────────────────────────────────────────
def run_server(port: int = 5002) -> None:
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, HTTPException, Header
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    import uvicorn

    pool: Optional[QwenModePool] = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal pool
        pool = QwenModePool(size=POOL_SIZE, model="Qwen3.8-Max-Preview")
        await pool.start()
        logging.basicConfig(level=logging.INFO)
        yield
        await pool.close()

    app = FastAPI(title="QwenMode API", lifespan=lifespan)

    # Restrictive CORS — change as needed
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.getenv("QWENMODE_CORS_ORIGINS", "*").split(","),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    class ChatRequest(BaseModel):
        model: str = "Qwen3.8-Max-Preview"
        messages: list
        stream: bool = False
        tools: Optional[list] = None

    def _verify_auth(authorization: Optional[str]) -> None:
        if not API_KEY:
            return
        if not authorization:
            raise HTTPException(status_code=401, detail="Missing Authorization header")
        # Support "Bearer <key>" or just "<key>"
        token = authorization.replace("Bearer ", "").strip()
        if token != API_KEY:
            raise HTTPException(status_code=403, detail="Invalid API key")

    @app.post("/v1/chat/completions")
    async def chat_completions(req: ChatRequest, authorization: Optional[str] = Header(None)):
        from fastapi.responses import StreamingResponse

        _verify_auth(authorization)

        if pool is None:
            raise HTTPException(status_code=503, detail="Pool not ready")

        result = await pool.chat(req.messages, tools=req.tools)

        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        content = result.get("content", "")
        reasoning = result.get("reasoning_content", "")
        has_tc = bool(result.get("tool_calls"))
        finish = "tool_calls" if has_tc else "stop"

        if not req.stream:
            msg = {"role": "assistant", "content": content}
            if has_tc:
                msg["tool_calls"] = result["tool_calls"]
            if reasoning:
                msg["reasoning_content"] = reasoning
            return {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": req.model,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}]
            }

        async def gen():
            if reasoning:
                yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": req.model, "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": reasoning}, "finish_reason": None}]})}\n\n'

            if content:
                # Stream content word by word for better UX
                words = content.split(" ")
                for i in range(0, len(words), 5):
                    chunk = " ".join(words[i:i+5])
                    yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": req.model, "choices": [{"index": 0, "delta": {"content": chunk + " "}, "finish_reason": None}]})}\n\n'
                    await asyncio.sleep(0.01)

            if has_tc:
                tc = result["tool_calls"][0]
                yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": req.model, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": tc["id"], "type": "function", "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]}, "finish_reason": None}]})}\n\n'

            yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": req.model, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})}\n\n'
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/health")
    async def health():
        if pool is None:
            return {"status": "not_ready"}
        healthy = sum(1 for s in pool._states if s is not None)
        return {"status": "ok", "pages": {"total": pool.size, "healthy": healthy}}

    @app.get("/v1/models")
    async def list_models():
        return {
            "object": "list",
            "data": [{
                "id": "Qwen3.8-Max-Preview",
                "object": "model",
                "created": 1700000000,
                "owned_by": "qwenmode",
            }]
        }

    @app.get("/debug")
    async def debug():
        if pool is None:
            raise HTTPException(status_code=503, detail="Pool not ready")
        info = {"pages": []}
        for i, state in enumerate(pool._states):
            if state is None:
                info["pages"].append({"index": i, "state": "none"})
                continue
            try:
                dom_info = await state.page.evaluate("""() => {
                    const ta = document.querySelector('textarea');
                    return {
                        hasTextarea: !!ta,
                        innerText: document.body?.innerText || '',
                        url: location.href,
                    };
                }""")
                info["pages"].append({
                    "index": i,
                    "state": "ok",
                    "busy": state.busy,
                    "last_used": state.last_used,
                    "dom": dom_info,
                })
            except Exception as e:
                info["pages"].append({"index": i, "state": f"error: {e}"})
        return info

    log.info(f"QwenMode API Server on http://0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    if "--login" in sys.argv:
        print("Opening browser for login. Please log in to chat.qwen.ai, then close the browser window.")
        async def do_login():
            p = await async_playwright().start()
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                headless=False,
                args=_LAUNCH_ARGS,
                viewport={"width": 1280, "height": 720}
            )
            page = await ctx.new_page()
            await page.goto(URL)
            print("Waiting for you to log in... Close the browser window when done.")
            try:
                await page.wait_for_event("close", timeout=0)
            except Exception:
                pass
            await ctx.close()
            await p.stop()
            print("Login complete. You can now run the server.")
        asyncio.run(do_login())
        sys.exit(0)
    elif "--server" in sys.argv:
        idx = sys.argv.index("--server")
        port = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 5002
        run_server(port)
    else:
        print("Usage: python qwenmode.py --server [port]   (start server)")

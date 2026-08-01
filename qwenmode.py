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


async def _select_model(page: Page, model: str) -> bool:
    """Select the given model in Qwen Studio's model dropdown.

    Live-verified selectors (Aug 2026):
      - trigger: SPAN.ant-dropdown-trigger / .index-module__model-selector___rdCim
      - item:    DIV.index-module__model-item-name___X8Hec (text = model name)
      - click:   .index-module__model-item___MkLlj (parent of the name)

    IMPORTANT: must use real Playwright (trusted) clicks — React/antd
    dropdowns ignore synthetic element.click() from evaluate().
    Returns True if the trigger text matches the requested model.
    """
    if not model:
        return True
    # The model selector DIV is the reliable trigger. .ant-dropdown-trigger
    # matches 4 elements (help "?" is first!) — never use it as .first.
    trigger_sel = '.index-module__model-selector___rdCim'
    for attempt in range(3):
        try:
            # 1. Close any guidance/onboarding popup that blocks the dropdown
            try:
                g = page.locator('.guidance-pc-close-btn').first
                if await g.count() > 0 and await g.is_visible():
                    await g.click(timeout=2000, force=True)
                    await asyncio.sleep(0.8)
            except Exception:
                pass

            # 2. Read current selection from trigger
            trigger = page.locator(trigger_sel).first
            if await trigger.count() > 0:
                cur = (await trigger.inner_text(timeout=2000)).strip()
                if cur == model:
                    return True

            # 3. Open dropdown with a real click
            try:
                await trigger.click(timeout=3000, force=True)
            except Exception:
                # Fallback: header element containing the current model text
                try:
                    await page.locator(f'text={model}').first.click(timeout=3000, force=True)
                except Exception:
                    pass

            # 4. Wait for dropdown items to render, then click the target item
            await asyncio.sleep(0.8)
            item = page.locator(
                f'.index-module__model-item-name___X8Hec:has-text("{model}")'
            ).first
            if await item.count() > 0:
                try:
                    await item.click(timeout=3000, force=True)
                except Exception:
                    # Click the clickable wrapper if the name itself is inert
                    wrapper = page.locator(
                        f'.index-module__model-item___MkLlj:has-text("{model}")'
                    ).first
                    if await wrapper.count() > 0:
                        await wrapper.click(timeout=3000, force=True)

            # 5. Verify selection took effect
            await asyncio.sleep(0.8)
            trigger = page.locator(trigger_sel).first
            if await trigger.count() > 0:
                cur = (await trigger.inner_text(timeout=2000)).strip()
                if cur == model:
                    return True
        except Exception as e:
            log.debug(f"_select_model attempt {attempt} failed: {e}")
            await asyncio.sleep(0.5)
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
        # Select the requested model (no-op if already default)
        selected = await _select_model(page, model)
        if not selected:
            log.warning(f"Model selection failed for '{model}' — continuing with default")
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

        # Error events: {"error": {"code": "quota_limit", "details": "..."}}
        if isinstance(ev, dict) and ev.get("error"):
            err = ev["error"]
            code = err.get("code", "unknown") if isinstance(err, dict) else "unknown"
            details = err.get("details", "") if isinstance(err, dict) else str(err)
            msg = f"[Qwen Error {code}]"
            if details:
                msg += f" {details}"
            return {"text": msg, "reasoning": ""}

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
        elif phase == "tool_call" or phase == "tool":
            # Tool call phases — capture as content for parsing
            if content:
                answer_parts.append(str(content))
        elif content:
            # Any other phase with content — accept it
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
    capture_ts = [0.0]  # track latest capture timestamp

    async def capture(resp) -> None:
        if _SSE_URL_PATTERN in resp.url and bodies["raw"] is None:
            try:
                raw = await asyncio.wait_for(resp.text(), timeout=timeout)
                now = time.time()
                # Only accept if this is the latest response (stale check)
                if now > capture_ts[0]:
                    capture_ts[0] = now
                    bodies["raw"] = raw
                    response_event.set()
                    log.info(f"SSE captured: {len(raw)} bytes, head: {raw[:200]!r}")
            except Exception:
                pass

    page.on("response", capture)

    try:
        await _dismiss_auth_dialog(page)
        await _dismiss_modal(page)
        await _type_text(page, prompt)
        await asyncio.sleep(0.5)

        # Verify textarea has the text before sending
        try:
            ta_val = await page.evaluate("() => document.querySelector('textarea')?.value || ''")
            if len(ta_val.strip()) < len(prompt.strip()):
                await _type_text(page, prompt)
                await asyncio.sleep(0.3)
        except Exception:
            pass

        # Send loop — press Enter, dismiss login modal if it appears, retry
        for send_attempt in range(3):
            response_event.clear()
            await _click_send(page)

            # Wait for SSE — Qwen3.8 has thinking phase that takes 5-10s
            try:
                await asyncio.wait_for(response_event.wait(), timeout=10.0)
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
            if not parsed.get("text") and not parsed.get("reasoning"):
                log.warning(f"SSE parsed EMPTY: {bodies['raw'][:800]!r}")
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
            # Preserve the working directory even when truncating the rest —
            # Qwen otherwise invents paths like /root or /home/user because
            # it doesn't know the client's cwd.
            wd_match = re.search(r"(?:Working directory|working directory|cwd)[:\s]+(\S+)", text)
            wd_note = ""
            if wd_match:
                wd_note = f"\nWORKING DIRECTORY: {wd_match.group(1)} (create files here with relative paths)"
            if len(text) > 2000:
                text = text[:2000] + "\n...[system truncated]"
            parts.append(f"[System]\n{text}{wd_note}")

        elif role == "user":
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"]
                text = "\n".join(texts)
            else:
                text = str(content)
            if len(text) > 4000:
                text = text[:4000] + "\n...[user message truncated]"
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
                if len(text) > 2000:
                    text = text[:2000] + "\n...[truncated]"
                parts.append(text)
            else:
                text = str(content)
                if len(text) > 2000:
                    text = text[:2000] + "\n...[truncated]"
                parts.append(text)

        elif role == "tool":
            text = str(content)
            if len(text) > 2000:
                text = text[:2000] + "\n...[tool result truncated]"
            # Include tool name from the preceding assistant message
            tool_name = m.get("name", "") or m.get("tool_name", "")
            if not tool_name and parts:
                # Scan back for last tool call from assistant
                for p in reversed(parts):
                    if '{"tool":' in p:
                        import re as _re
                        mm = _re.search(r'\{"tool": "([^"]+)"', p)
                        if mm:
                            tool_name = mm.group(1)
                        break
            prefix = f"[Tool result for {tool_name}]" if tool_name else "[Tool result]"
            parts.append(f"{prefix}\n{text}")

    return "\n\n".join(parts).strip()


def _build_tool_prompt(last_content: str, tools: list[dict]) -> str:
    """Append full tool descriptions (including JSON schema) to prompt."""
    desc_lines = ["# Available tools"]
    for t in tools:
        fn = t.get("function", {})
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})
        props = params.get("properties", {})
        required = params.get("required", [])
        # Tool header
        desc_lines.append(f"\n## {name}")
        if desc:
            short = desc.split("\n")[0] if desc else ""
            if len(short) > 300:
                short = short[:300] + "..."
            desc_lines.append(f"  {short}")
        # Arguments schema
        if props:
            desc_lines.append("  Arguments:")
            for pname, pinfo in props.items():
                ptype = pinfo.get("type", "string")
                pdesc = pinfo.get("description", "")
                preq = " (required)" if pname in required else ""
                pshort = pdesc.split("\n")[0][:120] if pdesc else ""
                desc_lines.append(f"    {pname}: {ptype}{preq}")
                if pshort:
                    desc_lines.append(f"      {pshort}")

    desc_lines.extend([
        "",
        "# Output Format",
        "You may respond with ONE tool call OR a normal text answer.",
        "",
        "IMPORTANT: Do NOT use any built-in Qwen tools, artifacts, code runner,",
        "file browser, or Qwen Studio tool buttons. You have NO access to them.",
        "Only the tools listed below exist. To call one, output the JSON format.",
        "",
        "For a tool call, output EXACTLY:",
        '{"tool": "tool_name", "arguments": {"arg1": "value1", "arg2": "value2"}}',
        "",
        "Use EXACTLY the argument names listed in the Arguments section above.",
        "Do NOT rename or substitute them (e.g. if the schema says \"path\", use \"path\", not \"filePath\").",
        "",
        "If you need to call MULTIPLE tools at once, output them on separate lines:",
        '{"tool": "tool1", "arguments": {...}}',
        '{"tool": "tool2", "arguments": {...}}',
        "",
        "NO additional text, NO markdown, NO explanation around tool calls.",
        "",
        last_content,
    ])
    return "\n".join(desc_lines)


# ─── Tool Call Parsing ──────────────────────────────────────────────────────
def _extract_all_tool_calls(text: str) -> list[tuple[str, dict]]:
    """Extract ALL tool call JSON objects from text."""
    results: list[tuple[str, dict]] = []

    # 1. Find all JSON blocks with "tool" or "function" keys
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
                        name = obj.get("tool") or obj.get("function", {}).get("name") or obj.get("function")
                        if name:
                            args = obj.get("arguments", {})
                            results.append((str(name), args))
                except Exception:
                    pass
                break
            i += 1

    # 2. If nothing found, try code blocks
    if not results:
        for m in re.finditer(r'`{3}(?:json)?\s*(\{.*?\}|\[.*?\])\s*`{3}', text, re.DOTALL):
            try:
                obj = json.loads(m.group(1))
                if isinstance(obj, dict):
                    name = obj.get("tool") or obj.get("function", {}).get("name") or obj.get("function")
                    if name:
                        args = obj.get("arguments", {})
                        results.append((str(name), args))
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, dict):
                            name = item.get("tool") or item.get("function", {}).get("name") or item.get("function")
                            if name:
                                args = item.get("arguments", {})
                                results.append((str(name), args))
            except Exception:
                pass

    return results


def _normalize_args(name: str, args: dict, schema_params: Optional[set] = None) -> dict:
    """Normalize argument keys to match the CLIENT's tool schema.

    Qwen tends to rename arguments (e.g. 'path' -> 'filePath') based on
    whatever style it saw in previous sessions. If we know the client's
    schema (schema_params), map synonyms onto the exact names the client
    expects. Without a schema, fall back to opencode conventions.
    """
    # Synonym groups — canonical first
    groups = [
        ["filePath", "path", "file_path", "filepath", "file", "source",
         "target", "destination", "dest"],
        ["oldString", "old_str", "oldstring", "old_text", "oldText", "old", "before"],
        ["newString", "new_str", "newstring", "new_text", "newText", "new", "after"],
        ["content", "code", "text", "data"],
        ["command", "cmd", "shell"],
    ]
    schema_params = schema_params or set()

    norm = {}
    for k, v in args.items():
        if schema_params and k not in schema_params:
            # Find a synonym that exists in the client's schema
            mapped = False
            for group in groups:
                if k in group:
                    for cand in group:
                        if cand in schema_params:
                            norm[cand] = v
                            mapped = True
                            break
                    break
            if not mapped:
                norm[k] = v
        else:
            norm[k] = v

    # Without a schema: legacy opencode mapping
    if not schema_params:
        legacy = {
            "file_path": "filePath", "filepath": "filePath", "path": "filePath",
            "old_str": "oldString", "oldstring": "oldString", "old": "oldString",
            "old_text": "oldString", "oldText": "oldString",
            "new_str": "newString", "newstring": "newString", "new": "newString",
            "new_text": "newString", "newText": "newString",
            "file": "filePath", "source": "filePath", "target": "filePath",
            "destination": "filePath", "dest": "filePath",
            "before": "oldString", "after": "newString",
            "code": "content", "text": "content", "data": "content",
        }
        norm = {legacy.get(k, k): v for k, v in args.items()}

    if name == "edit" and "edits" in norm and isinstance(norm["edits"], list) and len(norm["edits"]) > 0:
        ed = norm["edits"][0]
        if isinstance(ed, dict):
            if "oldString" not in norm and "old_str" not in norm and "old" not in norm:
                norm["oldString"] = ed.get("oldText") or ed.get("old") or ed.get("old_str") or ""
            if "newString" not in norm and "new_str" not in norm and "new" not in norm:
                norm["newString"] = ed.get("newText") or ed.get("new") or ed.get("new_str") or ""
            del norm["edits"]

    # pi-style edit schema: {path, edits: [{oldText, newText}]}
    # Qwen usually sends {path, oldString, newString} — convert.
    if name == "edit" and schema_params and "edits" in schema_params:
        old_k = next((k for k in ("oldString", "old_str", "oldText", "old_text", "old", "before") if k in norm), None)
        new_k = next((k for k in ("newString", "new_str", "newText", "new_text", "new", "after") if k in norm), None)
        if old_k is not None and new_k is not None and "edits" not in norm:
            norm["edits"] = [{"oldText": norm.pop(old_k), "newText": norm.pop(new_k)}]
    return norm


def _schema_params_for(tools: Optional[list]) -> dict:
    """Build {tool_name: set(param_names)} from the client's tool schemas."""
    if not tools:
        return {}
    smap = {}
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function", {})
        name = fn.get("name", "")
        if not name:
            continue
        props = fn.get("parameters", {}).get("properties", {})
        smap[name] = set(props.keys()) if isinstance(props, dict) else set()
    return smap


def _remove_explanation_prefix(text: str) -> str:
    """Strip thinking/explanation text before the first JSON tool call block."""
    # If text starts with analysis, then has a JSON block later
    lines = text.strip().split("\n")
    clean_lines = []
    found_json = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("{") and ('"tool"' in stripped or '"function"' in stripped):
            found_json = True
            clean_lines.append(stripped)
        elif found_json:
            clean_lines.append(stripped)
        elif not found_json and (stripped.startswith("{") or stripped == ""):
            pass  # skip non-JSON before first tool call
    if clean_lines:
        return "\n".join(clean_lines)
    return text


def _format_chat_result(content_text: str, reasoning: str, tools: Optional[list] = None) -> dict:
    """Format result into OpenAI-compatible response, supporting multiple tool calls."""
    tool_calls_raw = _extract_all_tool_calls(content_text)
    schema_map = _schema_params_for(tools)

    if tool_calls_raw:
        tc_list = []
        for name, raw_args in tool_calls_raw:
            schema_params = schema_map.get(name)
            norm_args = _normalize_args(name, raw_args, schema_params)
            tc_id = f"call_{uuid.uuid4().hex[:8]}"
            tc_list.append({
                "id": tc_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(norm_args)},
            })
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": tc_list,
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
# Cookie pool constants
COOKIE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies")


def _load_cookies_from_file(filepath: str) -> list[dict]:
    """Load and normalize cookies from a JSON file."""
    import json
    with open(filepath, "r") as f:
        raw = json.load(f)

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
    return cookies


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
        # Cookie rotation
        self._cookie_pool: list[list[dict]] = []  # list of cookie sets
        self._cookie_idx: int = 0

    def _scan_cookies(self) -> None:
        """Scan cookies/ directory for JSON files."""
        self._cookie_pool = []
        if not os.path.isdir(COOKIE_DIR):
            # Also check legacy cookies.json in script dir
            legacy = os.path.join(os.path.dirname(__file__), "cookies.json")
            if os.path.exists(legacy):
                try:
                    self._cookie_pool.append(_load_cookies_from_file(legacy))
                    log.info(f"Loaded legacy cookies.json as cookie set #0")
                except Exception as e:
                    log.warning(f"Failed to load legacy cookies.json: {e}")
            return

        files = sorted([f for f in os.listdir(COOKIE_DIR) if f.endswith(".json")])
        for fname in files:
            fpath = os.path.join(COOKIE_DIR, fname)
            try:
                cookies = _load_cookies_from_file(fpath)
                if cookies:
                    self._cookie_pool.append(cookies)
                    log.info(f"Loaded {len(cookies)} cookies from {fname}")
            except Exception as e:
                log.warning(f"Failed to load {fname}: {e}")

    async def _apply_cookies(self) -> None:
        """Apply current cookie set to browser context. Rotates on limit."""
        if not self._cookie_pool:
            return
        if self._cookie_idx >= len(self._cookie_pool):
            self._cookie_idx = 0  # Wrap around
        cookies = self._cookie_pool[self._cookie_idx]
        try:
            await self.ctx.clear_cookies()
            await self.ctx.add_cookies(cookies)
            log.info(f"Applied cookie set #{self._cookie_idx} ({len(cookies)} cookies)")
        except Exception as e:
            log.warning(f"Failed to apply cookies #{self._cookie_idx}: {e}")

    def _rotate_cookies(self) -> None:
        """Switch to next cookie set (called on limit hit)."""
        total = len(self._cookie_pool)
        if total <= 1:
            log.warning("Only 1 cookie set, cannot rotate")
            return
        old = self._cookie_idx
        self._cookie_idx = (self._cookie_idx + 1) % total
        log.info(f"Rotated cookies: #{old} -> #{self._cookie_idx} ({total} sets)")

    async def start(self) -> None:
        # NOTE: We do NOT wipe the profile in guest mode. Qwen blocks FRESH
        # guest sessions ("log in or sign up" modal on every send) but allows
        # STABLE guest sessions that carry cookies from previous visits —
        # exactly like a normal browser vs incognito. Persistent profile =
        # the guest session accumulates trust and sending works.
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

        # Load cookie pool (skip in guest mode)
        if not GUEST_MODE:
            self._scan_cookies()
            await self._apply_cookies()

        await self.ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'en']});
            // CRITICAL FIX (Aug 2026): Qwen Studio's incognito detector
            // (detect-incognito lib) flags headless Chromium as private mode:
            // in-memory IndexedDB makes strict/relaxed durability writes take
            // the same time (ratio < 1.3) -> isPrivate=true ->
            // isDisableGuestAccess=true -> "Log in or sign up" modal on EVERY
            // send. Force transaction durability to 'relaxed' so the
            // detector's strict check fails -> isPrivate=false -> guest OK.
            (() => {
                try {
                    const origTx = IDBDatabase.prototype.transaction;
                    IDBDatabase.prototype.transaction = function(...args) {
                        const tx = origTx.apply(this, args);
                        try {
                            Object.defineProperty(tx, 'durability', { value: 'relaxed', configurable: true });
                        } catch (e) {}
                        return tx;
                    };
                } catch (e) {}
            })();
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

    async def _recreate_page_with_cookies(self, idx: int) -> None:
        """Recreate a page, clearing and re-applying current cookie set."""
        old = self._states[idx]
        if old:
            try:
                await old.page.close()
            except Exception:
                pass
        try:
            if not GUEST_MODE and self._cookie_pool:
                await self.ctx.clear_cookies()
                await self._apply_cookies()
            page = await _create_page(self.ctx, self.model)
            self._states[idx] = PageState(page=page)
            log.info(f"Page {idx} recreated with cookie set #{self._cookie_idx}")
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

    async def execute(self, prompt: str, fresh: bool = False) -> dict:
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

            # OpenAI API semantics: EVERY request lands in a CLEAN chat.
            # Navigate to home so Qwen opens a brand-new conversation. If we're
            # already on home (first request after start), skip the nav.
            if fresh:
                try:
                    cur = state.page.url
                    if "/c/" in str(cur):
                        log.info(f"Page {idx}: fresh session — opening new chat")
                        await state.page.goto(URL, wait_until="domcontentloaded", timeout=30000)
                        # Wait only for textarea, nothing else (fast path)
                        for _ in range(20):
                            ta = await state.page.query_selector('textarea')
                            if ta:
                                break
                            await asyncio.sleep(0.15)
                        await _dismiss_auth_dialog(state.page)
                        await _dismiss_modal(state.page)
                        # Goto resets the model dropdown back to the default
                        # (Qwen3.7-Plus) — re-select the configured model.
                        await _select_model(state.page, self.model)
                except Exception as e:
                    log.warning(f"Page {idx} fresh-chat nav failed: {e}")

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
                for attempt in range(4):
                    res, new_page = await _wait_for_response(
                        state.page, prompt, self.model, SSE_TIMEOUT
                    )
                    text = res.get("text", "")
                    log.info(f"EXEC attempt={attempt} text_head={text[:120]!r}")
                    if text.startswith("[Qwen Error]"):
                        # "high demand" / quota_limit = temporary per-minute rate
                        # limit. Back off briefly and retry in the SAME session —
                        # do NOT burn cookies (guest session trust is precious).
                        if "high demand" in text.lower() or "quota" in text.lower() or "limit" in text.lower() or "usage" in text.lower():
                            log.warning(f"Page {idx} hit rate limit: {text[:120]}")
                            if "high demand" in text.lower() or "quota" in text.lower():
                                # Temporary throttle — wait and retry same page
                                backoff = 15 + attempt * 20
                                log.info(f"Guest mode: throttled, backing off {backoff}s and retrying same page")
                                await asyncio.sleep(backoff)
                                # Fresh chat page keeps the session alive
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
                                    log.warning(f"Page {idx} refresh failed on throttle retry: {e}")
                                continue  # Retry same page after backoff
                            if not GUEST_MODE:
                                self._rotate_cookies()
                            else:
                                # Guest mode: wipe cookies so the recreated page
                                # gets a fresh guest session (old one is spent)
                                try:
                                    await self.ctx.clear_cookies()
                                    log.info("Guest mode: cleared cookies on limit hit")
                                except Exception as e:
                                    log.warning(f"Guest mode: clear_cookies failed: {e}")
                            # Recreate page with new cookies
                            async with self._lock:
                                await self._recreate_page_with_cookies(idx)
                                new_state = self._states[idx]
                                if new_state is None:
                                    return {"text": "[QwenMode] Page unavailable after rotate", "reasoning": ""}
                                new_state.busy = True
                                state = new_state
                            continue  # Retry with new page
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
                    RESET_IDLE = 120.0
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
        roles = [m.get('role') for m in messages]
        # OpenAI API semantics: EVERY request is self-contained. The client
        # (opencode/pi/Claude Code) sends the full history — system, user,
        # assistant tool_calls AND tool results — inside messages on every
        # call. So every request must land in a CLEAN chat: if we reuse the
        # previous chat, the full prompt we type into the textarea is stacked
        # on top of Qwen's own chat history -> duplicated context -> the model
        # goes crazy and cross-contaminates sessions.
        log.info(f"CHAT: messages={len(messages)}, prompt_chars={len(last_content)}, tools={len(tools) if tools else 0}, roles={roles}")
        if not last_content:
            return {"role": "assistant", "content": "[QwenMode] No message"}

        if tools:
            prompt = _build_tool_prompt(last_content, tools)
        else:
            prompt = last_content

        result = await self.execute(prompt, fresh=True)
        return _format_chat_result(result.get("text", ""), result.get("reasoning", ""), tools)

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

        # Report the model actually used (what's selected in the browser),
        # not whatever the client happened to request.
        real_model = pool.model

        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        content = result.get("content", "")
        reasoning = result.get("reasoning_content", "")
        has_tc = bool(result.get("tool_calls"))
        finish = "tool_calls" if has_tc else ("stop" if content else "stop")

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
                "model": real_model,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}]
            }

        async def gen():
            has_tc = bool(result.get("tool_calls"))
            content = result.get("content", "")
            reasoning = result.get("reasoning_content", "")
            tc = result.get("tool_calls")

            # If tool calls: stream the entire tool call as first chunk, then finish
            if has_tc and tc:
                tc_deltas = []
                for i, t in enumerate(tc):
                    tc_deltas.append({
                        "index": i,
                        "id": t["id"],
                        "type": "function",
                        "function": {
                            "name": t["function"]["name"],
                            "arguments": t["function"]["arguments"],
                        },
                    })
                if reasoning:
                    yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": real_model, "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": reasoning}, "finish_reason": None}]})}\n\n'
                yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": real_model, "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": tc_deltas}, "finish_reason": None}]})}\n\n'
                yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": real_model, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})}\n\n'
                yield "data: [DONE]\n\n"
                return

            if reasoning:
                yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": real_model, "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": reasoning}, "finish_reason": None}]})}\n\n'

            if content:
                # Stream content word by word for better UX
                words = content.split(" ")
                for i in range(0, len(words), 5):
                    chunk = " ".join(words[i:i+5])
                    yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": real_model, "choices": [{"index": 0, "delta": {"content": chunk + " "}, "finish_reason": None}]})}\n\n'
                    await asyncio.sleep(0.01)

            yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": real_model, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})}\n\n'
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

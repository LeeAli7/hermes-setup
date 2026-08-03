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
import shutil
import uuid
from dataclasses import dataclass, field
from typing import Optional, Any
from playwright.async_api import async_playwright, Page, BrowserContext

# ─── Configuration ──────────────────────────────────────────────────────────
URL = os.getenv("QWENMODE_URL", "https://chat.qwen.ai")
POOL_SIZE = int(os.getenv("QWENMODE_POOL_SIZE", "2"))
SSE_TIMEOUT = int(os.getenv("QWENMODE_SSE_TIMEOUT", "90"))
# Activity-based waiting: the fixed SSE_TIMEOUT only applies when the model
# is SILENT. As long as new tokens keep arriving (SSE observed / DOM grows),
# the wait extends up to SSE_MAX_WAIT. SSE_ACTIVITY_IDLE = silence budget.
SSE_ACTIVITY_IDLE = float(os.getenv("QWENMODE_SSE_ACTIVITY_IDLE", "40"))
# Rolling "max silence" window: if the model emits NO new token (reasoning OR
# content, via the fetch hook / SSE POST) for STALL_CAP seconds, the attempt
# aborts and is retried. Every new token resets the deadline, so long agentic
# generations never die as long as tokens keep flowing — only a true stall
# (e.g. reasoning delivered, then the answer never materialises) aborts.
STALL_CAP = float(os.getenv("QWENMODE_STALL_CAP", "120"))
# If the SITE never opens the SSE stream at all (no POST to the chat endpoint
# observed) within this many seconds, the send is silently dead (guest is
# throttled/blocked). Abort the attempt as EMPTY rather than burn STALL_CAP.
# A live generation always emits the SSE .created event within a second or two.
EMPTY_NO_SSE = float(os.getenv("QWENMODE_EMPTY_NO_SSE", "30"))
# Absolute cap kept as a safety net only; the streaming path is governed by
# the rolling STALL_CAP above.
SSE_MAX_WAIT = float(os.getenv("QWENMODE_SSE_MAX_WAIT", "1200"))
# On a run of EMPTY responses, spin up a fresh guest profile (worse case) and
# retry the whole request, rather than silently returning an empty answer.
RECREATE_LIMIT = int(os.getenv("QWENMODE_RECREATE_LIMIT", "2"))
# Rotate the guest profile after this many requests on the SAME page (done in
# the background while idle, so it does not slow the request path). 0 disables.
GUEST_ROTATE_EVERY = int(os.getenv("QWENMODE_GUEST_ROTATE_EVERY", "3"))
MAX_ATTEMPTS = int(os.getenv("QWENMODE_MAX_ATTEMPTS", "2"))
# Total wall-clock budget (seconds) a single request is allowed to keep
# rotating to BRAND-NEW guest profiles on EMPTY / quota / overload responses,
# so the client gets a REAL answer instead of an early error. Kept under the
# client's own timeout. 0 disables the retry budget (uses MAX_ATTEMPTS only).
EMPTY_RETRY_BUDGET = float(os.getenv("QWENMODE_EMPTY_RETRY_BUDGET", "180"))
# Pause between guest-profile rotations (fresh sessions need a moment to be
# un-throttled; avoid hammering the site with rapid re-launches).
GUEST_COOLDOWN = float(os.getenv("QWENMODE_GUEST_COOLDOWN", "5"))
# Wait up to this long for an idle page before surfacing "No available pages"
# (smooths the transient window when a guest-profile rotation briefly has all
# pages busy).
NO_PAGE_TIMEOUT = float(os.getenv("QWENMODE_NO_PAGE_TIMEOUT", "20"))
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
# Enable to dump the live SSE request contract (headers/body) once per call.
_DIAG_CAPTURE = os.getenv("QWENMODE_DIAG_SSE", "").lower() in ("1", "true", "yes")

# ─── Page State ─────────────────────────────────────────────────────────────
@dataclass
class PageState:
    page: Page
    busy: bool = False
    resetting: bool = False
    last_used: float = field(default_factory=time.time)
    health_failures: int = 0
    empty_streak: int = 0


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
            // 0. Guest mode: click "Stay logged out" / "Оставаться вышедшим" FIRST.
            // This is THE button that lets the anonymous guest actually chat.
            // Qwen shows a login modal on EVERY send for fresh guests; the
            // only way past it is this button (X/close/backdrop clicks don't
            // work — the modal just reappears).
            {
                const cands = [];
                const allEls = document.querySelectorAll('*');
                for (const el of allEls) {
                    const t = (el.textContent || '').trim();
                    if (t === 'Оставаться вышедшим' || t === 'Stay logged out') {
                        cands.push(el);
                    }
                }
                if (cands.length) {
                    // Click the SMALLEST matching element (deepest node)
                    cands.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
                    const el = cands[0];
                    el.click();
                    return true;
                }
            }
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


def _detect_tool_error(text: str, declared_names: Optional[set]) -> Optional[str]:
    """Return the offending tool name if Qwen tried a tool that isn't usable.

    Two cases:
      - Qwen Studio echoed a literal error like "Tool read does not exists.".
      - A tool-call JSON referenced a tool the client did NOT declare in `tools`.
    Returns the tool name to blame, or None if the response is fine.
    """
    if not text:
        return None
    # Studio's literal error, e.g. "Tool read does not exists.Tool read does not exists."
    m = re.search(r"\btool\s+([A-Za-z_][\w.-]*)\s+does\s+not\s+exist", text, re.IGNORECASE)
    if m:
        return m.group(1)
    # JSON tool call naming an undeclared tool
    if declared_names:
        for name, _ in _extract_all_tool_calls(text):
            if name and name not in declared_names:
                return name
    return None


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
    last_reasoning = None  # dedup: identical consecutive thinking_summary lines
    last_answer = None     # dedup: identical consecutive content tokens

    def _push_reasoning(txt: str) -> None:
        nonlocal last_reasoning
        if not txt:
            return
        if txt == last_reasoning:
            return
        last_reasoning = txt
        reasoning_parts.append(txt)

    def _push_answer(txt: str) -> None:
        nonlocal last_answer
        if not txt:
            return
        if txt == last_answer:
            return
        last_answer = txt
        answer_parts.append(txt)

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
            detail_lower = (details or "").lower()
            # Transient terminal markers from Qwen when the stream is cut/ended
            # (e.g. 'The request is ended!'). NOT a quota/limit error — treat as
            # an aborted stream so the caller retries via the empty-response path
            # instead of wiping the whole guest profile.
            if ("request is ended" in detail_lower or "request ended" in detail_lower
                    or "ended" in detail_lower and "!" in str(err)):
                return {"text": "", "reasoning": ""}
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
                _push_reasoning(str(thought_content[0]))
        elif phase == "answer" and content:
            _push_answer(str(content))
        elif phase == "tool_call" or phase == "tool":
            # Tool call phases — capture as content for parsing
            _push_answer(str(content)) if content else None
        elif content:
            # Any other phase with content — accept it
            _push_answer(str(content))
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


async def _wait_for_response(page: Page, prompt: str, model: str, timeout: int,
                             chunk_q: Optional[asyncio.Queue] = None) -> tuple[dict, Page]:
    """Send prompt and wait for response via SSE or DOM fallback.

    If chunk_q is given, incremental text chunks are pushed into it as the
    model generates (live streaming via the window.__qwenStream fetch hook).
    """
    bodies: dict[str, Optional[str]] = {"raw": None}
    response_event = asyncio.Event()
    capture_ts = [0.0]  # track latest capture timestamp
    progress_ts = [time.time()]  # last observed activity (SSE start/end, DOM growth)
    sse_started = [False]  # a POST to the SSE endpoint was observed
    diag_dumped = [False]  # dump the SSE request contract exactly once per call

    async def capture(resp) -> None:
        if _SSE_URL_PATTERN in resp.url and bodies["raw"] is None:
            try:
                # Generous cap: a long reasoning+answer SSE can run minutes.
                raw = await asyncio.wait_for(resp.text(), timeout=max(300, timeout * 3))
                now = time.time()
                # Only accept if this is the latest response (stale check)
                if now > capture_ts[0]:
                    capture_ts[0] = now
                    bodies["raw"] = raw
                    progress_ts[0] = now  # activity: SSE fully arrived
                    response_event.set()
                    log.info(f"SSE captured: {len(raw)} bytes, head: {raw[:200]!r}")
            except Exception:
                pass

    def on_request(req) -> None:
        # SSE stream STARTED = the model accepted the message. This resets
        # the silence timer even before tokens hit the DOM (long thinking).
        if _SSE_URL_PATTERN in req.url and req.method == "POST":
            sse_started[0] = True
            progress_ts[0] = time.time()
            # DIAG: dump the live SSE request contract (auth/headers/body) once.
            try:
                if not diag_dumped[0] and _DIAG_CAPTURE:
                    diag_dumped[0] = True
                    hdrs = dict(req.headers)
                    hdrs_safe = {k: (v if not any(s in k.lower() for s in ("authorization", "token", "cookie")) else v[:40] + "...")
                                 for k, v in hdrs.items()}
                    post = None
                    try:
                        post = req.post_data
                    except Exception:
                        pass
                    log.info(f"DIAG-SSE-REQ url={req.url[:180]!r}")
                    log.info(f"DIAG-SSE-HEADERS {hdrs_safe!r}")
                    if post:
                        log.info(f"DIAG-SSE-BODY {post[:3000]!r}")
            except Exception as e:
                log.warning(f"DIAG-SSE-REQ failed: {e}")

    page.on("response", capture)
    page.on("request", on_request)

    # Reset the fetch-hook stream buffer before sending
    try:
        await page.evaluate("""() => {
            try { window.__qwenStream = ''; window.__qwenStreamDone = false; window.__qwenStreamLen = 0; } catch (e) {}
        }""")
    except Exception:
        pass

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

        # Send loop — press Enter, dismiss login modal if it appears, retry.
        # After clicking send, verify the message actually LEFT the textarea
        # (Qwen clears it on successful send). If it's still there after a
        # few seconds, the click was swallowed (modal/overlay) — re-send.
        for send_attempt in range(3):
            response_event.clear()
            await _click_send(page)

            # Wait for EITHER SSE arrival OR textarea clearing (send OK)
            sent_ok = False
            for _ in range(32):  # up to 8s
                if response_event.is_set():
                    sent_ok = True
                    break
                try:
                    cur = await page.evaluate("() => document.querySelector('textarea')?.value || ''")
                    if len(cur.strip()) < max(5, int(len(prompt.strip()) * 0.15)):
                        sent_ok = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.25)

            if response_event.is_set():
                break  # Got SSE!

            if sent_ok:
                # Message went out — wait for SSE with an ACTIVITY timeout:
                # as long as new tokens keep arriving (SSE observed, DOM
                # growing), keep waiting — Qwen thinking can exceed the fixed
                # SSE_TIMEOUT and we must NOT kill a live generation. Only
                # give up after SSE_ACTIVITY_IDLE seconds of total silence
                # past the base timeout, or the SSE_MAX_WAIT absolute cap.
                start_wait = time.time()
                progress_ts[0] = time.time()
                poller_stop = asyncio.Event()

                async def _dom_progress() -> None:
                    last_stream_len = 0
                    last_reason_len = 0
                    while not poller_stop.is_set():
                        # Live SSE chunks from the fetch hook -> forward to
                        # client AND treat every new token as activity. DOM
                        # growth is deliberately NOT used for activity: it
                        # includes the echoed user prompt and toasts, which
                        # kept the wait alive forever (the old 20-min hang).
                        try:
                            sraw = await page.evaluate("() => window.__qwenStream || ''")
                            if sraw:
                                sp = _parse_qwen_sse(sraw)
                                stext = sp.get("text", "")
                                sreason = sp.get("reasoning", "")
                                progressed = False
                                if len(stext) > last_stream_len:
                                    diff = stext[last_stream_len:]
                                    last_stream_len = len(stext)
                                    progressed = True
                                    if chunk_q is not None:
                                        try:
                                            chunk_q.put_nowait(("content", diff))
                                        except Exception:
                                            pass
                                if len(sreason) > last_reason_len:
                                    rdiff = sreason[last_reason_len:]
                                    last_reason_len = len(sreason)
                                    progressed = True
                                    if chunk_q is not None:
                                        try:
                                            chunk_q.put_nowait(("reasoning", rdiff))
                                        except Exception:
                                            pass
                                if progressed:
                                    progress_ts[0] = time.time()
                        except Exception:
                            pass
                        await asyncio.sleep(0.8)

                poller = asyncio.create_task(_dom_progress())
                try:
                    while True:
                        if response_event.is_set():
                            break
                        now = time.time()
                        # The SITE never even opened the SSE stream (no POST to
                        # the chat endpoint). This is a silently-dead send (guest
                        # throttled/blocked), not a slow generation — a live Qwen
                        # always emits the SSE .created POST within ~2s. Abort the
                        # attempt fast instead of burning the full STALL_CAP.
                        if not sse_started[0] and (now - start_wait) > EMPTY_NO_SSE:
                            log.info(f"No SSE POST within {EMPTY_NO_SSE}s — silent dead send, aborting (will retry)")
                            break
                        # Rolling stall: abort when the model went SILENT — no new
                        # reasoning/content token and no SSE POST for STALL_CAP.
                        # Every token resets progress_ts (see _dom_progress and
                        # on_request), so a live long generation is never killed;
                        # only a true stall (reasoning delivered, answer stuck)
                        # triggers a fresh retry.
                        if now - progress_ts[0] > STALL_CAP:
                            log.info(f"No new token for {STALL_CAP}s — stalled, giving up (will retry)")
                            break
                        await asyncio.sleep(1)
                finally:
                    poller_stop.set()
                    poller.cancel()
                    try:
                        await poller
                    except (asyncio.CancelledError, Exception):
                        pass
                break

            # Text still in textarea — send was swallowed. Check for modal.
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

            # No modal, no SSE, text still there — force re-type and re-send
            log.warning(f"Send attempt {send_attempt}: textarea not cleared, re-typing")
            await _type_text(page, prompt)
            await asyncio.sleep(1)
            continue

        if bodies["raw"] is not None:
            parsed = _parse_qwen_sse(bodies["raw"])
            log.info(f"PARSED: text_len={len(parsed.get('text',''))} reasoning_len={len(parsed.get('reasoning',''))} head={parsed.get('text','')[:80]!r}")
            if not parsed.get("text") and not parsed.get("reasoning"):
                log.warning(f"SSE parsed EMPTY: {bodies['raw'][:800]!r}")
            elif not parsed.get("text"):
                # Reasoning-only SSE — dump the tail where the answer should be
                log.warning(f"SSE reasoning-only (len={len(bodies['raw'])}): TAIL {bodies['raw'][-1500:]!r}")
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
        try:
            page.remove_listener("request", on_request)
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
            if len(text) > 20000:
                text = text[:20000] + "\n...[system truncated]"
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
                    # Explicit marker so Qwen understands this JSON is the
                    # model's OWN previous tool call, not a format example.
                    parts.append(f'[Assistant tool call]\n{{"tool": "{fn.get("name", "")}", "arguments": {fn.get("arguments", "{}")}}}')
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


def _build_tool_block(tools: list[dict]) -> str:
    """Build ONLY the tool-description + JSON-output-format block (no user content)."""
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
        "CRITICAL: You are connected through an API proxy. The ONLY way you can",
        "interact with files or the shell is by outputting the JSON tool-call format",
        "described below. You have NO built-in Qwen tools, artifacts, code runner,",
        "file browser, or Qwen Studio tool buttons. Do NOT try to use them.",
        "If you try to call a built-in tool you will only see an error like",
        "'Tool X does not exists'. When you need a tool, output JSON instead.",
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
        "CRITICAL: The JSON you output is parsed by a machine. When the content",
        "contains quotes (HTML/CSS/JS files, code), ESCAPE every double quote",
        'inside strings as \\" — e.g. lang=\\"ru\\", not lang="ru". Unescaped',
        "quotes corrupt the JSON and your tool call will be ignored.",
        "",
        "NO additional text, NO markdown, NO explanation around tool calls.",
    ])
    return "\n".join(desc_lines)


def _build_tool_prompt(last_content: str, tools: list[dict]) -> str:
    """Append full tool descriptions (including JSON schema) to prompt."""
    return _build_tool_block(tools) + "\n\n" + last_content


# ─── Tool Call Parsing ──────────────────────────────────────────────────────
def _repair_json(s: str) -> Optional[dict]:
    """Parse a JSON object, tolerating unescaped quotes inside string values.

    Qwen frequently emits file contents (HTML/CSS/JS) with raw double quotes
    like lang="ru" inside a JSON string — that is invalid JSON and
    json.loads() throws. We try progressively:
      1. plain json.loads
      2. heuristic re-escaping of inner quotes
      3. manual rebuild for the known tool-call shapes
    Returns the parsed dict or None.
    """
    # 1. Plain
    try:
        return json.loads(s)
    except Exception:
        pass

    # 2. Heuristic escape: a quote closes a string when the next non-space
    #    char is , } ] : or end; otherwise it's an inner quote -> escape it.
    out = []
    in_str = False
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            out.append(c)
            out.append(s[i + 1])
            i += 2
            continue
        if c == '"':
            nxt = ""
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j < n:
                nxt = s[j]
            if in_str:
                if nxt in ",}]:":
                    in_str = False
                    out.append(c)
                else:
                    out.append('\\"')
            else:
                prev = s[i - 1] if i > 0 else ""
                if prev in "{[,:" or nxt in "}],:" or i == 0:
                    in_str = True
                    out.append(c)
                else:
                    out.append('\\"')
            i += 1
            continue
        out.append(c)
        i += 1
    repaired = "".join(out)
    try:
        return json.loads(repaired)
    except Exception:
        pass

    # 3. Manual rebuild for known shapes (write/read/edit/bash...)
    try:
        m = re.search(r'"tool"\s*:\s*"([^"]+)"', s)
        if not m:
            return None
        name = m.group(1).strip()
        if "." in name or "/" in name:
            name = re.split(r"[./]", name)[-1]
        args: dict = {}
        # simple string args first (path, command, filePath...)
        for key in ("path", "filePath", "command", "oldString", "newString"):
            mk = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % key, s)
            if mk:
                args[key] = mk.group(1).replace('\\"', '"').replace("\\\\", "\\")
        # content: greedy to the final closing of the arguments object
        mc = re.search(r'"content"\s*:\s*"(.*)', s, re.DOTALL)
        if mc:
            rest = mc.group(1)
            # strip trailing `"}` / `", "` / `"}`
            idx = rest.rfind('"}')
            if idx != -1:
                content = rest[:idx]
            else:
                content = rest
            # unescape what we can
            content = content.replace('\\"', '"').replace("\\\\", "\\")
            args["content"] = content
        if name == "edit" and "path" in args:
            # keep edits if present and parseable
            me = re.search(r'"edits"\s*:\s*(\[.*)', s, re.DOTALL)
            if me:
                try:
                    args["edits"] = json.loads(me.group(1))
                except Exception:
                    pass
        if not args and name != "write":
            return None
        return {"tool": name, "arguments": args}
    except Exception:
        return None


def _extract_all_tool_calls(text: str) -> list[tuple[str, dict]]:
    """Extract ALL tool call JSON objects from text, tolerating sloppy formats.

    Handles:
      - {"tool": "...", "arguments": {...}}          (our canonical format)
      - {"tool": "...", "args": {...}}               (renamed args key)
      - {"tool": "...", "params": {...}}             (renamed params key)
      - {"tool": "...", "arguments": "{json str}"}   (arguments as JSON string)
      - {"function": {"name": ..., "arguments": ...}} (OpenAI shape)
      - {"name": ..., "arguments": ...}              (bare name key)
      - {"tool_call": {"name": ..., "arguments": ...}} (wrapper shape)
      - {"tool": {"name": ...}, ...}                 (tool as object)
      - fenced code blocks, arrays of the above
    Tool names are normalized: "functions.read" -> "read".
    """
    results: list[tuple[str, dict]] = []
    seen: set[tuple] = set()

    def _parse_obj(obj) -> None:
        if not isinstance(obj, dict):
            return
        # unwrap tool_call wrapper
        inner = obj.get("tool_call")
        if isinstance(inner, dict):
            obj = inner
        fn = obj.get("function")
        name_src = None
        name = obj.get("tool")
        if isinstance(name, str):
            name_src = "tool"
        else:
            name = obj.get("name")
            if isinstance(name, str):
                name_src = "name"
        if not isinstance(name, str) and isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                name_src = "function"
        if not isinstance(name, str) and isinstance(fn, str):
            name = fn
            name_src = "function"
        if not isinstance(name, str):
            # {"tool": {"name": ...}}
            t = obj.get("tool")
            if isinstance(t, dict):
                name = t.get("name") or t.get("tool")
                name_src = "tool"
        if not name:
            return
        name = str(name).strip()
        # normalize: "functions.read" -> "read", "bash_command" kept as-is
        if "." in name or "/" in name:
            name = re.split(r"[./]", name)[-1]
        if not name:
            return

        raw_args = None
        for k in ("arguments", "args", "params", "parameters", "input", "arg"):
            if k in obj:
                raw_args = obj[k]
                break
        if raw_args is None and isinstance(fn, dict):
            for k in ("arguments", "args", "params", "parameters", "input"):
                if k in fn:
                    raw_args = fn[k]
                    break
        # CRITICAL: a bare {"name": "X"} (no tool/function key, no arguments)
        # is a NESTED argument object (e.g. {"tool": "skill_view",
        # "arguments": {"name": "developer-portfolio"}}), NOT a tool call.
        # Treating it as one makes _detect_tool_error flag legit calls as
        # "undeclared tool" -> infinite re-prompt loop. Only accept a bare
        # name when it actually carries arguments.
        if name_src == "name" and raw_args is None:
            return
        args = {}
        if isinstance(raw_args, dict):
            args = raw_args
        elif isinstance(raw_args, str):
            s = raw_args.strip()
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    args = parsed
            except Exception:
                m = re.search(r"\{.*\}", s, re.DOTALL)
                if m:
                    try:
                        parsed = json.loads(m.group(0))
                        if isinstance(parsed, dict):
                            args = parsed
                    except Exception:
                        pass
        elif isinstance(raw_args, list):
            # some models wrap args in a single-element list
            if len(raw_args) == 1 and isinstance(raw_args[0], dict):
                args = raw_args[0]

        key = (name, json.dumps(args, sort_keys=True))
        if key not in seen:
            seen.add(key)
            results.append((name, args))

    # 1. Single pass over the text with brace matching (handles nesting, strings)
    stack: list[int] = []
    in_str = False
    esc = False
    for i, c in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            stack.append(i)
        elif c == "}":
            if stack:
                start = stack.pop()
                chunk = text[start:i + 1]
                try:
                    _parse_obj(json.loads(chunk))
                except Exception:
                    obj = _repair_json(chunk)
                    if obj is not None:
                        _parse_obj(obj)

    # 2. Fallback: fenced code blocks (whole object or array)
    if not results:
        for m in re.finditer(r'`{3}(?:json)?\s*(\{.*?\}|\[.*?\])\s*`{3}', text, re.DOTALL):
            try:
                obj = json.loads(m.group(1))
            except Exception:
                obj = _repair_json(m.group(1))
            if obj is None:
                continue
            if isinstance(obj, list):
                for item in obj:
                    _parse_obj(item)
            else:
                _parse_obj(obj)

    # 3. Last resort: the whole text may BE one tool-call object with broken
    #    quoting (e.g. write with a big HTML file) — repair it as a whole.
    if not results:
        obj = _repair_json(text)
        if obj is not None:
            _parse_obj(obj)
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
    def __init__(self, size: int = POOL_SIZE, model: str = "Qwen3.8-Max", headless: bool = HEADLESS):
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
        # Profile recreation serialization: only ONE daily-limit wipe at a time.
        # Concurrent limit hits wait and reuse the fresh profile instead of
        # each wiping it again (reduces guest churn / stops torn states).
        self._profile_lock = asyncio.Lock()

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
                    if GUEST_MODE:
                        # Guest mode: only anonymous guest cookie sets are usable.
                        # Account cookies (containing a token) would log Qwen into
                        # the real account, whose daily limits DO apply — that is
                        # exactly the "upper limit for today's usage" trap.
                        has_token = any(
                            "token" in (c.get("name") or "").lower()
                            for c in cookies
                        )
                        if has_token:
                            log.info(f"Skipping {fname}: account cookies (token) not usable in guest mode")
                            continue
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

    async def _launch_context(self) -> BrowserContext:
        """Launch a fresh persistent context (brand-new guest session)."""
        ctx = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=self.headless,
            args=_LAUNCH_ARGS,
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            locale="ru-RU",
            timezone_id="Europe/Helsinki",
        )
        await self._install_init_script(ctx)
        return ctx

    async def _install_init_script(self, ctx: BrowserContext) -> None:
        await ctx.add_init_script("""
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
            // LIVE STREAMING: intercept the SSE fetch and accumulate raw
            // chunks into window.__qwenStream so the Python side can forward
            // tokens to the client in real time (not just at the end).
            (() => {
                try {
                    const origFetch = window.fetch;
                    window.__qwenStream = '';
                    window.__qwenStreamDone = false;
                    window.__qwenStreamLen = 0;
                    window.fetch = async (...args) => {
                        const resp = await origFetch.apply(window, args);
                        let url = '';
                        try {
                            url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
                        } catch (e) {}
                        if (url.includes('/api/v2/chat/completions') && resp && resp.body) {
                            try {
                                const clone = resp.clone();
                                const reader = clone.body.getReader();
                                const decoder = new TextDecoder();
                                (async () => {
                                    try {
                                        while (true) {
                                            const {done, value} = await reader.read();
                                            if (done) break;
                                            window.__qwenStream += decoder.decode(value, {stream: true});
                                            window.__qwenStreamLen = window.__qwenStream.length;
                                        }
                                        window.__qwenStreamDone = true;
                                    } catch (e) {
                                        window.__qwenStreamDone = true;
                                    }
                                })();
                            } catch (e) {}
                        }
                        return resp;
                    };
                } catch (e) {}
            })();
        """)

    async def start(self) -> None:
        # NOTE: We do NOT wipe the profile in guest mode. Qwen blocks FRESH
        # guest sessions ("log in or sign up" modal on every send) but allows
        # STABLE guest sessions that carry cookies from previous visits —
        # exactly like a normal browser vs incognito. Persistent profile =
        # the guest session accumulates trust and sending works.
        self.playwright = await async_playwright().start()
        self.ctx = await self._launch_context()

        # Load cookie pool. IMPORTANT: in guest mode do NOT apply cookie files
        # from cookies/*.json — those are exports of an OLD session (possibly
        # already daily-limited). The persistent profile itself accumulates
        # fresh guest cookies naturally on each visit; applying stale exports
        # re-binds us to the burned session and resurrects "upper limit for
        # today's usage". Only the non-guest (account) mode uses cookie files.
        if not GUEST_MODE:
            self._scan_cookies()
            await self._apply_cookies()

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

    async def _recreate_profile(self, idx: int) -> None:
        """Nuke the whole persistent profile and relaunch — a BRAND-NEW guest session.

        Called when the current guest session is burned (daily "upper limit").
        Recreating pages inside the same profile does NOT reset the limit —
        the burned session cookies persist. Only a fresh profile (fresh
        cookies) restores unlimited guest usage.
        """
        # Serialize recreation: if another daily-limit hit is already wiping and
        # relaunching the profile, wait for it and reuse the fresh session rather
        # than tearing down again (avoids double-wipe / torn shared state).
        async with self._profile_lock:
            old = self._states[idx]
            if old:
                try:
                    await old.page.close()
                except Exception:
                    pass
            # Close the entire browser context (releases profile lock on disk)
            if self.ctx:
                try:
                    await self.ctx.close()
                except Exception as e:
                    log.warning(f"Profile close failed: {e}")
            # Wipe the profile dir — fresh guest cookies on next launch
            for attempt in range(3):
                try:
                    shutil.rmtree(USER_DATA_DIR, ignore_errors=True)
                    self.ctx = await self._launch_context()
                    if not GUEST_MODE:
                        await self._apply_cookies()
                    # Recreate ALL pages (the old context hosted them all)
                    for i in range(self.size):
                        try:
                            page = await _create_page(self.ctx, self.model)
                            self._states[i] = PageState(page=page)
                        except Exception as e:
                            log.warning(f"Profile recreate: page {i} failed: {e}")
                            self._states[i] = None
                    if self._states[idx] is not None:
                        log.info(f"Profile {idx} recreated — fresh guest session (dir wiped)")
                        return
                except Exception as e:
                    log.warning(f"Profile recreate attempt {attempt+1} failed: {e}")
                    await asyncio.sleep(5)
            self._states[idx] = None
            log.error(f"Profile {idx} recreate FAILED after 3 attempts")

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

    def _prep_sibling_fresh(self, busy_idx: int) -> None:
        """Best-effort background refresh: while `busy_idx` serves the current
        request, refresh every other idle page to a fresh home chat so the next
        request lands instantly in a clean session (skips the goto cost on the
        request's path). App errors are logged and ignored."""
        def spawn(_i: int) -> None:
            async def run() -> None:
                st = self._states[_i]
                if st is None or st.page is None:
                    return
                try:
                    if st.busy or st.resetting:
                        return
                    if "/c/" not in str(st.page.url):
                        return
                    async with self._lock:
                        if st.busy or st.resetting:
                            return
                        st.resetting = True
                    try:
                        await st.page.goto(URL, wait_until="domcontentloaded", timeout=20000)
                        for _ in range(20):
                            ta = await st.page.query_selector('textarea')
                            if ta:
                                break
                            await asyncio.sleep(0.2)
                        await _dismiss_auth_dialog(st.page)
                        await _dismiss_modal(st.page)
                        await _select_model(st.page, self.model)
                    finally:
                        st.resetting = False
                        st.last_used = time.time()
                except Exception as e:
                    try:
                        st = self._states[_i]
                        if st is not None:
                            st.resetting = False
                    except Exception:
                        pass
                    log.info(f"Page {_i} bg pre-fresh: {e}")
            try:
                asyncio.create_task(run())
            except Exception:
                pass

        for _i, st in enumerate(self._states):
            if _i == busy_idx or st is None or st.busy or st.resetting:
                continue
            if "/c/" in str(st.page.url):
                spawn(_i)

    async def _wait_idle_page(self, timeout: float = NO_PAGE_TIMEOUT) -> int:
        """Pick an idle page, waiting up to `timeout`s if every page is busy
        (e.g. mid guest-profile recreation when all pages briefly vanish).
        Prefers an already-fresh home page. Returns -1 if never idle.
        """
        deadline = time.time() + timeout
        while True:
            async with self._lock:
                idx = -1
                for i, st in enumerate(self._states):
                    if st is not None and not st.busy and not st.resetting and "/c/" not in str(st.page.url):
                        idx = i
                        break
                if idx == -1:
                    for i, st in enumerate(self._states):
                        if st is not None and not st.busy and not st.resetting:
                            idx = i
                            break
                if idx != -1:
                    self._states[idx].busy = True
                    self._states[idx].last_used = time.time()
                    return idx
            if time.time() > deadline:
                return -1
            await asyncio.sleep(0.5)

    async def execute(self, prompt: str, fresh: bool = False, tools: Optional[list] = None) -> dict:
        await self._available.acquire()
        try:
            declared_names = set(_schema_params_for(tools).keys()) if tools else None
            idx = await self._wait_idle_page()
            if idx == -1:
                return {"text": "[QwenMode] No available pages", "reasoning": ""}
            state = self._states[idx]

            # If this page has already produced EMPTY session(s), the guest is
            # likely silently dead. Rotate to a fresh guest NOW so we don't burn
            # repeated ~120s silences on a dead session.
            if state.empty_streak >= 1:
                log.warning(f"Page {idx} streak={state.empty_streak} — rotating guest before sending")
                try:
                    async with self._lock:
                        await self._recreate_profile(idx)
                        ns = self._states[idx]
                        if ns is not None:
                            state = ns
                            state.busy = True
                            state.empty_streak = 0
                except Exception as e:
                    log.warning(f"Page {idx} rotate-on-streak failed: {e}")
                state = self._states[idx]

            # Background: pre-fresh idle sibling pages so the next request
            # (always a new session) starts instantly, overlapping the goto.
            self._prep_sibling_fresh(idx)

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
                last_error_text = ""
                cur_prompt = prompt
                retry_deadline = time.time() + EMPTY_RETRY_BUDGET if EMPTY_RETRY_BUDGET > 0 else None
                for attempt in range(40 if EMPTY_RETRY_BUDGET > 0 else 4):
                    res, new_page = await _wait_for_response(
                        state.page, cur_prompt, self.model, SSE_TIMEOUT
                    )
                    text = res.get("text", "")
                    log.info(f"EXEC attempt={attempt} text_head={text[:120]!r}")
                    if retry_deadline is not None and time.time() > retry_deadline:
                        result = ({"text": last_error_text or "[QwenMode] Retry budget exhausted", "reasoning": ""}, state.page)
                        break
                    # ── Undeclared / built-in tool attempt ───────────────────
                    bad_tool = _detect_tool_error(text, declared_names)
                    if bad_tool and not text.startswith("[Qwen Error]"):
                        last_error_text = f"[QwenMode] Tool '{bad_tool}' not supported — re-prompting model"
                        if attempt < 3:
                            allowed = ", ".join(sorted(declared_names)) if declared_names else "the tools already listed in the prompt"
                            log.warning(f"Page {idx} used undeclared tool '{bad_tool}' (attempt {attempt}) — re-prompting")
                            try:
                                await state.page.goto(URL, wait_until="domcontentloaded", timeout=30000)
                                await asyncio.sleep(1 + attempt)
                                await _dismiss_auth_dialog(state.page)
                                await _dismiss_modal(state.page)
                                await _select_model(state.page, self.model)
                                for _ in range(15):
                                    ta = await state.page.query_selector('textarea')
                                    if ta:
                                        break
                                    await asyncio.sleep(0.5)
                            except Exception as e:
                                log.warning(f"Page {idx} refresh failed on tool retry: {e}")
                            cur_prompt = (
                                f"{prompt}\n\n[HARD INSTRUCTION] Tool '{bad_tool}' does NOT exist in this "
                                "system and will cause an error. You may ONLY use this exact set of tools: "
                                f"{allowed}. Never invent a tool name or call a built-in Qwen tool. If you "
                                "need a tool, reply with exactly ONE JSON object: "
                                '{"tool": "NAME", "arguments": {...}} where NAME is one of the allowed tools; '
                                "otherwise reply as plain text with no JSON."
                            )
                            continue
                    if text.startswith("[Qwen Error]"):
                        last_error_text = text
                        low = text.lower()
                        # ── DAILY limit (guest session burned) ───────────────
                        # "upper limit for today's usage" / RateLimited. This is
                        # NOT a temporary throttle: the current guest session is
                        # burned. Recreating pages inside the same profile is
                        # useless (same cookies). Must wipe the profile and
                        # start a BRAND-NEW guest session.
                        if "upper limit" in low or "ratelimited" in low:
                            log.warning(f"Page {idx} DAILY limit: {text[:120]} — recreating guest profile")
                            async with self._lock:
                                await self._recreate_profile(idx)
                                new_state = self._states[idx]
                                if new_state is None:
                                    return {"text": "[QwenMode] Guest session recreate failed — daily limit", "reasoning": ""}
                                new_state.busy = True
                                state = new_state
                            # Cooldown so the fresh session isn't slammed instantly
                            await asyncio.sleep(20 + attempt * 10)
                            continue
                        # ── Temporary throttle (high demand / quota_limit) ───
                        if "high demand" in low or "quota" in low:
                            # Same-page refresh can't help — the guest is the
                            # problem. Rotate to a BRAND-NEW guest and retry.
                            log.info(f"Page {idx} quota/overload: {text[:80]} — rotating to fresh guest")
                            async with self._lock:
                                await self._recreate_profile(idx)
                                new_state = self._states[idx]
                            state = new_state if new_state is not None else state
                            await asyncio.sleep(GUEST_COOLDOWN)
                            if self._states[idx] is None:
                                result = ({"text": "[QwenMode] All fresh guests failed (quota)", "reasoning": ""}, state.page)
                                break
                            continue
                        # ── Other Qwen errors (limit/usage wording) ──────────
                        if not GUEST_MODE:
                            self._rotate_cookies()
                        elif attempt < 2:
                            # Transient/unknown errors: first do a lightweight
                            # refresh+resend. Do NOT wipe the whole guest profile
                            # for an error that may just be a cut stream.
                            log.info(f"Page {idx} guest error {text[:60]} — refresh & retry (attempt {attempt})")
                            try:
                                await state.page.goto(URL, wait_until="domcontentloaded", timeout=30000)
                                await asyncio.sleep(1 + attempt)
                                await _dismiss_auth_dialog(state.page)
                                await _dismiss_modal(state.page)
                                await _select_model(state.page, self.model)
                                for _ in range(15):
                                    ta = await state.page.query_selector('textarea')
                                    if ta:
                                        break
                                    await asyncio.sleep(0.5)
                            except Exception as e:
                                log.warning(f"Page {idx} refresh failed on guest error retry: {e}")
                            continue
                        else:
                            # Same error kept repeating — the guest session is likely
                            # burned. Fall back to a fresh guest profile.
                            log.info(f"Guest mode: persistent error, rotating profile for {text[:80]}")
                            async with self._lock:
                                await self._recreate_profile(idx)
                                new_state = self._states[idx]
                                if new_state is None:
                                    return {"text": "[QwenMode] Guest session recreate failed", "reasoning": ""}
                                new_state.busy = True
                                state = new_state
                            await asyncio.sleep(15)
                            continue
                    elif not text.strip():
                        # EMPTY: send left but the SITE never streamed back in
                        # time (guest silently throttled/blocked). goto refresh
                        # won't help — cookies persist. Rotate to a BRAND-NEW
                        # guest profile so the retry runs un-throttled.
                        self._states[idx].empty_streak += 1
                        last_error_text = "[QwenMode] Empty response"
                        log.warning(f"Page {idx} EMPTY (attempt {attempt}) — rotating to fresh guest")
                        async with self._lock:
                            await self._recreate_profile(idx)
                            new_state = self._states[idx]
                        state = new_state if new_state is not None else state
                        await asyncio.sleep(GUEST_COOLDOWN)
                        if self._states[idx] is None:
                            result = ({"text": "[QwenMode] All fresh guests failed (profile gone)", "reasoning": ""}, state.page)
                            break
                        continue  # keep rotating to a NEW guest within the budget
                    if text.strip() and self._states[idx] is not None:
                        self._states[idx].empty_streak = 0
                    result = (res, new_page)
                    break

                if result is None:
                    # All 4 attempts failed with an error/empty — return the
                    # last error text explicitly, NEVER a blank string.
                    result = ({"text": last_error_text or "[QwenMode] No response after 4 attempts", "reasoning": ""}, state.page)
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
            # Always free the page — even on a client disconnect / generator
            # cancellation / error — or the slot leaks busy forever and the
            # pool reports "No available pages".
            try:
                cs = self._states[idx]
                if cs is not None:
                    cs.busy = False
                    cs.resetting = False
            except Exception:
                pass
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

        prompt = self._build_request_prompt(messages, tools)

        result = await self.execute(prompt, fresh=True, tools=tools)
        return _format_chat_result(result.get("text", ""), result.get("reasoning", ""), tools)

    def _build_request_prompt(self, messages: list[dict], tools: Optional[list]) -> str:
        """Build the textarea prompt: context + tool block + request (see chat())."""
        last_content = _build_prompt(messages)
        if tools:
            # Order matters: context (system + history) FIRST, then our tool
            # block, then the actual user request LAST. If the tool block is
            # prepended, Qwen reads it, then a 10KB system prompt (opencode's
            # own read/bash/edit/write descriptions) "retrains" it to use the
            # built-in Qwen Studio tools -> "Tool read does not exists" errors.
            # The instruction right before the request has the most weight.
            user_idx = -1
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    user_idx = i
                    break
            if user_idx > 0:
                ctx_text = _build_prompt(messages[:user_idx])
                user_text = _build_prompt(messages[user_idx:])
                return f"{ctx_text}\n\n{_build_tool_block(tools)}\n\n{user_text}"
            return _build_tool_prompt(last_content, tools)
        return last_content

    async def execute_stream(self, prompt: str, fresh: bool = False, tools: Optional[list] = None):
        """Like execute(), but YIELDS incremental text chunks as Qwen generates.

        Live streaming via the window.__qwenStream fetch hook (see
        _install_init_script). Final result (with tool_calls etc.) is not
        streamed here — use execute() for tool-using requests; this is for
        plain chat streaming.
        """
        await self._available.acquire()
        try:
            idx = await self._wait_idle_page()
            if idx == -1:
                yield ("final", {"text": "[QwenMode] No available pages", "reasoning": ""})
                return
            state = self._states[idx]

            # If this page has already produced EMPTY session(s), the guest is
            # likely silently dead. Rotate to a fresh guest NOW so we don't burn
            # repeated ~120s silences on a dead session.
            if state.empty_streak >= 1:
                log.warning(f"Page {idx} streak={state.empty_streak} — rotating guest before sending")
                try:
                    async with self._lock:
                        await self._recreate_profile(idx)
                        ns = self._states[idx]
                        if ns is not None:
                            state = ns
                            state.busy = True
                            state.empty_streak = 0
                except Exception as e:
                    log.warning(f"Page {idx} rotate-on-streak failed: {e}")
                state = self._states[idx]

            # Background: pre-fresh idle sibling pages so the next request
            # (always a new session) starts instantly, overlapping the goto.
            self._prep_sibling_fresh(idx)

            if fresh:
                try:
                    cur = state.page.url
                    if "/c/" in str(cur):
                        log.info(f"Page {idx}: fresh session — opening new chat")
                        await state.page.goto(URL, wait_until="domcontentloaded", timeout=30000)
                        for _ in range(20):
                            ta = await state.page.query_selector('textarea')
                            if ta:
                                break
                            await asyncio.sleep(0.15)
                        await _dismiss_auth_dialog(state.page)
                        await _dismiss_modal(state.page)
                        await _select_model(state.page, self.model)
                except Exception as e:
                    log.warning(f"Page {idx} fresh-chat nav failed: {e}")

            if not await self._check_health(state.page):
                async with self._lock:
                    await self._recreate_page(idx)
                    state = self._states[idx]
                    if state is None:
                        yield ("final", {"text": "[QwenMode] Page unavailable after recreate", "reasoning": ""})
                        return
                    state.busy = True

            chunk_q: asyncio.Queue = asyncio.Queue()

            chunk_q: asyncio.Queue = asyncio.Queue()
            declared_names = set(_schema_params_for(tools).keys()) if tools else None

            async def _attempt_loop():
                nonlocal state
                result = None
                last_error_text = ""
                cur_prompt = prompt
                retry_deadline = time.time() + EMPTY_RETRY_BUDGET if EMPTY_RETRY_BUDGET > 0 else None
                for attempt in range(40 if EMPTY_RETRY_BUDGET > 0 else 4):
                    res, new_page = await _wait_for_response(
                        state.page, cur_prompt, self.model, SSE_TIMEOUT, chunk_q
                    )
                    text = res.get("text", "")
                    log.info(f"EXEC-STREAM attempt={attempt} text_head={text[:120]!r}")
                    if retry_deadline is not None and time.time() > retry_deadline:
                        result = ({"text": last_error_text or "[QwenMode] Retry budget exhausted", "reasoning": ""}, state.page)
                        break
                    # ── Undeclared / built-in tool attempt ───────────────────
                    bad_tool = _detect_tool_error(text, declared_names)
                    if bad_tool and not text.startswith("[Qwen Error]"):
                        last_error_text = f"[QwenMode] Tool '{bad_tool}' not supported — re-prompting model"
                        if attempt < 3:
                            allowed = ", ".join(sorted(declared_names)) if declared_names else "the tools already listed in the prompt"
                            log.warning(f"Page {idx} used undeclared tool '{bad_tool}' (attempt {attempt}) — re-prompting")
                            try:
                                await state.page.goto(URL, wait_until="domcontentloaded", timeout=30000)
                                await asyncio.sleep(1 + attempt)
                                await _dismiss_auth_dialog(state.page)
                                await _dismiss_modal(state.page)
                                await _select_model(state.page, self.model)
                                for _ in range(15):
                                    ta = await state.page.query_selector('textarea')
                                    if ta:
                                        break
                                    await asyncio.sleep(0.5)
                            except Exception as e:
                                log.warning(f"Page {idx} refresh failed on tool retry: {e}")
                            cur_prompt = (
                                f"{prompt}\n\n[HARD INSTRUCTION] Tool '{bad_tool}' does NOT exist in this "
                                "system and will cause an error. You may ONLY use this exact set of tools: "
                                f"{allowed}. Never invent a tool name or call a built-in Qwen tool. If you "
                                "need a tool, reply with exactly ONE JSON object: "
                                '{"tool": "NAME", "arguments": {...}} where NAME is one of the allowed tools; '
                                "otherwise reply as plain text with no JSON."
                            )
                            continue
                    if text.startswith("[Qwen Error]"):
                        last_error_text = text
                        low = text.lower()
                        if "upper limit" in low or "ratelimited" in low:
                            log.warning(f"Page {idx} DAILY limit: {text[:120]} — recreating guest profile")
                            async with self._lock:
                                await self._recreate_profile(idx)
                                new_state = self._states[idx]
                                if new_state is None:
                                    return ({"text": "[QwenMode] Guest session recreate failed — daily limit", "reasoning": ""}, state.page)
                                new_state.busy = True
                            state = new_state
                            await asyncio.sleep(20 + attempt * 10)
                            continue
                        if "high demand" in low or "quota" in low:
                            log.info(f"Page {idx} quota/overload: {text[:80]} — rotating to fresh guest")
                            async with self._lock:
                                await self._recreate_profile(idx)
                                new_state = self._states[idx]
                            state = new_state if new_state is not None else state
                            await asyncio.sleep(GUEST_COOLDOWN)
                            if self._states[idx] is None:
                                result = ({"text": "[QwenMode] All fresh guests failed (quota)", "reasoning": ""}, state.page)
                                break
                            continue
                        # ── Other Qwen errors ────────────────────────────
                        if not GUEST_MODE:
                            self._rotate_cookies()
                        elif attempt < 2:
                            # Transient/unknown error — refresh+resend first,
                            # don't wipe the guest profile for a cut stream.
                            log.info(f"Page {idx} guest error {text[:60]} — refresh & retry (attempt {attempt})")
                            try:
                                await state.page.goto(URL, wait_until="domcontentloaded", timeout=30000)
                                await asyncio.sleep(1 + attempt)
                                await _dismiss_auth_dialog(state.page)
                                await _dismiss_modal(state.page)
                                await _select_model(state.page, self.model)
                                for _ in range(15):
                                    ta = await state.page.query_selector('textarea')
                                    if ta:
                                        break
                                    await asyncio.sleep(0.5)
                            except Exception as e:
                                log.warning(f"Page {idx} refresh failed on guest error retry: {e}")
                            continue
                        else:
                            log.info(f"Guest mode: persistent error, rotating profile for {text[:80]}")
                            async with self._lock:
                                await self._recreate_profile(idx)
                                new_state = self._states[idx]
                                if new_state is None:
                                    return ({"text": "[QwenMode] Guest session recreate failed", "reasoning": ""}, state.page)
                                new_state.busy = True
                            state = new_state
                            await asyncio.sleep(15)
                            continue
                    elif not text.strip():
                        # EMPTY: the send left but the SITE never streamed back
                        # in time (guest silently throttled/blocked). goto
                        # refresh never helps a throttled guest — the cookies
                        # persist. Rotate to a BRAND-NEW guest profile so the
                        # retry runs on an un-throttled session.
                        self._states[idx].empty_streak += 1
                        last_error_text = "[QwenMode] Empty response"
                        log.warning(f"Page {idx} EMPTY (attempt {attempt}) — rotating to fresh guest")
                        async with self._lock:
                            await self._recreate_profile(idx)
                            new_state = self._states[idx]
                        state = new_state if new_state is not None else state
                        await asyncio.sleep(GUEST_COOLDOWN)
                        if self._states[idx] is None:
                            result = ({"text": "[QwenMode] All fresh guests failed (profile gone)", "reasoning": ""}, state.page)
                            break
                        continue  # keep rotating to a NEW guest within the budget
                    if text.strip() and self._states[idx] is not None:
                        self._states[idx].empty_streak = 0
                    result = (res, new_page)
                    break
                # End of attempt loop.
                if result is None:
                    result = ({"text": last_error_text or "[QwenMode] No response after 4 attempts", "reasoning": ""}, state.page)
                return result

            task = asyncio.create_task(_attempt_loop())
            yielded_any = False
            while True:
                if task.done():
                    break
                try:
                    item = await asyncio.wait_for(chunk_q.get(), timeout=0.4)
                    if item and item[1]:
                        yielded_any = True
                        yield item  # ("content"|"reasoning", text)
                except asyncio.TimeoutError:
                    continue

            # Drain any chunks that arrived between last check and task end
            while not chunk_q.empty():
                try:
                    item = chunk_q.get_nowait()
                    if item and item[1]:
                        yielded_any = True
                        yield item
                except asyncio.QueueEmpty:
                    break

            final = task.result()
            result, new_page = final
            # Always emit the final result so the client can finish cleanly
            # (tool_calls delivered atomically here, content tail, errors).
            yield ("final", result)

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
        finally:
            # Always free the page on disconnect/cancel/error so a slot can
            # never leak busy (which would surface as "No available pages").
            try:
                cs = self._states[idx]
                if cs is not None:
                    cs.busy = False
                    cs.resetting = False
            except Exception:
                pass
            self._available.release()

    async def chat_stream(self, messages: list[dict], tools: Optional[list] = None):
        """Stream responses chunk by chunk.

        Yields ("content"|"reasoning", text) tuples, then ("final", result).
        Works with or without tools: for tool-using requests the client sees
        reasoning (keep-alive) and gets the final tool_calls atomically.
        """
        last_content = _build_prompt(messages)
        if not last_content:
            yield ("final", {"text": "[QwenMode] No message", "reasoning": ""})
            return
        prompt = self._build_request_prompt(messages, tools)
        async for item in self.execute_stream(prompt, fresh=True, tools=tools):
            if item[0] == "final":
                raw = item[1]
                formatted = _format_chat_result(raw.get("text", ""), raw.get("reasoning", ""), tools)
                yield ("final", formatted)
            else:
                yield item

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
        logging.basicConfig(level=logging.INFO)
        pool = QwenModePool(size=POOL_SIZE, model="Qwen3.8-Max")
        await pool.start()
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
        model: str = "Qwen3.8-Max"
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

        # Real-time streaming: content chunks for plain chats, reasoning
        # keep-alive + atomic final (content or tool_calls) for tool-using
        # requests. Streaming keeps client-side idle timeouts (pi's
        # httpIdleTimeoutMs) from killing long agentic generations.
        if req.stream:
            async def gen_live():
                cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                created = int(time.time())
                real_model = pool.model
                has_tools = bool(req.tools)
                # Live chunks (via the fetch hook) already stream content/reasoning.
                # Track how much was sent so the final chunk only emits the TAIL —
                # otherwise clients concatenate deltas and get duplicated output.
                streamed_content_len = 0
                streamed_reasoning_len = 0
                async for item in pool.chat_stream(req.messages, tools=req.tools):
                    if not isinstance(item, tuple) or len(item) < 2:
                        continue  # defensive: never let a bad yield kill the SSE stream
                    kind, payload = item[0], item[1]
                    if kind == "content" and not has_tools:
                        if payload:
                            streamed_content_len += len(payload)
                            yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": real_model, "choices": [{"index": 0, "delta": {"content": payload}, "finish_reason": None}]})}\n\n'
                    elif kind == "reasoning":
                        if payload:
                            streamed_reasoning_len += len(payload)
                            yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": real_model, "choices": [{"index": 0, "delta": {"reasoning_content": payload}, "finish_reason": None}]})}\n\n'
                    elif kind == "final":
                        content = payload.get("content") or payload.get("text", "")
                        reasoning = payload.get("reasoning_content") or payload.get("reasoning", "")
                        # Tool calls from the final result (atomic)
                        tc = payload.get("tool_calls")
                        if tc:
                            tc_deltas = []
                            for i, t in enumerate(tc):
                                tc_deltas.append({
                                    "index": i,
                                    "id": t["id"],
                                    "type": "function",
                                    "function": {"name": t["function"]["name"], "arguments": t["function"]["arguments"]},
                                })
                            yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": real_model, "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": tc_deltas}, "finish_reason": None}]})}\n\n'
                            yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": real_model, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})}\n\n'
                        else:
                            if reasoning:
                                tail = reasoning[streamed_reasoning_len:]
                                if tail:
                                    yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": real_model, "choices": [{"index": 0, "delta": {"reasoning_content": tail}, "finish_reason": None}]})}\n\n'
                            if content:
                                tail = content[streamed_content_len:]
                                if tail:
                                    yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": real_model, "choices": [{"index": 0, "delta": {"content": tail}, "finish_reason": None}]})}\n\n'
                            yield f'data: {json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": real_model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})}\n\n'
                yield "data: [DONE]\n\n"
            return StreamingResponse(gen_live(), media_type="text/event-stream")

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
                "id": "Qwen3.8-Max",
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

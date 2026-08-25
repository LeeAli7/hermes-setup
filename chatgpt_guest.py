#!/usr/bin/env python3
"""
ChatGPT-Guest — OpenAI-compatible API server for chatgpt.com (guest mode).
No account, no API key, no tokens. Headless browser + SSE interception.

  Client -> :5003 /v1/chat/completions -> Playwright -> chatgpt.com guest

Usage:
  python chatgpt_guest.py --server [port]
Env (all optional):
  CG_URL, CG_PORT, CG_POOL_SIZE, CG_API_KEY, CG_HEADLESS,
  CG_SSE_POLL_MS, CG_IDLE_TIMEOUT, CG_TOTAL_TIMEOUT, CG_PROFILE_DIR
"""
import asyncio, json, os, sys, time, uuid, logging, re, shutil, base64
from typing import Optional
from playwright.async_api import async_playwright, Page, BrowserContext
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

# ─── Config ────────────────────────────────────────────────────────────────
URL = os.getenv("CG_URL", "https://chatgpt.com/")
PORT = int(os.getenv("CG_PORT", "5003"))
POOL_SIZE = int(os.getenv("CG_POOL_SIZE", "1"))
API_KEY = os.getenv("CG_API_KEY", "")           # empty = no auth required
HEADLESS = os.getenv("CG_HEADLESS", "true").lower() == "true"
SSE_POLL_MS = int(os.getenv("CG_SSE_POLL_MS", "250"))
MOBILE_STABLE_S = float(os.getenv("CG_MOBILE_STABLE_S", "5"))  # silence to declare mobile stream done
IDLE_TIMEOUT = float(os.getenv("CG_IDLE_TIMEOUT", "60"))     # silence budget
TOTAL_TIMEOUT = float(os.getenv("CG_TOTAL_TIMEOUT", "300"))  # hard cap
CF_WAIT = float(os.getenv("CG_CF_WAIT", "150"))  # max wait for cloudflare
PROFILE_DIR = os.getenv("CG_PROFILE_DIR", "/tmp/chatgpt_guest_profile")
MODEL_NAME = os.getenv("CG_MODEL_NAME", "gpt-5-6")  # guest resolved model
MAX_ATTEMPTS = int(os.getenv("CG_MAX_ATTEMPTS", "3"))  # full retries per request (fresh profile between)

log = logging.getLogger("chatgpt-guest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

CHROME = os.getenv("CG_CHROME", "/home/ali/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome")
UA = os.getenv("CG_UA", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

LAUNCH_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-dev-shm-usage", "--disable-gpu",
    "--no-first-run", "--no-default-browser-check",
]
_proxy = os.getenv("CG_PROXY", "").strip()
if _proxy:
    LAUNCH_ARGS.insert(1, f"--proxy-server={_proxy}")

# ─── Injected once at context creation ─────────────────────────────────────
INIT_SCRIPT = """
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
  } catch (e) {}
  try {
    // soft-fingerprint normalisation
    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU','ru','en-US','en']});
  } catch (e) {}
})();
"""

# ─── SSE parsing for ChatGPT guest stream ──────────────────────────────────
def parse_delta_ops(data: str, cur_path=None):
    """Extract (path, value) text-appends from one SSE data payload.

    ChatGPT uses delta_encoding v1: the first chunk carries the full op
    ({"p": "...", "o": "append", "v": "..."}), subsequent chunks carry only
    {"v": "..."} and inherit the previous path. Returns (chunks, new_path).
    """
    chunks = []
    if not data:
        return chunks, cur_path
    if data.startswith("["):         # delta_encoding list
        return chunks, cur_path
    try:
        obj = json.loads(data)
    except Exception:
        return chunks, cur_path
    # delta op: {"p":"/message/content/parts/0","o":"append","v":"text"}
    if isinstance(obj, dict) and obj.get("o") == "append" and isinstance(obj.get("v"), str):
        p = obj.get("p") or ""
        if p.endswith("/parts/0"):
            chunks.append(obj["v"])
            return chunks, p
        return chunks, None
    # patch op: {"o":"patch","v":[{...append...}, ...]} — carries no "p" at top level
    if isinstance(obj, dict) and obj.get("o") == "patch" and isinstance(obj.get("v"), list):
        new_path = cur_path
        for op in obj["v"]:
            if isinstance(op, dict) and op.get("o") == "append" and isinstance(op.get("v"), str):
                p = op.get("p") or ""
                if p.endswith("/parts/0"):
                    chunks.append(op["v"])
                    new_path = p
        return chunks, new_path
    # add op (instant answers): {"p":"","o":"add","v":{"message":{...content.parts:[...]}}}
    # the whole assistant reply arrives in one shot, not streamed as deltas
    if isinstance(obj, dict) and obj.get("o") == "add" and isinstance(obj.get("v"), dict):
        msg = obj["v"].get("message") or {}
        if isinstance(msg, dict) and (msg.get("author") or {}).get("role") == "assistant":
            parts = ((msg.get("content") or {}).get("parts")) or []
            for part in parts:
                if isinstance(part, str):
                    chunks.append(part)
            if chunks:
                return chunks, "/message/content/parts/0"
        return chunks, None
    # delta-encoded continuation: {"v":"text"} inherits cur_path
    if isinstance(obj, dict) and "v" in obj and isinstance(obj.get("v"), str) and cur_path:
        if cur_path.endswith("/parts/0"):
            chunks.append(obj["v"])
    return chunks, cur_path

def stream_is_done(data: str) -> bool:
    return data.strip() == "[DONE]"

# ─── Mobile-web (unauth-mweb) DPU HTML parsing ────────────────────────────
# The A/B mobile UI replies over POST /unauth-mweb/conversation/updates with
# HTML templates. Assistant text lives in <p data-assistant-stream-block="">
# blocks; the turn ends with data-conversation-control="terminal-received"
# followed by "complete".
import re as _re
import html as _html

_MOBILE_STREAM_BLOCK = _re.compile(
    r'<p\s+data-assistant-stream-block="?"[^>]*>(.*?)</p>', _re.S)

def parse_mobile_html(data: str):
    """Extract assistant text from mobile-web DPU HTML updates.

    The reply appears TWICE: a streaming "pending" block and a final
    "committed" block inside <template data-web-mobile-dpu-terminal="">
    (with data-assistant-stream-block-index). Emitting both duplicates the
    text. We prefer the committed terminal block if present, else pending.
    Returns a list of text chunks in stream-block order.
    """
    def _blocks(scope):
        out = []
        for m in _re.finditer(
                r'<p\s+data-assistant-stream-block[^>]*data-assistant-stream-block-index="(\d+)"[^>]*>(.*?)</p>',
                scope, _re.S):
            idx = int(m.group(1))
            t = m.group(2)
            t = _re.sub(r"<\?[^>]*\?>", "", t)     # processing instructions
            t = _re.sub(r"<[^>]+>", "", t)          # any residual tags
            t = _html.unescape(t)
            if t.strip():
                out.append((idx, t))
        return out

    committed = []
    for tm in _re.finditer(r'<template[^>]*data-web-mobile-dpu-terminal[^>]*>(.*?)</template>', data, _re.S):
        committed.extend(_blocks(tm.group(1)))
    if committed:
        committed.sort(key=lambda x: x[0])
        return [t for _, t in committed]
    blocks = _blocks(data)
    blocks.sort(key=lambda x: x[0])
    return [t for _, t in blocks]

def is_mobile_body(data: str) -> bool:
    return "data-web-mobile-dpu-frame" in data

def mobile_done(body: str) -> bool:
    return 'data-conversation-control="terminal-received"' in body or \
           'data-conversation-control="complete"' in body

# ─── mobile variant B: plain-text render ─────────────────────────────────
# Some A/B buckets render the guest reply as plain text in the DOM (no
# data-assistant-stream-block, no data-conversation-control). The body
# innerText looks like:
#   Вы сказали:          |  You said:
#   <prompt>             |  <prompt>
#   ChatGPT сказал:      |  ChatGPT said:
#   <answer>             |  <answer>
#   ChatGPT — это ИИ…    |  ChatGPT can make mistakes…
# (followed by the sidebar UI: Новый чат / Войти …). We cut the answer
# between the "said:" marker and the FIRST footer/UI anchor.
#
# ⚠️ Disclaimer wording DRIFTS: 15.08 «ChatGPT — это ИИ и может ошибаться.»
# → 24.08 «ChatGPT — это ИИ, который может допускать ошибки.» The old exact
# regex stopped matching, so the disclaimer leaked into answers. Anchors are
# now broad + line-anchored for UI chrome.
_TXT_SAID = _re.compile(r'ChatGPT\s+(?:сказал|said)\s*:')
_TXT_STOP = _re.compile(
    r'ChatGPT\s*[—-]\s*это ИИ[^.\n]*ошиб'      # RU disclaimer (both wordings)
    r'|ChatGPT может допускать ошибки'
    r'|ChatGPT может ошибаться'
    r'|ChatGPT can make mistakes'
    r'|^\s*Чат с ChatGPT\s*$'
    r'|^\s*ChatGPT\s*$'
    r'|^\s*Новый чат\s*$|^\s*New chat\s*$'
    r'|^\s*Войти\s*$|^\s*Log in\s*$'
    r'|^\s*Зарегистрироваться\s*$|^\s*Sign up\s*$'
    r'|^\s*Sora\s*$|^\s*Codex\s*$'
    r'|^\s*Библиотека\s*$|^\s*Library\s*$'
    r'|^\s*Поиск\s*$|^\s*Search\s*$',
    _re.M)

def _last_said(text: str):
    """The LAST 'ChatGPT сказал/said:' marker — pages can hold several
    exchanges if a tab was reused, always answer from the newest one."""
    ms = list(_TXT_SAID.finditer(text))
    return ms[-1] if ms else None

def parse_mobile_innertext(text: str) -> str:
    """Return the assistant answer from the plain-text DOM render ('' if
    not ready yet — the marker appears only once the reply starts)."""
    m = _last_said(text)
    if not m:
        return ""
    ans = text[m.end():]
    f = _TXT_STOP.search(ans)
    if f:
        ans = ans[:f.start()]
    return ans.strip()

def mobile_text_done(text: str) -> bool:
    """Done when a footer/UI anchor rendered after the said-marker."""
    m = _last_said(text)
    if not m:
        return False
    return bool(_TXT_STOP.search(text, m.end()))

def mobile_text_stable(text: str, emitted: int) -> bool:
    """True once the answer stopped growing — the reliable end-of-stream
    signal for the plain-text render (the footer alone is NOT enough: the
    disclaimer is present on the page even before any reply exists)."""
    return emitted > 0 and len(parse_mobile_innertext(text)) == emitted

def stream_has_marker(data: str, marker: str) -> bool:
    if "message_marker" not in data:
        return False
    try:
        obj = json.loads(data)
    except Exception:
        return False
    return isinstance(obj, dict) and obj.get("type") == "message_marker" and obj.get("marker") == marker

# ─── Pool ──────────────────────────────────────────────────────────────────
class PageSlot:
    __slots__ = ("page", "busy", "last_used", "failures", "used_count",
                 "buf", "done", "hits", "cdp", "_net")
    def __init__(self, page):
        self.page = page; self.busy = False; self.last_used = 0.0; self.failures = 0
        self.used_count = 0
        # CDP-collected stream buffer (Python side). window.fetch patching is
        # dead — chatgpt.com wraps fetch with its own instrumentation before
        # our init script runs, so the only reliable capture is Playwright's
        # response event.
        self.buf = ""
        self.done = False
        self.hits = 0

class Pool:
    def __init__(self, size: int = 1):
        self.size = size
        self.pw = None
        self.ctx: Optional[BrowserContext] = None
        self.slots: list[PageSlot] = []
        self.busy_slots: set[PageSlot] = set()

    async def start(self):
        self.pw = await async_playwright().start()
        self.ctx = await self.pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR, executable_path=CHROME,
            headless=HEADLESS, args=LAUNCH_ARGS, user_agent=UA,
            viewport={"width": 1280, "height": 800},
        )
        await self.ctx.add_init_script(INIT_SCRIPT)
        for _ in range(self.size):
            try:
                slot = await self._new_slot()
                self.slots.append(slot)
            except Exception as e:
                log.warning(f"[pool] slot ready failed: {e}")
                # the profile may hold a broken guest session (stalls on a
                # ChatGPT title without a textarea). Wipe and relaunch once.
                raise
    async def start_with_wipe(self):
        """Like start(), but wipes a stale profile dir first."""
        if os.path.isdir(PROFILE_DIR):
            shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        await self.start()

    async def _new_slot(self) -> PageSlot:
        page = await self.ctx.new_page()
        slot = PageSlot(page)
        # CDP-level capture of streamed bodies via the Network domain.
        # resp.text() fails on the mobile long-poll ("partial+html" stream
        # is not buffered by CDP: Protocol error No data found). Instead we
        # subscribe to dataReceived chunks and reassemble them ourselves.
        try:
            cdp = await self.ctx.new_cdp_session(page)
            slot.cdp = cdp
            await cdp.send("Network.enable")
            slot._net = {"wanted": {}, "alias": {}}

            def _on_resp(evt):
                try:
                    req_id = evt.get("requestId") or ""
                    resp = evt.get("response") or {}
                    url = resp.get("url") or ""
                    if "conversation" in url or "sentinel" in url:
                        log.warning(f"[cdp] responseReceived {url[:80]}")
                    if "/f/conversation" in url or "/conversation/updates" in url:
                        slot.hits += 1
                        slot._net["wanted"][req_id] = ""
                        slot._net["alias"][req_id] = req_id
                except Exception:
                    pass

            def _on_data(evt):
                try:
                    req_id = evt.get("requestId") or ""
                    data = evt.get("data") or b""
                    wanted = req_id in slot._net["wanted"]
                    if not wanted:
                        return
                    if isinstance(data, str):
                        # CDP delivers dataReceived.data as base64 string
                        raw = base64.b64decode(data) if data else b""
                    else:
                        raw = bytes(data)
                    chunk = raw.decode("utf-8", "replace")
                    slot._net["wanted"][req_id] += chunk
                    # long-poll streams never fire loadingFinished — flush to
                    # the polling buffer immediately and detect completion here
                    slot.buf += chunk
                    # completion marker may span chunks — check the full body
                    full = slot._net["wanted"][req_id]
                    if "[DONE]" in full or mobile_done(full):
                        slot.done = True
                except Exception as e:
                    log.warning(f"[cdp] data err: {e}")

            def _on_done(evt):
                try:
                    req_id = evt.get("requestId") or ""
                    if req_id not in slot._net["wanted"]:
                        return
                    # data was already flushed to slot.buf chunk-by-chunk in
                    # _on_data — nothing to add here. Only ensure completion
                    # is flagged if the marker landed exactly at the end.
                    body = slot._net["wanted"].pop(req_id, "")
                    slot._net["alias"].pop(req_id, None)
                    if "[DONE]" in body or mobile_done(body):
                        slot.done = True
                except Exception as e:
                    log.warning(f"[cdp] done err: {e}")

            cdp.on("Network.responseReceived", _on_resp)
            cdp.on("Network.dataReceived", _on_data)
            cdp.on("Network.loadingFinished", _on_done)
        except Exception as e:
            log.warning(f"[slot] CDP capture init failed: {e}")
        await self._ready(slot)
        return slot

    async def _ready(self, slot: PageSlot):
        page = slot.page
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        # wait for Cloudflare challenge to resolve and prompt box to be visible.
        # A fresh profile often stalls on the first landing with a ChatGPT
        # title but no textarea — a reload (after cf_clearance cookies landed)
        # usually fixes it. So instead of failing, loop: reload and re-wait.
        for attempt in range(3):
            deadline = time.time() + CF_WAIT
            last_title = ""
            while time.time() < deadline:
                try:
                    title = await page.title()
                    if title != last_title:
                        log.info(f"[slot] title={title!r} url={page.url[:60]}")
                        last_title = title
                    if title != "Один момент…" and title not in ("", "Just a moment..."):
                        vis = await page.evaluate("""() => {
                            const el = document.querySelector('#prompt-textarea, #mobile-composer-prompt, textarea.wm-composer-textarea');
                            if (!el) return 0;
                            const r = el.getBoundingClientRect();
                            return (r.width > 50 && r.height > 20) ? 1 : 0;
                        }""")
                        if vis:
                            return True
                except Exception as e:
                    log.warning(f"[slot] check error: {e}")
                await asyncio.sleep(3)
            log.warning(f"[slot] attempt {attempt+1} timed out on title={last_title!r} — reloading")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                log.warning(f"[slot] reload after stall failed: {e}")
        raise RuntimeError(f"Cloudflare challenge did not resolve (last title={last_title!r})")

    async def acquire(self) -> PageSlot:
        deadline = time.time() + 120
        while True:
            for slot in self.slots:
                if not slot.busy:
                    slot.busy = True
                    # A page that already served a prompt cannot be reused —
                    # the second send in the same thread silently bypasses
                    # the SSE hook. Rotate the tab (fast: same context,
                    # Cloudflare already cleared).
                    if slot.used_count > 0:
                        try:
                            slot = await self._rotate_tab(slot)
                        except Exception as e:
                            log.warning(f"[pool] tab rotate failed: {e}")
                            slot.busy = False
                            return await self.acquire()
                    return slot
            if time.time() > deadline:
                raise RuntimeError("no free page slot")
            await asyncio.sleep(0.25)

    async def _rotate_tab(self, slot: PageSlot) -> PageSlot:
        """Close the used tab, open a replacement on the same (warm) context."""
        old = slot.page
        log.warning(f"[rotate] closing old page (used={slot.used_count})...")
        try:
            await old.close()
            log.warning("[rotate] old page closed")
        except Exception as e:
            log.warning(f"[rotate] old close failed: {e}")
        log.warning("[rotate] creating replacement page...")
        try:
            new_slot = await self._new_slot()
            log.warning("[rotate] replacement ready")
        except Exception as e:
            log.warning(f"[rotate] _new_slot failed: {e}")
            raise
        # preserve busy flag and counters for the caller
        new_slot.busy = True
        new_slot.used_count = 0
        self.slots[self.slots.index(slot)] = new_slot
        return new_slot

    def release(self, slot: PageSlot):
        slot.busy = False
        slot.used_count += 1
        slot.last_used = time.time()

    async def fresh(self, slot: PageSlot) -> PageSlot:
        """Rotate to a brand-new guest session on a clean profile dir.

        A stale guest session (empty streams, quota hit) lives in the
        profile cookies — creating a new page on the same profile does NOT
        help. We close the page, wipe the profile dir and relaunch the
        whole context. Cloudflare re-arms its own clearances on the fresh
        dir (few seconds to a minute).
        """
        try:
            await slot.page.close()
        except Exception:
            pass
        try:
            await self.ctx.close()
        except Exception:
            pass
        try:
            await self.pw.stop()
        except Exception:
            pass
        self.pw = None
        self.ctx = None
        self.slots = []
        self.busy_slots.clear()
        # wipe the profile dir so the guest session starts from scratch
        if os.path.isdir(PROFILE_DIR):
            try:
                shutil.rmtree(PROFILE_DIR, ignore_errors=True)
            except Exception as e:
                log.warning(f"[fresh] profile wipe failed: {e}")
        await self._start_with_retry()
        return self.slots[0]

    async def _start_with_retry(self):
        """Start the pool; if a fresh profile stalls on CF, retry with a wipe."""
        try:
            await self.start()
            return
        except Exception as e:
            log.warning(f"[pool] start failed ({e}) — wiping profile and retrying")
        if os.path.isdir(PROFILE_DIR):
            shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        await self.start()

    async def send(self, slot: PageSlot, prompt: str):
        """Type prompt, press Enter, then poll the intercepted SSE buffer.

        Yields OpenAI-style events: ('delta', text), ('stop', None), ('error', msg).
        NOTE: no page.reload() here! Reloading breaks the guest send (the app
        silently fails to post — probed). Fresh sessions come from
        pool.fresh() (full profile rotation) on empty streams instead.
        """
        page = slot.page
        log.info(f"[send] prompt {len(prompt)} chars")
        # reset the CDP buffer BEFORE sending (fast responses may start
        # streaming immediately after Enter — clearing afterwards would wipe them)
        slot.buf = ""
        slot.done = False
        slot.hits = 0
        # mobile-web composer is a plain TEXTAREA (React listens to input
        # events, keyboard.type only sets .value and never activates send);
        # desktop is a ProseMirror contenteditable DIV (needs keyboard.type).
        tag = await page.evaluate("""() => {
            const el = document.querySelector('#prompt-textarea, #mobile-composer-prompt, textarea.wm-composer-textarea');
            return el ? el.tagName : '';
        }""")
        log.info(f"[send] composer tag={tag}")
        if tag == "TEXTAREA":
            await page.evaluate("""() => { const el = document.querySelector('#prompt-textarea, #mobile-composer-prompt, textarea.wm-composer-textarea'); el.focus(); el.click(); }""")
            await asyncio.sleep(0.3)
            await page.fill("#mobile-composer-prompt, textarea.wm-composer-textarea, #prompt-textarea", prompt)
        else:
            await page.evaluate("""() => { const el = document.querySelector('#prompt-textarea, #mobile-composer-prompt, textarea.wm-composer-textarea'); el.focus(); el.click(); }""")
            await asyncio.sleep(0.3)
            await page.keyboard.type(prompt)
        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")

        known = 0
        pending = ""          # tail of the buffer without a trailing newline
        cur_path = None       # last known delta path (delta_encoding v1)
        last_activity = time.time()
        start = time.time()
        delta_count = 0
        saw_done = False
        emitted_mobile = 0
        emitted_mobile_prev = 0
        last_growth = time.time()
        while True:
            s = slot.buf or ""
            # mobile UI renders the answer into DOM blocks — poll them live
            # (CDP long-poll dataReceived is EMPTY, loadingFinished never
            # fires, so the network path is dead on mobile)
            if tag == "TEXTAREA":
                try:
                    bodytext = await page.evaluate("() => (document.body.innerText || '')")
                    try:
                        with open("/tmp/cg_innertext_last.txt", "w") as f:
                            f.write(bodytext)
                    except Exception:
                        pass
                    ans = parse_mobile_innertext(bodytext)
                    if ans:
                        last_activity = time.time()
                        if len(ans) > emitted_mobile:
                            last_growth = time.time()
                            emitted_mobile = len(ans)
                            yield ("delta", ans[emitted_mobile_prev:])
                            emitted_mobile_prev = emitted_mobile
                        elif time.time() - last_growth > MOBILE_STABLE_S:
                            # answer stopped growing -> stream finished
                            yield ("stop", None)
                            return
                    else:
                        # No usable answer text. If the said-marker AND a
                        # footer/UI anchor are both already rendered, the turn
                        # is OVER and the model produced nothing (stale guest
                        # session) — fail fast instead of waiting the full
                        # idle timeout.
                        sm = _last_said(bodytext)
                        if sm and _TXT_STOP.search(bodytext, sm.end()) \
                                and time.time() - last_growth > MOBILE_STABLE_S:
                            log.warning("[send] mobile: said-marker + footer present, answer EMPTY — stale guest session")
                            yield ("empty", None)
                            yield ("stop", None)
                            return
                        # still waiting for the reply to start; do NOT refresh
                        # last_activity here — the generic idle timeout below
                        # must be able to fire
                except Exception as e:
                    log.warning(f"[send] DOM poll failed: {e}")
            # desktop: SSE stream arrives via slot.buf
            # debug: if we saw a conversation POST but never accumulated bytes
            if slot.hits > 0 and len(s) == 0 and slot.done:
                log.warning("[send] fetch hit=%d but stream empty (done=%s)", slot.hits, slot.done)
                yield ("error", "stream intercept returned empty body")
                return
            if len(s) > known:
                last_activity = time.time()
                # mobile: whole HTML body arrives at once, parse blocks directly
                if is_mobile_body(s):
                    mobile_texts = parse_mobile_html(s)
                    if len(mobile_texts) > emitted_mobile:
                        for txt in mobile_texts[emitted_mobile:]:
                            delta_count += 1
                            yield ("delta", txt)
                        emitted_mobile = len(mobile_texts)
                    if mobile_done(s):
                        if delta_count == 0:
                            log.warning(f"[send] EMPTY mobile stream! fetchHits={slot.hits} buflen={len(s)}")
                        else:
                            try:
                                with open("/tmp/cg_mobile_dump.txt", "w") as f:
                                    f.write(s)
                            except Exception:
                                pass
                        yield ("stop", None)
                        return
                    known = len(s)
                    if time.time() - last_activity > IDLE_TIMEOUT:
                        yield ("error", "generation stalled")
                        return
                    if time.time() - start > TOTAL_TIMEOUT:
                        yield ("error", "generation timeout")
                        return
                    await asyncio.sleep(SSE_POLL_MS / 1000)
                    continue
                new = s[known:]
                buf = pending + new
                lines = buf.split("\n")
                pending = lines.pop()   # may be a partial line — keep for next poll
                known = len(s) - len(pending)
                for line in lines:
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    chunks, cur_path = parse_delta_ops(payload, cur_path)
                    for txt in chunks:
                        delta_count += 1
                        yield ("delta", txt)
                    if stream_has_marker(payload, "last_token"):
                        yield ("marker_last", None)
                    if stream_is_done(payload):
                        saw_done = True
                        if delta_count == 0:
                            log.warning(f"[send] EMPTY stream! fetchHits={slot.hits} done={slot.done} buflen={len(s)} tail={s[-800:]!r}")
                            try:
                                with open("/tmp/cg_dump.txt", "w") as f:
                                    f.write(s)
                                log.warning(f"[send] dumped full stream to /tmp/cg_dump.txt ({len(s)} chars)")
                            except Exception as e:
                                log.warning(f"[send] dump failed: {e}")
                            yield ("empty", None)
                        yield ("stop", None)
                        return
            if time.time() - last_activity > IDLE_TIMEOUT:
                yield ("error", "generation stalled")
                return
            if time.time() - start > TOTAL_TIMEOUT:
                yield ("error", "generation timeout")
                return
            await asyncio.sleep(SSE_POLL_MS / 1000)

    async def stop(self):
        if self.ctx:
            await self.ctx.close()
        if self.pw:
            await self.pw.stop()
# ─── Prompt Building ────────────────────────────────────────────────────────
# Tool-calls over a text-only textarea: the tool schemas are injected as text,
# the model is instructed to emit <tool_call>{"name":..,"arguments":{..}}
# </tool_call> and the proxy parses those tags back into native OpenAI
# tool_calls. Same pattern Qwen/Hermes use for text-only backends.
_TOOL_CALL_RE = _re.compile(
    r'<\s*tool_call\s*>\s*(.*?)\s*<\s*/\s*tool_call\s*>', _re.S | _re.I)

# Built-in ChatGPT search leaks through when the model ignores the protocol
# and uses its own web tool instead of our function tags.
_TOOL_LEAK_MARKERS = ("Поиск в интернете", "Searching the web", "Источники",
                      "timeanddate", "Time and Date", "Searched", "Веб-поиск",
                      "accuweather", "AccuWeather", "weather.com", "OpenStreetMap",
                      "Карты", "Learn more")

def _leaked_builtin(content: str) -> bool:
    if any(m in content for m in _TOOL_LEAK_MARKERS):
        return True
    # structural detector: native search/widget cards render as soup of tiny
    # lines ("27°", "C", "/", "F", weekday names) — real prose never does
    short = 0
    for ln in content.splitlines():
        s = ln.strip()
        if not s:
            continue
        if len(s) <= 4 or s.endswith("°") or s.rstrip("°CF/").isdigit():
            short += 1
            if short >= 8:
                return True
    return False

def _strip_leak(text: str) -> str:
    """Drop lines polluted by ChatGPT's built-in search cards."""
    lines = [ln for ln in text.splitlines()
             if not any(m in ln for m in _TOOL_LEAK_MARKERS)]
    out = "\n".join(lines).strip()
    if not out:
        # whole answer lived on marker-polluted line(s): excise just the
        # phrases so honest prose survives
        out = text
        for m in sorted(_TOOL_LEAK_MARKERS, key=len, reverse=True):
            out = out.replace(m, "")
        out = out.lstrip("… .·").strip()
    return out or text

def _extract_tool_calls(text: str):
    """Return (clean_content, [native tool_call dicts]). Non-<tool_call> text
    is kept as content; malformed JSON blocks are dropped silently."""
    calls = []

    def _sub(m):
        raw = m.group(1).strip()
        if raw.startswith("```"):                      # stray md fence
            raw = raw.strip("`").lstrip("json").strip()
        try:
            obj = json.loads(raw)
        except Exception:
            log.warning("[tools] unparsable tool_call: %r", raw[:200])
            return ""
        if not isinstance(obj, dict):
            return ""
        name = obj.get("name") or obj.get("function")
        args = obj.get("arguments", obj.get("parameters", {}))
        if not name:
            return ""
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        calls.append({
            "id": "call_" + uuid.uuid4().hex[:24],
            "type": "function",
            "function": {"name": str(name), "arguments": args},
        })
        return ""

    clean = _TOOL_CALL_RE.sub(_sub, text).strip()
    return clean, calls

def _tools_instruction(tools: list, tool_choice) -> str:
    """Render the tool-use instruction block appended to the prompt."""
    lines = [
        "",
        "[TOOL PROTOCOL — STRICT]",
        "This session runs over a TEXT-ONLY bridge. Your BUILT-IN tools (web",
        "search, browsing, widgets, image gen, code interpreter) are DISABLED:",
        "do NOT use them, do NOT browse, do NOT render weather/search cards.",
        "The ONLY way to access external data is the function protocol below.",
        "",
        "To CALL a function, your ENTIRE reply must be exactly one line:",
        '<tool_call>{"name": "<tool name>", "arguments": {<json object>}}</tool_call>',
        "",
        "Example — user asks: «Какая погода в Париже?» (tool get_weather exists):",
        'correct reply: <tool_call>{"name": "get_weather", "arguments": {"city": "Paris"}}</tool_call>',
        "",
        "Rules:",
        "- ONE call per reply. NO prose before or after the tag.",
        "- arguments MUST be valid JSON matching the tool's parameters schema.",
        "- When a [Tool result ...] message arrives, continue and produce the",
        "  final PLAIN-TEXT answer WITHOUT any tag.",
        "- If none of the listed tools helps, answer normally in plain text.",
        "- NEVER say you cannot access external data — you CAN, via the tools.",
        "- Never invent tool results.",
    ]
    if isinstance(tool_choice, dict):                    # forced specific tool
        fname = ((tool_choice.get("function") or {}).get("name")) or ""
        if fname:
            lines.append(f'- You MUST call the tool "{fname}" now, even if you '
                         f'could answer yourself.')
            lines.append(f'REMINDER (final): your next reply must be ONLY the '
                         f'line <tool_call>{{"name": "{fname}", "arguments": '
                         f'{{...}}}}</tool_call> and nothing else.')
    elif tool_choice == "required":
        lines.append("- You MUST call one of the tools now.")
        lines.append('REMINDER (final): your next reply must be ONLY a '
                     '<tool_call>...</tool_call> line.')
    lines.append("")
    lines.append("Available functions:")
    for t in tools:
        fn = (t or {}).get("function") or {}
        desc = {
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        }
        lines.append(json.dumps(desc, ensure_ascii=False))
    return "\n".join(lines)

def _forced_override(fname: str, level: int) -> str:
    """Escalating anti-search blocks for forced tool_choice. The guest loves
    its built-in web search; identical retries never change that, so each
    retry must speak LOUDER, not just repeat."""
    if level <= 0:
        return ""
    base = (
        "[FUNCTION-CALL OVERRIDE — HIGHEST PRIORITY]\n"
        f"Your next reply MUST be exactly ONE line:\n"
        f'<tool_call>{{"name": "{fname}", "arguments": {{...}}}}</tool_call>\n'
        "FORBIDDEN: searching the web, browsing, weather/search cards, any\n"
        "prose, any answer text. Do NOT answer the question yourself.\n"
        "The user's client executes the function and returns real data —\n"
        "your text reply is DISCARDED by the harness unless it is the tag.")
    hard = (
        "\nTHIS IS ATTEMPT 3 OF THE HARNESS. Previous replies were rejected as\n"
        "protocol violations. A search card or prose = total failure. Output\n"
        f"the single <tool_call> line for {fname} and NOTHING else.")
    return base + (hard if level >= 2 else "")

def _build_prompt(messages: list[dict], tools: Optional[list] = None,
                  tool_choice=None, escalation: int = 0) -> str:
    """Convert OpenAI messages format to plain text for the guest page.

    Mirrors qwenmode: system prompt goes first as [System], the platform
    MEDIA: delivery rule is appended so files come back as attachments,
    and history (user/assistant) is kept in order — the guest chat box
    receives the whole context in one paste, not just the last user turn.
    """
    # Platform rule appended to the system prompt so the model delivers
    # files via the MEDIA: marker instead of pasting file contents as code.
    MEDIA_DELIVERY_RULE = (
        "\n\nCRITICAL PLATFORM RULE (always follow): if the user asked to receive "
        "a file you created, deliver it by ending your final reply with exactly one "
        "line: MEDIA:/absolute/path/to/file (no code block, no explanation inside). "
        "The platform turns that line into a file attachment. NEVER paste file "
        "contents as a code block when a MEDIA: line is possible."
    )
    parts = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")

        if role == "system":
            text = str(content) if content else ""
            if len(text) > 20000:
                text = text[:20000] + "\n...[system truncated]"
            if text:
                parts.append(f"[System]\n{text}{MEDIA_DELIVERY_RULE}")

        elif role == "user":
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                text = "\n".join(t for t in texts if t)
            else:
                text = str(content)
            if len(text) > 4000:
                text = text[:4000] + "\n...[user message truncated]"
            if text:
                parts.append(f"[User]\n{text}")

        elif role == "assistant":
            text = ""
            if isinstance(content, str):
                text = content
            tcs = m.get("tool_calls") or []
            if tcs:
                called = "; ".join(
                    f'{(tc.get("function") or {}).get("name", "?")}('
                    f'{(tc.get("function") or {}).get("arguments", "")})'
                    for tc in tcs if isinstance(tc, dict))
                head = f"[Assistant] (you called: {called})"
                parts.append(head + ("\n" + text if text else ""))
            elif text:
                parts.append(f"[Assistant]\n{text}")

        elif role == "tool":
            name = m.get("name") or m.get("tool_call_id") or "tool"
            if isinstance(content, list):
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                text = "\n".join(t for t in texts if t)
            else:
                text = str(content)
            if len(text) > 8000:
                text = text[:8000] + "\n...[tool result truncated]"
            parts.append(f"[Tool result ({name})]\n{text}")

    prompt = "\n\n".join(parts)
    if tools:
        # primacy+recency sandwich: short notice on top, full protocol at end
        head = ("[HARNESS NOTICE] This conversation is processed by an "
                "automated function-calling harness simulating an "
                "OpenAI-compatible API endpoint. Assistant replies are parsed "
                "PROGRAMMATICALLY. In this simulation the functions listed at "
                "the end of this message DO exist, DO work and return results. "
                "NEVER claim you lack access to external data — you access it "
                "via <tool_call>. Follow the TOOL PROTOCOL EXACTLY.")
        prompt = head + "\n\n" + prompt + _tools_instruction(tools, tool_choice)
        forced_name = ""
        if isinstance(tool_choice, dict):
            forced_name = ((tool_choice.get("function") or {}).get("name")) or ""
        elif tool_choice == "required":
            forced_name = (tools[0] or {}).get("function", {}).get("name", "")
        if forced_name and escalation > 0:
            prompt += "\n\n" + _forced_override(forced_name, escalation)
    return prompt


# ─── FastAPI app ────────────────────────────────────────────────────────────
app = FastAPI(title="chatgpt-guest")
pool: Optional[Pool] = None

async def _noop():
    return None

def _authorized(req: Request) -> bool:
    if not API_KEY:
        return True
    auth = req.headers.get("Authorization", "")
    return auth == f"Bearer {API_KEY}"

@app.get("/health")
async def health():
    if pool is None:
        return {"status": "not_ready"}
    return {"status": "ok", "pages": pool.size}

@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [{"id": MODEL_NAME, "object": "model",
            "created": 1700000000, "owned_by": "chatgpt-guest"}]}

def _fmt_chunk(cid: str, delta: dict, finish=None) -> str:
    return "data: " + json.dumps({
        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }, ensure_ascii=False) + "\n\n"

@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    if not _authorized(req):
        return JSONResponse({"error": {"message": "invalid api key"}}, status_code=401)
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": {"message": "bad json"}}, status_code=400)
    messages = body.get("messages") or []
    stream = bool(body.get("stream", False))
    tools = body.get("tools") or []
    tool_choice = body.get("tool_choice", "auto")
    has_tools = bool(tools) and tool_choice != "none"
    forced = has_tools and tool_choice != "auto"   # dict / "required"
    if forced:
        # Escalating ladder: identical retries don't move the guest off its
        # built-in search — attempt N must speak LOUDER (_forced_override).
        prompts = [_build_prompt(messages, tools, tool_choice, escalation=n)
                   for n in range(3)]
    else:
        prompts = [_build_prompt(messages, tools if has_tools else None,
                                 tool_choice)]
    if not prompts[0]:
        return JSONResponse({"error": {"message": "no user message"}}, status_code=400)

    slot = await pool.acquire()
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    async def _retry_fresh(cur, attempt):
        """Rotate to a brand-new guest session between attempts."""
        log.warning(f"[api] attempt {attempt + 1}/{MAX_ATTEMPTS} failed — wiping profile, retrying")
        try:
            return await pool.fresh(cur)
        except Exception as e:
            log.warning(f"[api] fresh() failed: {e}")
            return cur

    async def gen():
        nonlocal slot
        released = False
        try:
            for attempt in range(MAX_ATTEMPTS):
                acc: list[str] = []          # buffered deltas (tools mode)
                live_emitted = False         # any delta already sent (live mode)
                hard = True                  # True -> wipe profile on retry
                agen = pool.send(slot, prompts[min(attempt, len(prompts) - 1)])
                try:
                    while True:
                        try:
                            ev = await agen.__anext__()
                        except StopAsyncIteration:
                            break
                        kind, payload = ev
                        if kind == "delta":
                            if has_tools:
                                acc.append(payload)   # buffer — tool tags may land anywhere
                            else:
                                live_emitted = True
                                yield _fmt_chunk(cid, {"content": payload})
                        elif kind == "error":
                            log.warning(f"[api] stream attempt {attempt + 1} error: {payload}")
                            break
                        elif kind == "stop":
                            break
                        # 'empty' events fall through: retry decided below
                except Exception as e:
                    log.warning(f"[api] stream attempt {attempt + 1} exception: {e}")
                finally:
                    try:
                        await agen.aclose()
                    except Exception:
                        pass
                pool.release(slot)
                released = True

                if has_tools:
                    clean, calls = _extract_tool_calls("".join(acc))
                    if calls:
                        # native OpenAI streaming shape: id/name chunk, then args
                        for i, tc in enumerate(calls):
                            yield _fmt_chunk(cid, {"tool_calls": [{
                                "index": i, "id": tc["id"], "type": "function",
                                "function": {"name": tc["function"]["name"],
                                             "arguments": ""}}]})
                            yield _fmt_chunk(cid, {"tool_calls": [{
                                "index": i,
                                "function": {"arguments": tc["function"]["arguments"]}}]})
                        yield _fmt_chunk(cid, {}, "tool_calls")
                        yield "data: [DONE]\n\n"
                        return
                    if clean.strip() and not forced:
                        if _leaked_builtin(clean) and attempt == 0:
                            # one light retry on builtin-search leak
                            log.warning("[api] stream leak — one light retry")
                            hard = False
                        else:
                            yield _fmt_chunk(cid, {"role": "assistant",
                                                   "content": _strip_leak(clean)})
                            yield _fmt_chunk(cid, {}, "stop")
                            yield "data: [DONE]\n\n"
                            return
                    elif clean.strip():
                        hard = False      # refusal, page is fine -> light retry
                else:
                    if live_emitted:
                        yield _fmt_chunk(cid, {}, "stop")
                        yield "data: [DONE]\n\n"
                        return
                if attempt < MAX_ATTEMPTS - 1:
                    if hard:
                        slot = await _retry_fresh(slot, attempt)
                    else:
                        log.warning("[api] protocol violation — light retry (rotate tab)")
                        try:
                            slot = await pool._rotate_tab(slot)
                        except Exception as e:
                            log.warning(f"[api] light rotate failed: {e}")
                    released = False
            yield 'data: {"error": {"message": "generation failed after %d attempts"}}\n\n' % MAX_ATTEMPTS
            yield _fmt_chunk(cid, {}, "stop")
            yield "data: [DONE]\n\n"
        finally:
            if not released:
                pool.release(slot)

    if stream:
        return StreamingResponse(gen(), media_type="text/event-stream")

    # non-stream: accumulate; retry on a fresh profile until real content or
    # attempts are exhausted
    last_err = None
    leak_retried = False
    last_content = None
    for attempt in range(MAX_ATTEMPTS):
        content = ""
        err = None
        hard = True                  # True -> wipe profile on retry
        agen = pool.send(slot, prompts[min(attempt, len(prompts) - 1)])
        try:
            while True:
                try:
                    ev = await agen.__anext__()
                except StopAsyncIteration:
                    break
                kind, payload = ev
                if kind == "delta":
                    content += payload
                elif kind == "error":
                    err = payload
                    break
                elif kind == "stop":
                    break
        except Exception as e:
            log.warning(f"[api] attempt {attempt + 1} exception: {e}")
            err = str(e)
        finally:
            try:
                await agen.aclose()
            except Exception:
                pass
        pool.release(slot)
        if err:
            last_err = err
        if not err and content.strip():
            last_content = content
            msg: dict = {"role": "assistant"}
            fin = "stop"
            do_retry = False
            clean, calls = (_extract_tool_calls(content)
                            if has_tools else (content, []))
            forced = tool_choice != "auto" and has_tools   # dict / "required"
            bad = forced   # only a refused forced-call burns attempts
            if calls:
                msg["content"] = clean or None
                msg["tool_calls"] = calls
                fin = "tool_calls"
            elif bad:
                # harness demands a call, model refused -> LIGHT retry
                last_err = f"forced call refused: {content[:120]!r}"
                hard = False
                do_retry = True
            elif not has_tools:
                msg["content"] = content
            elif _leaked_builtin(content) and not leak_retried \
                    and attempt < MAX_ATTEMPTS - 1:
                # built-in search card leaked through: ONE light retry, then
                # accept the cleaned text (never loop / never 502 on leaks)
                leak_retried = True
                last_err = "builtin search leak — one light retry"
                hard = False
                do_retry = True
            else:
                # auto mode: free-text answer; strip builtin-search card junk
                # (never return an empty string — keep raw if stripping ate all)
                msg["content"] = _strip_leak(content) or content
            if fin == "tool_calls" or not do_retry:
                return {
                    "id": cid, "object": "chat.completion", "created": int(time.time()),
                    "model": MODEL_NAME,
                    "choices": [{"index": 0, "message": msg, "finish_reason": fin}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
            # forced-refusal / leak-retry -> fall through to the retry loop
        if not err and last_err is None:
            last_err = "empty generation"
        if attempt < MAX_ATTEMPTS - 1:
            if hard:
                slot = await _retry_fresh(slot, attempt)
            else:
                log.warning("[api] protocol violation — light retry (rotate tab)")
                try:
                    slot = await pool._rotate_tab(slot)
                except Exception as e:
                    log.warning(f"[api] light rotate failed: {e}")
    # forced calls exhausted all attempts: a bare 502 kills agent loops —
    # degrade gracefully to the model's text reply if we have one
    if last_err and str(last_err).startswith("forced call refused") \
            and last_content:
        return {
            "id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": MODEL_NAME,
            "choices": [{"index": 0,
                         "message": {"role": "assistant",
                                     "content": _strip_leak(last_content)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    return JSONResponse({"error": {"message": last_err or "generation failed"}},
                        status_code=502)

if __name__ == "__main__":
    import uvicorn
    port = PORT
    if len(sys.argv) > 2 and sys.argv[1] == "--server":
        port = int(sys.argv[2])
    async def _boot():
        global pool
        pool = Pool(POOL_SIZE)
        await pool._start_with_retry()
        log.info(f"chatgpt-guest ready on :{port} (model {MODEL_NAME})")

        async def _warmup():
            """First real request after a cold start often stalls ~90s
            (CF/sentinel warm-up). Fire a throwaway generation so the first
            client request lands on a hot page."""
            await asyncio.sleep(2)
            try:
                slot = await pool.acquire()
                try:
                    agen = pool.send(slot, "Ответь одним словом: OK")
                    async for _ in agen:
                        pass
                finally:
                    pool.release(slot)
                log.info("[warmup] done — page hot")
            except Exception as e:
                log.warning(f"[warmup] failed (non-fatal): {e}")

        asyncio.get_event_loop().create_task(_warmup())
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(cfg)
        await server.serve()
    asyncio.run(_boot())

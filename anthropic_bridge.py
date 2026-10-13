"""Shared Anthropic Messages API <-> OpenAI chat translation.

Used by both forwarders (9000 opencode/zen, 9001 kilo/OpenRouter) to serve
Claude Code and other Anthropic-native clients without duplicating Tor,
CLI-emulation or retry logic: each forwarder converts the Anthropic request
to its own OpenAI/chat shape, reuses its existing upstream pipeline, then
converts the result back.

Pure stdlib, no framework deps. Streaming translators consume an iterable
of decoded SSE lines and emit via a callback, so both BaseHTTPRequestHandler
forwarders (and any future ASGI service) can share them.
"""

import hashlib
import json
import os
import secrets
import string
import time

_ANTHROPIC_ID_ALPHABET = string.ascii_letters + string.digits

_THINKING_CAPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "thinking_caps.json")
_THINKING_CAPS_CACHE = {"loaded": False, "caps": {}}


def get_thinking_cap(model):
    """Learned max thinking budget for a model (None = unknown/unlimited)."""
    try:
        if not _THINKING_CAPS_CACHE["loaded"]:
            with open(_THINKING_CAPS_FILE) as f:
                _THINKING_CAPS_CACHE["caps"] = json.load(f)
            _THINKING_CAPS_CACHE["loaded"] = True
        return _THINKING_CAPS_CACHE["caps"].get(model)
    except Exception:
        _THINKING_CAPS_CACHE["loaded"] = True
        return None


def note_thinking_cap(model, budget):
    """Persist a lowered thinking cap after upstream rejected the budget."""
    try:
        caps = dict(_THINKING_CAPS_CACHE.get("caps") or {})
        try:
            with open(_THINKING_CAPS_FILE) as f:
                caps.update(json.load(f))
        except Exception:
            pass
        prev = caps.get(model)
        if prev is None or budget < prev:
            caps[model] = int(budget)
            with open(_THINKING_CAPS_FILE, "w") as f:
                json.dump(caps, f)
            _THINKING_CAPS_CACHE["caps"] = caps
            return True
    except Exception:
        pass
    return False


def parse_thinking(req):
    """Anthropic thinking param -> (effort_str|None, budget_int|None).

    Accepts {"type":"enabled","budget_tokens":N} (Claude Code) and the
    OpenAI-style {"effort": "low|medium|high|xhigh"} passthrough.
    """
    th = req.get("thinking")
    if not isinstance(th, dict):
        return None, None
    if th.get("type") == "enabled":
        try:
            b = int(th.get("budget_tokens", 0))
        except Exception:
            b = 0
        if b > 0:
            if b <= 4000:
                effort = "low"
            elif b <= 12000:
                effort = "medium"
            elif b <= 32000:
                effort = "high"
            else:
                effort = "xhigh"
            return effort, b
        return None, None
    if isinstance(th.get("effort"), str) and th["effort"] in (
            "low", "medium", "high", "xhigh", "max"):
        return th["effort"], None
    return None, None


def clamp_thinking_budget(model, budget):
    """Apply learned cap: returns (budget, clamped_bool)."""
    cap = get_thinking_cap(model)
    if cap is not None and budget is not None and budget > cap:
        return cap, True
    return budget, False


def new_anthropic_msg_id():
    return "msg_" + "".join(secrets.choice(_ANTHROPIC_ID_ALPHABET) for _ in range(24))


def new_tool_id():
    return "toolu_" + "".join(secrets.choice(_ANTHROPIC_ID_ALPHABET) for _ in range(24))


def anthropic_error_body(err_type, message):
    return json.dumps({"type": "error", "error": {"type": err_type, "message": message}}).encode()


# ---------------------------------------------------------------------------
# Anthropic request -> OpenAI chat
# ---------------------------------------------------------------------------

def system_text(system):
    """Anthropic system (str | list[block] | None) -> plain str."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for b in system:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return ""


def _flatten_text_content(content):
    """tool_result content (str | list | None) -> str."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    parts.append(b["text"])
                elif isinstance(b.get("text"), str):
                    parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return str(content)


def _image_to_openai(block):
    src = block.get("source") or {}
    st = src.get("type")
    if st == "base64" and src.get("data"):
        mime = src.get("media_type", "image/png")
        return {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, src["data"])}}
    if st == "url" and src.get("url"):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None


def anthropic_messages_to_openai(messages):
    """Anthropic messages[] -> OpenAI messages[] (splits tool_result/tool_use)."""
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            if role in ("user", "system"):
                out.append({"role": "user" if role == "user" else "system", "content": content})
            else:
                out.append({"role": "assistant", "content": content})
            continue
        if not isinstance(content, list):
            out.append({"role": role if role in ("user", "assistant", "system") else "user",
                        "content": str(content or "")})
            continue
        texts, images, tool_uses, tool_results = [], [], [], []
        for b in content:
            if not isinstance(b, dict):
                if isinstance(b, str) and b:
                    texts.append(b)
                continue
            t = b.get("type")
            if t == "text" and isinstance(b.get("text"), str):
                texts.append(b["text"])
            elif t == "image":
                img = _image_to_openai(b)
                if img:
                    images.append(img)
            elif t == "tool_use":
                tool_uses.append(b)
            elif t == "tool_result":
                tool_results.append(b)
        if role == "user":
            if texts or images:
                parts = [{"type": "text", "text": "\n".join(texts)}] if texts else []
                parts.extend(images)
                if len(parts) == 1 and parts[0].get("type") == "text":
                    out.append({"role": "user", "content": parts[0]["text"]})
                else:
                    out.append({"role": "user", "content": parts})
            for tr in tool_results:
                cid = tr.get("tool_use_id") or ""
                ctext = _flatten_text_content(tr.get("content"))
                if tr.get("is_error") and ctext and not ctext.startswith("Error:"):
                    ctext = "Error: " + ctext
                out.append({"role": "tool", "tool_call_id": cid, "content": ctext})
            if not texts and not images and not tool_results:
                out.append({"role": "user", "content": ""})
        elif role == "assistant":
            text = "\n".join(texts)
            if tool_uses:
                tcs = []
                for tu in tool_uses:
                    try:
                        args = json.dumps(tu.get("input") if isinstance(tu.get("input"), dict) else {})
                    except Exception:
                        args = "{}"
                    tcs.append({"id": tu.get("id") or new_tool_id(), "type": "function",
                                "function": {"name": tu.get("name") or "unknown_tool",
                                             "arguments": args}})
                out.append({"role": "assistant", "content": text,
                            "tool_calls": tcs})
            else:
                out.append({"role": "assistant", "content": text})
        else:
            if texts:
                out.append({"role": role, "content": "\n".join(texts)})
    return out


def anthropic_tools_to_openai(tools):
    if not isinstance(tools, list) or not tools:
        return None
    out = []
    for t in tools:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        schema = t.get("input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        out.append({"type": "function", "function": {
            "name": t["name"],
            "description": t.get("description") or "",
            "parameters": schema,
        }})
    return out or None


def anthropic_tool_choice_to_openai(tc):
    if tc is None:
        return None
    if isinstance(tc, dict):
        t = tc.get("type", "auto")
        if t == "auto":
            return "auto"
        if t == "any":
            return "required"
        if t == "none":
            return "none"
        if t == "tool" and tc.get("name"):
            return {"type": "function", "function": {"name": tc["name"]}}
        return "auto"
    return "auto"


def normalize_model_id(model, default_model, known_models=None):
    """Strip vendor prefixes; substitute default for unknown ids.

    known_models=None -> passthrough (kilo: OpenRouter ids are open-ended).
    """
    m = (model or "").strip()
    if m.startswith("opencode/"):
        m = m[len("opencode/"):]
    if not m:
        return default_model, True
    if known_models is not None and m not in known_models:
        return default_model, True
    return m, False


def anthropic_to_chat(req, default_model, known_models=None):
    """Full Anthropic request -> (chat_body, client_stream, req_model, up_model)."""
    if not isinstance(req, dict):
        raise ValueError("request body must be a JSON object")
    chat = {}
    req_model = req.get("model") or default_model
    up_model, substituted = normalize_model_id(req_model, default_model, known_models)
    chat["model"] = up_model
    try:
        max_tokens = int(req.get("max_tokens", 4096))
    except Exception:
        raise ValueError("'max_tokens' must be an integer")
    if max_tokens < 1:
        raise ValueError("'max_tokens' must be >= 1")
    chat["max_tokens"] = max_tokens
    sys_text = system_text(req.get("system"))
    messages = anthropic_messages_to_openai(req.get("messages"))
    if sys_text:
        messages.insert(0, {"role": "system", "content": sys_text})
    chat["messages"] = messages
    client_stream = bool(req.get("stream", False))
    chat["stream"] = client_stream
    if isinstance(req.get("temperature"), (int, float)):
        chat["temperature"] = req["temperature"]
    if isinstance(req.get("top_p"), (int, float)):
        chat["top_p"] = req["top_p"]
    if isinstance(req.get("stop_sequences"), list) and req["stop_sequences"]:
        chat["stop"] = req["stop_sequences"]
    tools = anthropic_tools_to_openai(req.get("tools"))
    if tools:
        chat["tools"] = tools
    tc = anthropic_tool_choice_to_openai(req.get("tool_choice"))
    if tc is not None:
        chat["tool_choice"] = tc
    effort, budget = parse_thinking(req)
    thinking = None
    if effort or budget is not None:
        thinking = {"effort": effort or "medium"}
        if budget is not None:
            thinking["budget_tokens"] = budget
        chat["_anthropic_thinking"] = thinking
    return chat, client_stream, (req.get("model") or default_model), up_model, thinking


def conversation_key(req):
    """Stable per-conversation key for sticky upstream session pinning.

    Full history is resent every turn, so hash model + system + first user
    text: stable across turns of one conversation, distinct across chats.
    (Random session ids per request break sealed-reasoning replay.)
    """
    try:
        model = req.get("model") or ""
        sys = system_text(req.get("system"))
        first_user = ""
        for m in req.get("messages") or []:
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                first_user = c if isinstance(c, str) else _flatten_text_content(c)
                break
        raw = "%s\x00%s\x00%s" % (model, sys[:500], first_user[:500])
        return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]
    except Exception:
        return "fallback"


# ---------------------------------------------------------------------------
# OpenAI result -> Anthropic response
# ---------------------------------------------------------------------------

def map_finish_to_stop(finish):
    if finish in ("length", "max_tokens"):
        return "max_tokens"
    if finish == "tool_calls":
        return "tool_use"
    return "end_turn"


def _norm_tool_call_id(tc_id):
    if isinstance(tc_id, str) and tc_id:
        return tc_id
    return new_tool_id()


def _norm_tool_name(name):
    """Strip vendor prefixes ('functions.foo' -> 'foo', 'foo/1' -> 'foo')."""
    if not isinstance(name, str) or not name:
        return ""
    n = name.strip()
    if "." in n and "/" not in n:
        n = n.rsplit(".", 1)[-1]
    if "/" in n:
        n = n.rsplit("/", 1)[-1]
    return n.strip()


def _norm_tool_args(args):
    """arguments (str JSON | dict | list | None) -> JSON str (never raises)."""
    if args is None or args == "":
        return "{}"
    if isinstance(args, dict):
        try:
            return json.dumps(args)
        except Exception:
            return "{}"
    if isinstance(args, list):
        try:
            return json.dumps(args)
        except Exception:
            return "{}"
    if isinstance(args, str):
        s = args.strip()
        if not s:
            return "{}"
        try:
            json.loads(s)
            return s
        except Exception:
            try:
                return json.dumps(s)
            except Exception:
                return "{}"
    try:
        return json.dumps(args)
    except Exception:
        return "{}"


def normalize_openai_tool_calls(raw, _for_stream_merge=False):
    """Any OpenAI tool_calls shape -> clean [{id,type,function:{name,arguments}}].

    Covers: list in delta or message, function as dict or JSON string,
    arguments as str/dict/list/None, missing ids, prefixed names.

    _for_stream_merge=True: arguments are streaming FRAGMENTS — kept raw
    (no per-fragment JSON validation: '{"a":' is a fine fragment but not
    valid JSON, and wrapping it in quotes would corrupt the merge).
    """
    out = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return out
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        # some proxies nest: {"tool_call": {...}} or {"function_call": {...}}
        if "function" not in tc and isinstance(tc.get("tool_call"), dict):
            tc = tc["tool_call"]
        if "function" not in tc and isinstance(tc.get("function_call"), dict):
            tc = {"id": tc.get("id"), "type": "function",
                  "function": tc["function_call"]}
        fn = tc.get("function")
        if isinstance(fn, str):
            try:
                fn = json.loads(fn)
            except Exception:
                fn = {"name": fn}
        if not isinstance(fn, dict):
            fn = {}
        name = _norm_tool_name(fn.get("name"))
        if not name:
            # skip nameless fragments (arg-only continuation chunks are
            # merged by the caller via index/id, not here)
            if not tc.get("id"):
                continue
            name = "unknown_tool"
        if _for_stream_merge and isinstance((fn or {}).get("arguments"), str):
            args_raw = fn.get("arguments") or ""
        else:
            args_raw = _norm_tool_args(fn.get("arguments"))
        out.append({"id": _norm_tool_call_id(tc.get("id")),
                    "type": "function",
                    "index": tc.get("index"),
                    "function": {"name": name,
                                 "arguments": args_raw}})
    return out


def validate_tool_names(calls, declared_tools):
    """Split calls into (known, unknown) against Anthropic-declared tool names.

    declared_tools: list of OpenAI-shape tools (or None = accept all).
    Unknown calls are returned separately so the caller can render them
    as text instead of emitting tool_use Claude Code can't resolve.
    """
    if not declared_tools:
        return calls, []
    known_names = set()
    for t in declared_tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        n = fn.get("name") if isinstance(fn, dict) else None
        if n:
            known_names.add(_norm_tool_name(n))
    # CLI decoys (bash/edit/read) are always acceptable upstream-side names;
    # they never come from the model as tool_use, but keep them known so a
    # model echoing them isn't misclassified.
    known_names.update(("bash", "edit", "read"))
    known, unknown = [], []
    for c in calls:
        n = _norm_tool_name((c.get("function") or {}).get("name"))
        c["function"]["name"] = n or "unknown_tool"
        if n in known_names:
            known.append(c)
        else:
            unknown.append(c)
    return known, unknown


def unknown_calls_to_text(unknown):
    """Render unknown tool calls as a human-readable text fragment."""
    parts = []
    for c in unknown:
        fn = c.get("function") or {}
        parts.append("[tool %s %s]" % (fn.get("name") or "unknown",
                                       fn.get("arguments") or "{}"))
    return "\n".join(parts)


def openai_message_to_blocks(msg, declared_tools=None):
    """OpenAI assistant message -> (content_blocks, has_tool_use).

    declared_tools: OpenAI-shape tools from the request (or None) — calls
    to undeclared names are rendered as text, not tool_use.
    """
    blocks = []
    content = msg.get("content")
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        # some proxies return content as parts list
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
                blocks.append({"type": "text", "text": p["text"]})
            elif isinstance(p, str) and p:
                blocks.append({"type": "text", "text": p})
    calls = normalize_openai_tool_calls(msg.get("tool_calls"))
    # tool_calls may also arrive nested in message.function_call (legacy)
    if not calls and isinstance(msg.get("function_call"), dict):
        fc = msg["function_call"]
        calls = normalize_openai_tool_calls(
            [{"id": msg.get("id"), "type": "function",
              "function": {"name": fc.get("name"),
                           "arguments": fc.get("arguments")}}])
    known, unknown = validate_tool_names(calls, declared_tools)
    if unknown:
        blocks.append({"type": "text",
                       "text": unknown_calls_to_text(unknown)})
    for tc in known:
        fn = tc.get("function") or {}
        args = fn.get("arguments", "{}")
        try:
            parsed = json.loads(args) if isinstance(args, str) else (args or {})
            if not isinstance(parsed, dict):
                parsed = {}
        except Exception:
            parsed = {}
        blocks.append({"type": "tool_use", "id": tc.get("id") or new_tool_id(),
                       "name": fn.get("name") or "unknown_tool", "input": parsed})
    return blocks, bool(known)


def openai_usage_to_anthropic(usage):
    u = usage or {}
    return {"input_tokens": int(u.get("prompt_tokens") or u.get("input_tokens") or 0),
            "output_tokens": int(u.get("completion_tokens") or u.get("output_tokens") or 0)}


def anthropic_response_json(model, text, calls, finish, usage, reasoning=""):
    """Assembled (text, calls, finish, usage, reasoning) -> Anthropic message bytes.

    reasoning (upstream reasoning_content) goes FIRST as thinking blocks —
    nothing truncated, nothing dropped.
    """
    blocks = reasoning_blocks(reasoning)
    if text:
        blocks.append({"type": "text", "text": text})
    for c in calls or []:
        fn = c.get("function") or {}
        args = fn.get("arguments", "")
        try:
            parsed = json.loads(args) if isinstance(args, str) else (args or {})
            if not isinstance(parsed, dict):
                parsed = {}
        except Exception:
            parsed = {}
        blocks.append({"type": "tool_use", "id": c.get("id") or new_tool_id(),
                       "name": fn.get("name") or "unknown_tool", "input": parsed})
    if calls:
        stop = "tool_use"
    else:
        stop = map_finish_to_stop(finish)
    return json.dumps({
        "id": new_anthropic_msg_id(),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop,
        "usage": openai_usage_to_anthropic(usage),
    }).encode()


def openai_models_to_anthropic(data, fallback_ids=None, free_only=False):
    """OpenAI/OpenRouter models list -> Anthropic models list shape.

    free_only=True: keep only ids containing 'free' (public storefront).
    """
    ids = []
    try:
        if isinstance(data, dict):
            items = data.get("data", data.get("models", []))
        elif isinstance(data, list):
            items = data
        else:
            items = []
        for m in items:
            i = m.get("id") if isinstance(m, dict) else str(m)
            if i:
                if free_only and "free" not in i.lower():
                    continue
                ids.append(i)
    except Exception:
        ids = []
    if not ids and fallback_ids:
        ids = [i for i in fallback_ids
               if not free_only or "free" in i.lower()]
    now = int(time.time())
    return {"object": "list", "data": [
        {"type": "model", "id": i, "display_name": i, "created_at": now} for i in ids
    ], "has_more": False}


def filter_free_models_payload(data, fallback_ids=None):
    """Keep only 'free' ids in an OpenAI/OpenRouter models payload.

    Returns (payload, n_total, n_free). Structure preserved (OpenRouter
    dicts pass through untouched, only the list is trimmed).
    """
    try:
        if isinstance(data, dict):
            for key in ("data", "models"):
                items = data.get(key)
                if isinstance(items, list):
                    total = len(items)
                    kept = [m for m in items
                            if "free" in ((m.get("id") if isinstance(m, dict) else str(m)) or "").lower()]
                    data = dict(data)
                    data[key] = kept
                    return data, total, len(kept)
            return data, 0, 0
        if isinstance(data, list):
            kept = [m for m in data
                    if "free" in ((m.get("id") if isinstance(m, dict) else str(m)) or "").lower()]
            return kept, len(data), len(kept)
    except Exception:
        pass
    if fallback_ids is not None:
        kept = [i for i in fallback_ids if "free" in i.lower()]
        return {"data": [{"id": i} for i in kept]}, 0, len(kept)
    return data, 0, 0


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def iter_sse_dicts(line_iter):
    """Yield parsed JSON dicts from raw SSE lines (skips comments/[DONE])."""
    for raw in line_iter:
        if raw is None:
            continue
        if not isinstance(raw, str):
            try:
                raw = raw.decode("utf-8", "replace")
            except Exception:
                continue
        if not raw or raw.startswith(":") or not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        if isinstance(ev, dict):
            yield ev


def extract_reasoning_text(msg):
    """Pull reasoning text out of an OpenAI assistant message, any shape.

    Upstreams put it in message.reasoning_content (str), message.reasoning
    (str|dict{content}), or delta.reasoning_content in streams. Returns ""
    when absent. Never raises.
    """
    if not isinstance(msg, dict):
        return ""
    rc = msg.get("reasoning_content")
    if isinstance(rc, str) and rc:
        return rc
    r = msg.get("reasoning")
    if isinstance(r, str) and r:
        return r
    if isinstance(r, dict):
        for k in ("content", "text", "reasoning_content"):
            v = r.get(k)
            if isinstance(v, str) and v:
                return v
    return ""


def reasoning_blocks(reasoning_text):
    """reasoning str -> [thinking-block] (Anthropic shape, no truncation)."""
    if reasoning_text:
        return [{"type": "thinking", "thinking": reasoning_text, "signature": ""}]
    return []


class AnthropicStreamWriter:
    """Sequenced Anthropic SSE emitter (message_start -> blocks -> stop)."""

    def __init__(self, emit, model):
        self._emit = emit
        self._model = model
        self._index = -1
        self._open = None  # None | "text" | "tool"
        self._started = False
        self._out_tokens = 0

    def _send(self, event, data):
        self._emit("event: %s\ndata: %s\n\n" % (event, json.dumps(data)))

    def start(self):
        if self._started:
            return
        self._started = True
        self._send("message_start", {"type": "message_start", "message": {
            "id": new_anthropic_msg_id(), "type": "message", "role": "assistant",
            "model": self._model, "content": [], "stop_reason": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})

    def _close_block(self):
        if self._open is not None:
            self._send("content_block_stop", {"type": "content_block_stop", "index": self._index})
            self._open = None

    def text_delta(self, s):
        if not s:
            return
        self.start()
        if self._open != "text":
            self._close_block()
            self._index += 1
            self._send("content_block_start", {"type": "content_block_start",
                                               "index": self._index,
                                               "content_block": {"type": "text", "text": ""}})
            self._open = "text"
        self._out_tokens += max(1, len(s) // 4)
        self._send("content_block_delta", {"type": "content_block_delta",
                                           "index": self._index,
                                           "delta": {"type": "text_delta", "text": s}})

    def thinking_delta(self, s):
        if not s:
            return
        self.start()
        if self._open != "thinking":
            self._close_block()
            self._index += 1
            self._send("content_block_start", {"type": "content_block_start",
                                               "index": self._index,
                                               "content_block": {"type": "thinking",
                                                                 "thinking": ""}})
            self._open = "thinking"
        self._out_tokens += max(1, len(s) // 4)
        self._send("content_block_delta", {"type": "content_block_delta",
                                           "index": self._index,
                                           "delta": {"type": "thinking_delta",
                                                     "thinking": s}})

    def start_tool(self, call_id, name):
        self.start()
        if self._open == "tool" and getattr(self, "_cur_tool", None) == call_id:
            return
        self._close_block()
        self._index += 1
        self._cur_tool = call_id
        self._send("content_block_start", {"type": "content_block_start",
                                           "index": self._index,
                                           "content_block": {"type": "tool_use",
                                                             "id": call_id,
                                                             "name": name or "unknown_tool",
                                                             "input": {}}})
        self._open = "tool"

    def tool_json_delta(self, call_id, name, frag):
        if not frag:
            return
        if self._open != "tool" or getattr(self, "_cur_tool", None) != call_id:
            self.start_tool(call_id, name)
        self._out_tokens += max(1, len(frag) // 4)
        self._send("content_block_delta", {"type": "content_block_delta",
                                           "index": self._index,
                                           "delta": {"type": "input_json_delta",
                                                     "partial_json": frag}})

    def stop(self, stop_reason, usage=None):
        self.start()
        self._close_block()
        u = {"input_tokens": 0, "output_tokens": self._out_tokens}
        if isinstance(usage, dict) and (usage.get("prompt_tokens") or usage.get("input_tokens")):
            u = openai_usage_to_anthropic(usage)
        elif isinstance(usage, dict) and usage.get("output_tokens"):
            u["output_tokens"] = int(usage["output_tokens"])
        self._send("message_delta", {"type": "message_delta",
                                     "delta": {"stop_reason": stop_reason},
                                     "usage": {"output_tokens": u["output_tokens"]}})
        self._send("message_stop", {"type": "message_stop"})


def translate_chat_sse_to_anthropic(line_iter, model, writer, declared_tools=None):
    """OpenAI chat-completions SSE -> Anthropic SSE. Returns (finish, usage, reasoning).

    Two-phase per response: text streams live; tool_calls are BUFFERED until
    the upstream turn ends (finish_reason or stream end), then validated
    against declared_tools — known names go out as tool_use, unknown ones
    as text. This guarantees the model never sees a truncated tool_use
    (Claude Code fatals on those) and no tool is ever lost.
    reasoning (delta.reasoning_content / message.reasoning_content) streams
    as thinking-deltas and is also returned whole.
    """
    finish, usage = "stop", {}
    reasoning_parts = []
    tool_ids = {}
    tool_names = {}
    tool_argbuf = {}
    tool_order = []
    for ev in iter_sse_dicts(line_iter):
        if ev.get("usage") and not usage:
            try:
                usage = dict(ev["usage"])
            except Exception:
                pass
        for ch in ev.get("choices") or []:
            d = ch.get("delta") or {}
            # some proxies put the whole message (with tool_calls) in one delta
            if isinstance(d.get("message"), dict):
                m = d["message"]
                if m.get("content") and not d.get("content"):
                    d = dict(d)
                    d["content"] = m["content"]
                if m.get("tool_calls") and not d.get("tool_calls"):
                    d = dict(d)
                    d["tool_calls"] = m["tool_calls"]
            if isinstance(d.get("content"), list):
                for p in d["content"]:
                    if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
                        writer.text_delta(p["text"])
                    elif isinstance(p, str) and p:
                        writer.text_delta(p)
            elif d.get("content"):
                writer.text_delta(d["content"])
            rd = extract_reasoning_text(d)
            if rd:
                reasoning_parts.append(rd)
                writer.thinking_delta(rd)
            for tc in normalize_openai_tool_calls(d.get("tool_calls"),
                                                 _for_stream_merge=True):
                idx = tc.get("index")
                if idx is None:
                    idx = tc.get("id")
                fn = tc.get("function") or {}
                cid = None
                if not (isinstance(tc.get("id"), str) and tc["id"].startswith("toolu_")):
                    cid = tc.get("id") or None
                if cid is None:
                    cid = tool_ids.get(idx)
                if cid is None:
                    cid = new_tool_id()
                tool_ids[idx] = cid
                if idx not in tool_order:
                    tool_order.append(idx)
                if fn.get("name"):
                    tool_names[idx] = fn["name"]
                args = fn.get("arguments") or ""
                if args and args != "{}":
                    prev = tool_argbuf.get(idx, "")
                    if args == prev:
                        pass
                    elif prev and args.startswith(prev):
                        tool_argbuf[idx] = args
                    elif not prev:
                        tool_argbuf[idx] = args
                    else:
                        tool_argbuf[idx] = prev + args
                finish = "tool_calls"
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    # flush buffered tools AFTER the full turn is known: validate names,
    # emit known as tool_use, unknown as text — nothing lost, nothing cut.
    if finish == "tool_calls" or tool_order:
        calls = []
        for idx in tool_order:
            calls.append({"id": tool_ids.get(idx) or new_tool_id(),
                          "type": "function",
                          "function": {"name": tool_names.get(idx, "") or "unknown_tool",
                                       "arguments": _norm_tool_args(tool_argbuf.get(idx, "") or "{}")}})
        known, unknown = validate_tool_names(calls, declared_tools)
        for c in known:
            fn = c.get("function") or {}
            writer.start_tool(c["id"], fn.get("name") or "unknown_tool")
            if fn.get("arguments") and fn["arguments"] != "{}":
                writer.tool_json_delta(c["id"], fn.get("name") or "unknown_tool",
                                       fn["arguments"])
        if unknown:
            writer.text_delta(unknown_calls_to_text(unknown))
        if not known:
            finish = "stop"
        else:
            finish = "tool_calls"
    stop = "tool_use" if finish == "tool_calls" else map_finish_to_stop(finish)
    writer.stop(stop, usage)
    return finish, usage, "".join(reasoning_parts)


def translate_responses_sse_to_anthropic(line_iter, model, writer, declared_tools=None):
    """OpenAI Responses SSE -> Anthropic SSE. Returns (finish, usage, reasoning).

    Same two-phase contract as the chat translator: text streams live,
    function_calls are buffered until response.completed, then validated
    and flushed whole (tool_use for known, text for unknown).
    """
    finish, usage = "stop", {}
    reasoning_parts = []
    streamed_args = {}
    call_names = {}
    call_order = []
    for ev in iter_sse_dicts(line_iter):
        t = ev.get("type", "")
        if t == "response.output_text.delta":
            if ev.get("delta"):
                writer.text_delta(ev["delta"])
        elif t == "response.reasoning_summary_text.delta":
            if ev.get("delta"):
                reasoning_parts.append(ev["delta"])
                writer.thinking_delta(ev["delta"])
        elif t == "response.output_item.added":
            item = ev.get("item", {}) or {}
            if item.get("type") == "function_call":
                cid = item.get("call_id") or item.get("id") or new_tool_id()
                name = _norm_tool_name(item.get("name")) or "unknown_tool"
                call_names[cid] = name
                streamed_args.setdefault(cid, "")
                if cid not in call_order:
                    call_order.append(cid)
                finish = "tool_calls"
        elif t == "response.function_call_arguments.delta":
            cid = ev.get("item_id") or ev.get("call_id") or ""
            frag = ev.get("delta") or ""
            if frag:
                if not cid:
                    cid = new_tool_id()
                    if cid not in call_order:
                        call_order.append(cid)
                prev = streamed_args.get(cid, "")
                if frag == prev:
                    pass
                elif prev and frag.startswith(prev):
                    streamed_args[cid] = frag
                elif not prev:
                    streamed_args[cid] = frag
                else:
                    streamed_args[cid] = prev + frag
                finish = "tool_calls"
        elif t == "response.output_item.done":
            item = ev.get("item", {}) or {}
            if item.get("type") == "function_call":
                cid = item.get("call_id") or item.get("id") or new_tool_id()
                name = _norm_tool_name(item.get("name")) or call_names.get(cid, "unknown_tool")
                call_names[cid] = name
                if cid not in call_order:
                    call_order.append(cid)
                full = item.get("arguments") or ""
                prev = streamed_args.get(cid, "")
                if full and full != prev:
                    if prev and full.startswith(prev):
                        streamed_args[cid] = full
                    elif not prev:
                        streamed_args[cid] = full
                    # else: divergent repeat — keep streamed version, skip
                finish = "tool_calls"
        elif t == "response.completed":
            u = (ev.get("response", {}) or {}).get("usage", {}) or {}
            if u:
                usage = {"prompt_tokens": u.get("input_tokens", 0),
                         "completion_tokens": u.get("output_tokens", 0),
                         "total_tokens": u.get("total_tokens", 0)}
            break
        elif t in ("response.failed", "response.incomplete", "error"):
            break
    if finish == "tool_calls" or call_order:
        calls = []
        for cid in call_order:
            calls.append({"id": cid, "type": "function",
                          "function": {"name": call_names.get(cid, "unknown_tool"),
                                       "arguments": _norm_tool_args(streamed_args.get(cid, "") or "{}")}})
        known, unknown = validate_tool_names(calls, declared_tools)
        for c in known:
            fn = c.get("function") or {}
            writer.start_tool(c["id"], fn.get("name") or "unknown_tool")
            if fn.get("arguments") and fn["arguments"] != "{}":
                writer.tool_json_delta(c["id"], fn.get("name") or "unknown_tool",
                                       fn["arguments"])
        if unknown:
            writer.text_delta(unknown_calls_to_text(unknown))
        if not known:
            finish = "stop"
        else:
            finish = "tool_calls"
    stop = "tool_use" if finish == "tool_calls" else map_finish_to_stop(finish)
    writer.stop(stop, usage)
    return finish, usage, "".join(reasoning_parts)


def assemble_chat_sse(line_iter):
    """Chat-completions SSE -> (text, calls, finish, usage, reasoning).

    Tool fragments are merged via normalize (handles dict/str args,
    prefixed names, nested message) so assembled calls are always clean.
    """
    texts, calls, order, usage = [], {}, [], {}
    reasoning_parts = []
    finish = "stop"
    for ev in iter_sse_dicts(line_iter):
        if ev.get("usage") and not usage:
            try:
                usage = dict(ev["usage"])
            except Exception:
                pass
        for ch in ev.get("choices") or []:
            d = ch.get("delta") or {}
            if isinstance(d.get("message"), dict):
                m = d["message"]
                if m.get("content") and not d.get("content"):
                    d = dict(d)
                    d["content"] = m["content"]
                if m.get("tool_calls") and not d.get("tool_calls"):
                    d = dict(d)
                    d["tool_calls"] = m["tool_calls"]
            if isinstance(d.get("content"), str) and d["content"]:
                texts.append(d["content"])
            elif isinstance(d.get("content"), list):
                for p in d["content"]:
                    if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
                        texts.append(p["text"])
                    elif isinstance(p, str) and p:
                        texts.append(p)
            rd = extract_reasoning_text(d)
            if rd:
                reasoning_parts.append(rd)
            for tc in normalize_openai_tool_calls(d.get("tool_calls"),
                                                 _for_stream_merge=True):
                idx = tc.get("index")
                if idx is None:
                    idx = tc.get("id")
                fn = tc.get("function") or {}
                if idx not in order:
                    order.append(idx)
                    calls[idx] = {"id": "", "type": "function",
                                  "function": {"name": "", "arguments": ""}}
                c = calls[idx]
                if tc.get("id") and not c["id"].startswith("call_"):
                    c["id"] = tc["id"]
                if fn.get("name"):
                    c["function"]["name"] = fn["name"]
                args = fn.get("arguments") or ""
                if args and args != "{}":
                    if not c["function"]["arguments"]:
                        c["function"]["arguments"] = args
                    elif args == c["function"]["arguments"]:
                        pass
                    elif args.startswith(c["function"]["arguments"]):
                        c["function"]["arguments"] = args
                    else:
                        c["function"]["arguments"] += args
                finish = "tool_calls"
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    tcs = []
    for idx in order:
        c = calls[idx]
        if not c["id"]:
            c["id"] = new_tool_id()
        if not c["function"]["name"]:
            c["function"]["name"] = "unknown_tool"
        if not c["function"]["arguments"]:
            c["function"]["arguments"] = "{}"
        tcs.append({"index": len(tcs), "id": c["id"], "type": "function",
                    "function": c["function"]})
    return "".join(texts), tcs, finish, usage, "".join(reasoning_parts)


def assemble_responses_sse(line_iter):
    """Responses SSE -> (text, calls, finish, usage, reasoning)."""
    texts, calls, order, usage = [], {}, [], {}
    reasoning_parts = []
    finish = "stop"
    for ev in iter_sse_dicts(line_iter):
        t = ev.get("type", "")
        if t == "response.output_text.delta":
            if ev.get("delta"):
                texts.append(ev["delta"])
        elif t == "response.reasoning_summary_text.delta":
            if ev.get("delta"):
                reasoning_parts.append(ev["delta"])
        elif t == "response.output_item.added":
            item = ev.get("item", {}) or {}
            if item.get("type") == "function_call":
                cid = item.get("call_id") or item.get("id") or ""
                if cid not in calls:
                    calls[cid] = {"id": cid, "type": "function",
                                  "function": {"name": item.get("name") or "",
                                               "arguments": ""}}
                    order.append(cid)
                finish = "tool_calls"
        elif t == "response.function_call_arguments.delta":
            cid = ev.get("item_id") or ev.get("call_id") or ""
            if cid in calls:
                calls[cid]["function"]["arguments"] += ev.get("delta", "")
                finish = "tool_calls"
        elif t == "response.output_item.done":
            item = ev.get("item", {}) or {}
            if item.get("type") == "function_call":
                cid = item.get("call_id") or item.get("id") or ""
                if cid in calls:
                    if item.get("name"):
                        calls[cid]["function"]["name"] = _norm_tool_name(item["name"]) or calls[cid]["function"]["name"]
                    full = item.get("arguments") or ""
                    prev = calls[cid]["function"]["arguments"]
                    if full and full != prev:
                        if not prev or (prev and full.startswith(prev)):
                            calls[cid]["function"]["arguments"] = full
                        # else: divergent repeat — keep streamed version
                    finish = "tool_calls"
        elif t == "response.completed":
            u = (ev.get("response", {}) or {}).get("usage", {}) or {}
            usage = {"prompt_tokens": u.get("input_tokens", 0),
                     "completion_tokens": u.get("output_tokens", 0),
                     "total_tokens": u.get("total_tokens", 0)}
        elif t in ("response.failed", "response.incomplete", "error"):
            break
    tcs = []
    for idx, cid in enumerate(order):
        c = calls[cid]
        if not c["id"]:
            c["id"] = new_tool_id()
        tcs.append({"index": idx, "id": c["id"], "type": "function",
                    "function": c["function"]})
    return "".join(texts), tcs, finish, usage, "".join(reasoning_parts)

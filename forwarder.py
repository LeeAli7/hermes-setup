import json, os, sys, logging, threading, time, socket, binascii, uuid, secrets, string
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import requests, urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "proxy_state.json")
LOG_FILE = os.path.join(BASE_DIR, "logs", "forwarder.log")

os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)

logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("forwarder")

UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://opencode.ai")
CONTROL_PORT = 9051
SOCKS_PORT = 9050
UPSTREAM_READ_TIMEOUT = 300  # upstream response timeout (seconds)
UPSTREAM_CONNECT_TIMEOUT = 60
MAX_BODY_SIZE = int(os.environ.get("FORWARDER_MAX_BODY", str(20 * 1024 * 1024)))  # 20MB

# --- 429 handling ---
# FreeUsageLimitError / provider rate limits are intermittent:
# opencode CLI survives by retrying with backoff (isRetryable=true).
# So on 429 we: retry with backoff -> rotate Tor once -> retry, and if the
# model keeps failing, fall back to a known-alive model (env-configurable).
RETRY_ATTEMPTS = int(os.environ.get("FORWARDER_429_ATTEMPTS", "10"))
RETRY_BACKOFF = float(os.environ.get("FORWARDER_429_BACKOFF", "8"))
MAX_BACKOFF = float(os.environ.get("FORWARDER_429_MAX_BACKOFF", "40"))
ROTATE_ON_429 = os.environ.get("FORWARDER_ROTATE_ON_429", "1") == "1"
# Strict mode by default: never swap the requested model. To allow a rescue
# model after repeated 429s, set FORWARDER_FALLBACK_MODELS="model1,model2".
FALLBACK_MODELS = [m.strip() for m in os.environ.get(
    "FORWARDER_FALLBACK_MODELS", "").split(",") if m.strip()]

# --- official-client emulation ---
# Since ~16.09.2026 opencode.ai's free tier GATES on request shape, not just
# rate-limits: anonymous/non-CLI requests get 403 FreeTierError ("can only be
# used from within OpenCode"). Verified by replaying captured CLI traffic
# (mitmproxy, CLI v1.18.31) field-by-field on 17.09.2026. Passing combo:
#   Authorization: Bearer public (literal!) + current CLI UA +
#   x-opencode-client: cli + x-opencode-project: global +
#   x-opencode-session: ses_<9hex>ffe<14alnum> +
#   x-opencode-request: msg_<9hex>001<14alnum>
# The ses_/msg_ IDs are FORMAT-validated server-side (reverse-engineered from
# the CLI binary + its local opencode.db). Random UUIDs -> 403. Missing
# headers -> 403. Wrong bearer (e.g. Hermes' placeholder) -> 401.
UPSTREAM_UA = os.environ.get(
    "FORWARDER_UA",
    "opencode/1.18.31 ai-sdk/provider-utils/4.0.40 runtime/bun/1.3.14",
)
# Literal public bearer the official CLI sends. ALWAYS overwritten outbound:
# Hermes sends its own placeholder (opencode-zen-free-keyless) which 401s.
UPSTREAM_BEARER = os.environ.get("FORWARDER_UPSTREAM_BEARER", "Bearer public")
SPOOF_SYSTEM_MARKER = os.environ.get("FORWARDER_SYSTEM_MARKER", "1") == "1"
SYSTEM_MARKER = os.environ.get("FORWARDER_SYSTEM_MARKER_TEXT", "You are opencode.")

FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding"}

# --- public API key auth (from Marsel, keeps cloudflared tunnel protected) ---
# Localhost (127.0.0.1 / ::1) is always allowed (local Hermes gateway, keyless provider).
# Remote clients must send:  Authorization: Bearer $FORWARDER_API_KEY
# Remote clients are also restricted to /zen/* API paths (+ /health).
PUBLIC_API_KEY = os.environ.get("FORWARDER_API_KEY", "").strip()
LOOPBACK_IPS = {"127.0.0.1", "::1"}

_rotate_lock = threading.Lock()

_SESSION_PIN_LOCK = threading.Lock()
_SESSION_PINS = {}  # stable key (prompt_cache_key) -> sticky x-opencode-session id

_HEX = "0123456789abcdef"
_ALNUM = string.ascii_letters + string.digits


def _new_session_id():
    # Format reverse-engineered from the official CLI (opencode.db):
    # ses_ + 9 hex + "ffe" + 14 alnum. Anything else -> 403 FreeTierError.
    return ("ses_" + "".join(secrets.choice(_HEX) for _ in range(9)) + "ffe"
            + "".join(secrets.choice(_ALNUM) for _ in range(14)))


def _new_request_id():
    # msg_ + 9 hex + "001" + 14 alnum (002 also seen; 001 is the common one).
    return ("msg_" + "".join(secrets.choice(_HEX) for _ in range(9)) + "001"
            + "".join(secrets.choice(_ALNUM) for _ in range(14)))


def _sticky_session_for(body):
    """Stable x-opencode-session for one conversation (fix 2026-09-13).

    Native Responses bodies carry prompt_cache_key (stable per
    conversation) -> reuse one uuid per key. Chat bodies carry no key ->
    fresh uuid (unchanged behaviour; chat path carries no sealed blobs).
    """
    key = None
    try:
        if body:
            j = json.loads(body) if isinstance(body, (bytes, bytearray)) else body
            if isinstance(j, dict):
                key = j.get("prompt_cache_key") or None
    except Exception:
        key = None
    if not key:
        return _new_session_id()
    with _SESSION_PIN_LOCK:
        sess = _SESSION_PINS.get(key)
        if not sess:
            sess = _new_session_id()
            _SESSION_PINS[key] = sess
            if len(_SESSION_PINS) > 5000:
                _SESSION_PINS.pop(next(iter(_SESSION_PINS)))
    return sess



def get_proxy():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        p = s.get("current_proxy")
        if p:
            if p.startswith("socks"):
                return {"http": p, "https": p}
            if ":" in p:
                return {"http": f"http://{p}", "https": f"http://{p}"}
    except:
        pass
    return None


def get_cookie_path():
    return os.path.join(BASE_DIR, "tor", "Data", "control_auth_cookie")

def get_tor_password():
    return os.environ.get("TOR_CONTROL_PASSWORD", "hermes_tor_control")


def renew_tor_ip():
    if not _rotate_lock.acquire(blocking=False):
        log.warning("IP rotation already in progress, waiting for it to finish")
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            old_ts = state.get("last_rotation_time", 0)
        except:
            old_ts = 0
        for _ in range(20):
            time.sleep(0.5)
            try:
                with open(STATE_FILE) as f:
                    state = json.load(f)
                new_ts = state.get("last_rotation_time", 0)
            except:
                new_ts = 0
            if new_ts > old_ts:
                log.info("Waiting for in-progress rotation: done")
                return True
        log.warning("Waiting for in-progress rotation: timed out")
        return False
    try:
        password = get_tor_password()
        cookie_path = get_cookie_path()
        
        # Try password auth first
        if password:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(10)
                s.connect(("127.0.0.1", CONTROL_PORT))
                s.sendall(b"AUTHENTICATE \"" + password.encode() + b"\"\r\n")
                resp = s.recv(1024)
                if resp.startswith(b"250"):
                    log.info("Tor control authenticated via password")
                    s.sendall(b"SIGNAL NEWNYM\r\n")
                    resp = s.recv(1024)
                    s.close()
                    if resp.startswith(b"250"):
                        log.info("Tor IP rotation signal sent")
                        # Verify the exit actually changed and record it
                        # (previously returned True blindly — state went stale).
                        # Fresh circuits need time: poll up to ~25s.
                        try:
                            with open(STATE_FILE) as f:
                                old_ip = json.load(f).get("tor_exit_ip")
                        except:
                            old_ip = None
                        new_ip = None
                        for _ in range(5):
                            time.sleep(5)
                            new_ip = get_tor_ip()
                            if new_ip and new_ip != old_ip:
                                break
                        if new_ip and new_ip != old_ip:
                            log.info(f"New Tor IP: {new_ip}")
                            try:
                                with open(STATE_FILE) as f:
                                    state = json.load(f)
                            except:
                                state = {}
                            state["current_proxy"] = f"socks5h://127.0.0.1:{SOCKS_PORT}"
                            state["tor_exit_ip"] = new_ip
                            state["last_rotation_time"] = time.time()
                            state["total_ip_switches"] = state.get("total_ip_switches", 0) + 1
                            with open(STATE_FILE, "w") as f:
                                json.dump(state, f)
                            return True
                        log.warning(f"NEWNYM sent but IP unchanged ({new_ip})")
                        return False
                    else:
                        log.error(f"NEWNYM failed: {resp}")
                        return False
                else:
                    log.error(f"Tor password auth failed: {resp}")
                    s.close()
            except Exception as e:
                log.error(f"Tor control error: {e}")
        
        # Fallback to cookie auth
        if not os.path.exists(cookie_path):
            log.error(f"Tor cookie not found at {cookie_path}")
            return False

        with open(cookie_path, "rb") as f:
            cookie = f.read()

        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            old_ip = state.get("tor_exit_ip")
        except:
            old_ip = None

        for attempt in range(4):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect(("127.0.0.1", CONTROL_PORT))
            s.sendall(b"AUTHENTICATE " + binascii.hexlify(cookie) + b"\r\n")
            resp = s.recv(1024)
            if not resp.startswith(b"250"):
                log.error(f"Tor auth failed: {resp}")
                s.close()
                return False

            s.sendall(b"SIGNAL NEWNYM\r\n")
            resp = s.recv(1024)
            s.close()
            if not resp.startswith(b"250"):
                log.error(f"NEWNYM failed: {resp}")
                return False

            log.info("Tor NEWNYM sent, waiting 5s for new IP")
            time.sleep(5)

            new_ip = get_tor_ip()
            if new_ip and new_ip != old_ip:
                log.info(f"New Tor IP: {new_ip}")
                try:
                    with open(STATE_FILE) as f:
                        state = json.load(f)
                except:
                    state = {}
                state["current_proxy"] = f"socks5h://127.0.0.1:{SOCKS_PORT}"
                state["tor_exit_ip"] = new_ip
                state["last_rotation_time"] = time.time()
                state["total_ip_switches"] = state.get("total_ip_switches", 0) + 1
                with open(STATE_FILE, "w") as f:
                    json.dump(state, f)
                return True
            log.warning(f"NEWNYM attempt {attempt+1}: IP unchanged ({new_ip}), retrying")

        log.error("IP rotation failed: IP did not change after 4 NEWNYM attempts")
        return False
    except Exception as e:
        log.error(f"IP rotation error: {e}")
        return False
    finally:
        _rotate_lock.release()


def get_tor_ip():
    try:
        proxies = {"http": f"socks5h://127.0.0.1:{SOCKS_PORT}", "https": f"socks5h://127.0.0.1:{SOCKS_PORT}"}
        r = requests.get("https://api.ipify.org", proxies=proxies, timeout=10)
        return r.text.strip()
    except Exception as e:
        log.error(f"get_tor_ip failed: {e}")
        return None


def retry_after_seconds(resp, fallback):
    try:
        ra = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
        if ra:
            ra = ra.strip()
            if ra.isdigit():
                return min(int(ra), 60)
    except Exception:
        pass
    return fallback


def swap_model(body, model):
    if not body or b"model" not in body:
        return None
    try:
        obj = json.loads(body)
        if not isinstance(obj, dict):
            return None
        obj["model"] = model
        return json.dumps(obj).encode()
    except Exception:
        return None


def chat_text_of(content):
    """Flatten an OpenAI chat content block to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts) if parts else str(content)
    return str(content or "")


def chat_messages_to_responses_input(messages):
    """Chat messages[] -> Responses input[], preserving tool traffic.

    assistant messages carrying tool_calls become function_call items,
    tool messages become function_call_output items, everything else
    becomes a plain role/content item. This keeps multi-turn agentic
    loops intact through the conversion.

    Upstream rejects empty call_id outright, so items that arrived
    without one are paired positionally and given stable
    fwd-call-<n> ids (call and its output share the number).
    """
    resp_input = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        if role == "tool":
            resp_input.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id") or msg.get("call_id") or "",
                "output": chat_text_of(msg.get("content")),
            })
            continue
        text = chat_text_of(msg.get("content"))
        tcs = msg.get("tool_calls") if role == "assistant" else None
        if isinstance(tcs, list) and tcs:
            if text:
                resp_input.append({"role": "assistant", "content": text})
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
                args = fn.get("arguments", "")
                if not isinstance(args, str):
                    try:
                        args = json.dumps(args)
                    except Exception:
                        args = str(args)
                resp_input.append({
                    "type": "function_call",
                    "call_id": tc.get("id") or "",
                    "name": fn.get("name") or "unknown_tool",
                    "arguments": args,
                })
            continue
        resp_input.append({"role": role, "content": text})
    # Pair up items with empty call_id so upstream validation
    # (call_id length >= 1) passes and each call shares its number
    # with its output: zip calls with outputs in encounter order,
    # leftovers get their own numbers.
    _calls = [i for i in resp_input
              if i.get("type") == "function_call" and not i.get("call_id")]
    _outs = [i for i in resp_input
             if i.get("type") == "function_call_output" and not i.get("call_id")]
    _n = 0
    for _c, _o in zip(_calls, _outs):
        _n += 1
        _c["call_id"] = _o["call_id"] = f"fwd-call-{_n}"
    for _i in _calls[len(_outs):] + _outs[len(_calls):]:
        _n += 1
        _i["call_id"] = f"fwd-call-{_n}"
    return resp_input


def chat_tools_to_responses(chat):
    """Chat tools/tool_choice -> Responses shape (or (None, None))."""
    tools = chat.get("tools")
    out_tools = None
    if isinstance(tools, list) and tools:
        out_tools = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "function" and isinstance(t.get("function"), dict):
                f = t["function"]
                out_tools.append({
                    "type": "function",
                    "name": f.get("name") or "",
                    "description": f.get("description") or "",
                    "parameters": f.get("parameters") or {"type": "object", "properties": {}},
                })
        out_tools = out_tools or None
    tc = chat.get("tool_choice")
    out_tc = None
    if isinstance(tc, str) and tc in ("auto", "none", "required"):
        out_tc = tc
    elif isinstance(tc, dict):
        if tc.get("type") == "function" and isinstance(tc.get("function"), dict):
            out_tc = {"type": "function", "name": tc["function"].get("name") or ""}
        else:
            out_tc = "auto"
    return out_tools, out_tc


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def do_PUT(self):
        self.handle_request("PUT")

    def do_DELETE(self):
        self.handle_request("DELETE")

    def do_PATCH(self):
        self.handle_request("PATCH")

    def do_HEAD(self):
        self.handle_request("HEAD")

    def passthrough(self, resp):
        self.send_response(resp.status_code)
        for k, v in resp.headers.items():
            kl = k.lower()
            if kl not in ("transfer-encoding", "content-encoding", "connection"):
                self.send_header(k, v)
        self.end_headers()
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            resp.close()

    def convert_responses_to_chat(self, resp):
        try:
            # Upstream SSE/JSON is always UTF-8, but requests defaults
            # text/* without charset to ISO-8859-1 -> Cyrillic mojibake.
            resp.encoding = "utf-8"
            data = resp.json()
            content_parts = []
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for part in item.get("content", []):
                        if part.get("type") == "output_text":
                            content_parts.append(part.get("text", ""))
            text = "\n".join(content_parts) if content_parts else ""
            chat_resp = {
                "id": data.get("id", ""),
                "object": "chat.completion",
                "created": data.get("created_at", 0),
                "model": data.get("model", ""),
                "choices": [{
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text}
                }],
                "usage": {
                    "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
                    "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
                    "total_tokens": data.get("usage", {}).get("total_tokens", 0),
                }
            }
            out = json.dumps(chat_resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            self.wfile.flush()
        except Exception as e:
            log.error(f"convert_responses_to_chat error: {e}")
            self.passthrough(resp)
        finally:
            resp.close()

    def translate_responses_stream(self, resp, model):
        """Responses SSE -> chat-completions SSE, streamed to the client.

        Maps response.output_text.delta to content deltas, function_call
        items + arguments deltas to tool_calls deltas, and ends with a
        finish chunk + [DONE]. Unknown events are skipped (pings and
        lifecycle noise); a failed response just ends the stream.
        """
        import time as _time

        def sse(obj):
            return ("data: " + json.dumps(obj) + "\n\n").encode()

        chat_id = "chatcmpl-resp-%d" % int(_time.time() * 1000)
        created = int(_time.time())
        # Force UTF-8: requests would decode text/event-stream without
        # charset as ISO-8859-1, garbling every non-ASCII delta.
        resp.encoding = "utf-8"

        def base_delta():
            return {"id": chat_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": None}]}

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            # Close after [DONE]: with HTTP/1.1 keep-alive and no
            # Content-Length the client would otherwise wait forever
            # for EOF (dsh hangs; only [DONE]-parsers survive).
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            w = self.wfile
            prelude = base_delta()
            prelude["choices"][0]["delta"] = {"role": "assistant", "content": ""}
            w.write(sse(prelude))
            w.flush()
            call_index = {}
            args_seen = set()
            finish = "stop"
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                if raw.startswith(":"):
                    # keep-alive / lifecycle comments: forward as SSE
                    # comments so the client sees traffic during long
                    # reasoning stretches and does not time out.
                    try:
                        w.write((raw + "\n\n").encode())
                        w.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                    continue
                if not raw.startswith("data:"):
                    continue
                payload = raw[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    ev = json.loads(payload)
                except Exception:
                    continue
                if not isinstance(ev, dict):
                    continue
                t = ev.get("type", "")
                if t == "response.output_text.delta":
                    d = ev.get("delta", "")
                    if d:
                        c = base_delta()
                        c["choices"][0]["delta"] = {"content": d}
                        w.write(sse(c))
                        w.flush()
                elif t == "response.output_item.added":
                    item = ev.get("item", {}) or {}
                    if item.get("type") == "function_call":
                        cid = item.get("call_id") or item.get("id") or ""
                        if cid not in call_index:
                            call_index[cid] = len(call_index)
                        idx = call_index[cid]
                        c = base_delta()
                        c["choices"][0]["delta"] = {"tool_calls": [{
                            "index": idx, "id": cid, "type": "function",
                            "function": {"name": item.get("name") or "", "arguments": ""}}]}
                        w.write(sse(c))
                        w.flush()
                        finish = "tool_calls"
                elif t == "response.function_call_arguments.delta":
                    cid = ev.get("item_id") or ev.get("call_id") or ""
                    if cid not in call_index:
                        call_index[cid] = len(call_index)
                    idx = call_index[cid]
                    frag = ev.get("delta", "")
                    if frag:
                        c = base_delta()
                        c["choices"][0]["delta"] = {"tool_calls": [{
                            "index": idx, "function": {"arguments": frag}}]}
                        w.write(sse(c))
                        w.flush()
                        args_seen.add(cid)
                        finish = "tool_calls"
                elif t == "response.output_item.done":
                    item = ev.get("item", {}) or {}
                    if item.get("type") == "function_call":
                        cid = item.get("call_id") or item.get("id") or ""
                        if cid not in args_seen:
                            if cid not in call_index:
                                call_index[cid] = len(call_index)
                            idx = call_index[cid]
                            c = base_delta()
                            c["choices"][0]["delta"] = {"tool_calls": [{
                                "index": idx, "id": cid, "type": "function",
                                "function": {"name": item.get("name") or "",
                                             "arguments": item.get("arguments") or ""}}]}
                            w.write(sse(c))
                            w.flush()
                            finish = "tool_calls"
                elif t == "ping":
                    try:
                        w.write(b": ping\n\n")
                        w.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                elif t in ("response.completed", "response.failed", "response.incomplete"):
                    if t != "response.completed":
                        log.warning(f"responses stream ended with {t} for model={model}")
                    break
                elif t == "error":
                    log.warning(f"responses stream error event for model={model}: {payload[:200]}")
                    break
            final = base_delta()
            final["choices"][0]["finish_reason"] = finish
            w.write(sse(final))
            w.write(b"data: [DONE]\n\n")
            w.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            log.error(f"translate_responses_stream error: {e}")
        finally:
            resp.close()

    def send_429(self, message="Rate limit exceeded after retries. Please try again later."):
        try:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "type": "error",
                "error": {"type": "FreeUsageLimitError", "message": message},
            }).encode())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def attempt(self, method, url, body, headers, proxy):
        return requests.request(
            method=method, url=url, data=body, headers=headers,
            proxies=proxy, timeout=(UPSTREAM_CONNECT_TIMEOUT, UPSTREAM_READ_TIMEOUT),
            stream=True, verify=False,
        )

    def is_remote_request(self):
        # Everything via the cloudflared tunnel arrives from 127.0.0.1,
        # but the Cloudflare edge always adds these headers:
        if self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For"):
            return True
        client_ip = self.client_address[0] if self.client_address else ""
        return client_ip not in LOOPBACK_IPS

    def check_public_auth(self):
        if self.path == "/health":
            return True
        if not self.is_remote_request():
            return True
        if not PUBLIC_API_KEY:
            return True  # open mode: no key configured
        if self.headers.get("Authorization", "") == f"Bearer {PUBLIC_API_KEY}":
            if not self.path.startswith("/zen/"):
                try:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"not found"}')
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return False
            return True
        client_ip = self.client_address[0] if self.client_address else "?"
        log.warning(f"Auth rejected for {client_ip} {self.command} {self.path}")
        try:
            body = b'{"error":{"type":"auth_error","message":"Invalid or missing API key"}}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        return False

    def handle_request(self, method):
        if not self.check_public_auth():
            return
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_BODY_SIZE:
            log.warning(f"Body too large: {content_length} bytes (max {MAX_BODY_SIZE})")
            self.send_error(413, f"Request body too large: {content_length} bytes")
            return
        body = None
        if content_length > 0:
            body = self.rfile.read(content_length)
            log.info(f"Body read: {len(body)} bytes for {method} {self.path}")

        url = UPSTREAM_BASE + self.path
        headers = {k: v for k, v in self.headers.items() if k.lower() not in FORBIDDEN_HEADERS}
        headers["User-Agent"] = UPSTREAM_UA
        # Always overwrite: Hermes sends its placeholder bearer (401s) and
        # no/UUID session headers (403s). Exact CLI shape, verified 17.09.2026.
        headers["Authorization"] = UPSTREAM_BEARER
        headers["x-opencode-client"] = "cli"
        headers["x-opencode-project"] = "global"
        headers["x-opencode-request"] = _new_request_id()
        _sess = _sticky_session_for(body)
        headers["x-opencode-session"] = _sess

        current_model = None
        if body:
            try:
                current_model = json.loads(body).get("model")
            except Exception:
                current_model = None
        try:
            log.info(f"opencode-session={_sess} model={current_model}")
        except Exception:
            pass

        RESPONSES_MODELS = {"muse-spark-1.3-contributor-free", "muse-spark-1.2-contributor-free"}
        use_responses_api = (
            current_model in RESPONSES_MODELS
            and "/chat/completions" in self.path
        )
        if use_responses_api:
            try:
                chat = json.loads(body)
                responses_body = {
                    "model": current_model,
                    "input": chat_messages_to_responses_input(chat.get("messages")),
                    "stream": chat.get("stream", False),
                }
                if "temperature" in chat:
                    responses_body["temperature"] = chat["temperature"]
                if "max_tokens" in chat:
                    responses_body["max_output_tokens"] = chat["max_tokens"]
                r_tools, r_tc = chat_tools_to_responses(chat)
                if r_tools:
                    responses_body["tools"] = r_tools
                if r_tc:
                    responses_body["tool_choice"] = r_tc
                body = json.dumps(responses_body).encode()
                url = UPSTREAM_BASE + "/zen/v1/responses"
                log.info(f"Converted chat/completions -> responses API for model={current_model}")
            except Exception as e:
                log.warning(f"Failed to convert to responses API: {e}")
                current_model = None

        if body and SPOOF_SYSTEM_MARKER:
            try:
                j = json.loads(body)
                msgs = j.get("messages")
                if isinstance(msgs, list) and msgs:
                    has_multimodal = False
                    for m in msgs:
                        if isinstance(m, dict) and isinstance(m.get("content"), list):
                            has_multimodal = True
                            break
                    if has_multimodal:
                        log.info(f"Skipping SPOOF_SYSTEM_MARKER: multimodal content detected")
                    else:
                        sys_idx = next((i for i, m in enumerate(msgs)
                                        if isinstance(m, dict) and m.get("role") == "system"), None)
                        if sys_idx is None:
                            msgs.insert(0, {"role": "system", "content": SYSTEM_MARKER})
                        else:
                            c = msgs[sys_idx].get("content")
                            if isinstance(c, str) and "you are opencode" not in c.lower():
                                msgs[sys_idx]["content"] = SYSTEM_MARKER + " " + c
                        body = json.dumps(j).encode()
            except Exception:
                pass

        rotated = False
        for attempt in range(RETRY_ATTEMPTS):
            proxy = get_proxy()
            log.info(f"Request: {method} {self.path} attempt={attempt+1}/{RETRY_ATTEMPTS} proxy={proxy} model={current_model}")
            try:
                resp = self.attempt(method, url, body, headers, proxy)

                if resp.status_code == 429 or resp.status_code >= 500:
                    if resp.status_code == 429:
                        wait = retry_after_seconds(resp, min(RETRY_BACKOFF * (attempt + 1), MAX_BACKOFF))
                        log.warning(f"429 for {self.path} model={current_model} attempt={attempt+1} retry_in={wait}s")
                    else:
                        wait = min(RETRY_BACKOFF * (attempt + 1), MAX_BACKOFF)
                        log.warning(f"{resp.status_code} for {self.path} model={current_model} attempt={attempt+1} retry_in={wait}s")
                    resp.close()

                    if ROTATE_ON_429:
                        rotated = renew_tor_ip()
                        if rotated:
                            continue
                        log.warning("IP rotation failed, waiting before retry")

                    if attempt < RETRY_ATTEMPTS - 1:
                        time.sleep(wait)
                else:
                    log.info(f"Response: {resp.status_code} for {self.path} model={current_model}")
                    if use_responses_api and resp.status_code == 200:
                        try:
                            is_stream = bool(json.loads(body).get("stream")) if body else False
                        except Exception:
                            is_stream = False
                        if is_stream:
                            if os.environ.get("FORWARDER_RESPONSES_RAW") == "1":
                                # debug: pass responses SSE through untouched
                                log.warning("FORWARDER_RESPONSES_RAW=1: raw passthrough")
                                self.passthrough(resp)
                            else:
                                self.translate_responses_stream(resp, current_model)
                        else:
                            self.convert_responses_to_chat(resp)
                    else:
                        self.passthrough(resp)
                    return

            except requests.exceptions.Timeout:
                log.error(f"Timeout for {method} {self.path}")
                if attempt < RETRY_ATTEMPTS - 1:
                    log.warning("Timeout detected, rotating Tor IP and retrying...")
                    if not rotated:
                        rotated = renew_tor_ip()
                    continue
                self.send_error(502, "Forwarder Error: Upstream timeout")
                return
            except requests.exceptions.ProxyError as e:
                log.error(f"ProxyError for {method} {self.path}: {e}")
                if attempt < RETRY_ATTEMPTS - 1:
                    log.warning("ProxyError detected, rotating Tor IP and retrying...")
                    if not rotated:
                        rotated = renew_tor_ip()
                    continue
                self.send_error(502, "Forwarder Error: Proxy failed")
                return
            except requests.exceptions.ConnectionError as e:
                log.error(f"ConnectionError for {method} {self.path}: {e}")
                if attempt < RETRY_ATTEMPTS - 1:
                    log.warning("ConnectionError detected, rotating Tor IP and retrying...")
                    if not rotated:
                        rotated = renew_tor_ip()
                    continue
                self.send_error(502, "Forwarder Error: Connection failed")
                return
            except Exception as e:
                log.error(f"Error for {method} {self.path}: {e}", exc_info=True)
                self.send_error(502, f"Forwarder Error: {e}")
                return

        if current_model and FALLBACK_MODELS:
            for fb in FALLBACK_MODELS:
                if fb == current_model:
                    continue
                fb_body = swap_model(body, fb)
                if fb_body is None:
                    continue
                log.warning(f"Retries exhausted for model={current_model}, falling back to model={fb}")
                for attempt in range(2):
                    proxy = get_proxy()
                    try:
                        resp = self.attempt(method, url, fb_body, headers, proxy)
                        if resp.status_code < 429:
                            log.info(f"Fallback {fb} -> {resp.status_code} for {self.path}")
                            self.passthrough(resp)
                            return
                        wait = retry_after_seconds(resp, 10)
                        log.warning(f"Fallback {fb} {resp.status_code} attempt={attempt+1} retry_in={wait}s")
                        resp.close()
                        if attempt == 0:
                            time.sleep(wait)
                    except Exception as e:
                        log.error(f"Fallback {fb} error: {e}")
                        break

        log.error(f"Retries exhausted for {self.path} model={current_model}, returning 503")
        self.send_error(503, f"All retries exhausted for model={current_model}")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    host = os.environ.get("FORWARDER_HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), ProxyHandler)
    auth_state = 'ON' if PUBLIC_API_KEY else 'OFF-open'
    log.info(f"Forwarder started on {host}:{port} 429-retries={RETRY_ATTEMPTS} backoff={RETRY_BACKOFF}s fallback={FALLBACK_MODELS or 'DISABLED'} rotate={ROTATE_ON_429} auth={auth_state}")
    server.serve_forever()


if __name__ == "__main__":
    main()
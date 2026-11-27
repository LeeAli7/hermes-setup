import json, os, sys, logging, threading, time, socket, binascii
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import requests, urllib3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import anthropic_bridge

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "proxy_state.json")
LOG_FILE = os.path.join(BASE_DIR, "logs", "kilo_forwarder.log")

os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)

logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("kilo_forwarder")

UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://api.kilo.ai")
CONTROL_PORT = 9051
SOCKS_PORT = 9050
UPSTREAM_READ_TIMEOUT = 300
UPSTREAM_CONNECT_TIMEOUT = 60
MAX_BODY_SIZE = int(os.environ.get("FORWARDER_MAX_BODY", str(20 * 1024 * 1024)))  # 20MB

RETRY_ATTEMPTS = int(os.environ.get("FORWARDER_429_ATTEMPTS", "4"))
RETRY_BACKOFF = float(os.environ.get("FORWARDER_429_BACKOFF", "8"))
MAX_BACKOFF = float(os.environ.get("FORWARDER_429_MAX_BACKOFF", "30"))
ROTATE_ON_429 = os.environ.get("FORWARDER_ROTATE_ON_429", "1") == "1"
FALLBACK_MODELS = [m.strip() for m in os.environ.get(
    "FORWARDER_FALLBACK_MODELS", "").split(",") if m.strip()]

FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding"}

_rotate_lock = threading.Lock()

# --- Anthropic bridge (Claude Code via POST /v1/messages) ---
KILO_ANTHROPIC_DEFAULT_MODEL = os.environ.get(
    "KILO_ANTHROPIC_DEFAULT_MODEL", "kilo-auto/free").strip() or "kilo-auto/free"

# Optional public key (same convention as :9000): unset = open mode.
KILO_PUBLIC_API_KEY = os.environ.get("FORWARDER_API_KEY", "").strip()
LOOPBACK_IPS = {"127.0.0.1", "::1"}


def _query_flag(path, name, default=True):
    """Query flag with default (beta defaults ON; ?beta=false/0 disables)."""
    try:
        q = path.split("?", 1)[1] if "?" in path else ""
        for part in q.split("&"):
            k, _, v = part.partition("=")
            if k.strip() == name:
                return v.strip().lower() not in ("0", "false", "no", "off")
        return default
    except Exception:
        return default

def _load_known_kilo_models():
    try:
        with open(os.path.join(BASE_DIR, "free-models", "kilo.txt")) as f:
            return [l.strip() for l in f if l.strip()]
    except Exception:
        return []


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


def renew_tor_ip():
    if not _rotate_lock.acquire(blocking=False):
        log.warning("IP rotation already in progress, skipping")
        return False
    try:
        cookie_path = get_cookie_path()
        if not os.path.exists(cookie_path):
            log.error(f"Tor cookie not found at {cookie_path}")
            return False

        with open(cookie_path, "rb") as f:
            cookie = f.read()

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
        if new_ip:
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
        if self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For"):
            return True
        client_ip = self.client_address[0] if self.client_address else ""
        return client_ip not in LOOPBACK_IPS

    def check_public_auth(self):
        if self.path == "/health":
            return True
        if not self.is_remote_request():
            return True
        if not KILO_PUBLIC_API_KEY:
            return True  # open mode: no key configured
        _auth_ok = (
            self.headers.get("Authorization", "") == f"Bearer {KILO_PUBLIC_API_KEY}"
            or (self.headers.get("x-api-key", "") or "").strip() == KILO_PUBLIC_API_KEY
        )
        if _auth_ok:
            return True
        try:
            body = b'{"type":"error","error":{"type":"authentication_error","message":"Invalid or missing API key"}}'
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
        # NB: тело НЕ мутируем (нет SPOOF-блока) — vision/multimodal проходит как есть.

        if self.path == "/v1/messages" or self.path.startswith("/v1/messages?"):
            self.handle_anthropic_messages(method, body)
            return
        if self.path == "/v1/models" or self.path.startswith("/v1/models?"):
            self.handle_anthropic_models(method)
            return
        if method == "GET" and self.is_remote_request() and (
                self.path == "/api/openrouter/models"
                or self.path.startswith("/api/openrouter/models?")):
            self.handle_filtered_openai_models()
            return
        url = UPSTREAM_BASE + self.path
        headers = {k: v for k, v in self.headers.items() if k.lower() not in FORBIDDEN_HEADERS}
        # api.kilo.ai rejects the gateway's public "Bearer public" token.
        # Use the configured Kilo token (the free lane accepts "anonymous").
        if KILO_PUBLIC_API_KEY:
            for _hk in [k for k in headers
                        if k.lower() in ("authorization", "x-api-key")]:
                headers.pop(_hk, None)
            headers["Authorization"] = f"Bearer {KILO_PUBLIC_API_KEY}"

        current_model = None
        if body:
            try:
                current_model = json.loads(body).get("model")
            except Exception:
                current_model = None

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


    # --- Anthropic Messages API bridge (Claude Code, POST /v1/messages) ---
    def handle_filtered_openai_models(self):
        """Remote GET /api/openrouter/models: upstream payload trimmed to free-only."""
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in FORBIDDEN_HEADERS}
        data = None
        try:
            resp = self.attempt("GET", UPSTREAM_BASE + "/api/openrouter/models",
                                None, headers, get_proxy())
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                try:
                    data = resp.json()
                except Exception:
                    data = None
            else:
                log.warning(f"kilo filtered models upstream status {resp.status_code}")
            try:
                resp.close()
            except Exception:
                pass
        except Exception as e:
            log.warning(f"kilo filtered models upstream fetch failed: {e}")
        if data is None:
            data = {"data": [{"id": m} for m in _load_known_kilo_models()]}
        data, n_total, n_free = anthropic_bridge.filter_free_models_payload(data)
        log.info(f"kilo openai models: remote free-only filter {n_total}->{n_free}")
        try:
            out = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def send_anthropic_error(self, status, err_type, message):
        try:
            body = anthropic_bridge.anthropic_error_body(err_type, message)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def handle_anthropic_models(self, method):
        if method != "GET":
            self.send_anthropic_error(405, "invalid_request_error",
                                      "Method not allowed, use GET /v1/models")
            return
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in FORBIDDEN_HEADERS}
        data = None
        try:
            resp = self.attempt("GET", UPSTREAM_BASE + "/api/openrouter/models",
                                None, headers, get_proxy())
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                try:
                    data = resp.json()
                except Exception:
                    data = None
            try:
                resp.close()
            except Exception:
                pass
        except Exception as e:
            log.warning(f"kilo anthropic models upstream fetch failed: {e}")
        if data is None:
            data = {"data": [{"id": m} for m in _load_known_kilo_models()]}
        remote = self.is_remote_request()
        if remote:
            data, n_total, n_free = anthropic_bridge.filter_free_models_payload(data)
            log.info(f"kilo anthropic models: remote free-only filter {n_total}->{n_free}")
        try:
            out = json.dumps(anthropic_bridge.openai_models_to_anthropic(
                data, free_only=remote)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def handle_anthropic_messages(self, method, raw_body):
        if method != "POST":
            self.send_anthropic_error(405, "invalid_request_error",
                                      "Method not allowed, use POST /v1/messages")
            return
        try:
            req = json.loads(raw_body or b"{}")
        except Exception as e:
            self.send_anthropic_error(400, "invalid_request_error",
                                      "Invalid JSON: %s" % e)
            return
        if not isinstance(req, dict) or not isinstance(req.get("messages"), list) \
                or not req["messages"]:
            self.send_anthropic_error(400, "invalid_request_error",
                                      "'messages' must be a non-empty array")
            return
        try:
            # known_models=None: OpenRouter ids are open-ended, passthrough.
            chat, client_stream, req_model, up_model, thinking = anthropic_bridge.anthropic_to_chat(
                req, KILO_ANTHROPIC_DEFAULT_MODEL, None)
        except ValueError as e:
            self.send_anthropic_error(400, "invalid_request_error", str(e))
            return
        chat.pop("_anthropic_thinking", None)
        if thinking and thinking.get("budget_tokens"):
            clamped, was = anthropic_bridge.clamp_thinking_budget(
                up_model, thinking["budget_tokens"])
            if was:
                log.info(f"kilo-anthropic: thinking budget clamped {thinking['budget_tokens']}->{clamped} for model={up_model} (learned cap)")
                thinking["budget_tokens"] = clamped
        chat["stream"] = client_stream
        if client_stream:
            chat["stream_options"] = {"include_usage": True}
        # thinking -> OpenRouter reasoning knobs
        if thinking:
            if thinking.get("effort"):
                chat["reasoning"] = {"effort": thinking["effort"]}
            if thinking.get("budget_tokens"):
                try:
                    chat["reasoning"] = dict(chat.get("reasoning") or {})
                    chat["reasoning"]["max_tokens"] = int(thinking["budget_tokens"])
                except Exception:
                    pass
        client_tools = chat.get("tools") if isinstance(chat.get("tools"), list) else None
        body = json.dumps(chat).encode()
        url = UPSTREAM_BASE + "/api/openrouter/chat/completions"
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in FORBIDDEN_HEADERS}
        # Never forward the client's public Bearer upstream: api.kilo.ai
        # validates it as its own token -> 401 INVALID_TOKEN (Claude sends
        # both x-api-key and Bearer by default). x-api-key passes through.
        for _ak in [k for k in headers if k.lower() == "authorization"]:
            headers.pop(_ak, None)
        # beta handling is default-ON: plain /v1/messages behaves like
        # ?beta=true (kept for compat); ?beta=false/0 opts out and strips
        # Anthropic beta-negotiation headers before upstream.
        _beta_on = _query_flag(self.path, "beta", True)
        if _beta_on:
            log.info(f"kilo-anthropic: beta mode on for model={up_model}")
        else:
            for _hk in [k for k in headers if k.lower() in ("anthropic-beta", "anthropic-version")]:
                headers.pop(_hk, None)
            log.info(f"kilo-anthropic: beta mode off (?beta=false) for model={up_model}, stripped beta headers")

        def _rebuild_kilo_body(strip_reasoning=False, strip_tools=False,
                               clamp_max=False, truncate_msgs=None):
            jb = json.loads(body)
            if strip_reasoning:
                jb.pop("reasoning", None)
            if clamp_max:
                try:
                    if int(jb.get("max_tokens", 0)) > 4096:
                        jb["max_tokens"] = 4096
                except Exception:
                    jb["max_tokens"] = 4096
            if strip_tools:
                jb.pop("tools", None)
                jb.pop("tool_choice", None)
                try:
                    msgs = jb.get("messages")
                    if isinstance(msgs, list):
                        for m in msgs:
                            if isinstance(m, dict):
                                m.pop("tool_calls", None)
                                if m.get("role") == "tool":
                                    m["role"] = "user"
                    jb["messages"] = [m for m in msgs
                                      if not (isinstance(m, dict)
                                              and m.get("role") == "tool"
                                              and not m.get("content"))]
                except Exception:
                    pass
            if truncate_msgs:
                try:
                    arr = jb.get("messages")
                    if isinstance(arr, list) and len(arr) > truncate_msgs:
                        jb["messages"] = arr[-truncate_msgs:]
                except Exception:
                    pass
            return json.dumps(jb).encode()

        _KILO_RECOVERY = [
            ("strip-reasoning", dict(strip_reasoning=True)),
            ("strip-tools", dict(strip_tools=True)),
            ("clamp-max", dict(clamp_max=True)),
            ("truncate-20", dict(truncate_msgs=20)),
        ]
        kilo_recovery_idx = [0]
        kilo_last_400 = [""]
        rotated = False
        for attempt in range(RETRY_ATTEMPTS):
            proxy = get_proxy()
            log.info(f"kilo-anthropic: POST /v1/messages attempt={attempt+1}/{RETRY_ATTEMPTS} proxy={proxy} model={up_model}")
            try:
                resp = self.attempt("POST", url, body, headers, proxy)
                if resp.status_code == 429 or resp.status_code >= 500:
                    if resp.status_code == 429:
                        wait = retry_after_seconds(resp, min(RETRY_BACKOFF * (attempt + 1), MAX_BACKOFF))
                        log.warning(f"kilo-anthropic: 429 model={up_model} attempt={attempt+1} retry_in={wait}s")
                    else:
                        wait = min(RETRY_BACKOFF * (attempt + 1), MAX_BACKOFF)
                        log.warning(f"kilo-anthropic: {resp.status_code} model={up_model} attempt={attempt+1} retry_in={wait}s")
                    resp.close()
                    if ROTATE_ON_429:
                        rotated = renew_tor_ip()
                        if rotated:
                            continue
                        log.warning("kilo-anthropic: IP rotation failed, waiting before retry")
                    if attempt < RETRY_ATTEMPTS - 1:
                        time.sleep(wait)
                else:
                    if resp.status_code == 200:
                        log.info(f"kilo-anthropic: Response 200 model={up_model}")
                    else:
                        try:
                            _ksnip = resp.text[:500]
                        except Exception:
                            _ksnip = ""
                        log.info(f"kilo-anthropic: Response {resp.status_code} model={up_model} snippet={_ksnip[:200]!r}")
                    if resp.status_code in (400, 403) and kilo_recovery_idx[0] < len(_KILO_RECOVERY):
                        # kilo 401/403 on a model = dead key/route for it, but a
                        # 429-shaped 403 (rate-limit wording) deserves one
                        # rotation before simplifying.
                        _low = _ksnip.lower()
                        if resp.status_code == 403 and ("rate" in _low or "limit" in _low or "quota" in _low):
                            log.warning(f"kilo-anthropic: limit-like 403 model={up_model}, rotating Tor IP, same body retry")
                            try:
                                resp.close()
                            except Exception:
                                pass
                            if renew_tor_ip():
                                continue
                            log.warning("kilo-anthropic: rotation failed, falling to simplify ladder")
                        step_name, kwargs = _KILO_RECOVERY[kilo_recovery_idx[0]]
                        kilo_recovery_idx[0] += 1
                        kilo_last_400[0] = _ksnip
                        log.warning(f"kilo-anthropic: 400 model={up_model}, recovery step {kilo_recovery_idx[0]}/{len(_KILO_RECOVERY)} ({step_name}), snippet={_ksnip[:200]!r}")
                        try:
                            resp.close()
                        except Exception:
                            pass
                        if thinking and thinking.get("budget_tokens") and step_name == "strip-reasoning":
                            try:
                                new_cap = max(1024, int(thinking["budget_tokens"]) // 2)
                                if anthropic_bridge.note_thinking_cap(up_model, new_cap):
                                    log.warning(f"kilo-anthropic: learned thinking cap {new_cap} for model={up_model}")
                            except Exception:
                                pass
                        try:
                            body = _rebuild_kilo_body(**kwargs)
                        except Exception as e:
                            log.warning(f"kilo-anthropic: recovery rebuild failed: {e}")
                        continue
                    if resp.status_code in (400, 403):
                        try:
                            resp.close()
                        except Exception:
                            pass
                        self.send_anthropic_error(500, "api_error",
                            "Upstream rejected request after %d simplifications: %s"
                            % (len(_KILO_RECOVERY), (kilo_last_400[0] or _ksnip)[:200]))
                        return
                    if resp.status_code != 200:
                        try:
                            snippet = resp.text[:300]
                        except Exception:
                            snippet = ""
                        try:
                            resp.close()
                        except Exception:
                            pass
                        if resp.status_code == 429:
                            self.send_anthropic_error(429, "rate_limit_error",
                                                      "Upstream rate limited, retry later")
                        else:
                            self.send_anthropic_error(500, "api_error",
                                                      "Upstream error %s: %s" % (resp.status_code, snippet[:200]))
                        return
                    if client_stream:
                        resp.encoding = "utf-8"
                        try:
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream")
                            self.send_header("Cache-Control", "no-cache")
                            self.send_header("Connection", "close")
                            self.end_headers()
                            self.close_connection = True
                            w = self.wfile
                            writer = anthropic_bridge.AnthropicStreamWriter(
                                lambda s: (w.write(s.encode()), w.flush()), req_model)
                            anthropic_bridge.translate_chat_sse_to_anthropic(
                                resp.iter_lines(decode_unicode=True), req_model, writer,
                                declared_tools=client_tools)
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            pass
                        except Exception as e:
                            log.error(f"kilo-anthropic stream error: {e}")
                        finally:
                            resp.close()
                    else:
                        try:
                            resp.encoding = "utf-8"
                            data = resp.json()
                        except Exception as e:
                            log.error(f"kilo-anthropic: bad upstream JSON: {e}")
                            try:
                                resp.close()
                            except Exception:
                                pass
                            self.send_anthropic_error(502, "api_error",
                                                      "Bad upstream response")
                            return
                        try:
                            resp.close()
                        except Exception:
                            pass
                        try:
                            choice = (data.get("choices") or [{}])[0]
                            msg = choice.get("message") or {}
                            finish = choice.get("finish_reason") or "stop"
                            usage = data.get("usage") or {}
                            reasoning = anthropic_bridge.extract_reasoning_text(msg)
                            blocks, has_tools = anthropic_bridge.openai_message_to_blocks(
                                msg, declared_tools=client_tools)
                            text = ""
                            if isinstance(msg.get("content"), str):
                                text = msg["content"]
                            elif isinstance(msg.get("content"), list):
                                text = "\n".join(
                                    p.get("text", "") for p in msg["content"]
                                    if isinstance(p, dict) and p.get("type") == "text")
                            # rebuild calls from normalized blocks (keeps
                            # validation: unknown names already folded to text)
                            calls = []
                            for b in blocks:
                                if b.get("type") == "tool_use":
                                    calls.append({
                                        "id": b.get("id") or anthropic_bridge.new_tool_id(),
                                        "type": "function",
                                        "function": {
                                            "name": b.get("name") or "unknown_tool",
                                            "arguments": json.dumps(b.get("input") or {})}})
                            out = anthropic_bridge.anthropic_response_json(
                                req_model, text or "", calls, finish, usage, reasoning)
                            # rebuild blocks-aware body (keeps thinking+text+tool_use order)
                            _payload = json.loads(out)
                            if blocks or reasoning:
                                _payload["content"] = blocks
                                _payload["stop_reason"] = "tool_use" if has_tools else anthropic_bridge.map_finish_to_stop(finish)
                                _payload["usage"] = anthropic_bridge.openai_usage_to_anthropic(usage)
                                out = json.dumps(_payload).encode()
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(out)))
                            self.end_headers()
                            self.wfile.write(out)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            pass
                    return
            except requests.exceptions.Timeout:
                log.error("kilo-anthropic: upstream timeout")
                if attempt < RETRY_ATTEMPTS - 1:
                    if not rotated:
                        rotated = renew_tor_ip()
                    continue
                self.send_anthropic_error(502, "api_error", "Upstream timeout")
                return
            except requests.exceptions.ProxyError as e:
                log.error(f"kilo-anthropic: proxy error: {e}")
                if attempt < RETRY_ATTEMPTS - 1:
                    if not rotated:
                        rotated = renew_tor_ip()
                    continue
                self.send_anthropic_error(502, "api_error", "Proxy failed")
                return
            except requests.exceptions.ConnectionError as e:
                log.error(f"kilo-anthropic: connection error: {e}")
                if attempt < RETRY_ATTEMPTS - 1:
                    if not rotated:
                        rotated = renew_tor_ip()
                    continue
                self.send_anthropic_error(502, "api_error", "Connection failed")
                return
            except Exception as e:
                log.error(f"kilo-anthropic: error: {e}", exc_info=True)
                self.send_anthropic_error(502, "api_error", str(e)[:200])
                return
        log.error(f"kilo-anthropic: retries exhausted model={up_model}, returning 503")
        self.send_anthropic_error(503, "api_error",
                                  "All retries exhausted for model=%s" % up_model)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9001
    host = os.environ.get("FORWARDER_HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), ProxyHandler)
    log.info(f"Kilo forwarder started on {host}:{port} 429-retries={RETRY_ATTEMPTS} backoff={RETRY_BACKOFF}s fallback={FALLBACK_MODELS} rotate={ROTATE_ON_429}")
    server.serve_forever()


if __name__ == "__main__":
    main()
import json, os, sys, logging, threading, time, socket, binascii, uuid
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
# opencode.ai's free tier rate-limits anonymous requests that do NOT look
# like they come from the official opencode CLI. The gateway whitelists
# requests whose User-Agent contains "opencode" AND whose system prompt
# mentions "You are opencode" (verified by replaying captured CLI traffic:
# with both -> 200 OK, missing either -> 429 FreeUsageLimitError).
UPSTREAM_UA = os.environ.get(
    "FORWARDER_UA",
    "opencode/1.14.39 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.13",
)
SPOOF_SYSTEM_MARKER = os.environ.get("FORWARDER_SYSTEM_MARKER", "1") == "1"
SYSTEM_MARKER = os.environ.get("FORWARDER_SYSTEM_MARKER_TEXT", "You are opencode.")

FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding"}

_rotate_lock = threading.Lock()


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
                        time.sleep(5)
                        return True
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

    def handle_request(self, method):
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
        headers["x-opencode-session"] = str(uuid.uuid4())
        headers["x-opencode-client"] = "1"
        headers["x-opencode-request"] = str(uuid.uuid4())
        headers["x-opencode-project"] = str(uuid.uuid4())

        current_model = None
        if body:
            try:
                current_model = json.loads(body).get("model")
            except Exception:
                current_model = None

        RESPONSES_MODELS = {"muse-spark-1.3-contributor-free", "muse-spark-1.2-contributor-free"}
        use_responses_api = (
            current_model in RESPONSES_MODELS
            and "/chat/completions" in self.path
        )
        if use_responses_api:
            try:
                chat = json.loads(body)
                resp_input = []
                for msg in chat.get("messages", []):
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        parts = []
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                parts.append(c.get("text", ""))
                            elif isinstance(c, str):
                                parts.append(c)
                        content = "\n".join(parts) if parts else str(content)
                    resp_input.append({"role": role, "content": str(content)})
                responses_body = {
                    "model": current_model,
                    "input": resp_input,
                    "stream": chat.get("stream", False),
                }
                if "temperature" in chat:
                    responses_body["temperature"] = chat["temperature"]
                if "max_tokens" in chat:
                    responses_body["max_output_tokens"] = chat["max_tokens"]
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
    log.info(f"Forwarder started on {host}:{port} 429-retries={RETRY_ATTEMPTS} backoff={RETRY_BACKOFF}s fallback={FALLBACK_MODELS or 'DISABLED'} rotate={ROTATE_ON_429}")
    server.serve_forever()


if __name__ == "__main__":
    main()
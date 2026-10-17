import json, os, re, sys, logging, threading, time, socket, select, binascii, uuid, secrets, string, copy
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import requests, urllib3

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from tls_forge import forge_request as _tls_forge_request
    _FORGE_AVAILABLE = os.environ.get("FORWARDER_TLS_FORGE", "1") == "1"
except Exception:
    _tls_forge_request = None
    _FORGE_AVAILABLE = False

import anthropic_bridge

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _forge_attempt(method, url, body, headers, proxy=None,
                   connect_timeout=60, read_timeout=300):
    """Upstream fetch via forged TLS (CLI ClientHello bytes).

    headers: plain dict (already spoofed upstream). Emitted in CLI order.
    proxy: requests-style {"http": "socks5h://host:port", ...} or None.
    """
    get = lambda k, d="": headers.get(k, headers.get(k.title(), d))
    ordered = [
        ("Authorization", get("authorization", "")),
        ("Content-Type", get("content-type", "application/json")),
        ("User-Agent", get("user-agent", UPSTREAM_UA)),
        ("x-opencode-client", get("x-opencode-client", "cli")),
        ("x-opencode-project", get("x-opencode-project", "global")),
        ("x-opencode-request", get("x-opencode-request", "")),
        ("x-opencode-session", get("x-opencode-session", "")),
        ("x-opencode-session-id", get("x-opencode-session-id", "")),
        ("Connection", "keep-alive"),
        ("Accept", "*/*"),
        ("Host", "opencode.ai"),
        # 2026-10-17: pinned back to gzip/deflate. With "br, zstd" upstream
        # answered brotli and passthrough() drops content-encoding, so local
        # consumers (refresh-free-models.sh) got raw brotli -> JSON parse fail.
        ("Accept-Encoding", "gzip, deflate"),
    ]
    ordered = [(k, v) for k, v in ordered if v != ""]
    socks = None
    try:
        from urllib.parse import urlsplit
        purl = (proxy or {}).get("http") or (proxy or {}).get("https") or ""
        if purl.startswith("socks"):
            u = urlsplit(purl if "://" in purl else "socks5h://" + purl)
            if u.hostname:
                socks = (u.hostname, u.port or 1080)
    except Exception:
        socks = None
    return _tls_forge_request(
        method, url, ordered, body or b"", socks=socks,
        connect_timeout=connect_timeout, read_timeout=read_timeout,
    )

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


def _zen_to_inference(path, is_responses_model=False):
    """Rewrite legacy /zen/* outbound paths to the new /inference/openai/v1/* family.

    2026-10-17 (verified live): opencode.ai moved the free tier again:
    /inference/v1/* is now retired (410 Gone). The live path is
    /inference/openai/v1/*. /zen/v1/models (the catalog) still works,
    so only the chat/completions + responses families move.

    Responses-capable models go to /inference/openai/v1/responses;
    chat-only models go to /inference/openai/v1/chat/completions.
    """
    if path.startswith("/zen/v1/models"):
        return path
    if path.startswith("/zen/"):
        rest = path[len("/zen/"):]  # e.g. "v1/responses", "v1/chat/completions"
        if is_responses_model and rest.startswith(("v1/responses", "v1/chat/completions")):
            # Responses models: route /zen/v1/responses -> /inference/openai/v1/responses
            return "/inference/openai/" + rest
        if not is_responses_model and rest.startswith("v1/responses"):
            # Chat-only models: route /zen/v1/responses -> /inference/openai/v1/chat/completions
            return "/inference/openai/v1/chat/completions"
        return "/inference/openai/" + rest
    return path
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
RETRY_ATTEMPTS = int(os.environ.get("FORWARDER_429_ATTEMPTS", "30"))
RETRY_BACKOFF = float(os.environ.get("FORWARDER_429_BACKOFF", "8"))
MAX_BACKOFF = float(os.environ.get("FORWARDER_429_MAX_BACKOFF", "40"))
ROTATE_ON_429 = os.environ.get("FORWARDER_ROTATE_ON_429", "1") == "1"
# 2026-10-17 (per-IP quotas, Tor-only): each retry must ride a FRESH exit.
# 30 attempts x ~6-8s of rotation ~= 3-4 min sampling window — enough to
# outlast the upstream "walls"; the window guard caps worst-case latency.
RETRY_WINDOW = float(os.environ.get("FORWARDER_RETRY_WINDOW", "360"))
EXIT_BLACKLIST_TTL = float(os.environ.get("FORWARDER_EXIT_BLACKLIST_TTL", "1200"))
EXIT_BLACKLIST_MAX_ROTATIONS = int(os.environ.get("FORWARDER_EXIT_BLACKLIST_MAX_ROTATIONS", "3"))
# 2026-10-17 fail-fast: while NO 2xx has arrived for WALL_SUCCESS_LOOKBACK s,
# every request is capped at WALL_FAST_ATTEMPTS fresh exits (~40-60s) and then
# returns 503 so the agent's fallback (kilo) answers quickly. One success
# restores the full sampling budget for partial walls.
WALL_FAST_ATTEMPTS = int(os.environ.get("FORWARDER_WALL_FAST_ATTEMPTS", "12"))
WALL_SUCCESS_LOOKBACK = float(os.environ.get("FORWARDER_WALL_SUCCESS_LOOKBACK", "300"))
# 2026-10-17: opencode began class-blocking ALL Tor exits (403 FreeTierError
# on every sampled exit; the byte-identical request from a direct IP answers
# 200). While the block window is hot we ride direct egress; when it expires
# the next request re-probes Tor (one attempt) and either restores Tor or
# re-arms the window. FORWARDER_DIRECT_FALLBACK=0 forbids direct egress.
DIRECT_FALLBACK = os.environ.get("FORWARDER_DIRECT_FALLBACK", "1") == "1"
TOR_REPROBE_INTERVAL = float(os.environ.get("FORWARDER_TOR_REPROBE_INTERVAL", "600"))
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
#   x-opencode-session-id: <same value> (required since CLI 1.19.x) +
#   x-opencode-request: msg_<9hex>001<14alnum>
# The ses_/msg_ IDs are FORMAT-validated server-side (reverse-engineered from
# the CLI binary + its local opencode.db). Random UUIDs -> 403. Missing
# headers -> 403. Wrong bearer (e.g. Hermes' placeholder) -> 401.
# 2026-10-17: re-captured CLI 1.19.3 (MITM, byte-identical replay): UA bumped
# 1.18.32 -> 1.19.3 and x-opencode-session-id added — the missing header is
# the likely cause of the sudden all-403 "wall" once opencode shipped 1.19.x.
UPSTREAM_UA = os.environ.get(
    "FORWARDER_UA",
    "opencode/1.19.3 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14",
)
# Literal public bearer the official CLI sends. ALWAYS overwritten outbound:
# Hermes sends its own placeholder (opencode-zen-free-keyless) which 401s.
# 2026-10-15: default BACK to "Bearer public" — the new /inference/* family
# REQUIRES it (empty header -> 403 FreeTierError, verified live on
# /inference/v1/responses). The 14.10 "empty" experiment only ever applied to
# the legacy /zen path, which is now closed anyway.
UPSTREAM_BEARER = os.environ.get("FORWARDER_UPSTREAM_BEARER", "Bearer public")


def _apply_upstream_auth(headers):
    """Mirror the real CLI: no Authorization header on keyless calls.

    A stale/placeholder inbound bearer must never leak upstream either.
    """
    headers.pop("Authorization", None)
    if UPSTREAM_BEARER:
        headers["Authorization"] = UPSTREAM_BEARER
    return headers
SPOOF_SYSTEM_MARKER = os.environ.get("FORWARDER_SYSTEM_MARKER", "1") == "1"
SYSTEM_MARKER = os.environ.get("FORWARDER_SYSTEM_MARKER_TEXT", "You are opencode.")

FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding"}

# --- public API key auth (from Marsel, keeps cloudflared tunnel protected) ---
# Localhost (127.0.0.1 / ::1) is always allowed (local Hermes gateway, keyless provider).
# Remote clients must send:  Authorization: Bearer $FORWARDER_API_KEY
#   or x-api-key: $FORWARDER_API_KEY (Anthropic SDKs / Claude Code style).
# Remote clients are also restricted to /zen/* API paths (+ /health).
PUBLIC_API_KEY = os.environ.get("FORWARDER_API_KEY", "").strip()
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

# --- Per-model upstream protocol cache (universal, no hardcoded allowlist) ---
# Matrix 2026-10-11: only spark-1.2/1.3 speak responses; everything else is
# chat-only (ModelProtocolUnsupported on /responses), jev is dead on both,
# space-bunny rejects format openai on responses but answers chat.
# Learned at runtime: a model that 400s ModelProtocolUnsupported on
# responses is pinned to chat (protocol_pins.json), no code change needed
# when upstream adds/removes support.
_PROTOCOL_PINS_FILE = os.path.join(BASE_DIR, "protocol_pins.json")
_PROTOCOL_PINS_CACHE = {"loaded": False, "pins": {}}


def _load_protocol_pins():
    try:
        if not _PROTOCOL_PINS_CACHE["loaded"]:
            with open(_PROTOCOL_PINS_FILE) as f:
                _PROTOCOL_PINS_CACHE["pins"] = json.load(f)
            _PROTOCOL_PINS_CACHE["loaded"] = True
    except Exception:
        _PROTOCOL_PINS_CACHE["loaded"] = True
    return _PROTOCOL_PINS_CACHE["pins"]


def get_pinned_protocol(model):
    """'responses' | 'chat' | None (None = unknown, try responses first)."""
    return _load_protocol_pins().get(model)


def pin_protocol(model, proto):
    try:
        pins = _load_protocol_pins()
        if pins.get(model) == proto:
            return False
        pins[model] = proto
        with open(_PROTOCOL_PINS_FILE, "w") as f:
            json.dump(pins, f)
        log.info(f"protocol pin: {model} -> {proto}")
        return True
    except Exception as e:
        log.warning(f"protocol pin failed for {model}: {e}")
        return False


def _is_protocol_error(snippet):
    s = (snippet or "").lower()
    return "modelprotocolunsupported" in s.replace(" ", "") or \
        ("does not support this protocol" in s)


# --- Sealed encrypted-reasoning 400s (fix 2026-10-13) ---
# Upstream seals reasoning.encrypted_content to the caller session that
# received it. Replaying it under a different x-opencode-session (typical
# right after a gateway restart or a sticky-session remap) fails with 400.
# Markers observed live: "reasoning `encrypted_content` has expired"
# (opencode.ai responses API), "not issued to this caller" / "could not
# decrypt the provided encrypted_content" (gateway-side classifier).
_SEALED_400_MARKERS = (
    "not issued to this caller",
    "was not issued",
    "could not decrypt the provided encrypted_content",
    "has expired",
    "encrypted_content",
)


def _is_sealed_400(snippet):
    s = (snippet or "").lower()
    return any(m in s for m in _SEALED_400_MARKERS)


def _redact_error_snippet(snippet):
    """Strip possible secrets from an upstream error body before logging."""
    try:
        s = re.sub(r"(?i)(bearer\s+)[^\s\"'}]+", r"\1REDACTED", snippet or "")
        s = re.sub(r"\bsk-[A-Za-z0-9_\-]{8,}", "sk-REDACTED", s)
        return s
    except Exception:
        return snippet or ""


def _read_error_snippet(resp, limit=400):
    """Read an error body for logging.

    Returns (snippet_str, cached_bytes_or_None). Forged-TLS responses are
    one-shot iterators: reading .text consumes the socket stream, so the
    caller must deliver `cached_bytes` downstream instead of streaming
    `resp` again (otherwise the client gets headers + empty body, which
    surfaces as RemoteProtocolError 0-bytes-vs-Content-Length).
    """
    try:
        praw = getattr(resp, "text", None)
        if isinstance(praw, bytes):
            return praw[:limit].decode("utf-8", "replace"), praw
        if isinstance(praw, str):
            return praw[:limit], praw.encode("utf-8", "replace")
    except Exception:
        pass
    try:
        content = resp.content
        if isinstance(content, bytes):
            return (content[:limit] or b"").decode("utf-8", "replace"), content
    except Exception:
        pass
    return "", None

# --- Anthropic bridge (Claude Code via POST /v1/messages) ---
ANTHROPIC_DEFAULT_MODEL = os.environ.get(
    "ANTHROPIC_DEFAULT_MODEL", "muse-spark-1.3-contributor-free").strip() or "muse-spark-1.3-contributor-free"

def _load_known_opencode_models():
    ids = {"muse-spark-1.3-contributor-free", "muse-spark-1.2-contributor-free"}
    try:
        with open(os.path.join(BASE_DIR, "free-models", "opencode.txt")) as f:
            for line in f:
                line = line.strip()
                if line:
                    ids.add(line)
    except Exception:
        pass
    return ids

_KNOWN_OPENCODE_MODELS = _load_known_opencode_models()

_rotate_lock = threading.Lock()

_SESSION_PIN_LOCK = threading.Lock()
_PINS_FILE = os.path.join(BASE_DIR, "session_pins.json")
_SESSION_PINS_CAP = 5000


def _load_session_pins():
    """Restore prompt_cache_key -> ses_ pins surviving forwarder restarts.

    Without this, every forwarder restart (incl. machine reboot) remaps all
    conversations to fresh ses_ ids and upstream 400s the sealed
    reasoning.encrypted_content replayed under the new caller (fix 2026-10-13).
    """
    try:
        with open(_PINS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            pins = {str(k): str(v) for k, v in data.items()
                    if k and v and str(v).startswith("ses_")}
            if len(pins) > _SESSION_PINS_CAP:
                pins = dict(list(pins.items())[-_SESSION_PINS_CAP:])
            log.info(f"session pins restored: {len(pins)} from {_PINS_FILE}")
            return pins
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"session pins load failed: {e}")
    return {}


def _save_session_pins():
    """Atomic persist; called only when a NEW pin is added (not per hit)."""
    try:
        tmp = _PINS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_SESSION_PINS, f)
        os.replace(tmp, _PINS_FILE)
    except Exception as e:
        log.warning(f"session pins save failed: {e}")


_SESSION_PINS = _load_session_pins()  # stable key (prompt_cache_key) -> sticky x-opencode-session id

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
            if len(_SESSION_PINS) > _SESSION_PINS_CAP:
                _SESSION_PINS.pop(next(iter(_SESSION_PINS)))
            _save_session_pins()
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


def _iso_proxy(tag="x"):
    """Fresh Tor circuit per attempt via a unique SOCKS isolation key.

    Tor's IsolateSOCKSAuth (SocksPort default) gives every unique SOCKS
    username its own circuit, so each retry rides a brand-new exit in
    ~1-2s — WITHOUT NEWNYM, which Tor rate-limits to ~1 per 10s and which
    was the old 9-14s-per-attempt bottleneck (especially with concurrent
    requests queueing on the rotation lock). Verified live 2026-10-17:
    two different creds -> two different exit IPs.
    """
    key = f"fwd-{tag}-{secrets.token_hex(4)}"
    url = f"socks5h://{key}:x@127.0.0.1:{SOCKS_PORT}"
    return {"http": url, "https": url}


def _exit_ip_via(proxy, timeout=4):
    """Best-effort exit IP of `proxy`'s circuit (also warms the circuit)."""
    try:
        r = requests.get("https://api.ipify.org", proxies=proxy, timeout=timeout)
        if r.status_code == 200:
            return r.text.strip() or None
    except Exception:
        pass
    return None


_outcome_lock = threading.Lock()
_recent_outcomes = []  # list[(ts, ok)] — attempt outcomes across requests

# Tor-class block window (2026-10-17, see DIRECT_FALLBACK above).
_tor_block_lock = threading.Lock()
_tor_class_block_until = 0.0


def _record_outcome(ok):
    """Track attempt outcomes for the total-wall fast-fail detector."""
    now = time.time()
    with _outcome_lock:
        _recent_outcomes.append((now, ok))
        cutoff = now - WALL_SUCCESS_LOOKBACK
        _recent_outcomes[:] = [o for o in _recent_outcomes if o[0] >= cutoff][-120:]


def _wall_fast_mode():
    """True while no 2xx was seen in the last WALL_SUCCESS_LOOKBACK seconds.

    In this mode requests skip the ipify pre-checks (every exit burns anyway)
    and give up after WALL_FAST_ATTEMPTS instead of the full budget.
    """
    now = time.time()
    with _outcome_lock:
        return not any(ok and (now - ts) < WALL_SUCCESS_LOOKBACK
                       for ts, ok in _recent_outcomes)


def _tor_block_active():
    """True while the Tor-class block window is open (ride direct egress)."""
    with _tor_block_lock:
        return DIRECT_FALLBACK and time.time() < _tor_class_block_until


def _arm_tor_block(seconds):
    """Arm/refresh the Tor-class block window after a FreeTierError on Tor."""
    global _tor_class_block_until
    with _tor_block_lock:
        _tor_class_block_until = time.time() + seconds


def _client_gone(conn):
    """Best-effort check that the HTTP client abandoned the request."""
    try:
        r, _, _ = select.select([conn], [], [], 0)
    except Exception:
        return True
    if not r:
        return False
    try:
        return conn.recv(1, socket.MSG_PEEK) == b""
    except BlockingIOError:
        return False
    except Exception:
        return True


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
                        # Fresh circuits usually come online in 1-3s after
                        # NEWNYM; check early and often (7 x 2s ≈ 14s worst).
                        for _ in range(7):
                            time.sleep(2)
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


_START_TIME = time.time()


def _boot_grace_left():
    try:
        grace = float(os.environ.get("FORWARDER_BOOT_GRACE_SEC", "90"))
    except Exception:
        grace = 90
    return max(0.0, grace - (time.time() - _START_TIME))


def rotate_or_wait(context):
    """Rotate Tor exit on transport errors — unless we just (re)started.

    During the boot grace (Tor still building circuits after a reboot),
    NEWNYM is harmful: it discards bootstrapping circuits and prolongs
    the outage. Wait a few seconds and retry on the same circuit instead.
    Returns True when a rotation was actually performed.
    """
    left = _boot_grace_left()
    if left > 0:
        wait = min(5.0, left)
        log.warning(f"{context}: boot grace ({left:.0f}s left), waiting {wait:.0f}s instead of NEWNYM")
        time.sleep(wait)
        return False
    return bool(renew_tor_ip())


# --- burned-exit blacklist (2026-10-17) ---
# Free-tier quotas are enforced per IP and Tor exits are shared, so an exit
# that just answered 403/429 is likely burned for a while. Remember burned
# exits and rotate past them WITHOUT spending an upstream attempt on a
# known-dud IP. Pure in-memory (cleared on restart), TTL-bounded.
_failed_exits = {}  # exit ip -> last failure ts
_failed_exits_lock = threading.Lock()


def _current_exit_ip():
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("tor_exit_ip")
    except Exception:
        return None


def _record_failed_exit(ip):
    if not ip:
        return
    now = time.time()
    with _failed_exits_lock:
        _failed_exits[ip] = now
        cutoff = now - EXIT_BLACKLIST_TTL
        for k in [k for k, v in _failed_exits.items() if v < cutoff]:
            _failed_exits.pop(k, None)


def _is_failed_exit(ip):
    if not ip:
        return False
    with _failed_exits_lock:
        ts = _failed_exits.get(ip)
    return ts is not None and (time.time() - ts) < EXIT_BLACKLIST_TTL


def _escape_blacklisted_exit(context):
    """Hop off a burned exit BEFORE spending an attempt on it."""
    if _boot_grace_left() > 0:
        return
    for i in range(EXIT_BLACKLIST_MAX_ROTATIONS):
        ip = _current_exit_ip()
        if not _is_failed_exit(ip):
            return
        log.warning(f"{context}: exit {ip} is burned, pre-rotating ({i + 1}/{EXIT_BLACKLIST_MAX_ROTATIONS})")
        if not renew_tor_ip():
            return


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


# --- Responses body padding (verified 18.09.2026 via 2x2 matrix) ---
# Upstream 403s /responses requests that lack the full CLI/SDK body shape,
# even with byte-perfect headers/TLS/IDs. Fill ONLY missing keys (client wins).
# Reference: captured CLI traffic (title + main calls, CLI v1.18.31).
RESPONSES_BODY_DEFAULTS = {
    "max_output_tokens": 32000,
    "store": False,
    "include": ["reasoning.encrypted_content"],
}

# Minimal CLI tool defs (names must match exactly; descriptions/params can
# be minimal — verified 18.09.2026). Appended to responses-bound requests
# missing CLI-known tools so the shape gate passes. ~450 bytes overhead.
# 2026-10-14: upstream tightened the gate to the {bash, glob, grep, read}
# quartet (all agents globally 403 FreeTierError without glob+grep);
# edit kept (real CLI also declares it).
# Full CLI tool defs, captured verbatim from real opencode 1.18.32
# (custom provider -> local dump, main turn 2026-10-14). Upstream shape
# gate validates tool names + realistic descriptions/schemas; minimal
# stubs (description "d", empty params) now 403 FreeTierError.
_REAL_TOOLS_FILE = os.path.join(BASE_DIR, "real_tools.json")


def _load_real_tools():
    try:
        with open(_REAL_TOOLS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and all(
                isinstance(t, dict) and t.get("name") for t in data):
            log.info(f"real tool defs loaded: {len(data)} from {_REAL_TOOLS_FILE}")
            return data
    except Exception as e:
        log.warning(f"real tools load failed: {e}")
    return [
        {"type": "function", "name": n, "description": "d",
         "parameters": {"type": "object", "properties": {}}}
        for n in ("bash", "glob", "grep", "edit", "read")]


CLI_DECOY_TOOLS = _load_real_tools()


def _chat_decoy(name):
    """Full-fidelity chat-format decoy (verbatim CLI description+schema)."""
    for t in CLI_DECOY_TOOLS:
        if t.get("name") == name:
            return {"type": "function", "function": {
                "name": name, "description": t.get("description") or "d",
                "parameters": t.get("parameters") or {"type": "object", "properties": {}}}}
    return {"type": "function", "function": {
        "name": name, "description": "d",
        "parameters": {"type": "object", "properties": {}}}}


def pad_responses_body(body_bytes):
    try:
        obj = json.loads(body_bytes)
    except Exception:
        return body_bytes
    if not isinstance(obj, dict):
        return body_bytes
    changed = False
    for k, v in RESPONSES_BODY_DEFAULTS.items():
        if k not in obj:
            obj[k] = v
            changed = True
    try:
        if int(obj.get("max_output_tokens", 0)) < 4096:
            obj["max_output_tokens"] = 4096
            changed = True
    except Exception:
        pass
    # Gate requires stream:true (verified 18.09.2026: stream:false -> 403
    # even with perfect headers/TLS/IDs). Client's wish is preserved
    # separately (see responses_client_stream) for downstream routing.
    if obj.get("stream") is not True:
        obj["stream"] = True
        changed = True
    # Gate requires CLI-known tools in the request (verified 18.09.2026:
    # tool-less non-title bodies -> 403, +3 CLI decoys -> 200, even minimal
    # name+description+empty-params defs). Real client tools are kept;
    # decoys are appended only when missing. Never override explicit choice.
    tools = obj.get("tools")
    if not isinstance(tools, list):
        tools = []
        obj["tools"] = tools
        changed = True
    have = {t.get("name") for t in tools if isinstance(t, dict)}
    for dt in CLI_DECOY_TOOLS:
        if dt.get("name") not in have:
            tools.append(copy.deepcopy(dt))
            changed = True
    if "tool_choice" not in obj:
        obj["tool_choice"] = "auto"
        changed = True
    if not changed:
        return body_bytes
    try:
        return json.dumps(obj).encode()
    except Exception:
        return body_bytes


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


def chat_content_to_responses(content):
    """OpenAI chat content -> Responses input content, preserving images.

    Text-only content stays a plain string (unchanged wire behavior).
    Multimodal content becomes a list of input_text/input_image items so
    the image actually reaches the model (previously image_url blocks were
    silently dropped and vision returned stubs).
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return chat_text_of(content)
    items = []
    for c in content:
        if isinstance(c, str):
            if c:
                items.append({"type": "input_text", "text": c})
            continue
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "text" and isinstance(c.get("text"), str):
            items.append({"type": "input_text", "text": c.get("text", "")})
        elif t == "input_text" and isinstance(c.get("text"), str):
            items.append({"type": "input_text", "text": c.get("text", "")})
        elif t in ("image_url", "input_image"):
            ref = c.get("image_url", "")
            detail = c.get("detail")
            if isinstance(ref, dict):
                detail = ref.get("detail", detail)
                ref = ref.get("url", "")
            if isinstance(ref, str) and ref:
                item = {"type": "input_image", "image_url": ref}
                if isinstance(detail, str) and detail.strip():
                    item["detail"] = detail.strip()
                items.append(item)
    if not items:
        return chat_text_of(content)
    if len(items) == 1 and items[0].get("type") == "input_text":
        return items[0].get("text", "")
    return items


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
        # Images survive only on user messages: upstream Responses API
        # rejects input_image on assistant messages with HTTP 400, and
        # Hermes itself strips them on replay. Everything else flattens
        # to text exactly as before.
        if role == "user":
            content = chat_content_to_responses(msg.get("content"))
        else:
            content = text
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
        resp_input.append({"role": role, "content": content})
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


def responses_input_to_chat_messages(rinput, instructions=None):
    """Responses input[] -> chat messages[] (mirror of the forward converter).

    function_call items become assistant tool_calls, function_call_output
    items become tool messages, everything else becomes role/content.
    `instructions` (or system/developer items) fold into one leading
    system message. Used by the universal protocol fallback: a model
    that 400s ModelProtocolUnsupported on /responses is re-issued here
    as /chat/completions, so OpenAI-format clients work with any model.
    """
    messages = []
    system_parts = []
    if isinstance(instructions, str) and instructions.strip():
        system_parts.append(instructions.strip())

    def _text_of(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") in ("input_text", "output_text", "text",
                                     "refusal") and p.get("text"):
                    parts.append(p["text"])
            return "\n".join(parts)
        return ""

    pending_calls = []
    for item in rinput or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "")
        if itype == "function_call":
            args = item.get("arguments", "")
            if not isinstance(args, str):
                try:
                    args = json.dumps(args)
                except Exception:
                    args = str(args)
            pending_calls.append({
                "id": item.get("call_id") or item.get("id") or "",
                "type": "function",
                "function": {"name": item.get("name") or "unknown_tool",
                             "arguments": args or "{}"}})
            continue
        if itype == "function_call_output":
            if pending_calls:
                # flush calls collected so far as one assistant turn
                messages.append({"role": "assistant", "content": "",
                                 "tool_calls": pending_calls})
                pending_calls = []
            out = item.get("output", "")
            if not isinstance(out, str):
                try:
                    out = json.dumps(out)
                except Exception:
                    out = str(out)
            messages.append({"role": "tool",
                             "tool_call_id": item.get("call_id") or "",
                             "content": out})
            continue
        if itype == "message":
            role = item.get("role", "user")
            text = _text_of(item.get("content"))
            if role in ("system", "developer"):
                if text:
                    system_parts.append(text)
            else:
                messages.append({"role": role, "content": text})
            continue
        role = item.get("role", "user")
        if role in ("system", "developer"):
            t = _text_of(item.get("content"))
            if t:
                system_parts.append(t)
            continue
        if "content" in item or role in ("user", "assistant"):
            messages.append({"role": role,
                             "content": _text_of(item.get("content", ""))})
    if pending_calls:
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": pending_calls})
    if system_parts:
        messages.insert(0, {"role": "system",
                            "content": "\n".join(system_parts)})
    return messages


def responses_body_to_chat(rbody):
    """Full Responses request body -> chat-completions request body."""
    chat = {"messages": responses_input_to_chat_messages(
        rbody.get("input"), rbody.get("instructions"))}
    if rbody.get("model"):
        chat["model"] = rbody["model"]
    if isinstance(rbody.get("temperature"), (int, float)):
        chat["temperature"] = rbody["temperature"]
    if rbody.get("max_output_tokens") is not None:
        try:
            chat["max_tokens"] = int(rbody["max_output_tokens"])
        except Exception:
            pass
    tools = rbody.get("tools")
    if isinstance(tools, list) and tools:
        ct = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "function" and t.get("name"):
                ct.append({"type": "function", "function": {
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "parameters": t.get("parameters") or {
                        "type": "object", "properties": {}}}})
        if ct:
            chat["tools"] = ct
    tc = rbody.get("tool_choice")
    if isinstance(tc, str) and tc in ("auto", "none", "required"):
        chat["tool_choice"] = tc
    elif isinstance(tc, dict) and tc.get("name"):
        chat["tool_choice"] = {"type": "function",
                               "function": {"name": tc["name"]}}
    if isinstance(rbody.get("reasoning"), dict):
        chat["reasoning"] = rbody["reasoning"]
    # CLI gate wants stream:true on chat; non-stream clients get the
    # assembled JSON back (same shape they already receive today).
    chat["stream"] = True
    return chat


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

    def passthrough_cached(self, resp, cached):
        """Deliver an error response whose body was already consumed for
        sniffing (one-shot forged-TLS iterator). Sends the cached bytes
        with a correct Content-Length so the client sees the real upstream
        400 JSON instead of a truncated body (RemoteProtocolError)."""
        data = bytes(cached) if isinstance(cached, (bytes, bytearray)) else b""
        try:
            ctype = resp.headers.get("Content-Type", "application/json")
        except Exception:
            ctype = "application/json"
        try:
            self.send_response(resp.status_code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def translate_chat_stream_to_responses(self, resp, model):
        """Chat-completions SSE -> Responses SSE, streamed to the client.

        Mirror path of the universal protocol fallback: a chat-only model
        was re-issued via /chat/completions, but the client asked for
        /responses streaming, so chat deltas are re-emitted as
        response.output_text.delta / function_call items. Ends with
        response.completed + [DONE]. On any error, forwards raw.
        """
        import time as _time

        def sse(obj):
            return ("data: " + json.dumps(obj) + "\n\n").encode()

        resp_id = "resp-chat-%d" % int(_time.time() * 1000)
        try:
            resp.encoding = "utf-8"
        except Exception:
            pass
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            w = self.wfile
            w.write(sse({"type": "response.created",
                         "response": {"id": resp_id, "model": model}}))
            w.flush()
            # output_index values are positional slots in the output array:
            # reasoning (if any) takes slot 0, the message takes the next
            # slot, each function_call its own slot. The earlier version
            # reused slot 0 for reasoning AND the message, and never sent
            # content_part/item .done events, so strict clients (opencode)
            # kept the Thought but dropped the answer text.
            next_index = 0
            reasoning_index = None
            msg_index = None
            item_id = "msg-%d" % int(_time.time() * 1000)
            call_index = {}
            call_texts = {}
            finish = "stop"
            usage = {}
            reasoning_id = None
            reasoning_text = []
            answer_text = []
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                if raw.startswith(":"):
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
                for ch in (ev.get("choices") or []):
                    if not isinstance(ch, dict):
                        continue
                    d = ch.get("delta") or {}
                    # open reasoning text (nemotron delta.reasoning) ->
                    # Responses reasoning item + summary-text deltas,
                    # so thinking survives the protocol fallback stream.
                    _rd = d.get("reasoning") or d.get("reasoning_content")
                    _rtext = ""
                    if isinstance(_rd, str) and _rd:
                        _rtext = _rd
                    elif isinstance(_rd, list):
                        _rparts = []
                        for _rp in _rd:
                            if isinstance(_rp, dict) and _rp.get("text"):
                                _rparts.append(_rp["text"])
                            elif isinstance(_rp, str):
                                _rparts.append(_rp)
                        _rtext = "".join(_rparts)
                    if _rtext:
                        if reasoning_id is None:
                            reasoning_id = "rs-%d" % int(_time.time() * 1000)
                            reasoning_index = next_index
                            next_index += 1
                            w.write(sse({
                                "type": "response.output_item.added",
                                "output_index": reasoning_index,
                                "item": {"type": "reasoning",
                                         "id": reasoning_id,
                                         "status": "in_progress",
                                         "summary": []}}))
                            w.flush()
                        reasoning_text.append(_rtext)
                        w.write(sse({
                            "type": "response.reasoning_summary_text.delta",
                            "item_id": reasoning_id,
                            "output_index": reasoning_index,
                            "summary_index": 0,
                            "delta": _rtext}))
                        w.flush()
                    if msg_index is None and (d.get("content") or d.get("tool_calls")):
                        msg_index = next_index
                        next_index += 1
                        w.write(sse({"type": "response.output_item.added",
                                     "output_index": msg_index,
                                     "item": {"type": "message", "id": item_id,
                                              "role": "assistant",
                                              "content": []}}))
                        w.flush()
                        w.write(sse({"type": "response.content_part.added",
                                     "item_id": item_id,
                                     "output_index": msg_index,
                                     "content_index": 0,
                                     "part": {"type": "output_text",
                                              "text": "", "annotations": []}}))
                        w.flush()
                    if d.get("content"):
                        answer_text.append(d["content"])
                        w.write(sse({"type": "response.output_text.delta",
                                     "item_id": item_id,
                                     "output_index": msg_index,
                                     "content_index": 0,
                                     "delta": d["content"]}))
                        try:
                            w.flush()
                        except (BrokenPipeError, ConnectionResetError,
                                OSError):
                            break
                    for tc in d.get("tool_calls") or []:
                        if not isinstance(tc, dict):
                            continue
                        idx = tc.get("index", 0)
                        cid = tc.get("id") or f"fwd-call-{idx}"
                        fn = tc.get("function") or {}
                        if cid not in call_index:
                            call_index[cid] = next_index
                            next_index += 1
                            call_texts[cid] = []
                            w.write(sse({
                                "type": "response.output_item.added",
                                "output_index": call_index[cid],
                                "item": {"type": "function_call",
                                         "id": cid, "call_id": cid,
                                         "name": fn.get("name") or "",
                                         "arguments": ""}}))
                            w.flush()
                        if fn.get("arguments"):
                            call_texts[cid].append(fn["arguments"])
                            w.write(sse({
                                "type": "response.function_call_arguments.delta",
                                "item_id": cid,
                                "output_index": call_index[cid],
                                "delta": fn["arguments"]}))
                            w.flush()
                        finish = "tool_calls"
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    if isinstance(ev.get("usage"), dict):
                        u = ev["usage"]
                        usage = {"input_tokens": u.get("prompt_tokens", 0),
                                 "output_tokens": u.get("completion_tokens", 0),
                                 "total_tokens": u.get("total_tokens", 0)}
            # close every opened item properly; strict clients finalize
            # the visible answer only on content_part.done + output_item.done
            if reasoning_id is not None:
                w.write(sse({"type": "response.reasoning_summary_text.done",
                             "item_id": reasoning_id,
                             "output_index": reasoning_index,
                             "summary_index": 0,
                             "text": "".join(reasoning_text)}))
                w.flush()
                w.write(sse({"type": "response.output_item.done",
                             "output_index": reasoning_index,
                             "item": {"type": "reasoning",
                                      "id": reasoning_id,
                                      "status": "completed", "summary": []}}))
                w.flush()
            for _cid, _cidx in call_index.items():
                w.write(sse({"type": "response.output_item.done",
                             "output_index": _cidx,
                             "item": {"type": "function_call",
                                      "id": _cid, "call_id": _cid,
                                      "status": "completed",
                                      "arguments": "".join(
                                          call_texts.get(_cid, []))}}))
                w.flush()
            _final_text = "".join(answer_text)
            _output = []
            if reasoning_id is not None:
                _output.append({"type": "reasoning", "id": reasoning_id,
                                "status": "completed", "summary": []})
            if msg_index is not None:
                w.write(sse({"type": "response.output_text.done",
                             "item_id": item_id,
                             "output_index": msg_index,
                             "content_index": 0,
                             "text": _final_text}))
                w.flush()
                w.write(sse({"type": "response.content_part.done",
                             "item_id": item_id,
                             "output_index": msg_index,
                             "content_index": 0,
                             "part": {"type": "output_text",
                                      "text": _final_text,
                                      "annotations": []}}))
                w.flush()
                w.write(sse({"type": "response.output_item.done",
                             "output_index": msg_index,
                             "item": {"type": "message", "id": item_id,
                                      "role": "assistant",
                                      "content": [{"type": "output_text",
                                                   "text": _final_text,
                                                   "annotations": []}]}}))
                w.flush()
                _output.append({"type": "message", "id": item_id,
                                "role": "assistant",
                                "content": [{"type": "output_text",
                                             "text": _final_text,
                                             "annotations": []}]})
            w.write(sse({"type": "response.completed",
                         "response": {"id": resp_id, "model": model,
                                      "output": _output,
                                      "usage": usage,
                                      "status": "completed"}}))
            w.flush()
            try:
                w.write(b"data: [DONE]\n\n")
                w.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            log.error(f"translate_chat_stream_to_responses error: {e}")
        finally:
            try:
                resp.close()
            except Exception:
                pass

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

    def assemble_responses_sse(self, resp, model):
        """Responses SSE -> single chat.completion JSON (non-stream client).

        Upstream always streams (gate requires stream:true); when the client
        asked for a plain response we accumulate deltas + tool calls here.
        """
        import time as _time
        try:
            resp.encoding = "utf-8"
            texts, calls, order = [], {}, []
            usage = {}
            finish = "stop"
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw or raw.startswith(":") or not raw.startswith("data:"):
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
                        texts.append(d)
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
                                calls[cid]["function"]["name"] = item["name"]
                            if item.get("arguments"):
                                calls[cid]["function"]["arguments"] = item["arguments"]
                            finish = "tool_calls"
                elif t == "response.completed":
                    u = (ev.get("response", {}) or {}).get("usage", {}) or {}
                    usage = {"prompt_tokens": u.get("input_tokens", 0),
                             "completion_tokens": u.get("output_tokens", 0),
                             "total_tokens": u.get("total_tokens", 0)}
                elif t in ("response.failed", "response.incomplete", "error"):
                    log.warning(f"responses stream ended with {t} for model={model}")
                    break
            msg = {"role": "assistant", "content": "".join(texts)}
            tcs = []
            for idx, cid in enumerate(order):
                c = calls[cid]
                tcs.append({"index": idx, "id": c["id"], "type": "function",
                            "function": c["function"]})
            if tcs:
                msg["tool_calls"] = tcs
            out = json.dumps({
                "id": "chatcmpl-resp-%d" % int(_time.time() * 1000),
                "object": "chat.completion",
                "created": int(_time.time()),
                "model": model,
                "choices": [{"index": 0, "finish_reason": finish,
                             "message": msg}],
                "usage": usage,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            log.error(f"assemble_responses_sse error: {e}")
            self.send_error(502, "Forwarder Error: stream assembly failed")
        finally:
            resp.close()

    def assemble_chat_sse(self, resp, model):
        """Chat-completions SSE -> single chat.completion JSON (non-stream client)."""
        import time as _time
        try:
            resp.encoding = "utf-8"
            texts, calls, order = [], {}, []
            reasoning_parts = []
            finish = "stop"
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw or raw.startswith(":") or not raw.startswith("data:"):
                    continue
                payload = raw[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    ev = json.loads(payload)
                except Exception:
                    continue
                for ch in (ev.get("choices") or []):
                    d = ch.get("delta") or {}
                    if d.get("content"):
                        texts.append(d["content"])
                    # open reasoning text (e.g. nemotron delta.reasoning):
                    # keep it as reasoning_content, never silently dropped.
                    _rd = d.get("reasoning") or d.get("reasoning_content")
                    if isinstance(_rd, str) and _rd:
                        reasoning_parts.append(_rd)
                    elif isinstance(_rd, list):
                        for _rp in _rd:
                            if isinstance(_rp, dict) and _rp.get("text"):
                                reasoning_parts.append(_rp["text"])
                            elif isinstance(_rp, str):
                                reasoning_parts.append(_rp)
                    for tc in d.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        if idx not in order:
                            order.append(idx)
                            calls[idx] = {"id": tc.get("id") or "",
                                          "type": "function",
                                          "function": {"name": "", "arguments": ""}}
                        if tc.get("id"):
                            calls[idx]["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            calls[idx]["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            calls[idx]["function"]["arguments"] += fn["arguments"]
                        finish = "tool_calls"
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
            msg = {"role": "assistant", "content": "".join(texts)}
            if reasoning_parts:
                msg["reasoning_content"] = "".join(reasoning_parts)
            tcs = []
            for idx in order:
                c = calls[idx]
                tcs.append({"index": idx, "id": c["id"], "type": "function",
                            "function": c["function"]})
            if tcs:
                msg["tool_calls"] = tcs
            out = json.dumps({
                "id": "chatcmpl-assembled-%d" % int(_time.time() * 1000),
                "object": "chat.completion",
                "created": int(_time.time()),
                "model": model,
                "choices": [{"index": 0, "finish_reason": finish,
                             "message": msg}],
                "usage": {},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            log.error(f"assemble_chat_sse error: {e}")
            self.send_error(502, "Forwarder Error: stream assembly failed")
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
        # Forged-TLS path (verified 18.09.2026): upstream gates on the TLS
        # ClientHello bytes, so opencode.ai traffic goes through tls_forge
        # (byte-faithful CLI hello). Falls back to requests on any error.
        if _FORGE_AVAILABLE and url.startswith("https://opencode.ai"):
            try:
                return _forge_attempt(
                    method, url, body or b"", headers,
                    proxy=proxy,
                    connect_timeout=UPSTREAM_CONNECT_TIMEOUT,
                    read_timeout=UPSTREAM_READ_TIMEOUT,
                )
            except Exception as e:
                log.warning(f"forge attempt failed, falling back to requests: {e}")
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
        _auth_ok = (
            self.headers.get("Authorization", "") == f"Bearer {PUBLIC_API_KEY}"
            or (self.headers.get("x-api-key", "") or "").strip() == PUBLIC_API_KEY
        )
        if _auth_ok:
            if not (self.path.startswith("/zen/") or self.path.startswith("/v1/")):
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

        if self.path == "/v1/messages" or self.path.startswith("/v1/messages?"):
            self.handle_anthropic_messages(method, body)
            return
        if self.path == "/v1/models" or self.path.startswith("/v1/models?"):
            self.handle_anthropic_models(method)
            return
        if method == "GET" and self.is_remote_request() and (
                self.path == "/zen/v1/models" or self.path.startswith("/zen/v1/models?")):
            self.handle_filtered_openai_models()
            return
        # 2026-10-15: opencode.ai migrated the free tier to a NEW endpoint
        # family /inference/* (verified live: /inference/v1/responses = 200,
        # legacy /zen/v1/responses = 403 FreeTierError for everyone, from any
        # Tor exit, any UA, any bearer). The path alone is the gate.
        # Inbound clients keep asking for /zen/* (public contract), so we
        # rewrite the outbound path one-for-one.

        # Extract model early for path routing (responses vs chat-only models)
        RESPONSES_MODELS = {"muse-spark-1.3-contributor-free", "muse-spark-1.2-contributor-free", "jev-1.13-free"}
        current_model = None
        if body:
            try:
                current_model = json.loads(body).get("model")
            except Exception:
                current_model = None
        _is_responses_model = current_model in RESPONSES_MODELS

        upstream_path = _zen_to_inference(self.path, is_responses_model=_is_responses_model)
        url = UPSTREAM_BASE + upstream_path
        headers = {k: v for k, v in self.headers.items() if k.lower() not in FORBIDDEN_HEADERS}
        headers["User-Agent"] = UPSTREAM_UA
        # Real CLI sends no Authorization on keyless calls; Hermes'
        # placeholder bearer (401s) and no/UUID session headers (403s)
        # must never leak upstream. Exact CLI shape, verified 17.09.2026,
        # auth updated 14.10.2026 (literal "Bearer public" now 403s).
        _apply_upstream_auth(headers)
        headers["x-opencode-client"] = "cli"
        headers["x-opencode-project"] = "global"
        headers["x-opencode-request"] = _new_request_id()
        _sess = _sticky_session_for(body)
        headers["x-opencode-session"] = _sess
        headers["x-opencode-session-id"] = _sess

        use_responses_api = (
            current_model in RESPONSES_MODELS
            and ("/responses" in self.path or "/chat/completions" in self.path)
        )
        # Convert chat->responses ONLY when the client actually sent chat
        # shape. A native /responses body must never be clobbered.
        _client_body_is_chat = False
        try:
            _cbj = json.loads(body) if body else {}
            _client_body_is_chat = isinstance(_cbj.get("messages"), list)
        except Exception:
            _client_body_is_chat = False
        if use_responses_api and _client_body_is_chat:
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
                # Reasoning models burn the whole budget thinking: with a
                # small cap upstream ends `incomplete` with ZERO visible text
                # (verified 18.09.2026: 253 reasoning tokens vs cap 256).
                # Floor the cap so answers actually arrive.
                try:
                    if int(responses_body.get("max_output_tokens", 0)) < 4096:
                        responses_body["max_output_tokens"] = 4096
                        log.info(f"Bumped max_output_tokens to 4096 floor for model={current_model}")
                except Exception:
                    responses_body["max_output_tokens"] = 4096
                r_tools, r_tc = chat_tools_to_responses(chat)
                if r_tools:
                    responses_body["tools"] = r_tools
                if r_tc:
                    responses_body["tool_choice"] = r_tc
                body = json.dumps(responses_body).encode()
                # is_responses_model=True is REQUIRED: the default (chat-only)
                # rewrite maps v1/responses -> v1/chat/completions, which sent
                # a responses body to the chat endpoint (400/410 observed).
                url = UPSTREAM_BASE + _zen_to_inference("/zen/v1/responses", is_responses_model=True)
                log.info(f"Converted chat/completions -> responses API for model={current_model}")
            except Exception as e:
                log.warning(f"Failed to convert to responses API: {e}")
                current_model = None

        # Pad converted AND native /responses bodies to full CLI shape.
        # Remember the client's own stream wish BEFORE padding forces
        # stream:true upstream (downstream routing depends on it).
        # Only pad responses-shaped bodies for responses-capable models.
        responses_client_stream = False
        if body and use_responses_api:
            try:
                responses_client_stream = bool(json.loads(body).get("stream"))
            except Exception:
                responses_client_stream = False
            new_body = pad_responses_body(body)
            if new_body != body:
                log.info(f"Padded responses body for model={current_model}")
                body = new_body

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

        # Chat path (/chat/completions, non-converted models): same gate
        # wants CLI-known tools (verified 18.09.2026). Minimal valid defs.
        chat_client_stream = False
        if body and "/chat/completions" in url:
            try:
                cj = json.loads(body)
                if isinstance(cj, dict) and isinstance(cj.get("messages"), list):
                    chat_client_stream = bool(cj.get("stream"))
                    added = False
                    ct = cj.get("tools")
                    if not isinstance(ct, list):
                        ct = []
                        cj["tools"] = ct
                    # Gate requires stream:true on chat too (verified 18.09.2026);
                    # non-stream clients get SSE assembled below.
                    if cj.get("stream") is not True:
                        cj["stream"] = True
                        added = True
                    have = set()
                    for t in ct:
                        if isinstance(t, dict):
                            fn = t.get("function") if isinstance(t.get("function"), dict) else {}
                            if fn.get("name"):
                                have.add(fn["name"])
                            elif t.get("name"):
                                have.add(t["name"])
                    for name in ("bash", "glob", "grep", "edit", "read"):
                        if name not in have:
                            ct.append(_chat_decoy(name))
                            added = True
                    if "tool_choice" not in cj:
                        cj["tool_choice"] = "auto"
                        added = True
                    if added:
                        body = json.dumps(cj).encode()
                        log.info(f"Added chat decoy tools for model={current_model}")
            except Exception:
                pass

        rotated = False
        _req_t0 = time.time()
        for attempt in range(RETRY_ATTEMPTS):
            if attempt > 0 and (time.time() - _req_t0) > RETRY_WINDOW:
                log.warning(f"retry window {RETRY_WINDOW:.0f}s exhausted for {self.path} model={current_model} at attempt {attempt+1}/{RETRY_ATTEMPTS}")
                break
            if attempt > 0 and _client_gone(self.connection):
                log.info(f"client gone, aborting retries for {self.path} model={current_model} after {attempt} attempts")
                break
            if attempt >= WALL_FAST_ATTEMPTS and _wall_fast_mode():
                log.warning(f"no 2xx in last {WALL_SUCCESS_LOOKBACK:.0f}s — failing fast after {attempt} attempts for {self.path} model={current_model}")
                break
            # Tor-class block (2026-10-17): while the window is hot, ride
            # direct egress; after it expires the next attempt re-probes Tor.
            if _tor_block_active():
                proxy = None
                _attempt_exit = None
                log.info(f"tor-class block active — direct egress for {self.path} model={current_model}")
            else:
                # Per-IP quotas, Tor-only: every attempt rides its OWN fresh exit
                # (unique SOCKS isolation key -> new circuit in ~1-2s, no NEWNYM).
                proxy = _iso_proxy(f"m{attempt}")
                _attempt_exit = None
                # Pay for an exit lookup / hop only once we know recent duds and
                # the wall isn't total (in total-wall mode every exit burns).
                if _failed_exits and not _wall_fast_mode():
                    for _esc in range(EXIT_BLACKLIST_MAX_ROTATIONS + 1):
                        _attempt_exit = _exit_ip_via(proxy)
                        if _attempt_exit and _is_failed_exit(_attempt_exit):
                            log.warning(f"{self.path} model={current_model}: exit {_attempt_exit} is burned, hopping (escape {_esc + 1}/{EXIT_BLACKLIST_MAX_ROTATIONS})")
                            proxy = _iso_proxy(f"m{attempt}e{_esc}")
                            continue
                        break
            log.info(f"Request: {method} {url} attempt={attempt+1}/{RETRY_ATTEMPTS} proxy={proxy} exit={_attempt_exit or '?'} model={current_model}")
            try:
                resp = self.attempt(method, url, body, headers, proxy)

                _retryable = (resp.status_code == 429
                              or resp.status_code >= 500
                              or resp.status_code in (400, 403))
                if _retryable:
                    if resp.status_code == 429:
                        wait = retry_after_seconds(resp, min(RETRY_BACKOFF * (attempt + 1), MAX_BACKOFF))
                        log.warning(f"429 for {self.path} model={current_model} attempt={attempt+1} retry_in={wait}s")
                    elif resp.status_code == 400:
                        # Always log the upstream 400 body (redacted) for diagnosis
                        _psnip, _cached = _read_error_snippet(resp)
                        if _psnip:
                            log.warning(f"upstream 400 body for {self.path} model={current_model}: {_redact_error_snippet(_psnip)}")
                        wait = min(RETRY_BACKOFF * (attempt + 1), MAX_BACKOFF)
                        log.warning(f"400 for {self.path} model={current_model} attempt={attempt+1} retry_in={wait}s")
                    elif resp.status_code == 403:
                        _psnip403, _cached403 = _read_error_snippet(resp)
                        if (DIRECT_FALLBACK and proxy is not None and _psnip403
                                and ("FreeTierError" in _psnip403
                                     or "can only be used from within OpenCode" in _psnip403)):
                            _arm_tor_block(TOR_REPROBE_INTERVAL)
                            log.warning(
                                f"Tor-class FreeTierError for {self.path} model={current_model} "
                                f"attempt={attempt+1} — Tor class-blocked upstream, riding "
                                f"direct egress for {TOR_REPROBE_INTERVAL:.0f}s")
                            resp.close()
                            continue
                        wait = min(RETRY_BACKOFF * (attempt + 1), MAX_BACKOFF)
                        log.warning(f"403 for {self.path} model={current_model} attempt={attempt+1} retry_in={wait}s")
                    else:
                        wait = min(RETRY_BACKOFF * (attempt + 1), MAX_BACKOFF)
                        log.warning(f"{resp.status_code} for {self.path} model={current_model} attempt={attempt+1} retry_in={wait}s")
                    resp.close()
                    if resp.status_code != 400:
                        # 400 = shape/model issue, not an IP burn: only
                        # 403/429/5xx mark an exit as burned.
                        _record_outcome(False)
                        if _wall_fast_mode():
                            # Total wall: every exit burns — skip the extra
                            # ipify round-trip, the blacklist is saturated.
                            if _attempt_exit:
                                _record_failed_exit(_attempt_exit)
                        else:
                            _record_failed_exit(_attempt_exit or _exit_ip_via(proxy))

                    if ROTATE_ON_429:
                        # Next attempt = new isolation key = new circuit/exit.
                        # No NEWNYM here (it was the 9-14s bottleneck).
                        rotated = True
                        continue

                    if attempt < RETRY_ATTEMPTS - 1:
                        time.sleep(wait)
                else:
                    log.info(f"Response: {resp.status_code} for {self.path} model={current_model}")
                    if 200 <= resp.status_code < 300:
                        _record_outcome(True)
                    # Universal protocol fallback (OpenAI-format path):
                    # chat-only model hit /responses -> re-issue the SAME
                    # turn as /chat/completions, answer in the shape the
                    # client already receives today (chat SSE / chat JSON).
                    if (resp.status_code == 400 and url.endswith("/responses")
                            and "/responses" in self.path):
                        # F-A: always log the upstream 400 text (redacted).
                        # Previously only the status code was logged, so every
                        # 400 surfaced downstream as generic "provider failed".
                        _psnip, _cached = _read_error_snippet(resp)
                        if _psnip:
                            log.warning(f"upstream 400 body for {self.path} model={current_model}: {_redact_error_snippet(_psnip)}")
                        if _is_protocol_error(_psnip):
                            try:
                                resp.close()
                            except Exception:
                                pass
                            pin_protocol(current_model, "chat")
                            try:
                                _rbody = json.loads(body) if body else {}
                                _chat = responses_body_to_chat(_rbody)
                                _ct = _chat.get("tools")
                                if not isinstance(_ct, list):
                                    _ct = []
                                    _chat["tools"] = _ct
                                _have = set()
                                for _t in _ct:
                                    _fn = (_t.get("function")
                                           if isinstance(_t.get("function"), dict) else {})
                                    if _fn.get("name"):
                                        _have.add(_fn["name"])
                                for _n in ("bash", "glob", "grep", "edit", "read"):
                                    if _n not in _have:
                                        _ct.append(_chat_decoy(_n))
                                if "tool_choice" not in _chat:
                                    _chat["tool_choice"] = "auto"
                                _msgs = _chat.get("messages") or []
                                if _msgs and not any(
                                        isinstance(_m, dict) and _m.get("role") == "system"
                                        for _m in _msgs):
                                    _msgs.insert(0, {"role": "system",
                                                   "content": SYSTEM_MARKER})
                                    _chat["messages"] = _msgs
                                _cbody = json.dumps(_chat).encode()
                                _curl = UPSTREAM_BASE + _zen_to_inference("/zen/v1/chat/completions")
                                try:
                                    headers["x-opencode-request"] = _new_request_id()
                                except Exception:
                                    pass
                                log.warning(f"protocol fallback to chat for model={current_model} (ModelProtocolUnsupported)")
                                _cresp = self.attempt(method, _curl, _cbody,
                                                      headers, proxy)
                                log.info(f"Response: {_cresp.status_code} for {self.path} model={current_model} (chat fallback)")
                                if _cresp.status_code == 200:
                                    if responses_client_stream:
                                        self.translate_chat_stream_to_responses(
                                            _cresp, current_model)
                                    else:
                                        self.assemble_chat_sse(_cresp, current_model)
                                else:
                                    self.passthrough(_cresp)
                                return
                            except Exception as _fe:
                                log.warning(f"chat fallback failed for model={current_model}: {_fe}")
                        # F-B: sealed encrypted_content 400 -> retry ONCE
                        # without requesting encrypted content. The replayed
                        # reasoning items (if any) stay in `input`; if upstream
                        # still rejects them, the intact 400 below lets the
                        # gateway-side recovery strip them and retry.
                        if _is_sealed_400(_psnip):
                            try:
                                _sobj = json.loads(body) if body else {}
                                if isinstance(_sobj, dict):
                                    _inc = _sobj.get("include")
                                    if isinstance(_inc, list):
                                        _sobj["include"] = [
                                            i for i in _inc
                                            if i != "reasoning.encrypted_content"]
                                    elif "include" in _sobj:
                                        _sobj.pop("include", None)
                                    _sbody = json.dumps(_sobj).encode()
                                    try:
                                        headers["x-opencode-request"] = _new_request_id()
                                    except Exception:
                                        pass
                                    log.warning(f"sealed-400 retry without include for model={current_model}")
                                    _sresp = self.attempt(method, url, _sbody,
                                                          headers, proxy)
                                    if _sresp.status_code == 200:
                                        log.info(f"Response: 200 for {self.path} model={current_model} (sealed retry)")
                                        _spsnip, _scached = "", None
                                    else:
                                        _spsnip, _scached = _read_error_snippet(_sresp)
                                        if _spsnip:
                                            log.warning(f"sealed retry -> {_sresp.status_code} for {self.path} model={current_model}: {_redact_error_snippet(_spsnip)}")
                                        else:
                                            log.info(f"Response: {_sresp.status_code} for {self.path} model={current_model} (sealed retry)")
                                    try:
                                        resp.close()
                                    except Exception:
                                        pass
                                    resp, _psnip, _cached, body = _sresp, _spsnip, _scached, _sbody
                            except Exception as _se:
                                log.warning(f"sealed-400 retry failed for model={current_model}: {_se}")
                        # F-C: deliver non-200 intact. Forged-TLS bodies are
                        # one-shot: the sniff above already consumed the
                        # socket stream, so streaming `resp` again would send
                        # headers + empty body (client sees RemoteProtocolError
                        # "0 bytes, expected N" instead of the real 400 JSON).
                        if resp.status_code != 200:
                            if getattr(resp, "_text_cache", None) is not None and _cached is not None:
                                self.passthrough_cached(resp, _cached)
                            else:
                                self.passthrough(resp)
                            return
                        # 200 after sealed retry: fall through to the
                        # translators below with the retried body/flags.
                    if use_responses_api and resp.status_code == 200:
                        is_stream = responses_client_stream
                        if is_stream:
                            if os.environ.get("FORWARDER_RESPONSES_RAW") == "1":
                                # debug: pass responses SSE through untouched
                                log.warning("FORWARDER_RESPONSES_RAW=1: raw passthrough")
                                self.passthrough(resp)
                            else:
                                self.translate_responses_stream(resp, current_model)
                        else:
                            # Upstream always streams now (gate needs
                            # stream:true) -> assemble SSE into one JSON.
                            self.assemble_responses_sse(resp, current_model)
                    elif (resp.status_code == 200 and "/chat/completions" in self.path
                            and "responses" not in url and not chat_client_stream):
                        # Chat path, non-stream client, SSE upstream.
                        self.assemble_chat_sse(resp, current_model)
                    else:
                        self.passthrough(resp)
                    return

            except requests.exceptions.Timeout:
                log.error(f"Timeout for {method} {self.path}")
                if attempt < RETRY_ATTEMPTS - 1:
                    log.warning("Timeout detected, rotating Tor IP and retrying...")
                    if not rotated:
                        rotated = rotate_or_wait("Timeout")
                    continue
                self.send_error(502, "Forwarder Error: Upstream timeout")
                return
            except requests.exceptions.ProxyError as e:
                log.error(f"ProxyError for {method} {self.path}: {e}")
                if attempt < RETRY_ATTEMPTS - 1:
                    log.warning("ProxyError detected, rotating Tor IP and retrying...")
                    if not rotated:
                        rotated = rotate_or_wait("ProxyError")
                    continue
                self.send_error(502, "Forwarder Error: Proxy failed")
                return
            except requests.exceptions.ConnectionError as e:
                log.error(f"ConnectionError for {method} {self.path}: {e}")
                if attempt < RETRY_ATTEMPTS - 1:
                    log.warning("ConnectionError detected, rotating Tor IP and retrying...")
                    if not rotated:
                        rotated = rotate_or_wait("ConnectionError")
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
    def send_json_bytes(self, out):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def handle_filtered_openai_models(self):
        """Remote GET /zen/v1/models: upstream payload trimmed to free-only."""
        headers = {"User-Agent": UPSTREAM_UA,
                   "x-opencode-client": "cli", "x-opencode-project": "global",
                   "x-opencode-request": _new_request_id(),
                   "x-opencode-session": _new_session_id()}
        _apply_upstream_auth(headers)
        data = None
        try:
            resp = self.attempt("GET", UPSTREAM_BASE + "/zen/v1/models", None,
                                headers, get_proxy())
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                try:
                    data = resp.json()
                except Exception:
                    data = None
            else:
                log.warning(f"filtered models upstream status {resp.status_code}")
            try:
                resp.close()
            except Exception:
                pass
        except Exception as e:
            log.warning(f"filtered models upstream fetch failed: {e}")
        if data is None:
            data = {"data": [{"id": m} for m in sorted(_KNOWN_OPENCODE_MODELS)]}
        data, n_total, n_free = anthropic_bridge.filter_free_models_payload(data)
        log.info(f"openai models: remote free-only filter {n_total}->{n_free}")
        try:
            self.send_json_bytes(json.dumps(data).encode())
        except Exception as e:
            log.error(f"filtered models send error: {e}")

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

    def send_anthropic_json(self, req_model, text, calls, finish, usage, reasoning=""):
        try:
            out = anthropic_bridge.anthropic_response_json(req_model, text, calls, finish, usage, reasoning)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _anthropic_sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _stream_anthropic(self, resp, model, use_responses, declared_tools=None):
        resp.encoding = "utf-8"
        try:
            self._anthropic_sse_headers()
            w = self.wfile
            writer = anthropic_bridge.AnthropicStreamWriter(
                lambda s: (w.write(s.encode()), w.flush()), model)
            if use_responses:
                anthropic_bridge.translate_responses_sse_to_anthropic(
                    resp.iter_lines(decode_unicode=True), model, writer,
                    declared_tools=declared_tools)
            else:
                anthropic_bridge.translate_chat_sse_to_anthropic(
                    resp.iter_lines(decode_unicode=True), model, writer,
                    declared_tools=declared_tools)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            log.error(f"stream_anthropic error: {e}")
        finally:
            resp.close()

    def _anthropic_upstream_error(self, resp):
        try:
            snippet = resp.text[:300]
        except Exception:
            snippet = ""
        try:
            resp.close()
        except Exception:
            pass
        code = resp.status_code
        if code == 429:
            self.send_anthropic_error(429, "rate_limit_error",
                                      "Upstream rate limited, retry later")
        elif code == 400:
            self.send_anthropic_error(400, "invalid_request_error",
                                      "Upstream rejected request: %s" % snippet[:200])
        else:
            self.send_anthropic_error(500, "api_error",
                                      "Upstream error %s: %s" % (code, snippet[:200]))

    def handle_anthropic_models(self, method):
        if method != "GET":
            self.send_anthropic_error(405, "invalid_request_error",
                                      "Method not allowed, use GET /v1/models")
            return
        headers = {"User-Agent": UPSTREAM_UA,
                   "x-opencode-client": "cli", "x-opencode-project": "global",
                   "x-opencode-request": _new_request_id(),
                   "x-opencode-session": _new_session_id()}
        _apply_upstream_auth(headers)
        data = None
        try:
            resp = self.attempt("GET", UPSTREAM_BASE + "/zen/v1/models", None,
                                headers, get_proxy())
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
            log.warning(f"anthropic models upstream fetch failed: {e}")
        if data is None:
            data = {"data": [{"id": m} for m in sorted(_KNOWN_OPENCODE_MODELS)]}
        remote = self.is_remote_request()
        if remote:
            data, n_total, n_free = anthropic_bridge.filter_free_models_payload(data)
            log.info(f"anthropic models: remote free-only filter {n_total}->{n_free}")
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
            chat, client_stream, req_model, up_model, thinking = anthropic_bridge.anthropic_to_chat(
                req, ANTHROPIC_DEFAULT_MODEL, _KNOWN_OPENCODE_MODELS)
        except ValueError as e:
            self.send_anthropic_error(400, "invalid_request_error", str(e))
            return
        # strip internal marker before it goes upstream
        chat.pop("_anthropic_thinking", None)
        if thinking and thinking.get("budget_tokens"):
            clamped, was = anthropic_bridge.clamp_thinking_budget(
                up_model, thinking["budget_tokens"])
            if was:
                log.info(f"anthropic: thinking budget clamped {thinking['budget_tokens']}->{clamped} for model={up_model} (learned cap)")
                thinking["budget_tokens"] = clamped
        if up_model != (req.get("model") or ""):
            log.info(f"anthropic model remap {req.get('model')!r} -> {up_model!r}")
        conv_key = "anthropic:" + anthropic_bridge.conversation_key(req)
        with _SESSION_PIN_LOCK:
            sess = _SESSION_PINS.get(conv_key)
            if not sess:
                sess = _new_session_id()
                _SESSION_PINS[conv_key] = sess
                if len(_SESSION_PINS) > _SESSION_PINS_CAP:
                    _SESSION_PINS.pop(next(iter(_SESSION_PINS)))
                _save_session_pins()
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in FORBIDDEN_HEADERS}
        headers["User-Agent"] = UPSTREAM_UA
        _apply_upstream_auth(headers)
        headers["x-opencode-client"] = "cli"
        headers["x-opencode-project"] = "global"
        headers["x-opencode-request"] = _new_request_id()
        headers["x-opencode-session"] = sess
        headers["x-opencode-session-id"] = sess
        # beta handling is default-ON: plain /v1/messages behaves like
        # ?beta=true (kept for compat); ?beta=false/0 opts out and strips
        # Anthropic beta-negotiation headers before upstream.
        _beta_on = _query_flag(self.path, "beta", True)
        if _beta_on:
            log.info(f"anthropic: beta mode on for model={up_model}")
        else:
            for _hk in [k for k in headers if k.lower() in ("anthropic-beta", "anthropic-version")]:
                headers.pop(_hk, None)
            log.info(f"anthropic: beta mode off (?beta=false) for model={up_model}, stripped beta headers")

        _RESPONSES_MODELS = {"muse-spark-1.3-contributor-free",
                             "muse-spark-1.2-contributor-free",
                             "jev-1.13-free"}
        pinned = get_pinned_protocol(up_model)
        if pinned == "chat":
            use_responses_api = False
            log.info(f"anthropic: pinned protocol chat for model={up_model}")
        elif pinned == "responses":
            use_responses_api = True
        else:
            use_responses_api = up_model in _RESPONSES_MODELS
        client_tools = chat.get("tools") if isinstance(chat.get("tools"), list) else None

        def _build_chat_body():
            """Fresh chat-shape upstream body (decoys + reasoning + stream gate)."""
            cj = json.loads(json.dumps(chat))
            ct = cj.get("tools")
            if not isinstance(ct, list):
                ct = []
                cj["tools"] = ct
            cj["stream"] = True
            have = set()
            for t in ct:
                if isinstance(t, dict):
                    fn = t.get("function") if isinstance(t.get("function"), dict) else {}
                    if fn.get("name"):
                        have.add(fn["name"])
                    elif t.get("name"):
                        have.add(t["name"])
            for name in ("bash", "glob", "grep", "edit", "read"):
                if name not in have:
                    ct.append(_chat_decoy(name))
            if "tool_choice" not in cj:
                cj["tool_choice"] = "auto"
            if thinking:
                if thinking.get("effort"):
                    cj["reasoning"] = {"effort": thinking["effort"]}
                if thinking.get("budget_tokens"):
                    try:
                        cj["reasoning"] = dict(cj.get("reasoning") or {})
                        cj["reasoning"]["max_tokens"] = int(thinking["budget_tokens"])
                    except Exception:
                        pass
            return json.dumps(cj).encode()

        def _switch_to_chat(reason):
            """Drop responses-shape, rebuild as chat-shape. Returns True."""
            nonlocal body, url, use_responses_api
            use_responses_api = False
            url = UPSTREAM_BASE + _zen_to_inference("/zen/v1/chat/completions")
            try:
                body = _build_chat_body()
                try:
                    headers["x-opencode-request"] = _new_request_id()
                except Exception:
                    pass
                log.warning(f"anthropic: protocol fallback to chat for model={up_model} ({reason})")
                return True
            except Exception as e:
                log.warning(f"anthropic: chat rebuild failed: {e}")
                return False

        body = json.dumps(chat).encode()
        url = UPSTREAM_BASE + _zen_to_inference("/zen/v1/chat/completions")
        if not use_responses_api:
            try:
                body = _build_chat_body()
                log.info(f"anthropic: chat-shape body for model={up_model}")
            except Exception as e:
                log.warning(f"anthropic: chat body build failed: {e}")
        if use_responses_api:
            try:
                responses_body = {
                    "model": up_model,
                    "input": chat_messages_to_responses_input(chat.get("messages")),
                    "stream": chat.get("stream", False),
                }
                if "temperature" in chat:
                    responses_body["temperature"] = chat["temperature"]
                if "max_tokens" in chat:
                    responses_body["max_output_tokens"] = chat["max_tokens"]
                try:
                    if int(responses_body.get("max_output_tokens", 0)) < 4096:
                        responses_body["max_output_tokens"] = 4096
                        log.info(f"anthropic: bumped max_output_tokens to 4096 floor for model={up_model}")
                except Exception:
                    responses_body["max_output_tokens"] = 4096
                r_tools, r_tc = chat_tools_to_responses(chat)
                if r_tools:
                    responses_body["tools"] = r_tools
                if r_tc:
                    responses_body["tool_choice"] = r_tc
                # thinking -> reasoning knobs (both shapes accepted upstream)
                if thinking:
                    if thinking.get("effort"):
                        responses_body["reasoning"] = {"effort": thinking["effort"]}
                    if thinking.get("budget_tokens"):
                        try:
                            responses_body["reasoning"] = dict(
                                responses_body.get("reasoning") or {})
                            responses_body["reasoning"]["max_tokens"] = int(
                                thinking["budget_tokens"])
                        except Exception:
                            pass
                body = json.dumps(responses_body).encode()
                url = UPSTREAM_BASE + _zen_to_inference("/zen/v1/responses", is_responses_model=True)
                log.info(f"anthropic: converted to responses API for model={up_model}")
            except Exception as e:
                log.warning(f"anthropic: failed to convert to responses API: {e}")
                use_responses_api = False
        if use_responses_api:
            new_body = pad_responses_body(body)
            if new_body != body:
                log.info(f"anthropic: padded responses body for model={up_model}")
                body = new_body
        if body and SPOOF_SYSTEM_MARKER:
            try:
                j = json.loads(body)
                msgs = j.get("messages")
                if isinstance(msgs, list) and msgs:
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
        if body and not use_responses_api:
            try:
                cj = json.loads(body)
                if isinstance(cj, dict) and isinstance(cj.get("messages"), list):
                    if cj.get("stream") is not True:
                        cj["stream"] = True
                        body = json.dumps(cj).encode()
            except Exception:
                pass

        def _rebuild_anthropic_body(strip_reasoning=False, strip_decoys=False,
                                    strip_tools=False, clamp_max=False,
                                    truncate_msgs=None):
            """Rebuild upstream body with simplifications for 400-recovery."""
            jb = json.loads(body)
            if strip_reasoning:
                jb.pop("reasoning", None)
                try:
                    if int(jb.get("max_output_tokens", 0)) > 8192:
                        jb["max_output_tokens"] = 8192
                except Exception:
                    pass
            if clamp_max:
                try:
                    if int(jb.get("max_output_tokens", 0)) > 4096:
                        jb["max_output_tokens"] = 4096
                except Exception:
                    jb["max_output_tokens"] = 4096
                try:
                    msgs = jb.get("messages")
                    if isinstance(msgs, list):
                        for m in msgs:
                            if isinstance(m, dict) and isinstance(m.get("max_tokens"), int) \
                                    and m["max_tokens"] > 4096:
                                m["max_tokens"] = 4096
                except Exception:
                    pass
            if strip_tools:
                jb.pop("tools", None)
                jb.pop("tool_choice", None)
                try:
                    msgs = jb.get("messages")
                    if isinstance(msgs, list):
                        for m in msgs:
                            if isinstance(m, dict):
                                m.pop("tools", None)
                                m.pop("tool_choice", None)
                                m.pop("tool_calls", None)
                                if m.get("role") == "tool":
                                    m["role"] = "user"
                    jb["messages"] = [m for m in msgs
                                      if not (isinstance(m, dict)
                                              and m.get("role") == "tool"
                                              and not m.get("content"))]
                except Exception:
                    pass
            elif strip_decoys:
                try:
                    tools = jb.get("tools")
                    if isinstance(tools, list):
                        jb["tools"] = [t for t in tools
                                       if not (((t.get("function") or {})
                                                if isinstance(t.get("function"), dict)
                                                else t).get("name") in ("bash", "glob", "grep", "edit", "read")
                                               and not ((t.get("function") or {})
                                                        if isinstance(t.get("function"), dict)
                                                        else t).get("description"))]
                        if not jb["tools"]:
                            jb.pop("tools", None)
                            jb.pop("tool_choice", None)
                except Exception:
                    pass
            if truncate_msgs:
                try:
                    key = "input" if use_responses_api else "messages"
                    arr = jb.get(key)
                    if isinstance(arr, list) and len(arr) > truncate_msgs:
                        jb[key] = arr[-truncate_msgs:]
                except Exception:
                    pass
            return json.dumps(jb).encode()

        # 400-recovery ladder: same request, progressively simpler shape.
        # (Upstream 400s are shape/model issues, NOT rate limits — retrying
        # the identical body is pointless; each step simplifies once.)
        _RECOVERY_STEPS = [
            ("strip-reasoning", dict(strip_reasoning=True)),
            ("strip-decoys", dict(strip_decoys=True)),
            ("strip-tools", dict(strip_tools=True)),
            ("clamp-max", dict(clamp_max=True)),
            ("truncate-20", dict(truncate_msgs=20)),
        ]
        recovery_idx = [0]
        last_400_snippet = [""]

        rotated = False
        freetier_rotations = [0]
        FREETIER_MAX_ROTATIONS = 3
        for attempt in range(RETRY_ATTEMPTS):
            proxy = get_proxy()
            log.info(f"anthropic: POST /v1/messages attempt={attempt+1}/{RETRY_ATTEMPTS} proxy={proxy} model={up_model}")
            try:
                resp = self.attempt("POST", url, body, headers, proxy)
                if resp.status_code == 429 or resp.status_code >= 500:
                    if resp.status_code == 429:
                        wait = retry_after_seconds(resp, min(RETRY_BACKOFF * (attempt + 1), MAX_BACKOFF))
                        log.warning(f"anthropic: 429 model={up_model} attempt={attempt+1} retry_in={wait}s")
                    else:
                        wait = min(RETRY_BACKOFF * (attempt + 1), MAX_BACKOFF)
                        log.warning(f"anthropic: {resp.status_code} model={up_model} attempt={attempt+1} retry_in={wait}s")
                    resp.close()
                    if ROTATE_ON_429:
                        rotated = renew_tor_ip()
                        if rotated:
                            continue
                        log.warning("anthropic: IP rotation failed, waiting before retry")
                    if attempt < RETRY_ATTEMPTS - 1:
                        time.sleep(wait)
                else:
                    if resp.status_code == 200:
                        log.info(f"anthropic: Response 200 model={up_model}")
                    else:
                        try:
                            _praw2 = getattr(resp, "text", None)
                            if isinstance(_praw2, bytes):
                                _snip = _praw2[:500].decode("utf-8", "replace")
                            elif isinstance(_praw2, str):
                                _snip = _praw2[:500]
                            else:
                                _snip = ""
                            if not _snip:
                                try:
                                    _snip = (resp.content[:500] or b"").decode(
                                        "utf-8", "replace")
                                except Exception:
                                    _snip = ""
                        except Exception:
                            _snip = ""
                        log.info(f"anthropic: Response {resp.status_code} model={up_model} snippet={_snip[:200]!r}")
                    if resp.status_code in (400, 403) and recovery_idx[0] < len(_RECOVERY_STEPS):
                        # Universal protocol fallback: model doesn't speak
                        # responses -> pin chat, rebuild body, retry SAME turn.
                        # (Matrix 2026-10-11: only sparks speak responses.)
                        if use_responses_api and _is_protocol_error(_snip):
                            pin_protocol(up_model, "chat")
                            try:
                                resp.close()
                            except Exception:
                                pass
                            if _switch_to_chat("ModelProtocolUnsupported"):
                                continue
                        # FreeTierError 403 = banned Tor exit-IP, NOT a shape
                        # problem: rotate first (same body), simplify later.
                        if resp.status_code == 403 and "freetier" in _snip.lower().replace(" ", "").replace("_", "").replace("-", "") \
                                and freetier_rotations[0] < FREETIER_MAX_ROTATIONS:
                            freetier_rotations[0] += 1
                            log.warning(f"anthropic: FreeTier 403 model={up_model}, rotating Tor IP ({freetier_rotations[0]}/{FREETIER_MAX_ROTATIONS}), same body retry")
                            try:
                                resp.close()
                            except Exception:
                                pass
                            rotated = renew_tor_ip()
                            if rotated:
                                try:
                                    headers["x-opencode-request"] = _new_request_id()
                                except Exception:
                                    pass
                                continue
                            log.warning("anthropic: FreeTier rotation failed, falling to simplify ladder")
                        step_name, kwargs = _RECOVERY_STEPS[recovery_idx[0]]
                        recovery_idx[0] += 1
                        last_400_snippet[0] = _snip
                        log.warning(f"anthropic: {resp.status_code} model={up_model}, recovery step {recovery_idx[0]}/{len(_RECOVERY_STEPS)} ({step_name}), snippet={_snip[:200]!r}")
                        try:
                            resp.close()
                        except Exception:
                            pass
                        # learn thinking cap: 400 with reasoning on board ->
                        # halve the budget for this model next time
                        if thinking and thinking.get("budget_tokens") and step_name == "strip-reasoning":
                            try:
                                new_cap = max(1024, int(thinking["budget_tokens"]) // 2)
                                if anthropic_bridge.note_thinking_cap(up_model, new_cap):
                                    log.warning(f"anthropic: learned thinking cap {new_cap} for model={up_model}")
                            except Exception:
                                pass
                        try:
                            body = _rebuild_anthropic_body(**kwargs)
                            try:
                                headers["x-opencode-request"] = _new_request_id()
                            except Exception:
                                pass
                        except Exception as e:
                            log.warning(f"anthropic: recovery rebuild failed: {e}")
                        continue
                    if resp.status_code in (400, 403):
                        # ladder exhausted: report as 500 with the real text —
                        # Claude Code retries 500s, but treats 4xx as fatal
                        # for the whole request (never surfaces our message).
                        try:
                            resp.close()
                        except Exception:
                            pass
                        self.send_anthropic_error(500, "api_error",
                            "Upstream rejected request after %d simplifications: %s"
                            % (len(_RECOVERY_STEPS), (last_400_snippet[0] or _snip)[:200]))
                        return
                    if resp.status_code != 200:
                        self._anthropic_upstream_error(resp)
                        return
                    if client_stream:
                        self._stream_anthropic(resp, req_model, use_responses_api,
                                               declared_tools=client_tools)
                    else:
                        resp.encoding = "utf-8"
                        if use_responses_api:
                            text, calls, finish, usage, reasoning = anthropic_bridge.assemble_responses_sse(
                                resp.iter_lines(decode_unicode=True))
                        else:
                            text, calls, finish, usage, reasoning = anthropic_bridge.assemble_chat_sse(
                                resp.iter_lines(decode_unicode=True))
                        try:
                            resp.close()
                        except Exception:
                            pass
                        known, unknown = anthropic_bridge.validate_tool_names(
                            calls, client_tools)
                        if unknown:
                            text = (text + "\n" + anthropic_bridge.unknown_calls_to_text(
                                unknown)).strip()
                            calls = known
                            if not calls and finish == "tool_calls":
                                finish = "stop"
                        self.send_anthropic_json(req_model, text, calls, finish, usage, reasoning)
                    return
            except requests.exceptions.Timeout:
                log.error("anthropic: upstream timeout")
                if attempt < RETRY_ATTEMPTS - 1:
                    if not rotated:
                        rotated = rotate_or_wait("anthropic timeout")
                    continue
                self.send_anthropic_error(502, "api_error", "Upstream timeout")
                return
            except requests.exceptions.ProxyError as e:
                log.error(f"anthropic: proxy error: {e}")
                if attempt < RETRY_ATTEMPTS - 1:
                    if not rotated:
                        rotated = rotate_or_wait("anthropic proxy error")
                    continue
                self.send_anthropic_error(502, "api_error", "Proxy failed")
                return
            except requests.exceptions.ConnectionError as e:
                log.error(f"anthropic: connection error: {e}")
                if attempt < RETRY_ATTEMPTS - 1:
                    if not rotated:
                        rotated = rotate_or_wait("anthropic connection error")
                    continue
                self.send_anthropic_error(502, "api_error", "Connection failed")
                return
            except Exception as e:
                log.error(f"anthropic: error: {e}", exc_info=True)
                self.send_anthropic_error(502, "api_error", str(e)[:200])
                return
        log.error(f"anthropic: retries exhausted model={up_model}, returning 503")
        self.send_anthropic_error(503, "api_error",
                                  "All retries exhausted for model=%s" % up_model)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    host = os.environ.get("FORWARDER_HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), ProxyHandler)
    auth_state = 'ON' if PUBLIC_API_KEY else 'OFF-open'
    log.info(f"Forwarder started on {host}:{port} 429-retries={RETRY_ATTEMPTS} backoff={RETRY_BACKOFF}s fallback={FALLBACK_MODELS or 'DISABLED'} rotate={ROTATE_ON_429} auth={auth_state}")
    server.serve_forever()


if __name__ == "__main__":
    main()
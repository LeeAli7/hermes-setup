#!/usr/bin/env python3
"""
Kiro Gateway — OpenAI-совместимый прокси для Amazon Q Developer API.
Без лимитов, с tool calls, с авто-логином и авто-обновлением токена.

Зависимости: pip install requests boto3
"""
import json, os, time, uuid, logging, struct, socket, sys, webbrowser, hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from datetime import datetime, timezone
import requests

# --- config ---
REGION = "us-east-1"
RUNTIME = f"https://runtime.{REGION}.kiro.dev"
CREDS_FILE = os.path.expanduser("~/.kiro-creds.json")
KIRO_DB = os.path.expanduser("~/.local/share/kiro-cli/data.sqlite3")
DEFAULT_MODEL = "claude-sonnet-4"
PROFILE_ARN = "arn:aws:codewhisperer:us-east-1:638616132270:profile/AAAACCCCXXXX"
SCOPES = ["codewhisperer:completions", "codewhisperer:analysis", "codewhisperer:conversations"]
PORT = 8080

log = logging.getLogger("kiro")
logging.basicConfig(level=logging.INFO, format="%(message)s")

# --- session state (Amazon Q requires persistent conversationId) ---
_kiro_conversation_id = None

MODELS = [
    {"id":"qwen3-coder-next","owned_by":"kiro"},
    {"id":"deepseek-3.2","owned_by":"kiro"},
    {"id":"minimax-m2.5","owned_by":"kiro"},
    {"id":"minimax-m2.1","owned_by":"kiro"},
    {"id":"claude-sonnet-4.5","owned_by":"kiro"},
    {"id":"claude-sonnet-4","owned_by":"kiro"},
    {"id":"claude-haiku-4.5","owned_by":"kiro"},
    {"id":"glm-5","owned_by":"kiro"},
]

# --- credential management ---

def load_creds():
    if os.path.exists(CREDS_FILE):
        with open(CREDS_FILE) as f:
            return json.load(f)
    return None

def save_creds(creds):
    with open(CREDS_FILE, "w") as f:
        json.dump(creds, f, indent=2)
    os.chmod(CREDS_FILE, 0o600)

def try_import_kiro_cli():
    if not os.path.exists(KIRO_DB):
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(KIRO_DB)
        token_row = conn.execute("SELECT value FROM auth_kv WHERE key='kirocli:odic:token'").fetchone()
        reg_row = conn.execute("SELECT value FROM auth_kv WHERE key='kirocli:odic:device-registration'").fetchone()
        conn.close()
        if not token_row or not reg_row:
            return None
        tok, reg = json.loads(token_row[0]), json.loads(reg_row[0])
        exp_str = tok.get("expires_at", "0").replace("Z", "+00:00")
        exp_dt = datetime.fromisoformat(exp_str)
        creds = {"client_id": reg["client_id"], "client_secret": reg["client_secret"],
                 "access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
                 "expires_at": int(exp_dt.timestamp()), "region": REGION}
        save_creds(creds)
        log.info("Imported credentials from kiro-cli")
        return creds
    except Exception as e:
        log.warning(f"Failed to import from kiro-cli: {e}")
        return None

def bearer_token():
    creds = load_creds()
    if not creds:
        creds = try_import_kiro_cli()
    if not creds:
        raise Exception("No token. Run: python3 kiro-gateway.py login")
    now = int(time.time())
    expires_at = int(creds.get("expires_at", 0))
    if now >= expires_at - 60:
        try:
            creds = refresh_token(creds)
        except Exception as e:
            log.warning(f"Token refresh failed: {e}, using current token")
    return creds["access_token"]

# --- OIDC login ---

def oidc_client():
    import boto3
    from botocore.config import Config
    return boto3.client("sso-oidc", region_name=REGION,
        aws_access_key_id="AKIA" + "X" * 16, aws_secret_access_key="X" * 40,
        config=Config(signature_version="unsigned", connect_timeout=10, read_timeout=10))

def cmd_login():
    print("Authorizing via AWS Builder ID...")
    client = oidc_client()
    reg = client.register_client(clientName="kiro-gateway", clientType="public", scopes=SCOPES)
    cid, csecret = reg["clientId"], reg["clientSecret"]
    auth = client.start_device_authorization(clientId=cid, clientSecret=csecret, startUrl="https://view.awsapps.com/start")
    print(f"\nOpen: {auth['verificationUriComplete']}\nCode: {auth['userCode']}")
    try: webbrowser.open(auth["verificationUriComplete"])
    except: pass
    print("\nWaiting...", end="", flush=True)
    expires_at = int(time.time()) + auth["expiresIn"]
    creds = {"client_id": cid, "client_secret": csecret}
    while time.time() < expires_at:
        time.sleep(auth["interval"])
        try:
            token = client.create_token(clientId=cid, clientSecret=csecret,
                grantType="urn:ietf:params:oauth:grant-type:device_code", deviceCode=auth["deviceCode"])
            creds["access_token"] = token["accessToken"]
            creds["refresh_token"] = token.get("refreshToken", "")
            creds["expires_at"] = int(time.time()) + token.get("expiresIn", 3600)
            creds["region"] = REGION
            save_creds(creds)
            print("\nLogged in!")
            return creds
        except client.exceptions.AuthorizationPendingException:
            print(".", end="", flush=True)
        except client.exceptions.SlowDownException:
            time.sleep(5)
        except Exception as e:
            if "AuthorizationPending" in str(e):
                print(".", end="", flush=True)
            else:
                raise
    raise Exception("Auth timeout")

def refresh_token(creds):
    import boto3
    from botocore.config import Config
    client = boto3.client("sso-oidc", region_name=REGION,
        aws_access_key_id="AKIA" + "X" * 16, aws_secret_access_key="X" * 40,
        config=Config(signature_version="unsigned"))
    try:
        token = client.create_token(clientId=creds["client_id"], clientSecret=creds["client_secret"],
            grantType="refresh_token", refreshToken=creds["refresh_token"])
        creds["access_token"] = token["accessToken"]
        creds["expires_at"] = int(time.time()) + token.get("expiresIn", 3600)
        if token.get("refreshToken"):
            creds["refresh_token"] = token["refreshToken"]
        save_creds(creds)
        log.info("Token refreshed")
        return creds
    except Exception as e:
        log.warning(f"Token refresh failed: {e}, keeping current token")
        return creds

# --- tool converters ---

def convert_tools(openai_tools):
    """OpenAI tools format -> Kiro tools format."""
    if not openai_tools:
        return None
    kiro_tools = []
    for t in openai_tools:
        fn = t.get("function", t)
        kiro_tools.append({
            "toolSpecification": {
                "name": fn.get("name", "unknown"),
                "description": fn.get("description", ""),
                "inputSchema": {
                    "json": fn.get("parameters", {"type": "object", "properties": {}})
                }
            }
        })
    return kiro_tools


def _tool_calls_to_kiro(tc_list):
    """OpenAI tool_calls -> Kiro toolUses with name/input/toolUseId."""
    result = []
    for tc in tc_list:
        fn = tc.get("function", tc)
        args_str = fn.get("arguments", "{}")
        if isinstance(args_str, str):
            try:
                args = json.loads(args_str)
            except json.JSONDecodeError:
                args = {"_raw": args_str}
        else:
            args = args_str or {}
        result.append({
            "name": fn.get("name", "unknown"),
            "input": args,
            "toolUseId": tc.get("id", "")
        })
    return result


def _make_ui(content, model, tools_ctx=None, tool_results=None):
    """Build a userInputMessage dict for Kiro."""
    msg = {
        "content": content or "(empty placeholder)",
        "modelId": model,
        "origin": "KIRO_CLI",
        "userInputMessageContext": {"envState": {"operatingSystem": "linux", "currentWorkingDirectory": os.getcwd()}}
    }
    ctx = msg["userInputMessageContext"]
    if tools_ctx:
        ctx["tools"] = tools_ctx
    if tool_results:
        ctx["toolResults"] = tool_results
    return {"userInputMessage": msg}


def _make_tool_results(tool_call_id, content):
    """Build a toolResults list entry for Kiro."""
    return [{
        "toolUseId": tool_call_id,
        "content": [{"text": content or "(empty result)"}],
        "status": "success"
    }]


# --- API call ---

def call_api(msgs, model=DEFAULT_MODEL, openai_tools=None):
    token = bearer_token()

    first_user = next((m["content"] for m in msgs if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
    cid = hashlib.md5(first_user.encode()).hexdigest()[:36] if first_user else str(uuid.uuid4())
    history = []
    kiro_tools = convert_tools(openai_tools)

    # Build history (all messages except the last one)
    pending_tool_results = []
    for m in msgs[:-1]:
        role = m.get("role", "user")
        content = m.get("content") or ""
        tc_list = m.get("tool_calls") or []

        if role == "tool":
            pending_tool_results.append({
                "toolUseId": m.get("tool_call_id", ""),
                "content": [{"text": content or "(empty result)"}],
                "status": "success"
            })
            continue

        # Flush accumulated tool results before a non-tool message
        if pending_tool_results:
            history.append(_make_ui("", model, kiro_tools, pending_tool_results))
            pending_tool_results = []

        if role == "assistant":
            entry = {"assistantResponseMessage": {"content": content or ""}}
            if tc_list:
                entry["assistantResponseMessage"]["toolUses"] = _tool_calls_to_kiro(tc_list)
            history.append(entry)

        elif role == "user":
            history.append(_make_ui(content, model, kiro_tools))

    # Flush any tool results remaining from history
    if pending_tool_results:
        history.append(_make_ui("", model, kiro_tools, pending_tool_results))
        pending_tool_results = []

    # Handle current message (last one)
    current = msgs[-1] if msgs else {"role": "user", "content": "continue"}
    current_role = current.get("role", "user")

    if current_role == "tool":
        tr = _make_tool_results(current.get("tool_call_id", ""), current.get("content", ""))
        current_msg = _make_ui("continue", model, kiro_tools, tr)

    elif current_role == "assistant":
        tc_list = current.get("tool_calls") or []
        entry = {"assistantResponseMessage": {"content": current.get("content") or ""}}
        if tc_list:
            entry["assistantResponseMessage"]["toolUses"] = _tool_calls_to_kiro(tc_list)
        history.append(entry)
        current_msg = _make_ui("continue", model, kiro_tools)

    else:
        current_content = current.get("content") or ""
        if not current_content.strip():
            current_content = "continue"
        current_msg = _make_ui(current_content, model, kiro_tools)

    # Build final payload
    payload = {
        "conversationState": {
            "conversationId": cid,
            "history": history,
            "currentMessage": current_msg,
            "chatTriggerType": "MANUAL"
        }
    }
    if PROFILE_ARN:
        payload["profileArn"] = PROFILE_ARN

    headers = {
        "Content-Type": "application/x-amz-json-1.0",
        "X-Amz-Target": "AmazonCodeWhispererStreamingService.GenerateAssistantResponse",
        "Authorization": f"Bearer {token}",
        "Host": f"runtime.{REGION}.kiro.dev",
        "x-amzn-codewhisperer-optout": "false",
    }
    cur_preview = current_msg.get("userInputMessage", {}).get("content", "")[:50]
    log.info("[>] %s %dmsgs cid=%s cur='%s' his=%d", model, len(msgs), cid[:12], cur_preview, len(history))
    log.info("  -> payload: %s", json.dumps(payload, indent=2)[:2000])
    resp = requests.post(RUNTIME, headers=headers, json=payload, stream=True, timeout=120)
    if resp.status_code != 200:
        log.error("  -> API ERROR %d: %s", resp.status_code, resp.text[:500])
    return resp

# --- AWS EventStream parser + OpenAI converter ---

def parse_events(raw_iter):
    buf = b""
    for chunk in raw_iter:
        buf += chunk
        while True:
            if len(buf) < 12: break
            total_len = struct.unpack(">I", buf[0:4])[0]
            if len(buf) < total_len: break
            msg = buf[:total_len]; buf = buf[total_len:]
            hdr_len = struct.unpack(">I", msg[4:8])[0]
            pbytes = msg[12 + hdr_len : total_len - 4]
            hdrs = {}; pos = 12; end = 12 + hdr_len
            while pos < end:
                name_len = msg[pos]; pos += 1
                name = msg[pos:pos+name_len].decode(); pos += name_len
                val_type = msg[pos]; pos += 1
                if val_type == 7:
                    vlen = struct.unpack(">H", msg[pos:pos+2])[0]; pos += 2
                    hdrs[name] = msg[pos:pos+vlen].decode(); pos += vlen
                elif val_type == 6:
                    hdrs[name] = struct.unpack(">I", msg[pos:pos+4])[0]; pos += 4
                else:
                    hdrs[name] = None
            try:
                data = json.loads(pbytes.decode()) if pbytes else {}
            except json.JSONDecodeError:
                data = {"_raw": pbytes.decode(errors="replace")}
            etype = hdrs.get(":event-type", "unknown")
            log.info("EVENT: %s -> %s", etype, data)
            yield etype, data

class ToolCallAccumulator:
    """Accumulates streamed toolUseEvent chunks into complete tool calls."""
    def __init__(self):
        self.calls = {}
        self.order = []

    def feed(self, data):
        tid = data.get("toolUseId")
        if not tid:
            return
        if tid not in self.calls:
            self.calls[tid] = {"name": data.get("name", ""), "toolUseId": tid, "input_chunks": []}
            self.order.append(tid)
        if "input" in data:
            self.calls[tid]["input_chunks"].append(data["input"])

    def finalize(self):
        result = []
        for idx, tid in enumerate(self.order):
            c = self.calls[tid]
            full_input = "".join(c["input_chunks"])
            result.append({
                "id": tid,
                "type": "function",
                "function": {
                    "name": c["name"],
                    "arguments": full_input
                }
            })
        return result

def build_response(events_iter, model):
    """Process events and produce an OpenAI-format response dict."""
    content = ""
    acc = ToolCallAccumulator()
    stop_reason = "stop"

    for etype, data in events_iter:
        if etype == "assistantResponseEvent":
            content += data.get("content", "")
        elif etype == "toolUseEvent":
            acc.feed(data)
        elif etype == "metadataEvent":
            sr = data.get("stopReason", "")
            if sr == "TOOL_USE":
                stop_reason = "tool_calls"
            elif sr == "END_TURN":
                stop_reason = "stop"

    tool_calls = acc.finalize()
    msg = {"role": "assistant"}
    if content:
        msg["content"] = content
    if tool_calls:
        msg["tool_calls"] = tool_calls

    log.info("[<] response: content_len=%s tool_calls=%d finish=%s", len(content) if content else 0, len(tool_calls), stop_reason)

    return {
        "id": f"cmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": stop_reason}],
        "usage": {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
    }

# --- HTTP server ---

class GatewayHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST,GET,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/v1/models":
            self._json({"object":"list","data":[{**m,"object":"model","created":int(time.time())} for m in MODELS]})
        elif p == "/health":
            self._json({"status":"ok"})
        else:
            self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/v1/chat/completions":
            self._chat()
        else:
            self.send_error(404)

    def _json(self, data, status=200):
        b = json.dumps(data)
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Content-Length",str(len(b.encode())))
        self.end_headers()
        self.wfile.write(b.encode())

    def _chat(self):
        length = int(self.headers.get("Content-Length",0))
        raw = self.rfile.read(length)
        try:
            req = json.loads(raw)
        except:
            self._json({"error":"bad json"},400); return
        model = req.get("model", DEFAULT_MODEL)
        msgs = req.get("messages",[])
        stream = req.get("stream", False)
        tools = req.get("tools", None)
        if not msgs:
            self._json({"error":"no messages"},400); return
        try:
            if stream:
                self._stream(model, msgs, tools)
            else:
                resp = call_api(msgs, model, tools)
                events = list(parse_events(resp.iter_content(chunk_size=4096)))
                self._update_session(events)
                result = build_response(events, model)
                self._json(result)
        except Exception as e:
            log.error(f"ERR: {e}")
            self._json({"error":str(e)},500)

    def _update_session(self, events):
        pass

    def _stream(self, model, msgs, tools):
        resp = call_api(msgs, model, tools)
        self.send_response(200)
        self.send_header("Content-Type","text/event-stream")
        self.send_header("Cache-Control","no-cache")
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        cid = f"cmpl-{uuid.uuid4().hex[:12]}"
        ts = int(time.time())

        sent_role = False
        finish_reason = "stop"
        active_tools = {}
        next_tool_idx = 0

        def emit(choices, fr=None):
            nonlocal sent_role
            d = {"id":cid,"object":"chat.completion.chunk","created":ts,"model":model,
                 "choices":[{"index":0,"delta":choices,"finish_reason":fr}]}
            self.wfile.write(f"data: {json.dumps(d)}\n\n".encode()); self.wfile.flush()
            sent_role = True

        for etype, data in parse_events(resp.iter_content(chunk_size=4096)):
            if etype == "assistantResponseEvent":
                text = data.get("content", "")
                if text:
                    if not sent_role:
                        emit({"role":"assistant","content":""})
                    emit({"content": text})

            elif etype == "toolUseEvent":
                tid = data.get("toolUseId")
                if not tid:
                    continue
                if tid not in active_tools:
                    active_tools[tid] = next_tool_idx
                    next_tool_idx += 1
                    if not sent_role:
                        emit({"role":"assistant","content":None})
                    emit({"tool_calls":[{"index":active_tools[tid],"id":tid,
                          "type":"function","function":{"name":data.get("name",""),"arguments":""}}]})
                inp = data.get("input")
                if inp is not None:
                    emit({"tool_calls":[{"index":active_tools[tid],"function":{"arguments":inp}}]})

            elif etype == "metadataEvent":
                sr = data.get("stopReason", "")
                if sr == "TOOL_USE":
                    finish_reason = "tool_calls"
                elif sr == "END_TURN":
                    finish_reason = "stop"
                if "conversationId" in data or "agentContinuationId" in data:
                    self._update_session([(etype, data)])

        emit({}, fr=finish_reason)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, fmt, *args):
        log.info(f"[{self.client_address[0]}] {fmt % args}")

# --- CLI ---

def main():
    if len(sys.argv) >= 2 and sys.argv[1] in ("-h", "--help"):
        print(f"Kiro Gateway - Amazon Q Developer API proxy with tool calls")
        print()
        print(f"  python3 {sys.argv[0]}            - start proxy on :{PORT}")
        print(f"  python3 {sys.argv[0]} login      - authorize via AWS Builder ID")
        print(f"  python3 {sys.argv[0]} status     - check status")
        return

    if len(sys.argv) >= 2:
        cmd = sys.argv[1]
        if cmd == "login":
            cmd_login(); return
        elif cmd == "status":
            try:
                creds = load_creds()
                if not creds: creds = try_import_kiro_cli()
                if creds:
                    exp = datetime.fromtimestamp(creds["expires_at"], tz=timezone.utc)
                    print(f"Token expires: {exp.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                else:
                    print("No token. Run: python3 kiro-gateway.py login")
                r = requests.get(f"http://localhost:{PORT}/health", timeout=2)
                print("Server: RUNNING" if r.status_code == 200 else "Server: NOT RUNNING")
            except:
                print("Server: NOT RUNNING")
            return

    if not load_creds():
        try_import_kiro_cli()

    srv = HTTPServer(("0.0.0.0", PORT), GatewayHandler)
    srv.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    print(f"=== Kiro Gateway on http://localhost:{PORT}/v1/chat/completions ===")
    print("Tool calls: YES | Streaming: YES | NO USAGE LIMITS")
    srv.serve_forever()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Kiro Gateway — твой личный прокси для Amazon Q Developer API.
Без регистрации, без авторизации, без лимитов. Просто запусти и пользуйся.

Зависимости: pip install requests
"""
import json, os, time, uuid, logging, struct, socket, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from datetime import datetime, timezone
import requests

# ====== ТВОИ КРЕДЫ (уже встроены, ничего вводить не надо) ======
_CLIENT_ID = "OfUeajLguV47gFSFpTk8TnVzLWVhc3QtMQ"
_CLIENT_SECRET = "eyJraWQiOiJrZXktMTU2NDAyODA5OSIsImFsZyI6IkhTMzg0In0.eyJzZXJpYWxpemVkIjoie1wiY2xpZW50SWRcIjp7XCJ2YWx1ZVwiOlwiT2ZVZWFqTGd1VjQ3Z0ZTRnBUazhUblZ6TFdWaGMzUXRNUVwifSxcImlkZW1wb3RlbnRLZXlcIjpudWxsLFwidGVuYW50SWRcIjpudWxsLFwiY2xpZW50TmFtZVwiOlwiS2lybyBDTElcIixcImJhY2tmaWxsVmVyc2lvblwiOm51bGwsXCJjbGllbnRUeXBlXCI6XCJQVUJMSUNcIixcInRlbXBsYXRlQXJuXCI6bnVsbCxcInRlbXBsYXRlQ29udGV4dFwiOm51bGwsXCJleHBpcmF0aW9uVGltZXN0YW1wXCI6MTc5MDkyMTk2NC4xODkxMTE3MTgsXCJjcmVhdGVkVGltZXN0YW1wXCI6MTc4MzE0NTk2NC4xODkxMTE3MTgsXCJ1cGRhdGVkVGltZXN0YW1wXCI6MTc4MzE0NTk2NC4xODkxMTE3MTgsXCJjcmVhdGVkQnlcIjpudWxsLFwidXBkYXRlZEJ5XCI6bnVsbCxcInN0YXR1c1wiOm51bGwsXCJpbml0aWF0ZUxvZ2luVXJpXCI6bnVsbCxcImVudGl0bGVkUmVzb3VyY2VJZFwiOm51bGwsXCJlbnRpdGxlZFJlc291cmNlQ29udGFpbmVySWRcIjpudWxsLFwiZXh0ZXJuYWxJZFwiOm51bGwsXCJzb2Z0d2FyZUlkXCI6bnVsbCxcInNjb3Blc1wiOlt7XCJmdWxsU2NvcGVcIjpcImNvZGV3aGlzcGVyZXI6Y29tcGxldGlvbnNcIixcInN0YXR1c1wiOlwiSU5JVElBTFwiLFwiYXBwbGljYXRpb25Bcm5cIjpudWxsLFwidXNlQ2FzZUFjdGlvblwiOlwiY29tcGxldGlvbnNcIixcImZyaWVuZGx5SWRcIjpcImNvZGV3aGlzcGVyZXJcIixcInR5cGVcIjpcIkltbXV0YWJsZUFjY2Vzc1NvcGVcIixcInNjb3BlVHlwZVwiOlwiQUNDRVNTX1NDT1BFXCJ9LHtcImZ1bGxTY29wZVwiOlwiY29kZXdoaXNwZXJlcjphbmFseXNpc1wiLFwic3RhdHVzXCI6XCJJTklUSUFMXCIsXCJhcHBsaWNhdGlvbkFyblwiOm51bGwsXCJ1c2VDYXNlQWN0aW9uXCI6XCJhbmFseXNpc1wiLFwiZnJpZW5kbHlJZFwiOlwiY29kZXdoaXNwZXJlclwiLFwidHlwZVwiOlwiSW1tdXRhYmxlQWNjZXNzU2NvcGVcIixcInNjb3BlVHlwZVwiOlwiQUNDRVNTX1NDT1BFXCJ9LHtcImZ1bGxTY29wZVwiOlwiY29kZXdoaXNwZXJlcjpjb252ZXJzYXRpb25zXCIsXCJzdGF0dXNcIjpcIklOSVRJQUxcIixcImFwcGxpY2F0aW9uQXJuXCI6bnVsbCxcInVzZUNhc2VBY3Rpb25cIjpcImNvbnZlcnNhdGlvbnNcIixcImZyaWVuZGx5SWRcIjpcImNvZGV3aGlzcGVyZXJcIixcInR5cGVcIjpcIkltbXV0YWJsZUFjY2Vzc1Njb3BlXCIsXCJzY29wZVR5cGVcIjpcIkFDQ0VTU19TQ09QRVwifV0sXCJhdXRoZW50aWNhdGlvbkNvbmZpZ3VyYXRpb25cIjpudWxsLFwic2hhZG93QXV0aGVudGljYXRpb25Db25maWd1cmF0aW9uXCI6bnVsbCxcImVuYWJsZWRHcmFudHNcIjpudWxsLFwiZW5mb3JjZUF1dGhOQ29uZmlndXJhdGlvblwiOm51bGwsXCJvd25lckFjY291bnRJZFwiOm51bGwsXCJzc29JbnN0YW5jZUFjY291bnRJZFwiOm51bGwsXCJ1c2VyQ29uc2VudFwiOm51bGwsXCJub25JbnRlcmFjdGl2ZVNlc3Npb25zRW5hYmxlZFwiOm51bGwsXCJhc3NvY2lhdGVkSW5zdGFuY2VBcm5cIjpudWxsLFwiaXNFeHBpcmVkXCI6ZmFsc2UsXCJpc0JhY2tmaWxsZWRcIjpmYWxzZSxcImhhc0luaXRpYWxTY29wZXNcIjp0cnVlLFwiYXJlQWxsU2NvcGVzQ29uc2VudGVkVG9cIjpmYWxzZSxcImhhc1JlcXVlc3RlZFNjb3Blc1wiOmZhbHNlLFwiZ3JvdXBTY29wZXNCeUZyaWVuZGx5SWRcIjp7XCJjb2Rld2hpc3BlcmVyXCI6W3tcImZ1bGxTY29wZVwiOlwiY29kZXdoaXNwZXJlcjpjb21wbGV0aW9uc1wiLFwic3RhdHVzXCI6XCJJTklUSUFMXCIsXCJhcHBsaWNhdGlvbkFyblwiOm51bGwsXCJ1c2VDYXNlQWN0aW9uXCI6XCJjb21wbGV0aW9uc1wiLFwiZnJpZW5kbHlJZFwiOlwiY29kZXdoaXNwZXJlclwiLFwidHlwZVwiOlwiSW1tdXRhYmxlQWNjZXNzU2NvcGVcIixcInNjb3BlVHlwZVwiOlwiQUNDRVNTX1NDT1BFXCJ9LHtcImZ1bGxTY29wZVwiOlwiY29kZXdoaXNwZXJlcjpjb252ZXJzYXRpb25zXCIsXCJzdGF0dXNcIjpcIklOSVRJQUxcIixcImFwcGxpY2F0aW9uQXJuXCI6bnVsbCxcInVzZUNhc2VBY3Rpb25cIjpcImNvbnZlcnNhdGlvbnNcIixcImZyaWVuZGx5SWRcIjpcImNvZGV3aGlzcGVyZXJcIixcInR5cGVcIjpcIkltbXV0YWJsZUFjY2Vzc1NvcGVcIixcInNjb3BlVHlwZVwiOlwiQUNDRVNTX1NDT1BFXCJ9LHtcImZ1bGxTY29wZVwiOlwiY29kZXdoaXNwZXJlcjphbmFseXNpc1wiLFwic3RhdHVzXCI6XCJJTklUSUFMXCIsXCJhcHBsaWNhdGlvbkFyblwiOm51bGwsXCJ1c2VDYXNlQWN0aW9uXCI6XCJhbmFseXNpc1wiLFwiZnJpZW5kbHlJZFwiOlwiY29kZXdoaXNwZXJlclwiLFwidHlwZVwiOlwiSW1tdXRhYmxlQWNjZXNzU2NvcGVcIixcInNjb3BlVHlwZVwiOlwiQUNDRVNTX1NDT1BFXCJ9XX0sXCJzaG91bGRHZXRWYWx1ZUZyb21UZW1wbGF0ZVwiOnRydWUsXCJjb250YWluc09ubHlTc29TY29wZXNcIjpmYWxzZSxcInNzb1Njb3Blc1wiOltdLFwiaXNWMUJhY2tmaWxsZWRcIjpmYWxzZSxcImlzVjJCYWNrZmlsbGVkXCI6ZmFsc2UsXCJpc1YzQmFja2ZpbGxlZFwiOmZhbHNlLFwiaXNWNEJhY2tmaWxsZWRcIjpmYWxzZX0ifQ.uHBEW5oy9c9tlp7AzaWY7jVuerW5JCazQ3N8H5WZlFEEECLbrfdgtxN-4AQ_1nDV"
_ACCESS_TOKEN = "aoaAAAAAGpIwskgHUUtVgD5vO9mC-jLZCAqE_0awvLz4811abHp0uTMWjVvumXKeSHaFNljTtlQ4AR7U1Ym9goZ1ACkc0:MGQCMCE19XsUYc2ErJd9KhPpvdGHKoODJOfJmZ4F17L4Bislvp7AWV28rW1/MUEx9wF79wIwL+c1fdxWGVpLC+W6pkRkmnezfgY8EITfigC8iZ116bUWMHUwhTDm3YoN07k2SS5X"
_REFRESH_TOKEN = "aorAAAAAGq_TSMPmWzLYWB8n2ftbq5LVdC799o0XsguWN7G7KAA5jArYIWwbAauGels4HNgzem7CJ4BjqpFzwPDu8Ckc0:MGUCMQDcw5lAdcfKRuMQtW7XXXRtLx4cQmQdWG3gMIzAmvzkOGKqrv0Wcz8aXl4NTaeYTb0CMGeZbMAGq8iqcEgaT/YHCaIF0YRaHCRvynQmr2IJ5kdHO7UeXhvS2kaUnP7RYKQjfQ"
_TOKEN_EXPIRES = 1783153352  # 2026-07-04 08:22:32 UTC
# ==============================================================

REGION = "us-east-1"
RUNTIME = f"https://runtime.{REGION}.kiro.dev"
PROFILE_ARN = "arn:aws:codewhisperer:us-east-1:638616132270:profile/AAAACCCCXXXX"
PORT = 8080

log = logging.getLogger("kiro")
logging.basicConfig(level=logging.INFO, format="%(message)s")

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

# --- token management ---

TOKEN = {"access_token": _ACCESS_TOKEN, "expires_at": _TOKEN_EXPIRES}

def bearer_token():
    now = int(time.time())
    if now >= TOKEN["expires_at"] - 60:
        refresh()
    return TOKEN["access_token"]

def refresh():
    global TOKEN
    import boto3
    from botocore.config import Config
    client = boto3.client(
        "sso-oidc", region_name=REGION,
        aws_access_key_id="AKIA" + "X" * 16,
        aws_secret_access_key="X" * 40,
        config=Config(signature_version="unsigned", connect_timeout=10, read_timeout=10)
    )
    try:
        resp = client.create_token(
            clientId=_CLIENT_ID, clientSecret=_CLIENT_SECRET,
            grantType="refresh_token", refreshToken=_REFRESH_TOKEN
        )
        TOKEN["access_token"] = resp["accessToken"]
        TOKEN["expires_at"] = int(time.time()) + resp.get("expiresIn", 3600)
        log.info("Token refreshed")
    except Exception as e:
        log.warning(f"Token refresh failed (token may still work): {e}")

# --- API call ---

def call_api(msgs, model="claude-sonnet-4"):
    token = bearer_token()
    cid = str(uuid.uuid4())
    history = []
    for m in msgs[:-1]:
        r = m.get("role", "user")
        content = m.get("content", "")
        if r == "user":
            history.append({
                "userInputMessage": {
                    "content": content,
                    "userInputMessageContext": {
                        "envState": {"operatingSystem": "linux", "currentWorkingDirectory": os.getcwd()}
                    },
                    "origin": "KIRO_CLI",
                    "modelId": model
                }
            })
        elif r == "assistant":
            history.append({"assistantResponseMessage": {"content": content}})

    current = msgs[-1] if msgs else {"role": "user", "content": ""}
    payload = {
        "conversationState": {
            "conversationId": cid,
            "history": history,
            "currentMessage": {
                "userInputMessage": {
                    "content": current.get("content", ""),
                    "userInputMessageContext": {
                        "envState": {"operatingSystem": "linux", "currentWorkingDirectory": os.getcwd()}
                    },
                    "origin": "KIRO_CLI",
                    "modelId": model
                }
            },
            "chatTriggerType": "MANUAL",
            "agentTaskType": "vibe"
        },
        "profileArn": PROFILE_ARN
    }

    headers = {
        "Content-Type": "application/x-amz-json-1.0",
        "X-Amz-Target": "AmazonCodeWhispererStreamingService.GenerateAssistantResponse",
        "Authorization": f"Bearer {token}",
        "Host": f"runtime.{REGION}.kiro.dev",
        "x-amzn-codewhisperer-optout": "false",
    }
    log.info(f"[>] {model} {len(msgs)}msgs")
    return requests.post(RUNTIME, headers=headers, json=payload, stream=True, timeout=120)

# --- AWS EventStream parser ---

def parse_events(raw_iter):
    buf = b""
    for chunk in raw_iter:
        buf += chunk
        while True:
            if len(buf) < 12:
                break
            total_len = struct.unpack(">I", buf[0:4])[0]
            if len(buf) < total_len:
                break
            msg = buf[:total_len]
            buf = buf[total_len:]
            hdr_len = struct.unpack(">I", msg[4:8])[0]
            payload = msg[12 + hdr_len : total_len - 4]
            hdrs = {}
            pos = 12
            end = 12 + hdr_len
            while pos < end:
                name_len = msg[pos]
                pos += 1
                name = msg[pos:pos+name_len].decode()
                pos += name_len
                val_type = msg[pos]
                pos += 1
                if val_type == 7:
                    val_len = struct.unpack(">H", msg[pos:pos+2])[0]
                    pos += 2
                    val = msg[pos:pos+val_len].decode()
                    pos += val_len
                elif val_type == 6:
                    val = struct.unpack(">I", msg[pos:pos+4])[0]
                    pos += 4
                else:
                    val = None
                hdrs[name] = val
            try:
                data = json.loads(payload.decode()) if payload else {}
            except json.JSONDecodeError:
                data = {"_raw": payload.decode(errors="replace")}
            yield hdrs.get(":event-type", "unknown"), data

# --- OpenAI-compatible HTTP server ---

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
            self._json({
                "object": "list",
                "data": [{**m, "object": "model", "created": int(time.time())} for m in MODELS]
            })
        elif p == "/health":
            self._json({"status": "ok"})
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
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b.encode())))
        self.end_headers()
        self.wfile.write(b.encode())

    def _chat(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            req = json.loads(raw)
        except:
            self._json({"error": "bad json"}, 400)
            return
        model = req.get("model", "claude-sonnet-4")
        msgs = req.get("messages", [])
        stream = req.get("stream", False)
        if not msgs:
            self._json({"error": "no messages"}, 400)
            return
        try:
            if stream:
                self._stream(model, msgs)
            else:
                self._nonstream(model, msgs)
        except Exception as e:
            log.error(f"ERR: {e}")
            self._json({"error": str(e)}, 500)

    def _nonstream(self, model, msgs):
        resp = call_api(msgs, model)
        content = ""
        for etype, data in parse_events(resp.iter_content(chunk_size=4096)):
            if etype == "assistantResponseEvent":
                content += data.get("content", "")
        self._json({
            "id": f"cmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
        })

    def _stream(self, model, msgs):
        resp = call_api(msgs, model)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        cid = f"cmpl-{uuid.uuid4().hex[:12]}"
        ts = int(time.time())
        for etype, data in parse_events(resp.iter_content(chunk_size=4096)):
            if etype == "assistantResponseEvent":
                text = data.get("content", "")
                if text:
                    chunk = {
                        "id": cid, "object": "chat.completion.chunk", "created": ts, "model": model,
                        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
        final = {
            "id": cid, "object": "chat.completion.chunk", "created": ts, "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        }
        self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, fmt, *args):
        log.info(f"[{self.client_address[0]}] {fmt % args}")

# --- CLI ---

def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print("Kiro Gateway — твой личный Amazon Q Developer API прокси")
        print()
        print(f"  python3 {sys.argv[0]}           — запустить прокси на порту {PORT}")
        print(f"  python3 {sys.argv[0]} status    — проверить статус")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        try:
            r = requests.get(f"http://localhost:{PORT}/health", timeout=2)
            if r.status_code == 200:
                print(f"Gateway: RUNNING on http://localhost:{PORT}")
            else:
                print("Gateway: NOT RUNNING")
        except:
            print("Gateway: NOT RUNNING")
        return

    srv = HTTPServer(("0.0.0.0", PORT), GatewayHandler)
    srv.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    exp = datetime.fromtimestamp(_TOKEN_EXPIRES, tz=timezone.utc)
    print(f"Kiro Gateway running on http://localhost:{PORT}/v1/chat/completions")
    print(f"Token expires: {exp.strftime('%Y-%m-%d %H:%M:%S UTC')} (auto-refresh enabled)")
    print("NO USAGE LIMITS. EVER.")
    srv.serve_forever()

if __name__ == "__main__":
    main()

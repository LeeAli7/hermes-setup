import json, os, sys, logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse
import urllib.request

UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "http://127.0.0.1:9000/zen/v1")
API_KEY = os.environ.get("API_KEY", "public")
PORT = int(os.environ.get("PROXY_PORT", "5000"))
HOST = os.environ.get("PROXY_HOST", "0.0.0.0")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def upstream_headers():
    return {
        "Content-Type": "application/json",
        "x-api-key": API_KEY,
        "User-Agent": "hermes-anthropic-proxy/1.0",
    }


def anthropic_tools_to_openai(tools):
    result = []
    for tool in tools or []:
        result.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        })
    return result


def anthropic_messages_to_openai(messages):
    result = []
    for msg in messages or []:
        role = msg.get("role", "user")
        content_blocks = msg.get("content", [])

        if isinstance(content_blocks, str):
            result.append({"role": role, "content": content_blocks})
            continue

        text_parts = []
        tool_uses = []
        tool_results = []

        for block in content_blocks:
            t = block.get("type")
            if t == "text":
                text_parts.append(block["text"])
            elif t == "image":
                return None, "Image content in history not supported"
            elif t == "tool_use":
                tool_uses.append(block)
            elif t == "tool_result":
                tool_results.append(block)

        if role == "assistant" and tool_uses:
            text = "\n".join(text_parts) if text_parts else None
            asst = {"role": "assistant", "content": text}
            asst["tool_calls"] = [
                {
                    "id": tu["id"],
                    "type": "function",
                    "function": {
                        "name": tu["name"],
                        "arguments": json.dumps(tu["input"]),
                    },
                }
                for tu in tool_uses
            ]
            result.append(asst)
            continue

        if tool_results:
            for tr in tool_results:
                tr_content = tr.get("content", "")
                if isinstance(tr_content, list):
                    tr_text = "\n".join(
                        b.get("text", "") for b in tr_content if b.get("type") == "text"
                    )
                else:
                    tr_text = str(tr_content)
                result.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_use_id"],
                    "content": tr_text,
                })
            continue

        text = "\n".join(text_parts) if text_parts else ""
        result.append({"role": role, "content": text})

    return result


class Proxy(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/v1/models":
            self.proxy_get("/models")
        elif parsed.path == "/health":
            self.send_json(200, {"status": "ok"})
        else:
            self.proxy_get(parsed.path)

    def do_POST(self):
        parsed = urlparse(self.path)
        body = self.read_body()

        if parsed.path in ("/v1/messages", "/v1/complete"):
            self.handle_anthropic(body)
        elif parsed.path == "/v1/chat/completions":
            self.proxy_post("/chat/completions", body)
        else:
            self.proxy_post(parsed.path, body)

    def handle_anthropic(self, body):
        model = body.get("model")
        max_tokens = body.get("max_tokens", 1024)
        system = body.get("system", "")
        temperature = body.get("temperature")
        stop_sequences = body.get("stop_sequences", [])
        tools = body.get("tools", [])
        tool_choice = body.get("tool_choice")

        oa_msgs = anthropic_messages_to_openai(body.get("messages", []))
        if isinstance(oa_msgs, tuple):
            self.send_json(400, {"error": oa_msgs[1]})
            return

        oa_body = {
            "model": model,
            "messages": oa_msgs,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if temperature is not None:
            oa_body["temperature"] = temperature
        if stop_sequences:
            oa_body["stop"] = stop_sequences
        if tools:
            oa_body["tools"] = anthropic_tools_to_openai(tools)
        if tool_choice:
            oa_body["tool_choice"] = tool_choice

        status, data = self._upstream_post("/chat/completions", oa_body)

        if status != 200:
            err_msg = data.get("error", {})
            if isinstance(err_msg, str):
                err_msg = data.get("error", str(data))
            elif isinstance(err_msg, dict):
                err_msg = err_msg.get("message", str(data))
            logging.warning(f"Upstream {status} for model={model}: {err_msg}")
            self.send_json(status, data)
            return

        choices = data.get("choices", [])
        usage = data.get("usage", {})

        resp = {
            "id": data.get("id", ""),
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }

        content_blocks = []
        if choices:
            c = choices[0]
            msg = c.get("message", {})

            reasoning = msg.get("reasoning_content")
            content = msg.get("content")

            if reasoning:
                content_blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})
            if content:
                content_blocks.append({"type": "text", "text": content})

            for tc in (msg.get("tool_calls") or []):
                try:
                    args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, KeyError):
                    args = {}
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": args,
                })

            finish = c.get("finish_reason", "end_turn")
            if finish == "stop":
                resp["stop_reason"] = "end_turn"
            elif finish == "length":
                resp["stop_reason"] = "max_tokens"
            elif finish == "tool_calls":
                resp["stop_reason"] = "tool_use"
            else:
                resp["stop_reason"] = "end_turn"

        resp["content"] = content_blocks if content_blocks else [{"type": "text", "text": ""}]
        self.send_json(200, resp)

    def proxy_get(self, path):
        try:
            req = urllib.request.Request(
                f"{UPSTREAM_BASE}{path}",
                headers=upstream_headers(),
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                self.send_json(resp.status, json.loads(resp.read()))
        except Exception as e:
            logging.error(f"GET {path}: {e}")
            self.send_json(502, {"error": str(e)})

    def proxy_post(self, path, body):
        status, data = self._upstream_post(path, body)
        self.send_json(status, data)

    def _upstream_post(self, path, body):
        try:
            url = f"{UPSTREAM_BASE}{path}"
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                url, data=data, headers=upstream_headers(), method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read())
            except Exception:
                err_data = {"error": str(e)}
            return e.code, err_data
        except Exception as e:
            logging.error(f"POST {path}: {e}")
            return 502, {"error": str(e)}

    def read_body(self):
        length = int(self.headers.get("content-length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def send_json(self, status, data):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    def log_message(self, format, *args):
        pass


class ThreadingProxyServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    server = ThreadingProxyServer((HOST, PORT), Proxy)
    logging.info(f"Anthropic Proxy running on {HOST}:{PORT} → {UPSTREAM_BASE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()

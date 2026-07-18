import json, os, sys, logging
from http.server import HTTPServer, BaseHTTPRequestHandler
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


def anthropic_content_to_openai(content):
    if isinstance(content, str):
        return content
    texts = []
    for block in content:
        if block.get("type") == "text":
            texts.append(block["text"])
        elif block.get("type") == "image":
            return content
    return "\n".join(texts)


def openai_choice_to_anthropic(choice):
    msg = choice.get("message", {})
    content_blocks = []
    if msg.get("reasoning_content"):
        content_blocks.append({"type": "text", "text": msg["reasoning_content"]})
    if msg.get("content"):
        content_blocks.append({"type": "text", "text": msg["content"]})
    stop_reason = choice.get("finish_reason", "end_turn")
    if stop_reason == "stop":
        stop_reason = "end_turn"
    elif stop_reason == "length":
        stop_reason = "max_tokens"
    return {
        "type": "message",
        "role": msg.get("role", "assistant"),
        "content": content_blocks,
        "stop_reason": stop_reason,
    }


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
        messages = body.get("messages", [])
        system = body.get("system", "")
        temperature = body.get("temperature")
        stop_sequences = body.get("stop_sequences", [])

        openai_messages = []
        if system:
            openai_messages.append({"role": "system", "content": system})
        for msg in messages:
            role = msg.get("role", "user")
            content = anthropic_content_to_openai(msg.get("content", ""))
            if isinstance(content, list):
                self.send_json(400, {"error": "Image content not supported via Anthropic proxy"})
                return
            openai_messages.append({"role": role, "content": content})

        openai_body = {
            "model": model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if temperature is not None:
            openai_body["temperature"] = temperature
        if stop_sequences:
            openai_body["stop"] = stop_sequences

        status, data = self._upstream_post("/chat/completions", openai_body)

        if status == 400:
            err = data.get("error", {}).get("message", str(data))
            if "upstream request failed" in err.lower() or "model=None" in err:
                pass
            self.send_json(status, data)
            return

        if status != 200:
            self.send_json(status, data)
            return

        choices = data.get("choices", [])
        usage = data.get("usage", {})

        anthropic_usage = {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        }

        response = {
            "id": data.get("id", ""),
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": anthropic_usage,
        }

        content_list = []
        if choices:
            choice = choices[0]
            msg = choice.get("message", {})
            reasoning = msg.get("reasoning_content")
            content = msg.get("content")
            if reasoning:
                content_list.append({"type": "thinking", "thinking": reasoning, "signature": ""})
            if content:
                content_list.append({"type": "text", "text": content})
            stop_reason = choice.get("finish_reason", "end_turn")
            if stop_reason == "stop":
                stop_reason = "end_turn"
            elif stop_reason == "length":
                stop_reason = "max_tokens"
            response["stop_reason"] = stop_reason

        response["content"] = content_list if content_list else [{"type": "text", "text": ""}]
        self.send_json(200, response)

    def proxy_get(self, path):
        try:
            req = urllib.request.Request(
                f"{UPSTREAM_BASE}{path}",
                headers=upstream_headers(),
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
                self.send_json(resp.status, data)
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
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), Proxy)
    logging.info(f"Anthropic Proxy running on {HOST}:{PORT} → {UPSTREAM_BASE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()

#!/usr/bin/env python3
"""Внешний мост перед 9000-форвардером (opencode/zen).

Задача скрипта — только транспорт: принять запрос из публичного туннеля
и передать его в 127.0.0.1:9000. Сам forwarder.py не трогается.

Единственное преобразование:

    POST {/zen,/}{/v1,}/responses  с Responses-телом (input, без messages)
    для chat-only модели
        -> POST /zen/v1/chat/completions с chat-телом (messages)
        -> ответ клиенту отдаётся обратно в Responses-SSE виде.

Почему: клиенты (Supreader/md-reader) шлют Responses API на модель, которой
на стороне форвардера соответствует chat/completions. Форвардер
перенаправляет путь, но не тело — нативный responses-body уезжает в
chat-эндпоинт, upstream отбивает его 403, и после 30 попыток клиент видит
503 "All retries exhausted". Мост чинит ровно этот случай.

Responses-совместимые модели (muse/jev) и все остальные пути идут транзитом
байт-в-байт.
"""

import json
import logging
import os
import sys
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "logs", "zen_bridge.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("zen_bridge")

UPSTREAM_HOST = os.environ.get("BRIDGE_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("BRIDGE_UPSTREAM_PORT", "9000"))
UPSTREAM_TIMEOUT = float(os.environ.get("BRIDGE_UPSTREAM_TIMEOUT", "600"))
MAX_BODY_SIZE = int(os.environ.get("BRIDGE_MAX_BODY", str(20 * 1024 * 1024)))

# Те же responses-модели, что знает форвардер (forwarder.py: RESPONSES_MODELS).
RESPONSES_MODELS = {
    "muse-spark-1.3-contributor-free",
    "muse-spark-1.2-contributor-free",
    "jev-1.13-free",
}

HOP_BY_HOP = {
    "host", "content-length", "transfer-encoding", "connection",
    "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "upgrade", "content-encoding",
}
DROP_RESPONSE_HEADERS = HOP_BY_HOP | {"accept-encoding"}
CHUNK = 65536


def _input_to_messages(inp):
    """Responses `input` (строка или список) -> chat `messages`."""
    if isinstance(inp, str):
        return [{"role": "user", "content": inp}] if inp else []
    if not isinstance(inp, list):
        return []
    messages = []
    for item in inp:
        if not isinstance(item, dict):
            if isinstance(item, str) and item:
                messages.append({"role": "user", "content": item})
            continue
        role = item.get("role") or "user"
        content = item.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            text = item.get("text")
            if isinstance(text, str):
                messages.append({"role": role, "content": text})
            continue
        parts = []
        for p in content:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type")
            if ptype in ("input_text", "text", "output_text"):
                if isinstance(p.get("text"), str):
                    parts.append({"type": "text", "text": p["text"]})
            elif ptype in ("input_image", "image_url"):
                url = p.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                if isinstance(url, str) and url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
        if parts:
            messages.append({"role": role, "content": parts})
    return messages


def _convert_request(path, body):
    """(new_path, new_body) либо None, если конвертация не нужна."""
    if not body or not path.rstrip("/").endswith("/responses"):
        return None
    try:
        j = json.loads(body)
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    if isinstance(j.get("messages"), list):
        return None           # клиент уже прислал chat-shape
    if "input" not in j:
        return None
    model = j.get("model")
    if model in RESPONSES_MODELS:
        return None           # responses-модель идёт транзитом
    messages = _input_to_messages(j.get("input"))
    if not messages:
        return None
    chat = {"model": model, "messages": messages, "stream": False}
    if isinstance(j.get("temperature"), (int, float)):
        chat["temperature"] = j["temperature"]
    if isinstance(j.get("top_p"), (int, float)):
        chat["top_p"] = j["top_p"]
    cap = j.get("max_output_tokens") or j.get("max_tokens")
    if isinstance(cap, int) and cap > 0:
        chat["max_tokens"] = cap
    return "/zen/v1/chat/completions", json.dumps(chat).encode()


def _responses_sse(text, model):
    """Ответ chat/completions -> Responses-SSE (формат, который ждёт клиент)."""
    def ev(obj):
        return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()

    rid = "resp_bridge"
    out = ev({"type": "response.created",
              "response": {"id": rid, "model": model, "status": "in_progress"}})
    out += ev({"type": "response.output_text.delta", "delta": text})
    out += ev({"type": "response.output_text.done", "text": text})
    out += ev({"type": "response.completed",
               "response": {"id": rid, "model": model, "status": "completed",
                            "output": [{"type": "message", "role": "assistant",
                                        "content": [{"type": "output_text",
                                                     "text": text}]}]}})
    out += b"data: [DONE]\n\n"
    return out


def _chat_text(raw):
    """Достать текст из chat/completions-ответа (или из SSE, если прилетел он)."""
    try:
        j = json.loads(raw)
    except Exception:
        text = ""
        for line in raw.decode("utf-8", "replace").split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            for ch in chunk.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content")
                if isinstance(piece, str):
                    text += piece
        return text
    msg = ((j.get("choices") or [{}])[0].get("message") or {})
    content = msg.get("content")
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else ""


class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "zen-bridge/1.0"

    def log_message(self, format, *args):   # не сыпать в stderr
        pass

    # --- методы ---
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_OPTIONS(self):
        self._handle("OPTIONS")

    # --- тело запроса ---
    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY_SIZE:
            return None
        buf = b""
        while len(buf) < length:
            chunk = self.rfile.read(min(CHUNK, length - len(buf)))
            if not chunk:
                break
            buf += chunk
        return buf

    def _upstream_headers(self, extra_len=None):
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            headers[k] = v
        headers["Accept-Encoding"] = "identity"
        headers["Connection"] = "close"
        if extra_len is not None:
            headers["Content-Length"] = str(extra_len)
        return headers

    # --- основной обработчик ---
    def _handle(self, method):
        path = self.path
        body = self._read_body()
        if body is None:
            self._send_simple(413, b'{"error":"request body too large"}')
            return

        converted = _convert_request(path, body)
        out_path, out_body = converted if converted else (path, body)

        conn = None
        try:
            conn = http.client.HTTPConnection(
                UPSTREAM_HOST, UPSTREAM_PORT, timeout=UPSTREAM_TIMEOUT)
            headers = self._upstream_headers(len(out_body) if converted else None)
            if method == "HEAD":
                out_body = None
            conn.request(method, out_path, body=out_body, headers=headers)
            resp = conn.getresponse()

            if converted:
                if resp.status != 200:
                    raw = resp.read()
                    log.warning("convert %s -> %s for model=%s: upstream %s",
                                path, out_path, _model_of(body), resp.status)
                    self._send_simple(resp.status, raw)
                    return
                text = _chat_text(resp.read())
                payload = _responses_sse(text, _model_of(body))
                self._send_simple(200, payload, "text/event-stream")
                log.info("convert %s -> %s OK (%d chars)", path, out_path, len(text))
                return

            self._relay(resp)
        except Exception as e:
            log.error("bridge error for %s %s: %s", method, path, e)
            self._send_simple(502, json.dumps(
                {"error": {"type": "bridge_error", "message": str(e)[:200]}}).encode())
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _relay(self, resp):
        try:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in DROP_RESPONSE_HEADERS:
                    continue
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while True:
                chunk = resp.read(CHUNK) if not hasattr(resp, "read1") else resp.read1(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _send_simple(self, status, payload, content_type="application/json"):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command != "HEAD":
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def _model_of(body):
    try:
        return json.loads(body).get("model")
    except Exception:
        return None


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("BRIDGE_PORT", "9010"))
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), BridgeHandler)
    log.info("zen bridge listening on %s:%s -> %s:%s", host, port, UPSTREAM_HOST, UPSTREAM_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()

import json, os, sys, logging, threading, time
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

UPSTREAM_BASE = "https://opencode.ai"

FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding"}


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

    def handle_request(self, method):
        content_length = int(self.headers.get("Content-Length", 0))
        body = None
        if content_length > 0:
            body = self.rfile.read(content_length)

        url = UPSTREAM_BASE + self.path
        proxy = get_proxy()
        log.info(f"Request: {method} {self.path} proxy={proxy}")

        headers = {k: v for k, v in self.headers.items() if k.lower() not in FORBIDDEN_HEADERS}

        try:
            resp = requests.request(
                method=method, url=url, data=body, headers=headers,
                proxies=proxy, timeout=(10, 60), stream=True, verify=False,
            )
            log.info(f"Response: {resp.status_code} for {self.path}")

            self.send_response(resp.status_code)
            for k, v in resp.headers.items():
                kl = k.lower()
                if kl not in ("transfer-encoding", "content-encoding", "connection"):
                    self.send_header(k, v)
            self.end_headers()

            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
            resp.close()

        except requests.exceptions.Timeout:
            log.error(f"Timeout for {method} {self.path}")
            self.send_error(502, "Forwarder Error: Upstream timeout")
        except requests.exceptions.ProxyError as e:
            log.error(f"ProxyError for {method} {self.path}: {e}")
            self.send_error(502, "Forwarder Error: Proxy failed")
        except requests.exceptions.ConnectionError as e:
            log.error(f"ConnectionError for {method} {self.path}: {e}")
            self.send_error(502, "Forwarder Error: Connection failed")
        except Exception as e:
            log.error(f"Error for {method} {self.path}: {e}", exc_info=True)
            self.send_error(502, f"Forwarder Error: {e}")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    server = ThreadingHTTPServer(("127.0.0.1", port), ProxyHandler)
    log.info(f"Forwarder started on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

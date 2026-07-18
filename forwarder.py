import json, os, sys, logging, threading, time, socket, binascii
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
CONTROL_PORT = 9051
SOCKS_PORT = 9050
UPSTREAM_READ_TIMEOUT = 300  # upstream response timeout (seconds)
UPSTREAM_CONNECT_TIMEOUT = 60

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
        headers = {k: v for k, v in self.headers.items() if k.lower() not in FORBIDDEN_HEADERS}

        for attempt in range(2):
            proxy = get_proxy()
            log.info(f"Request: {method} {self.path} attempt={attempt+1} proxy={proxy}")
            try:
                resp = requests.request(
                    method=method, url=url, data=body, headers=headers,
                    proxies=proxy, timeout=(UPSTREAM_CONNECT_TIMEOUT, UPSTREAM_READ_TIMEOUT), stream=True, verify=False,
                )
                log.info(f"Response: {resp.status_code} for {self.path} attempt={attempt+1}")

                if resp.status_code == 429 and attempt == 0:
                    log.warning(f"429 detected for {self.path}, rotating Tor IP...")
                    resp.close()
                    if renew_tor_ip():
                        log.info(f"IP rotated, retrying {self.path}")
                        continue
                    log.warning("IP rotation failed, returning original 429")
                    self.send_response(429)
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
                    return

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
                return

            except requests.exceptions.Timeout:
                log.error(f"Timeout for {method} {self.path}")
                self.send_error(502, "Forwarder Error: Upstream timeout")
                return
            except requests.exceptions.ProxyError as e:
                log.error(f"ProxyError for {method} {self.path}: {e}")
                self.send_error(502, "Forwarder Error: Proxy failed")
                return
            except requests.exceptions.ConnectionError as e:
                log.error(f"ConnectionError for {method} {self.path}: {e}")
                self.send_error(502, "Forwarder Error: Connection failed")
                return
            except Exception as e:
                log.error(f"Error for {method} {self.path}: {e}", exc_info=True)
                self.send_error(502, f"Forwarder Error: {e}")
                return


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    host = os.environ.get("FORWARDER_HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), ProxyHandler)
    log.info(f"Forwarder started on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""HTTP CONNECT proxy for a VPN network namespace.

Listens on <host>:<port> (e.g. 10.200.0.2:8897) and connects upstream with
the namespace's normal routing, i.e. through tun0 once OpenVPN is up.  No
source binding here: binding the veth address would leak a private source
into the tunnel.
"""
import socket
import socketserver
import subprocess
import sys
import threading

LISTEN = sys.argv[1] if len(sys.argv) > 1 else "10.200.0.2:8897"
HOST, _, PORT = LISTEN.rpartition(":")
PORT = int(PORT)


def vpn_ready():
    """Refuse to proxy unless OpenVPN owns the namespace routes.

    Without this guard a dead tunnel silently falls back to the host IP via
    the veth default route — exactly the identity we are trying to avoid.
    """
    try:
        addr = subprocess.run(["ip", "-4", "addr", "show", "tun0"],
                              capture_output=True, text=True, timeout=3)
        routes = subprocess.run(["ip", "route"], capture_output=True,
                                text=True, timeout=3)
        return "inet " in addr.stdout and "0.0.0.0/1" in routes.stdout
    except Exception:
        return False


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(65536).decode("latin1").strip()
            if not line:
                return
            method, target, _ = line.split(" ", 2)
            while True:
                h = self.rfile.readline(65536)
                if h in (b"\r\n", b"\n", b""):
                    break
            if method.upper() != "CONNECT":
                self.wfile.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            if not vpn_ready():
                self.wfile.write(b"HTTP/1.1 503 VPN Down\r\n\r\n")
                return
            host, _, port = target.rpartition(":")
            port = int(port or 443)
            try:
                up = socket.create_connection((host, port), timeout=20)
            except OSError as e:
                sys.stderr.write(f"connect {host}:{port} failed: {e}\n")
                self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            up.settimeout(None)
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            down = self.connection

            def pipe(a, b):
                try:
                    while True:
                        d = a.recv(65536)
                        if not d:
                            break
                        b.sendall(d)
                except OSError:
                    pass
                finally:
                    try:
                        b.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            t = threading.Thread(target=pipe, args=(down, up), daemon=True)
            t.start()
            pipe(up, down)
            t.join(timeout=2)
        except Exception as e:
            sys.stderr.write(f"netns proxy err: {e}\n")


class Srv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    Srv((HOST, PORT), Handler).serve_forever()

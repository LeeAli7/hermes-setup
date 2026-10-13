#!/bin/sh
# Boot gate for hermes-gateway: do not start serving until Tor SOCKS and
# both forwarder bridges accept TCP. Cheap socket probes only — no upstream
# traffic (no model calls, no Tor rotations).
# Usage: wait-for-backend.sh [timeout_sec]  (default 120)
set -e
TIMEOUT="${1:-120}"
/usr/bin/python3 - "$TIMEOUT" <<'EOF'
import socket, sys, time
timeout = float(sys.argv[1])
targets = [("127.0.0.1", 9050), ("127.0.0.1", 9000), ("127.0.0.1", 9001)]
deadline = time.time() + timeout
while True:
    pending = []
    for host, port in targets:
        try:
            s = socket.create_connection((host, port), timeout=3)
            s.close()
        except OSError:
            pending.append(port)
    if not pending:
        print("wait-for-backend: 9050/9000/9001 all accepting", flush=True)
        sys.exit(0)
    if time.time() >= deadline:
        print(f"wait-for-backend: TIMEOUT, still closed: {pending}", flush=True)
        sys.exit(1)
    time.sleep(2)
EOF

#!/usr/bin/env python3
"""Keep one working VPNGate tunnel alive in an isolated netns.

The host route and SSH are never touched.  Inside netns `vgprobe` OpenVPN
installs its own default routes; a CONNECT proxy listens on
10.200.0.2:8897 and is reachable from the host over the veth pair.  The
supervisor rotates to the next VPNGate server when the tunnel dies.

Run as root (systemd system service).
"""
import base64
import csv
import glob
import json
import os
import random
import signal
import subprocess
import sys
import time
import urllib.request

BASE = os.environ.get(
    "HERMES_EGRESS_HOME", os.path.expanduser("~/.hermes/ippool"))
SLOT = (os.environ.get("VPNGATE_SLOT", "0") or "0").strip()
if SLOT == "0":
    NETNS = "vgprobe"
    VETH = "veth-vg"
    VETH_NS = "veth-vg-ns"
    HOST_IP = "10.200.0.1"
    NS_IP = "10.200.0.2"
    PROXY_PORT = 8897
    SUBNET = "10.200.0.0/24"
    RUN_DIR = os.path.join(BASE, "vpngate")
    RESULTS = os.path.join(BASE, "vpngate_live.json")
    CONTROL_DIR = os.path.join(BASE, "vpngate_control")
else:
    NETNS = f"vgprobe{SLOT}"
    VETH = f"veth-vg{SLOT}"
    VETH_NS = f"veth-vg{SLOT}-ns"
    HOST_IP = f"10.20{SLOT}.0.1"
    NS_IP = f"10.20{SLOT}.0.2"
    PROXY_PORT = 8897 + int(SLOT)
    SUBNET = f"10.20{SLOT}.0.0/24"
    RUN_DIR = os.path.join(BASE, f"vpngate{SLOT}")
    RESULTS = os.path.join(BASE, f"vpngate_live_{SLOT}.json")
    CONTROL_DIR = os.path.join(BASE, f"vpngate_control{SLOT}")
PROXY_ADDR = f"{NS_IP}:{PROXY_PORT}"


def _default_phys_if():
    try:
        out = subprocess.run(["ip", "route", "show", "default"],
                             capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            parts = line.split()
            if "dev" in parts:
                return parts[parts.index("dev") + 1]
    except Exception:
        pass
    return "eth0"


PHYS_IF = os.environ.get("VPNGATE_PHYS_IF") or _default_phys_if()
# Slot 1 must not race slot 0 to the same "fastest" VPNGate server: give each
# slot a disjoint slice of the candidate list (40 servers apart).
OFFSET = int(os.environ.get("VPNGATE_OFFSET", str(int(SLOT) * 40)) or 0)
AUTH_FILE = os.path.join(RUN_DIR, "auth")
PROXY_LOG = os.path.join(RUN_DIR, "proxy.log")
FAILED_FILE = os.path.join(RUN_DIR, "failed.json")
PROXY_SCRIPT = os.path.join(BASE, "netns_connect_proxy.py")
ROTATE_FILE = os.path.join(CONTROL_DIR, "rotate.request")
FAILED = {}
FAIL_TTL = 3600
# Optional comma-separated "known good" IPs.  Empty on a fresh device: a
# hardcoded list from another host would only bias the search toward exits
# that are already burned there.
GOOD_IPS = {
    ip.strip() for ip in os.environ.get("VPNGATE_GOOD_IPS", "").split(",")
    if ip.strip()
}
# 2026-11-06: temporary pin for pulse-alpha-free.  The model is gated by
# egress region and the shared VPNGate exits are already rate-limited, so
# rotation only finds more burned IPs.  While pinned we reconnect to the
# same server and ignore watchdog rotation requests.
PIN_HOST = os.environ.get("VPNGATE_PIN_HOST", "").strip()
PIN_IP = os.environ.get("VPNGATE_PIN_IP", "").strip()
PINNED = bool(PIN_HOST or PIN_IP)
STOP = False
PROXY_PROC = None


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def run(args, timeout=20, **kw):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, **kw)
    except Exception as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def ns(*args, timeout=20, **kw):
    return run(["ip", "netns", "exec", NETNS, *args], timeout=timeout, **kw)


def ensure_setup():
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(CONTROL_DIR, exist_ok=True)
    with open(AUTH_FILE, "w") as f:
        f.write("vpn\nvpn\n")
    need_create = NETNS not in run(["ip", "netns", "list"]).stdout.split()
    if not need_create:
        addr = ns("ip", "-4", "addr", "show", VETH_NS).stdout
        host_link = run(["ip", "link", "show", VETH]).returncode == 0
        if not host_link or NS_IP not in addr:
            log("netns exists but veth is incomplete; recreating")
            run(["ip", "netns", "del", NETNS])
            run(["ip", "link", "del", VETH])
            need_create = True
    if need_create:
        log("creating netns/veth/NAT")
        run(["ip", "netns", "add", NETNS])
        run(["ip", "link", "del", VETH])
        run(["ip", "link", "add", VETH, "type", "veth",
             "peer", "name", VETH_NS])
        run(["ip", "link", "set", VETH_NS, "netns", NETNS])
        run(["ip", "addr", "add", f"{HOST_IP}/24", "dev", VETH])
        run(["ip", "link", "set", VETH, "up"])
        ns("ip", "addr", "add", f"{NS_IP}/24", "dev", VETH_NS)
        ns("ip", "link", "set", VETH_NS, "up")
        ns("ip", "link", "set", "lo", "up")
        ns("ip", "route", "add", "default", "via", HOST_IP)
        os.makedirs(f"/etc/netns/{NETNS}", exist_ok=True)
        with open(f"/etc/netns/{NETNS}/resolv.conf", "w") as f:
            f.write("nameserver 1.1.1.1\nnameserver 8.8.8.8\n")
    run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
    # Idempotent NAT/forward rules for the veth subnet.
    if run(["iptables", "-t", "nat", "-C", "POSTROUTING", "-s",
            SUBNET, "-o", PHYS_IF, "-j", "MASQUERADE"]).returncode != 0:
        run(["iptables", "-t", "nat", "-I", "POSTROUTING", "1", "-s",
             SUBNET, "-o", PHYS_IF, "-j", "MASQUERADE"])
    if run(["iptables", "-C", "FORWARD", "-s", SUBNET, "-o",
            PHYS_IF, "-j", "ACCEPT"]).returncode != 0:
        run(["iptables", "-I", "FORWARD", "1", "-s", SUBNET,
             "-o", PHYS_IF, "-j", "ACCEPT"])
    if run(["iptables", "-C", "FORWARD", "-d", SUBNET, "-i",
            PHYS_IF, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
            "-j", "ACCEPT"]).returncode != 0:
        run(["iptables", "-I", "FORWARD", "1", "-d", SUBNET, "-i",
             PHYS_IF, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
             "-j", "ACCEPT"])


def host_main_ip():
    out = run(["curl", "-4", "-sS", "--max-time", "8",
               "https://api.ipify.org"]).stdout.strip()
    return out or os.environ.get("VPNGATE_HOST_MAIN", "")


def proxy_alive():
    out = run(["python3", "-c",
               "import socket;s=socket.socket();s.settimeout(2);"
               f"s.connect(('{NS_IP}',{PROXY_PORT}));s.close();print('ok')"],
              timeout=5)
    return "ok" in out.stdout


def start_proxy():
    global PROXY_PROC
    if PROXY_PROC is not None and PROXY_PROC.poll() is None and proxy_alive():
        return True
    if PROXY_PROC is not None and PROXY_PROC.poll() is None:
        PROXY_PROC.terminate()
        try:
            PROXY_PROC.wait(timeout=3)
        except Exception:
            PROXY_PROC.kill()
    log("starting netns CONNECT proxy")
    out = open(PROXY_LOG, "a", buffering=1)
    PROXY_PROC = subprocess.Popen(
        ["ip", "netns", "exec", NETNS, "/usr/bin/python3", PROXY_SCRIPT,
         PROXY_ADDR],
        stdout=out, stderr=subprocess.STDOUT,
    )
    for _ in range(15):
        if proxy_alive():
            return True
        time.sleep(1)
    return False


def kill_openvpn():
    run(["pkill", "-9", "-f", "openvpn --config " + RUN_DIR + "/"])
    time.sleep(2)
    ns("ip", "link", "del", "tun0")


def wait_tunnel(timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        addr = ns("ip", "-4", "addr", "show", "tun0").stdout
        routes = ns("ip", "route").stdout
        if "inet " in addr and "0.0.0.0/1" in routes:
            return True
        time.sleep(1)
    return False


def tunnel_exit():
    for _ in range(2):
        r = ns("curl", "-sS", "--max-time", "12", "https://api.ipify.org",
               timeout=18)
        ip = (r.stdout or "").strip()
        if ip:
            return ip
        time.sleep(2)
    return ""


def load_failed():
    try:
        with open(FAILED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        FAILED.update({k: float(v) for k, v in data.items()
                       if float(v) > now - FAIL_TTL})
    except Exception:
        pass


def save_failed():
    try:
        with open(FAILED_FILE, "w", encoding="utf-8") as f:
            json.dump(FAILED, f)
    except Exception:
        pass


def rotation_requested():
    try:
        if os.path.exists(ROTATE_FILE):
            os.unlink(ROTATE_FILE)
            return True
    except Exception:
        pass
    return False


def mark_current_failed():
    """Skip the current server after an explicit rotation request."""
    try:
        with open(RESULTS, encoding="utf-8") as f:
            data = json.load(f)
        host = data.get("host")
        if host:
            FAILED[host] = time.time()
            save_failed()
            log(f"rotation requested; marking {host} failed")
    except Exception:
        pass


def cached_servers():
    """Fallback when vpngate.net is unreachable: reuse saved .ovpn configs."""
    now = time.time()
    out = []
    for path in sorted(glob.glob(os.path.join(RUN_DIR, "vg_*.ovpn"))):
        host = "cached:" + os.path.basename(path)
        if not PINNED and FAILED.get(host, 0) > now - FAIL_TTL:
            continue
        try:
            with open(path, "rb") as f:
                raw = f.read()
            if PINNED:
                text = raw.decode("utf-8", "replace")
                if PIN_HOST and PIN_IP:
                    if PIN_HOST not in text and PIN_IP not in text:
                        continue
                elif PIN_HOST:
                    if PIN_HOST not in text:
                        continue
                elif PIN_IP:
                    if PIN_IP not in text:
                        continue
            b64 = base64.b64encode(raw).decode()
        except Exception:
            continue
        out.append([host, os.path.basename(path), "0", "0", "99999999",
                    "0", "cached", "0", "0", "0", "0", "0", "0", "0", b64])
    if out:
        log(f"vpngate list fallback: {len(out)} cached configs")
    return out


def fetch_servers():
    # curl with a hard deadline: Python urllib can hang in DNS for minutes on
    # this host, and vpngate.net is frequently unreachable from OCI.
    # The VPNGate CSV is ~1.3 MB and often takes 15-25 s from this host.
    # A 15 s cap truncated it mid-transfer, so the supervisor fell back to
    # stale cached configs that no longer connect.
    r = run(["curl", "-4", "-sS", "--max-time", "45",
             "https://www.vpngate.net/api/iphone/"], timeout=50)
    raw = (r.stdout or "").strip()
    if r.returncode != 0 or not raw:
        log(f"vpngate list failed: curl rc={r.returncode} "
            f"{(r.stderr or '').strip()[:140]}")
        return cached_servers()
    rows = list(csv.reader([l for l in raw.splitlines()
                            if l and not l.startswith("*")]))
    data = [r for r in rows[1:] if len(r) >= 15]
    if not data:
        return cached_servers()

    if PINNED:
        pinned = [
            r for r in data
            if (PIN_HOST and r[0] == PIN_HOST)
            or (PIN_IP and r[1] == PIN_IP)
        ]
        if pinned:
            log(f"vpngate pin: {pinned[0][1]} ({pinned[0][0]})")
            return pinned[:1]
        log(f"vpngate pin {PIN_HOST or PIN_IP} not in list; using cached")
        return cached_servers()

    def num(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    data.sort(key=lambda r: (0 if r[1] in GOOD_IPS else 1,
                             num(r[7]), -num(r[2])))
    now = time.time()
    picked, per_country = [], {}
    for r in data:
        if num(r[4]) < float(os.environ.get("VPNGATE_MIN_SPEED", "0")):
            continue
        host = r[0]
        if FAILED.get(host, 0) > now - FAIL_TTL:
            continue
        c = r[6]
        cap = int(os.environ.get("VPNGATE_PER_COUNTRY", "8"))
        if per_country.get(c, 0) >= cap:
            continue
        per_country[c] = per_country.get(c, 0) + 1
        picked.append(r)
        if len(picked) >= 120:
            break
    # Both hot slots read the same CSV.  Without an offset they race to the
    # same "fastest" server, share its quota and burn together.  Give each
    # slot a disjoint slice and let it try less popular (less burned) exits.
    off = OFFSET
    if off and picked:
        off %= len(picked)
        picked = picked[off:] + picked[:off]
    return picked


def try_server(row, idx):
    host, ip, country = row[0], row[1], row[6]
    cfg = os.path.join(RUN_DIR, f"vg_{idx}.ovpn")
    logfile = os.path.join(RUN_DIR, f"vg_{idx}.log")
    try:
        with open(cfg, "w") as f:
            f.write(base64.b64decode(row[14]).decode("utf-8", "replace"))
    except Exception as e:
        log(f"bad config {host}: {e}")
        return None
    kill_openvpn()
    log(f"trying {country} {ip} ({host})")
    proc = subprocess.Popen(
        ["ip", "netns", "exec", NETNS, "openvpn", "--config", cfg,
         "--auth-user-pass", AUTH_FILE, "--auth-nocache",
         "--script-security", "0", "--connect-timeout", "10",
         "--connect-retry-max", "1", "--verb", "3", "--log", logfile],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if not wait_tunnel():
        log(f"vpn-fail {ip}")
        FAILED[host] = time.time()
        save_failed()
        proc.terminate()
        kill_openvpn()
        return None
    exit_ip = tunnel_exit()
    if not exit_ip or exit_ip == host_main_ip():
        log(f"egress-fail {ip} exit={exit_ip or '-'}")
        FAILED[host] = time.time()
        save_failed()
        proc.terminate()
        kill_openvpn()
        return None
    with open(RESULTS, "w") as f:
        json.dump({"host": host, "ip": ip, "country": country,
                   "exit": exit_ip, "ts": time.time()}, f)
    log(f"tunnel up: {country} {ip} -> {exit_ip}")
    return proc


def monitor(proc):
    fails = 0
    while not STOP:
        time.sleep(20)
        if not PINNED and rotation_requested():
            mark_current_failed()
            return False
        if proc.poll() is not None:
            log("openvpn exited")
            return False
        if not proxy_alive():
            log("netns proxy died; restarting")
            start_proxy()
        if proxy_alive() and tunnel_exit():
            fails = 0
            continue
        fails += 1
        log(f"tunnel check failed ({fails}/3)")
        if fails >= 3:
            return False
    return False


def main():
    global STOP, PROXY_PROC
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("STOP", True))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("STOP", True))
    ensure_setup()
    load_failed()
    if not start_proxy():
        log("proxy failed to start; exiting")
        return 1
    while not STOP:
        servers = fetch_servers()
        if not servers:
            log("no candidates; sleeping 60s")
            time.sleep(60)
            continue
        servers = servers[:20]
        for i, row in enumerate(servers):
            if STOP:
                break
            proc = try_server(row, i)
            if not proc:
                continue
            if monitor(proc):
                continue
            if PINNED:
                log("pinned VPNGate server lost; reconnecting in 15s")
                kill_openvpn()
                time.sleep(15)
                continue
            log("rotating VPNGate server")
            kill_openvpn()
        time.sleep(15)
    kill_openvpn()
    if PROXY_PROC is not None and PROXY_PROC.poll() is None:
        PROXY_PROC.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())

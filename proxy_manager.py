import os, sys, json, time, re, subprocess, socket, threading, urllib.request, urllib.error, urllib.parse, signal
from pathlib import Path

os.environ["PYTHONUNBUFFERED"] = "1"

HERMES_HOME = Path(__file__).parent
VENV_PY = Path.home() / "hermes-agent" / "venv" / "bin" / "python3"
FORWARDER_SCRIPT = HERMES_HOME / "forwarder.py"
GATEWAY_SCRIPT = Path.home() / "hermes-agent" / "venv" / "bin" / "hermes"
HERMES_LOG = Path.home() / ".hermes" / "logs" / "agent.log"
STATE_FILE = HERMES_HOME / "proxy_state.json"
MANAGER_LOG = HERMES_HOME / "logs" / "proxy_manager.log"
FORWARDER_LOG = HERMES_HOME / "logs" / "forwarder.log"
ENV_FILE = HERMES_HOME / ".env"
SOCKS_PORT = 9050
CONTROL_PORT = 9051


def log(msg):
    t = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    with open(MANAGER_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{t}] {msg}\n")
    print(f"[{t}] {msg}")


def send_telegram(msg):
    try:
        import requests
        state_path = HERMES_HOME / "gateway_state.json"
        if state_path.exists():
            with open(state_path) as f:
                d = json.load(f)
            pid = d.get("pid")
            if pid:
                requests.get(
                    f"http://127.0.0.1:19327/notify?text={urllib.parse.quote(msg)}&pid={pid}",
                    timeout=3,
                )
    except:
        pass


def write_state(**kw):
    state = {"current_proxy": None, "tor_exit_ip": None, "last_rotation_time": 0, "total_ip_switches": 0}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                state.update(json.load(f))
        except:
            pass
    state.update(kw)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def read_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}


def is_port_open(port, host="127.0.0.1", timeout=3):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except:
        return False


def ensure_torrc():
    tor_dir = HERMES_HOME / "tor"
    torrc_path = tor_dir / "torrc"
    torrc_path.parent.mkdir(parents=True, exist_ok=True)
    if not torrc_path.exists():
        user = os.environ.get("USER", os.environ.get("USERNAME", "user"))
        content = (
            f"SocksPort 127.0.0.1:{SOCKS_PORT}\n"
            f"ControlPort 127.0.0.1:{CONTROL_PORT}\n"
            f"CookieAuthentication 1\n"
            f"DataDirectory {tor_dir / 'Data'}\n"
            f"GeoIPFile /usr/share/tor/geoip\n"
            f"GeoIPv6File /usr/share/tor/geoip6\n"
            f"SafeLogging 1\n"
            f"Log notice file {tor_dir / 'tor.log'}\n"
            f"ExitNodes {{de}},{{fr}},{{us}},{{nl}},{{gb}}\n"
            f"StrictNodes 1\n"
        )
        with open(torrc_path, "w") as f:
            f.write(content)
        log("torrc created")


def kill_tor():
    try:
        subprocess.run(["pkill", "-f", "tor.*9050"], capture_output=True, timeout=5)
        subprocess.run(["pkill", "-f", "tor.*9051"], capture_output=True, timeout=5)
    except:
        pass


def start_tor():
    if is_port_open(SOCKS_PORT):
        log("Tor already running on port 9050")
        return True
    ensure_torrc()
    torrc = HERMES_HOME / "tor" / "torrc"
    log("Starting Tor...")
    try:
        subprocess.Popen(
            ["tor", "-f", str(torrc)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log("Tor process launched")
        return True
    except Exception as e:
        log(f"Failed to start Tor: {e}")
        return False


def wait_for_tor(timeout=120):
    log("Waiting for Tor bootstrap...")
    for i in range(timeout):
        if not is_port_open(SOCKS_PORT):
            time.sleep(1)
            continue
        try:
            import requests
            proxies = {"http": f"socks5h://127.0.0.1:{SOCKS_PORT}", "https": f"socks5h://127.0.0.1:{SOCKS_PORT}"}
            r = requests.get("https://api.ipify.org", proxies=proxies, timeout=5)
            ip = r.text.strip()
            log(f"Tor bootstrapped, exit IP: {ip}")
            return True
        except:
            time.sleep(2)
    log(f"Tor not ready after {timeout}s")
    return False


def get_tor_ip():
    try:
        import requests
        proxies = {"http": f"socks5h://127.0.0.1:{SOCKS_PORT}", "https": f"socks5h://127.0.0.1:{SOCKS_PORT}"}
        r = requests.get("https://api.ipify.org", proxies=proxies, timeout=10)
        return r.text.strip()
    except Exception as e:
        log(f"get_tor_ip failed: {e}")
        return None


def renew_tor_ip():
    log("Rotating Tor IP (NEWNYM)...")
    try:
        from stem import Signal
        from stem.control import Controller
        with Controller.from_port(port=CONTROL_PORT) as c:
            c.authenticate()
            c.signal(Signal.NEWNYM)
        log("NEWNYM sent, waiting 5s...")
        time.sleep(5)
        ip = get_tor_ip()
        if ip:
            log(f"New Tor IP: {ip}")
            write_state(tor_exit_ip=ip, last_rotation_time=time.time())
            return True
        log("Could not verify new IP")
        return False
    except ImportError:
        log("stem not available for IP rotation")
        return False
    except Exception as e:
        log(f"rotate Tor IP failed: {e}")
        return False


def kill_forwarder():
    try:
        subprocess.run(["pkill", "-f", "forwarder.py"], capture_output=True, timeout=5)
    except:
        pass


def is_forwarder_alive():
    try:
        r = subprocess.run(["pgrep", "-f", "forwarder.py"], capture_output=True, text=True, timeout=5)
        return len(r.stdout.strip()) > 0
    except:
        return False


def start_forwarder():
    kill_forwarder()
    time.sleep(1)
    log("Starting forwarder...")
    try:
        p = subprocess.Popen(
            [str(VENV_PY), str(FORWARDER_SCRIPT), "9000"],
            cwd=str(HERMES_HOME),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log(f"Forwarder started PID {p.pid}")
        time.sleep(3)
        if is_forwarder_alive():
            log("Forwarder health check passed")
            return True
        log("Forwarder health check failed")
        return False
    except Exception as e:
        log(f"Failed to start forwarder: {e}")
        return False


def is_hermes_alive():
    try:
        r = subprocess.run(["pgrep", "-f", "hermes.*gateway"], capture_output=True, text=True, timeout=5)
        return len(r.stdout.strip()) > 0
    except:
        return False


def start_hermes():
    log("Starting Hermes...")
    try:
        env = os.environ.copy()
        if ENV_FILE.exists():
            with open(ENV_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line and "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip()
        p = subprocess.Popen(
            [str(GATEWAY_SCRIPT), "gateway", "run"],
            cwd=str(HERMES_HOME),
            env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log(f"Hermes started PID {p.pid}")
        return True
    except Exception as e:
        log(f"Failed to start Hermes: {e}")
        return False


RATE_LIMIT_PATTERNS = [
    r"Error code: 429", r"status_code.?=?=?429", r"429 Too Many Requests",
    r"FreeUsageLimitError", r"error_code.*?429", r"HTTP 502",
    r"Error code: 502", r"Streaming failed before delivery", r"Proxy failed", r"Upstream timeout",
]


def scan_for_429(state):
    try:
        pos = state.get("log_position", 0)
        if not HERMES_LOG.exists():
            return []
        size = HERMES_LOG.stat().st_size
        if pos > size:
            pos = 0
        if pos == size:
            return []
        with open(HERMES_LOG, "r", encoding="utf-8", errors="replace") as f:
            f.seek(pos)
            lines = f.readlines()
            state["log_position"] = f.tell()
        save_state(state)
        hits = []
        for line in lines:
            for pat in RATE_LIMIT_PATTERNS:
                if re.search(pat, line, re.IGNORECASE):
                    hits.append(line.strip()[:120])
                    break
        return hits
    except Exception as e:
        log(f"scan error: {e}")
        return []


def main():
    log("=== Hermes Proxy Manager v7 (Linux) starting ===")

    kill_forwarder()

    tor_ok = False
    if is_port_open(SOCKS_PORT):
        log("Tor detected on port 9050")
        tor_ok = True
    else:
        kill_tor()
        if start_tor():
            if wait_for_tor(timeout=120):
                tor_ok = True

    if tor_ok:
        ip = get_tor_ip()
        write_state(
            current_proxy=f"socks5h://127.0.0.1:{SOCKS_PORT}",
            tor_exit_ip=ip,
            last_rotation_time=time.time(),
            total_ip_switches=read_state().get("total_ip_switches", 0),
        )
        log(f"Tor exit IP: {ip}")
        send_telegram(f"Hermes started via TOR (IP: {ip})")
    else:
        write_state(current_proxy=None, tor_exit_ip=None)
        log("Tor not available. Running WITHOUT proxy (direct).")
        send_telegram("Hermes started WITHOUT proxy (Tor not available)")

    if not is_forwarder_alive():
        start_forwarder()

    if not is_hermes_alive():
        start_hermes()

    log("Entering main monitoring loop")
    err_count = 0
    while True:
        try:
            time.sleep(10)
            state = read_state()
            hits = scan_for_429(state)
            if hits:
                for h in hits:
                    log(f"429/502: {h}")
                err_count += len(hits)

            if err_count >= 2:
                log(f"Triggering rotation ({err_count} errors)")
                if tor_ok:
                    if renew_tor_ip():
                        ip = get_tor_ip()
                        write_state(
                            current_proxy=f"socks5h://127.0.0.1:{SOCKS_PORT}",
                            tor_exit_ip=ip,
                            last_rotation_time=time.time(),
                            total_ip_switches=read_state().get("total_ip_switches", 0) + 1,
                        )
                        log(f"New Tor IP: {ip}")
                        send_telegram(f"TOR IP rotated → {ip}")
                        err_count = 0
                    else:
                        log("Tor rotation failed, will retry")
                        err_count = max(0, err_count - 1)
                else:
                    err_count = 0

            if not is_forwarder_alive():
                log("Forwarder died, restarting...")
                start_forwarder()

            if not is_hermes_alive():
                log("Hermes died, starting...")
                start_hermes()

            if tor_ok and not is_port_open(SOCKS_PORT):
                log("Tor died, restarting...")
                kill_tor()
                start_tor()
                if wait_for_tor(timeout=30):
                    tor_ok = True
                else:
                    tor_ok = False
                    write_state(current_proxy=None, tor_exit_ip=None)
                    log("Tor restart failed, running without proxy")
                    send_telegram("Tor restart failed, now running without proxy")
                    err_count = 0

        except KeyboardInterrupt:
            log("Shutting down...")
            break
        except Exception as e:
            log(f"Main loop error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()

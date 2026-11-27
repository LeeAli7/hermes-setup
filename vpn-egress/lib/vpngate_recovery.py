#!/usr/bin/env python3
"""Rotate VPNGate to a fresh server after a pool 429.

The supervisor checks vpngate_control/rotate.request every 20s, marks the
current server failed, and moves to the next cached config.  This watchdog
waits for the new tunnel, probes free Zen, and resets the pool source only on
a real 200.
"""
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get(
    "HERMES_EGRESS_HOME", os.path.expanduser("~/.hermes/ippool"))
SLOT = (os.environ.get("VPNGATE_SLOT", "0") or "0").strip()
SUFFIX = "" if SLOT == "0" else SLOT
SOURCE = "vpngate" if SLOT == "0" else f"vpngate{int(SLOT) + 1}"
RELAY_PORT = int(os.environ.get(
    "VPNGATE_RELAY_PORT_BASE", "9120")) + int(SLOT)
LOCK_FILE = f"/tmp/vpngate_recovery{SUFFIX}.lock"
STATE_FILE = os.path.join(BASE, f"vpngate_recovery_state{SUFFIX}.json")
LOG_FILE = os.path.join(BASE, f"vpngate_recovery{SUFFIX}.log")
CONTROL_DIR = os.path.join(BASE, f"vpngate_control{SUFFIX}")
ROTATE_FILE = os.path.join(CONTROL_DIR, "rotate.request")
LIVE_FILE = (os.path.join(BASE, "vpngate_live.json") if SLOT == "0"
             else os.path.join(BASE, f"vpngate_live_{SLOT}.json"))
POOL = os.environ.get("EGRESS_POOL_URL", "http://127.0.0.1:9200")
PROBE_PY = os.environ.get("HERMES_PYTHON", sys.executable)
PROBE = os.path.join(BASE, "probe_free_zen.py")
CONFIG = os.environ.get(
    "HERMES_CONFIG", os.path.expanduser("~/.hermes/config.yaml"))


def active_model():
    """Probe the model the gateway is actually using.

    The old hard-coded deepseek probe could mark a source healthy even when
    the active stealth model was region-gated or removed from the catalog.
    """
    try:
        import yaml

        with open(CONFIG, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        model = (cfg.get("model") or {}).get("default")
        if model:
            return str(model).strip()
    except Exception:
        pass
    return "crouton-alpha-free"
MIN_INTERVAL = 60.0
# VPNGate is the main fresh-IP source after the 2.2.5 quota change.  Try
# more servers before pausing; a 30-minute pause emptied the whole pool
# whenever five cached configs happened to be dead.
MAX_ATTEMPTS = 8
PAUSE_AFTER = 900.0


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + str(msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run(cmd, timeout=60):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 125, "", str(e))


def http_json(method, url, payload=None, timeout=5):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
        except Exception:
            body = {}
        return e.code, body
    except Exception as e:
        return 0, {"error": str(e)}


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2)
            f.write("\n")
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"state save failed: {e}")


def pool_source(name):
    code, body = http_json("GET", POOL + "/health", timeout=4)
    if code != 200:
        return None
    for s in body.get("sources") or []:
        if s.get("name") == name:
            return s
    return None


def set_state(name, state, seconds=None):
    payload = {"state": state}
    if seconds is not None:
        payload["seconds"] = seconds
    code, body = http_json("POST", f"{POOL}/source/{name}/state",
                           payload, timeout=5)
    if code != 200:
        log(f"pool state {name}={state} failed: {code} {str(body)[:160]}")
    return code == 200


def live_meta():
    try:
        with open(LIVE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return float(d.get("ts") or 0), str(d.get("host") or "")
    except Exception:
        return 0.0, ""


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
            return True
    except OSError:
        return False


def probe():
    """Validate a rotated tunnel without spending the model's free quota.

    Free models allow roughly one request per IP+model per day.  The old
    implementation probed with the *active* model, got 200, and thereby
    consumed the fresh IP's only request before the agent's real turn.
    A fresh vpngate_live.json timestamp means the tunnel connected and
    verified its exit IP inside the netns; that is enough for recovery.
    The actual agent request will be the first model call on this IP.
    """
    ts, host = live_meta()
    age = time.time() - ts if ts else 1e9
    if ts and age < 180 and port_open(RELAY_PORT):
        return {"ok": True, "status": 200, "skipped": "tunnel-only",
                "host": host, "age": round(age, 1)}
    return {"ok": False, "status": 0,
            "detail": f"tunnel not ready (age={age:.0f}s)"}


def main():
    try:
        lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    except Exception as e:
        log(f"lock failed: {e}")
        return 1
    try:
        st = load_state()
        now = time.time()
        if now < float(st.get("pause_until") or 0):
            return 0
        src = pool_source(SOURCE)
        if not src:
            return 0
        state = src.get("state")
        last_status = src.get("last_status")
        if state == "recovering" and float(src.get("cooldown_until") or 0) > now:
            return 0
        if state not in ("cooldown", "suspect", "recovering"):
            return 0
        if last_status not in (429, 0, 502, 503):
            return 0
        last = float(st.get("last_attempt") or 0)
        if now - last < MIN_INTERVAL:
            return 0
        attempts = int(st.get("attempts") or 0)
        if attempts >= MAX_ATTEMPTS:
            st["pause_until"] = now + PAUSE_AFTER
            st["attempts"] = 0
            save_state(st)
            log(f"pause {PAUSE_AFTER:.0f}s after {MAX_ATTEMPTS} attempts")
            set_state(SOURCE, "cooldown", PAUSE_AFTER)
            return 0
        st["last_attempt"] = now
        st["attempts"] = attempts + 1
        save_state(st)
        old_ts, old_host = live_meta()
        log(f"rotate attempt {attempts + 1}/{MAX_ATTEMPTS} "
            f"(pool state={state} last={last_status} old={old_host})")
        set_state(SOURCE, "recovering", 480)
        os.makedirs(CONTROL_DIR, exist_ok=True)
        with open(ROTATE_FILE, "w", encoding="utf-8") as f:
            f.write(str(now))
        # Wait for the supervisor to pick a different server and for the
        # netns proxy/relay to accept connections.
        deadline = time.time() + 150
        changed = False
        while time.time() < deadline:
            ts, host = live_meta()
            if ts > old_ts and host != old_host:
                changed = True
                break
            time.sleep(3)
        if not changed:
            log("no new VPNGate server within 150s")
            set_state(SOURCE, "cooldown", 300)
            return 0
        for _ in range(30):
            if port_open(RELAY_PORT):
                break
            time.sleep(2)
        res = probe()
        log(f"probe status={res.get('status')} ok={res.get('ok')} "
            f"host={live_meta()[1]}")
        if res.get("ok"):
            st["attempts"] = 0
            st["pause_until"] = 0
            save_state(st)
            set_state(SOURCE, "ok")
            log("VPNGate recovered")
            return 0
        ra = res.get("retry_after")
        try:
            secs = int(float(ra)) if ra not in (None, "") else 0
        except Exception:
            secs = 0
        set_state(SOURCE, "cooldown", max(90, min(secs, 300)))
        log("VPNGate still not usable; will try another server")
        return 0
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())

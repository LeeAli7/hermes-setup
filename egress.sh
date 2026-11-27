#!/usr/bin/env bash
# egress.sh — единое управление egress/IP для Hermes на этом хосте.
#
# Способы выхода, которые умеет переключать/крутить:
#   tor     — SOCKS 127.0.0.1:9050, NEWNYM + выбор страны через control port
#   warp    — Cloudflare WARP через warpctl (если установлен)
#   vpn     — VPNGate-пул vpn-egress (:9200, слоты :9120+)
#   direct  — прямой IP хоста (без ротации)
#   source  — прямой выход с привязкой к локальному адресу (multi-IP хост)
#
# Режим forwarder'а задаётся drop-in'ом
#   ~/.config/systemd/user/hermes-opencode-forwarder.service.d/zz-egress-mode.conf
# и применяется рестартом только opencode-forwarder'а (kilo остаётся на Tor).
#
# Примеры:
#   ./egress.sh status
#   ./egress.sh list
#   ./egress.sh rotate tor
#   ./egress.sh country DE
#   ./egress.sh country reset
#   ./egress.sh mode tor|direct|warp|vpn|auto
#   ./egress.sh mode source 10.0.0.25
#   ./egress.sh test
#
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
STATE_FILE="${EGRESS_STATE_FILE:-$BASE_DIR/proxy_state.json}"
TOR_SOCKS_PORT="${TOR_SOCKS_PORT:-9050}"
TOR_CONTROL_PORT="${TOR_CONTROL_PORT:-9051}"
TOR_COOKIE="${TOR_COOKIE:-$BASE_DIR/tor/Data/control_auth_cookie}"
FORWARDER_UNIT="${FORWARDER_UNIT:-hermes-opencode-forwarder.service}"
FORWARDER_URL="${FORWARDER_URL:-http://127.0.0.1:9000}"
DROPIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/${FORWARDER_UNIT}.d"
MODE_FILE="$DROPIN_DIR/zz-egress-mode.conf"
VPN_DROPIN="$DROPIN_DIR/egress-vpn.conf"
EGRESS_POOL_URL="${EGRESS_POOL_URL:-http://127.0.0.1:9200}"
VPN_RELAY_PORT_BASE="${VPNGATE_RELAY_PORT_BASE:-9120}"
VPNGATE_SLOTS="${VPNGATE_SLOTS:-2}"
WARPCTL="${WARPCTL:-/usr/local/bin/warpctl}"

info() { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

port_open() {
  local port="$1"
  if have ss; then
    ss -ltn 2>/dev/null | grep -q "127.0.0.1:${port} "
  else
    (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null
  fi
}

curl_ip() { # $1 = proxy url or empty for direct
  local proxy="${1:-}"
  if [ -n "$proxy" ]; then
    curl -fsS -m 25 -x "$proxy" https://api.ipify.org 2>/dev/null
  else
    curl -fsS -m 12 --noproxy '*' https://api.ipify.org 2>/dev/null
  fi
}

ip_direct() { curl_ip ""; }
ip_tor()    { curl_ip "socks5h://127.0.0.1:${TOR_SOCKS_PORT}"; }

tor_geo() {
  curl -fsS -m 25 -x "socks5h://127.0.0.1:${TOR_SOCKS_PORT}" \
    "http://ip-api.com/json/?fields=status,query,countryCode" 2>/dev/null
}

tor_ctl() { # tor_ctl "SIGNAL NEWNYM" ["SETCONF ..." ...]
  python3 - "$TOR_CONTROL_PORT" "$TOR_COOKIE" "$@" <<'PY'
import binascii, socket, sys

port, cookie_path = int(sys.argv[1]), sys.argv[2]
commands = sys.argv[3:]

try:
    cookie = binascii.hexlify(open(cookie_path, "rb").read()).decode()
except OSError as e:
    print(f"cookie: {e}", file=sys.stderr)
    sys.exit(2)

s = socket.create_connection(("127.0.0.1", port), 10)
s.settimeout(15)


def read_reply():
    buf = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            return None, buf
        buf += chunk
        for line in buf.split(b"\r\n"):
            if len(line) >= 4 and line[:3].isdigit() and line[3:4] == b" ":
                return int(line[:3]), buf
    return None, buf


s.sendall(b"AUTHENTICATE " + cookie.encode() + b"\r\n")
code, raw = read_reply()
if code != 250:
    print("auth failed: %r" % raw, file=sys.stderr)
    sys.exit(3)

for cmd in commands:
    s.sendall(cmd.encode() + b"\r\n")
    code, raw = read_reply()
    if code != 250:
        print("command failed (%s): %r" % (cmd, raw), file=sys.stderr)
        sys.exit(4)
s.close()
PY
}

update_state() { # update_state <new_exit_ip>
  python3 - "$STATE_FILE" "$1" "$TOR_SOCKS_PORT" <<'PY'
import json, os, sys, time

path, ip, port = sys.argv[1], sys.argv[2], sys.argv[3]
state = {}
try:
    with open(path) as f:
        state = json.load(f)
except Exception:
    pass
state.update({
    "current_proxy": f"socks5h://127.0.0.1:{port}",
    "tor_exit_ip": ip,
    "last_rotation_time": time.time(),
    "total_ip_switches": int(state.get("total_ip_switches", 0)) + 1,
})
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(state, f)
os.replace(tmp, path)
PY
}

wait_tor() { # wait until SOCKS answers and exit IP is known
  local i ip
  for i in $(seq 1 40); do
    if port_open "$TOR_SOCKS_PORT"; then
      ip="$(ip_tor)" && [ -n "$ip" ] && { printf '%s' "$ip"; return 0; }
    fi
    sleep 1
  done
  return 1
}

forwarder_health() {
  curl -fsS -m 3 --noproxy '*' "${FORWARDER_URL}/health" 2>/dev/null
}

restart_forwarder() {
  systemctl --user daemon-reload || return 1
  systemctl --user restart "$FORWARDER_UNIT" || return 1
  local i
  for i in $(seq 1 30); do
    [ "$(forwarder_health)" = "OK" ] && return 0
    sleep 0.5
  done
  return 1
}

warpctl_bin() {
  if have warpctl; then command -v warpctl; return 0; fi
  [ -x "$WARPCTL" ] && { printf '%s' "$WARPCTL"; return 0; }
  return 1
}

vpn_cli() { have hermes-egress && command -v hermes-egress; }

vpn_pool_health() { curl -fsS -m 4 --noproxy '*' "${EGRESS_POOL_URL}/health" 2>/dev/null; }

vpn_slots() {
  local h
  h="$(vpn_pool_health)" || return 1
  printf '%s' "$h" | python3 -c '
import json, sys, time
d = json.load(sys.stdin)
now = time.time()
for s in d.get("sources", []):
    cd = max(0, int(float(s.get("cooldown_until") or 0) - now))
    print("  {:<10} {:<10} last={} cd={}s relay={}".format(
        s.get("name", "?"), s.get("state", "?"), s.get("last_status"), cd,
        s.get("relay", "?")))
'
}

effective_mode() {
  if [ -f "$MODE_FILE" ]; then
    if grep -q 'FORWARDER_EGRESS_POOL=http' "$MODE_FILE"; then
      echo vpn
    elif grep -qE 'FORWARDER_SOURCE_ADDR=[0-9a-fA-F]' "$MODE_FILE"; then
      echo source
    elif grep -q 'FORWARDER_DISABLE_TOR=1' "$MODE_FILE"; then
      if grep -q 'FORWARDER_WARP_ROTATE=1' "$MODE_FILE"; then
        echo warp
      else
        echo direct
      fi
    else
      echo tor
    fi
  elif [ -f "$VPN_DROPIN" ]; then
    echo vpn
  else
    echo auto
  fi
}

write_mode() { # write_mode <mode> [source_addr]
  local mode="$1" source_addr="${2:-}"
  mkdir -p "$DROPIN_DIR"
  case "$mode" in
    tor)
      cat > "$MODE_FILE" <<'EOF'
# Managed by egress.sh — режим: только Tor (без direct fallback).
[Service]
Environment="FORWARDER_DISABLE_TOR=0"
Environment="FORWARDER_DIRECT_FALLBACK=0"
Environment="FORWARDER_WARP_ROTATE=0"
Environment="FORWARDER_EGRESS_POOL="
Environment="FORWARDER_UPSTREAM_RELAY="
Environment="FORWARDER_SOURCE_ADDR="
EOF
      ;;
    direct)
      cat > "$MODE_FILE" <<'EOF'
# Managed by egress.sh — режим: прямой IP хоста.
[Service]
Environment="FORWARDER_DISABLE_TOR=1"
Environment="FORWARDER_DIRECT_FALLBACK=1"
Environment="FORWARDER_WARP_ROTATE=0"
Environment="FORWARDER_EGRESS_POOL="
Environment="FORWARDER_UPSTREAM_RELAY="
Environment="FORWARDER_SOURCE_ADDR="
EOF
      ;;
    warp)
      cat > "$MODE_FILE" <<'EOF'
# Managed by egress.sh — режим: direct-полоса через Cloudflare WARP.
[Service]
Environment="FORWARDER_DISABLE_TOR=1"
Environment="FORWARDER_DIRECT_FALLBACK=1"
Environment="FORWARDER_WARP_ROTATE=1"
Environment="FORWARDER_EGRESS_POOL="
Environment="FORWARDER_UPSTREAM_RELAY="
Environment="FORWARDER_SOURCE_ADDR="
EOF
      ;;
    vpn)
      cat > "$MODE_FILE" <<'EOF'
# Managed by egress.sh — режим: VPNGate-пул (vpn-egress, см. TZ.md).
[Service]
Environment="FORWARDER_DISABLE_TOR=1"
Environment="FORWARDER_DIRECT_FALLBACK=0"
Environment="FORWARDER_WARP_ROTATE=0"
Environment="FORWARDER_EGRESS_POOL=http://127.0.0.1:9200"
Environment="FORWARDER_UPSTREAM_RELAY=http://127.0.0.1:9120"
Environment="FORWARDER_SOURCE_ADDR="
Environment="FORWARDER_429_ATTEMPTS=3"
Environment="FORWARDER_WALL_FAST_ATTEMPTS=2"
Environment="FORWARDER_429_BACKOFF=1"
Environment="FORWARDER_429_MAX_BACKOFF=2"
Environment="FORWARDER_RETRY_WINDOW=180"
Environment="FORWARDER_LARGE_BODY=204800"
Environment="FORWARDER_LARGE_BODY_TIMEOUT=60"
Environment="FORWARDER_FREE_ZEN_SESSION_SOURCE=server"
EOF
      ;;
    source)
      [ -n "$source_addr" ] || die "mode source требует локальный адрес: egress.sh mode source 10.0.0.25"
      cat > "$MODE_FILE" <<EOF
# Managed by egress.sh — режим: direct с source-addr ${source_addr}.
[Service]
Environment="FORWARDER_DISABLE_TOR=1"
Environment="FORWARDER_DIRECT_FALLBACK=1"
Environment="FORWARDER_WARP_ROTATE=0"
Environment="FORWARDER_EGRESS_POOL="
Environment="FORWARDER_UPSTREAM_RELAY="
Environment="FORWARDER_SOURCE_ADDR=${source_addr}"
EOF
      ;;
    *) die "неизвестный режим: $mode" ;;
  esac
  restart_forwarder || { warn "forwarder не поднялся после смены режима"; return 1; }
}

# ---------------------------------------------------------------- commands ---

cmd_status() {
  local mode health direct tor_ip geo warp vpn
  mode="$(effective_mode)"
  health="$(forwarder_health)"
  info "== forwarder =="
  info "  unit:   ${FORWARDER_UNIT}"
  info "  health: ${health:-FAIL}"
  info "  mode:   ${mode}"
  if [ "$mode" = source ]; then
    info "  source: $(grep -oE 'FORWARDER_SOURCE_ADDR=.+' "$MODE_FILE" | cut -d= -f2 | tr -d '"')"
  fi
  if [ -f "$VPN_DROPIN" ] && [ "$mode" != vpn ]; then
    info "  note:   найден drop-in vpn-egress ($VPN_DROPIN)"
  fi

  info "== direct =="
  direct="$(ip_direct)"
  info "  ip: ${direct:-n/a}"

  info "== tor =="
  if port_open "$TOR_SOCKS_PORT"; then
    tor_ip="$(ip_tor)"
    geo="$(tor_geo)"
    info "  socks: 127.0.0.1:${TOR_SOCKS_PORT} up"
    info "  exit:  ${tor_ip:-n/a}${geo:+ $(printf '%s' "$geo" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("("+str(d.get("countryCode","?"))+")")' 2>/dev/null)}"
  else
    info "  socks: DOWN"
  fi

  info "== warp =="
  warp="$(warpctl_bin)" || true
  if [ -n "${warp:-}" ]; then
    info "  bin:   $warp"
    "$warp" status 2>&1 | sed 's/^/  /' | head -5
  else
    info "  not installed (warpctl)"
  fi

  info "== vpn (VPNGate pool) =="
  if vpn="$(vpn_pool_health)"; then
    info "  pool:  ${EGRESS_POOL_URL} up"
    vpn_slots || true
    local i
    for ((i = 0; i < VPNGATE_SLOTS; i++)); do
      local port=$((VPN_RELAY_PORT_BASE + i))
      if port_open "$port"; then info "  relay ${i}: :${port} up"; else info "  relay ${i}: :${port} DOWN"; fi
    done
  else
    info "  pool:  DOWN/not installed"
  fi
}

cmd_list() {
  local warp vpn
  warp="$(warpctl_bin)" || true
  vpn="$(vpn_cli)" || true
  info "Доступные способы egress на этом хосте:"
  if port_open "$TOR_SOCKS_PORT"; then
    info "  tor      ДА    rotate: ./egress.sh rotate tor    страна: ./egress.sh country DE"
  else
    info "  tor      НЕТ   (порт ${TOR_SOCKS_PORT} не слушает; systemctl --user start hermes-tor)"
  fi
  if [ -n "${warp:-}" ]; then
    info "  warp     ДА    rotate: ./egress.sh rotate warp   режим: ./egress.sh mode warp"
  else
    info "  warp     НЕТ   (нет warpctl; см. warpctl в репозитории)"
  fi
  if [ -n "${vpn:-}" ] || vpn_pool_health >/dev/null; then
    info "  vpn      ДА    rotate: ./egress.sh rotate vpn [slot]   режим: ./egress.sh mode vpn"
  else
    info "  vpn      НЕТ   (cd vpn-egress && ./install.sh --yes)"
  fi
  info "  direct   ДА    ротации нет; режим: ./egress.sh mode direct"
  info "  source   ДА    ./egress.sh mode source <local-ip>"
  info ""
  info "Текущий режим: $(effective_mode)"
}

cmd_rotate() {
  local what="${1:-auto}"
  [ "$what" = auto ] && what="$(effective_mode)"
  case "$what" in
    tor|auto)
      local before after i
      port_open "$TOR_SOCKS_PORT" || die "Tor не слушает 127.0.0.1:${TOR_SOCKS_PORT}"
      [ -r "$TOR_COOKIE" ] || die "нет cookie Tor: $TOR_COOKIE"
      before="$(ip_tor)"
      info "Tor exit before: ${before:-n/a}"
      tor_ctl "SIGNAL NEWNYM" || die "не удалось отправить SIGNAL NEWNYM"
      after=""
      for i in $(seq 1 30); do
        sleep 1
        after="$(ip_tor)"
        [ -n "$after" ] && [ "$after" != "$before" ] && break
      done
      if [ -z "$after" ] || [ "$after" = "$before" ]; then
        warn "Tor exit не сменился за 30с (было ${before:-?}, стало ${after:-?})"
        return 1
      fi
      update_state "$after" || warn "не удалось обновить $STATE_FILE"
      info "Tor exit after:  $after"
      ;;
    warp)
      local w
      w="$(warpctl_bin)" || die "warpctl не найден"
      info "WARP rotate через $w ..."
      if [ "$(id -u)" -eq 0 ]; then
        "$w" rotate --reason manual
      else
        sudo -n "$w" rotate --reason manual || die "нужен sudo без пароля для warpctl (или запусти от root)"
      fi
      "$w" status 2>&1 | head -3
      ;;
    vpn)
      local slot="${2:-0}" cli
      cli="$(vpn_cli)" || die "hermes-egress не найден (cd vpn-egress && ./install.sh --yes)"
      info "VPNGate rotate slot ${slot} ..."
      "$cli" rotate "$slot"
      info "ротация запущена; проверь: ./egress.sh status"
      ;;
    direct)
      warn "direct нельзя ротировать: это IP хоста. Используй tor/warp/vpn или mode source."
      return 1
      ;;
    source)
      warn "source-адрес меняется на уровне сети/провайдера; скрипт только привязывает forwarder к нему."
      return 1
      ;;
    all)
      local rc=0
      cmd_rotate tor || rc=1
      if warpctl_bin >/dev/null; then cmd_rotate warp || rc=1; else info "warp: пропуск (нет warpctl)"; fi
      if vpn_cli >/dev/null; then
        local s
        for ((s = 0; s < VPNGATE_SLOTS; s++)); do cmd_rotate vpn "$s" || rc=1; done
      else
        info "vpn: пропуск (не установлен)"
      fi
      return "$rc"
      ;;
    *) die "usage: egress.sh rotate [tor|warp|vpn [slot]|all]" ;;
  esac
}

cmd_country() {
  local cc="${1:-}"
  [ -n "$cc" ] || die "usage: egress.sh country <CC|reset>"
  port_open "$TOR_SOCKS_PORT" || die "Tor не слушает 127.0.0.1:${TOR_SOCKS_PORT}"
  if [ "$cc" = reset ]; then
    tor_ctl "RESETCONF ExitNodes" "RESETCONF StrictNodes" "SIGNAL NEWNYM" \
      || die "не удалось сбросить ExitNodes"
    info "Tor: ExitNodes сброшены к torrc"
  else
    cc="$(printf '%s' "$cc" | tr '[:lower:]' '[:upper:]')"
    [[ "$cc" =~ ^[A-Z]{2}$ ]] || die "страна должна быть двухбуквенным кодом, например DE"
    tor_ctl "SETCONF ExitNodes={$cc}" "SETCONF StrictNodes=1" "SIGNAL NEWNYM" \
      || die "не удалось выставить ExitNodes {$cc}"
    info "Tor: ExitNodes={$cc}, StrictNodes=1"
  fi
  local ip i
  for i in $(seq 1 45); do
    sleep 1
    ip="$(ip_tor)"
    [ -n "$ip" ] && break
  done
  [ -n "$ip" ] || { warn "Tor не отдал exit IP за 45с"; return 1; }
  update_state "$ip" || true
  info "Tor exit: $ip"
}

cmd_mode() {
  local mode="${1:-}" addr="${2:-}"
  [ -n "$mode" ] || die "usage: egress.sh mode tor|direct|warp|vpn|source <ip>|auto"
  if [ "$mode" = source ]; then
    [ -n "$addr" ] || die "usage: egress.sh mode source <local-ip>"
    [[ "$addr" =~ ^[0-9a-fA-F:.]+$ ]] || die "некорректный локальный адрес: $addr"
  fi
  case "$mode" in
    auto)
      rm -f "$MODE_FILE"
      restart_forwarder || warn "forwarder не поднялся"
      info "режим auto (drop-in удалён); effective=$(effective_mode)"
      ;;
    tor|direct|warp|vpn|source)
      if [ "$mode" = warp ] && ! warpctl_bin >/dev/null; then
        die "warpctl не найден; режим warp недоступен"
      fi
      if [ "$mode" = vpn ] && ! vpn_pool_health >/dev/null; then
        warn "пул ${EGRESS_POOL_URL} не отвечает; forwarder будет ждать VPNGate"
      fi
      write_mode "$mode" "$addr" || exit 1
      info "режим ${mode} применён; effective=$(effective_mode)"
      cmd_status
      ;;
    *) die "неизвестный режим: $mode" ;;
  esac
}

cmd_test() {
  local model body code out
  out="${TMPDIR:-/tmp}/hermes-egress-test.json"
  model="$(awk '{print $2}' "$HOME/.config/hermes-switch.state" 2>/dev/null)"
  model="${model:-deepseek-v4.1-flash-free}"
  body="$(printf '{"model":"%s","messages":[{"role":"user","content":"Reply with OK"}],"max_tokens":5,"stream":false}' "$model")"
  info "chat probe: model=$model via ${FORWARDER_URL}/zen/v1/chat/completions"
  code="$(curl -sS -m 90 --noproxy '*' -o "$out" -w '%{http_code}' \
    -X POST "${FORWARDER_URL}/zen/v1/chat/completions" \
    -H 'Content-Type: application/json' -H 'Authorization: Bearer public' \
    -d "$body" 2>/dev/null)"
  info "HTTP ${code:-000}"
  head -c 400 "$out" 2>/dev/null; echo
  [ "$code" = 200 ]
}

cmd_doctor() {
  local rc=0
  info "== egress doctor =="
  for c in curl python3 systemctl; do
    if have "$c"; then info "  OK   $c"; else info "  MISS $c"; rc=1; fi
  done
  if port_open "$TOR_SOCKS_PORT"; then info "  OK   tor socks :${TOR_SOCKS_PORT}"; else info "  MISS tor socks :${TOR_SOCKS_PORT}"; rc=1; fi
  if [ -r "$TOR_COOKIE" ]; then
    if tor_ctl "GETINFO version" >/dev/null 2>&1; then info "  OK   tor control :${TOR_CONTROL_PORT}"; else info "  MISS tor control auth"; rc=1; fi
  else
    info "  MISS tor cookie $TOR_COOKIE"; rc=1
  fi
  if [ "$(forwarder_health)" = "OK" ]; then info "  OK   forwarder health"; else info "  MISS forwarder health"; rc=1; fi
  if warpctl_bin >/dev/null; then
    info "  OK   warpctl $(warpctl_bin)"
    if [ "$(id -u)" -eq 0 ] || sudo -n true 2>/dev/null; then info "  OK   sudo -n (warpctl rotate)"; else info "  WARN sudo без пароля недоступен"; fi
  else
    info "  --   warpctl не установлен"
  fi
  if vpn_cli >/dev/null; then info "  OK   hermes-egress"; else info "  --   vpn-egress не установлен"; fi
  if vpn_pool_health >/dev/null; then info "  OK   vpn pool ${EGRESS_POOL_URL}"; else info "  --   vpn pool не отвечает"; fi
  info "  mode $(effective_mode)"
  return "$rc"
}

usage() {
  cat <<'EOF'
egress.sh — управление egress/IP для Hermes

  status                     показать режим, IP и состояние всех выходов
  list                       какие способы доступны на этом хосте
  rotate [what]              what: tor|warp|vpn [slot]|all (по умолчанию активный)
  country <CC|reset>         Tor exit-страна (например DE), reset — вернуть torrc
  mode <mode> [addr]         tor|direct|warp|vpn|source <local-ip>|auto
  test                       chat-проба через forwarder (тратит free-квоту)
  doctor                     проверка зависимостей и портов

Файлы:
  drop-in режима: ~/.config/systemd/user/hermes-opencode-forwarder.service.d/zz-egress-mode.conf
  VPNGate-пул:    vpn-egress/ (см. vpn-egress/TZ.md)

Примеры:
  ./egress.sh rotate tor
  ./egress.sh country NL
  ./egress.sh mode warp
  ./egress.sh mode vpn
  ./egress.sh mode source 10.0.0.25
EOF
}

case "${1:-}" in
  status)  shift; cmd_status "$@" ;;
  list)    shift; cmd_list "$@" ;;
  rotate)  shift; cmd_rotate "$@" ;;
  country) shift; cmd_country "$@" ;;
  mode)    shift; cmd_mode "$@" ;;
  test)    shift; cmd_test "$@" ;;
  doctor)  shift; cmd_doctor "$@" ;;
  ""|-h|--help|help) usage ;;
  *) usage; exit 2 ;;
esac

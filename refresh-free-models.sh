#!/bin/sh
# Обновление списков free-моделей для switch.sh.
# Источники: форвардеры через Tor (могут отвечать 30-60 c).
# При ошибке сети старый файл НЕ трогается, выход ненулевой.
set -u

DIR="$(dirname "$0")/free-models"
OPENCODE_URL="http://127.0.0.1:9000/zen/v1/models"
KILO_URL="http://127.0.0.1:9001/api/openrouter/models"
TIMEOUT=90
FAIL=0

mkdir -p "$DIR"
log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >&2; }

port_open() { ss -tln 2>/dev/null | grep -q ":$1 "; }

# kilo спит по sleep-политике (switch.sh укладывает неактивных).
# Будим только на время опроса, потом укладываем обратно.
KILO_STARTED_BY_US=0
ensure_kilo() {
  if port_open 9001; then return 0; fi
  if ! systemctl --user cat hermes-kilo-forwarder.service >/dev/null 2>&1; then
    log "kilo: нет юнита и порт молчит — пропускаю"
    return 1
  fi
  log "kilo спит — бужу на время опроса..."
  systemctl --user start hermes-kilo-forwarder.service >/dev/null 2>&1 || return 1
  KILO_STARTED_BY_US=1
  i=0
  while [ "$i" -lt 30 ]; do
    port_open 9001 && { log "kilo проснулся"; return 0; }
    sleep 2; i=$((i+1))
  done
  log "kilo не проснулся за 60с — пропускаю"
  return 1
}
release_kilo() {
  if [ "$KILO_STARTED_BY_US" = "1" ]; then
    systemctl --user stop hermes-kilo-forwarder.service >/dev/null 2>&1 || true
    log "kilo уложен обратно спать"
  fi
}

# stdin JSON (OpenRouter-формат: {"data":[{"id":...}]},
# допускаются {"models":[...]}, голый [...]) -> id с "free" на stdout.
filter_free() {
  python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print(f"JSON parse error: {e}", file=sys.stderr)
    sys.exit(1)
if isinstance(d, dict):
    items = d.get("data", d.get("models", []))
elif isinstance(d, list):
    items = d
else:
    items = []
n = 0
for m in items:
    i = m.get("id", "") if isinstance(m, dict) else str(m)
    if "free" in i.lower():
        print(i)
        n += 1
if n == 0:
    print("no free models in response", file=sys.stderr)
    sys.exit(1)
'
}

fetch_one() {
  name="$1"; url="$2"; dest="$3"
  tmp="$dest.tmp.$$"
  log "fetch $name <- $url"
  if curl -sf --max-time "$TIMEOUT" "$url" 2>"$tmp.curlerr" | filter_free > "$tmp" 2>"$tmp.filtererr"; then
    mv -f "$tmp" "$dest"
    rm -f "$tmp.curlerr" "$tmp.filtererr"
    log "OK $name: $(wc -l < "$dest") моделей -> $dest"
  else
    log "FAIL $name: оставляю старый $([ -f "$dest" ] && wc -l < "$dest" || echo нет) список"
    [ -s "$tmp.curlerr" ] && head -c 500 "$tmp.curlerr" >&2
    [ -s "$tmp.filtererr" ] && head -c 500 "$tmp.filtererr" >&2
    rm -f "$tmp" "$tmp.curlerr" "$tmp.filtererr"
    FAIL=1
  fi
}

fetch_one opencode "$OPENCODE_URL" "$DIR/opencode.txt"
if ensure_kilo; then
  fetch_one kilo "$KILO_URL" "$DIR/kilo.txt"
  release_kilo
else
  FAIL=1
fi

[ "$FAIL" -eq 0 ] && log "done: оба списка обновлены" || log "done: есть ошибки, exit $FAIL"
exit "$FAIL"

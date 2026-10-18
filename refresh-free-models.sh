#!/bin/sh
# Обновление списков free-моделей для switch.sh.
# Использование: refresh-free-models.sh [opencode|kilo]   (без аргумента — оба)
# Источники: локальные форвардеры (opencode 9000, kilo 9001 — через Tor).
# При ошибке сети старый файл НЕ трогается, выход ненулевой.
set -u

DIR="$(dirname "$0")/free-models"
OPENCODE_URL="http://127.0.0.1:9000/zen/v1/models"
KILO_URL="http://127.0.0.1:9001/api/openrouter/models"
TIMEOUT=90
FAIL=0

mkdir -p "$DIR"
log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >&2; }

# Фильтр по провайдеру (18.10.2026): switch.sh обновляет только нужный
# список — не дёргает kilo, если нужен только opencode, и наоборот.
WANT_OPENCODE=1
WANT_KILO=1
case "${1:-all}" in
  all) ;;
  opencode) WANT_KILO=0 ;;
  kilo)     WANT_OPENCODE=0 ;;
  *) log "неизвестный аргумент '$1' (use: opencode|kilo|пусто)"; exit 2 ;;
esac

port_open() { ss -tln 2>/dev/null | grep -q ":$1 "; }

# kilo — always-on с 2026-10-11 (Anthropic-мост + OpenRouter на 9001).
# Будим только если упал (рестарт юнита сам поднимет порт), обратно
# спать НЕ укладываем.
KILO_STARTED_BY_US=0
ensure_kilo() {
  if port_open 9001; then return 0; fi
  if ! systemctl --user cat hermes-kilo-forwarder.service >/dev/null 2>&1; then
    log "kilo: нет юнита и порт молчит — пропускаю"
    return 1
  fi
  log "kilo always-on, но порт молчит — бужу..."
  systemctl --user start hermes-kilo-forwarder.service >/dev/null 2>&1 || return 1
  i=0
  while [ "$i" -lt 30 ]; do
    port_open 9001 && { log "kilo поднялся"; return 0; }
    sleep 2; i=$((i+1))
  done
  log "kilo не поднялся за 60с — пропускаю"
  return 1
}
release_kilo() {
  : # always-on: спать не укладываем
}

# stdin JSON (OpenRouter-формат: {"data":[{"id":...}]},
# допускаются {"models":[...]}, голый [...]) -> id с "free" на stdout.
filter_free() {
  python3 -c '
import json, sys
# Мёртвые/неподдерживаемые модели: upstream-каталог их перечисляет, но
# инференс отдаёт 400 ModelProtocolUnsupported на обоих протоколах
# (jev проверен 2026-10-17). Не пускаем их в списки switch.sh.
DEAD_MODELS = {"jev-1.13-free"}
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
    if "free" in i.lower() and i not in DEAD_MODELS:
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
  if curl -sf --max-time "$TIMEOUT" "$url" 2>"$tmp.curlerr" | python3 -c '
# 2026-10-17: gzip -dc || cat ate the first ~256KB of a NON-gzip body before
# failing, so identity JSON arrived truncated mid-stream. Decode only when
# the body actually is gzip; otherwise pass the bytes through untouched.
import sys, gzip
b = sys.stdin.buffer.read()
if b[:2] == b"\x1f\x8b":
    b = gzip.decompress(b)
sys.stdout.buffer.write(b)
' | filter_free > "$tmp" 2>"$tmp.filtererr"; then
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

if [ "$WANT_OPENCODE" = "1" ]; then
  fetch_one opencode "$OPENCODE_URL" "$DIR/opencode.txt"
else
  log "opencode: пропускаю (не запрошен)"
fi
if [ "$WANT_KILO" = "1" ]; then
  if ensure_kilo; then
    fetch_one kilo "$KILO_URL" "$DIR/kilo.txt"
    release_kilo
  else
    FAIL=1
  fi
else
  log "kilo: пропускаю (не запрошен)"
fi

if [ "$FAIL" -eq 0 ]; then log "done: списки обновлены"; else log "done: есть ошибки, exit $FAIL"; fi
exit "$FAIL"

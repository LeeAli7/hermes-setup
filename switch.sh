#!/bin/sh
set -e

HERMES="/home/ubuntu/.hermes/hermes-agent/venv/bin/hermes"
STATE="$HOME/.config/hermes-switch.state"

# --- Live free models (06.09.2026, проверено через Tor) ---
# opencode (https://opencode.ai/zen/v1/models): ВСЕ 7 с приставкой free.
#   200 OK: ling-3.0-flash-fin-free, mimo-v2.5-free, nemotron-3-ultra-free, nemotron-3.5-lightning-free.
#   Нестабильные (forwarder ретраит с ротацией IP): deepseek-v4-flash-free (400 Model is unavailable),
#   muse-spark-1.2/1.3-contributor-free (500 Internal server error).
opencode_models="deepseek-v4-flash-free ling-3.0-flash-fin-free mimo-v2.5-free muse-spark-1.2-contributor-free muse-spark-1.3-contributor-free nemotron-3-ultra-free nemotron-3.5-lightning-free"
# kilo (https://api.kilo.ai/api/openrouter/models): ВСЕ 19 с free в id (200 OK, проверено).
#   minimax-m3/inkling/inkling-small — живые, но 429-лимит (forwarder ротирует IP).
kilo_models="kilo-auto/free openrouter/free stepfun/step-3.7-flash:free poolside/laguna-s-2.1:free minimax/minimax-m3:free inclusionai/ling-3.0-flash-sante:free inclusionai/ling-3.0-flash-fin:free dots-studio/dots-3-note-preview:free liquid/lfm-2.5-2.6b:free nvidia/nemotron-3.5-lightning:free thinkingmachines/inkling-small:free thinkingmachines/inkling:free poolside/laguna-xs-2.1:free cohere/north-mini-code:free nvidia/nemotron-3.5-content-safety:free nvidia/nemotron-3-ultra-550b-a55b:free nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free nvidia/nemotron-3-super-120b-a12b:free minimax/minimax-m2.7:free"
kiro_models="claude-sonnet-4 claude-sonnet-4.5 claude-haiku-4.5 qwen3-coder-next deepseek-3.2 minimax-m2.5 minimax-m2.1 glm-5"
qwenmode_models="Qwen3.8-Max"
chatgpt_models="gpt-5-6"

# --- provider -> systemd unit / port / health URL ---
# Все провайдеры спят, кроме выбранного (RAM!): switch = wake target + stop others.
ALL_UNITS="qwenmode.service chatgpt-guest.service hermes-opencode-forwarder.service hermes-kilo-forwarder.service hermes-kiro-gateway.service"

unit_for() {
  case "$1" in
    opencode) echo "hermes-opencode-forwarder.service" ;;
    kilo)     echo "hermes-kilo-forwarder.service" ;;
    kiro)     echo "hermes-kiro-gateway.service" ;;
    qwenmode) echo "qwenmode.service" ;;
    chatgpt)  echo "chatgpt-guest.service" ;;
    *) return 1 ;;
  esac
}

port_for() {
  case "$1" in
    opencode) echo 9000 ;;
    kilo)     echo 9001 ;;
    kiro)     echo 8080 ;;
    qwenmode) echo 5002 ;;
    chatgpt)  echo 5003 ;;
  esac
}

health_for() {
  # явный health-endpoint есть не у всех; пусто = обычный порт-проб
  case "$1" in
    chatgpt) echo "http://127.0.0.1:5003/health" ;;
    *)       echo "" ;;
  esac
}

usage() {
  cat >&2 <<'USAGE'
Usage:
  switch.sh [provider] [model]
  switch.sh --help | -h
  switch.sh --list | -l [provider]

Providers:
  opencode  — opencode.ai через Tor (http://127.0.0.1:9000/zen/v1)
  kilo      — api.kilo.ai через Tor (http://127.0.0.1:9001/api/openrouter)
  kiro      — Amazon Q Developer (http://127.0.0.1:8080/v1)
  qwenmode  — chat.qwen.ai через Playwright (http://127.0.0.1:5002/v1)
  chatgpt   — chatgpt.com guest mode через Playwright (http://127.0.0.1:5003/v1)

Переключение будит юнит целевого провайдера и укладывает спать остальных
(экономия RAM). Выбор запоминается и восстанавливается после ребута
(provider-restore.service).

Examples:
  switch.sh chatgpt gpt-5-6
  switch.sh opencode mimo-v2.5-free
  switch.sh opencode              # интерактивное меню модели
  switch.sh                       # меню провайдера + модели
  switch.sh --list opencode
USAGE
}

list_models() {
  p="$1"
  case "$p" in
    opencode) echo "$opencode_models" | tr ' ' '\n' ;;
    kilo) echo "$kilo_models" | tr ' ' '\n' ;;
    kiro) echo "$kiro_models" | tr ' ' '\n' ;;
    qwenmode) echo "$qwenmode_models" | tr ' ' '\n' ;;
    chatgpt) echo "$chatgpt_models" | tr ' ' '\n' ;;
    *) echo "Unknown provider: $p" >&2; exit 1 ;;
  esac
}

is_valid_model() {
  provider="$1"
  model="$2"
  case "$provider" in
    opencode) list="$opencode_models" ;;
    kilo) list="$kilo_models" ;;
    kiro) list="$kiro_models" ;;
    qwenmode) list="$qwenmode_models" ;;
    chatgpt) list="$chatgpt_models" ;;
    *) return 1 ;;
  esac
  for m in $list; do
    if [ "$m" = "$model" ]; then
      return 0
    fi
  done
  return 1
}

# --- preflight checks ---
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] || [ "${1:-}" = "help" ]; then
  usage
  exit 0
fi

if [ "${1:-}" = "-l" ] || [ "${1:-}" = "--list" ] || [ "${1:-}" = "list" ]; then
  if [ $# -eq 2 ]; then
    list_models "$2"
    exit 0
  fi
  for p in opencode kilo kiro qwenmode chatgpt; do
    echo "--- $p ---" >&2
    list_models "$p" >&2
  done
  exit 0
fi

if [ ! -x "$HERMES" ]; then
  echo "Error: HERMES not found or not executable at $HERMES" >&2
  echo "Checked: $HERMES" >&2
  exit 1
fi

menu() {
  title="$1"
  items="$2"
  printf "  %s\n" "$title" >&2
  i=1
  for m in $items; do
    printf "  %d. %s\n" "$i" "$m" >&2
    i=$((i+1))
  done
  printf "  > " >&2
  if ! read -r choice; then
    echo "" >&2
    echo "No input (EOF)" >&2
    exit 1
  fi
  choice=$(printf "%s" "$choice" | tr -d '[:space:]')
  case "$choice" in
    ''|*[!0-9]*)
      echo "" >&2
      echo "Invalid choice: '$choice' (expected number)" >&2
      exit 1
      ;;
  esac
  i=1
  for m in $items; do
    if [ "$i" = "$choice" ]; then
      printf "%s" "$m"
      return 0
    fi
    i=$((i+1))
  done
  echo "" >&2
  echo "Invalid choice: $choice (out of range)" >&2
  exit 1
}

VALID_PROV="opencode|kilo|kiro|qwenmode|chatgpt"

if [ $# -ge 2 ]; then
  PROVIDER="$1"
  MODEL="$2"
  case "$PROVIDER" in
    opencode|kilo|kiro|qwenmode|chatgpt) ;;
    *) echo "Unknown provider: $PROVIDER (use: $VALID_PROV)" >&2; usage; exit 1 ;;
  esac
  if ! is_valid_model "$PROVIDER" "$MODEL"; then
    echo "Warning: model '$MODEL' not in known list for $PROVIDER" >&2
    echo "Known $PROVIDER models:" >&2
    list_models "$PROVIDER" | sed 's/^/  - /' >&2
    echo "Proceeding anyway (upstream may reject unknown models)..." >&2
  fi
elif [ $# -eq 1 ]; then
  PROVIDER="$1"
  case "$PROVIDER" in
    opencode|kilo|kiro|qwenmode|chatgpt) ;;
    *) echo "Unknown: $PROVIDER (use: $VALID_PROV)" >&2; usage; exit 1 ;;
  esac
  echo "--- Provider: $PROVIDER ---" >&2
  case "$PROVIDER" in
    opencode) MODEL=$(menu "Select model:" "$opencode_models") ;;
    kilo)     MODEL=$(menu "Select model:" "$kilo_models") ;;
    kiro)     MODEL=$(menu "Select model:" "$kiro_models") ;;
    qwenmode) MODEL=$(menu "Select model:" "$qwenmode_models") ;;
    chatgpt)  MODEL=$(menu "Select model:" "$chatgpt_models") ;;
  esac
else
  echo "--- Select provider ---" >&2
  echo "  1. opencode  — opencode.ai через Tor" >&2
  echo "  2. kilo      — api.kilo.ai через Tor" >&2
  echo "  3. kiro      — Amazon Q Developer (без Tor)" >&2
  echo "  4. qwenmode  — chat.qwen.ai через Playwright (без Tor)" >&2
  echo "  5. chatgpt   — chatgpt.com guest mode через Playwright (без Tor)" >&2
  printf "  > " >&2
  if ! read -r choice; then
    echo "" >&2
    echo "No input (EOF)" >&2
    exit 1
  fi
  choice=$(printf "%s" "$choice" | tr -d '[:space:]')
  case "$choice" in
    1) PROVIDER="opencode"; MODEL=$(menu "Select model:" "$opencode_models") ;;
    2) PROVIDER="kilo";     MODEL=$(menu "Select model:" "$kilo_models") ;;
    3) PROVIDER="kiro";     MODEL=$(menu "Select model:" "$kiro_models") ;;
    4) PROVIDER="qwenmode"; MODEL=$(menu "Select model:" "$qwenmode_models") ;;
    5) PROVIDER="chatgpt";  MODEL=$(menu "Select model:" "$chatgpt_models") ;;
    *) echo "Invalid choice: $choice" >&2; exit 1 ;;
  esac
fi

# ensure MODEL not empty after menu
if [ -z "${MODEL:-}" ]; then
  echo "Error: no model selected" >&2
  exit 1
fi

case "$PROVIDER" in
  # opencode-free, НЕ opencode-zen: keyless-провайдер Hermes (тот же Zen relay),
  # иначе gate требует OPENCODE_ZEN_API_KEY для моделей вне keyless-allowlist
  # (напр. muse-spark-1.3-contributor-free) и агент падает с AuthError.
  opencode) PROVIDER_NAME="opencode-free"; BASE_URL="http://127.0.0.1:9000/zen/v1" ;;
  kilo)     PROVIDER_NAME="kilo";         BASE_URL="http://127.0.0.1:9001/api/openrouter" ;;
  kiro)     PROVIDER_NAME="kiro";         BASE_URL="http://127.0.0.1:8080/v1" ;;
  qwenmode) PROVIDER_NAME="qwenmode";     BASE_URL="http://127.0.0.1:5002/v1" ;;
  chatgpt)  PROVIDER_NAME="chatgpt-guest"; BASE_URL="http://127.0.0.1:5003/v1" ;;
  *) echo "Unknown provider: $PROVIDER" >&2; exit 1 ;;
esac

UNIT=$(unit_for "$PROVIDER")
PORT=$(port_for "$PROVIDER")
HEALTH_URL=$(health_for "$PROVIDER")

# opencode/kilo ride through Tor — no Tor, no upstream. Wake it first and
# never treat it as a sleeper (it is NOT in ALL_UNITS).
NEEDS_TOR=0
case "$PROVIDER" in
  opencode|kilo) NEEDS_TOR=1 ;;
esac
tor_ready() { ss -tln 2>/dev/null | grep -q ':9050 '; }
tor_unit_exists() { systemctl --user cat hermes-tor.service >/dev/null 2>&1; }
if [ "$NEEDS_TOR" = "1" ]; then
  if tor_ready; then
    : # системный Tor уже слушает :9050 — будить нечего
  elif systemctl --user is-active --quiet hermes-tor.service 2>/dev/null; then
    : # юнит активен, порт ещё поднимается — дождёмся в цикле ниже
  elif tor_unit_exists; then
    echo "--- Буджу hermes-tor (провайдер ходит через Tor)..."
    systemctl --user start hermes-tor.service >/dev/null 2>&1 || true
  else
    echo "!!! Порт :9050 молчит, а юнита hermes-tor.service нет." >&2
    echo "!!! Проверь системный Tor: systemctl status tor@default" >&2
  fi
fi

# ─── Sleep / wake cycle ─────────────────────────────────────────────────────
# Кто был активен ДО переключения (для отката, если целевой не поднимется).
PREV_UNIT=""
for u in $ALL_UNITS; do
  if [ "$u" != "$UNIT" ] && systemctl --user is-active --quiet "$u" 2>/dev/null; then
    PREV_UNIT="$u"
  fi
done

TARGET_WAS_ACTIVE=0
systemctl --user is-active --quiet "$UNIT" 2>/dev/null && TARGET_WAS_ACTIVE=1

# Single-tenant RAM policy is enforced ALWAYS, even when the target unit is
# already up (drift: manual restarts, tests) — strays must go back to sleep.
STRAY=""
for u in $ALL_UNITS; do
  if [ "$u" != "$UNIT" ] && systemctl --user is-active --quiet "$u" 2>/dev/null; then
    STRAY="$STRAY $u"
    systemctl --user stop "$u" >/dev/null 2>&1 || true
  fi
done

# Tor is a DEPENDENT sleeper: lives only while a tor-backed provider
# (opencode/kilo) is active. Target doesn't need it -> it goes to sleep too.
if [ "$NEEDS_TOR" != "1" ] && systemctl --user is-active --quiet hermes-tor.service 2>/dev/null; then
  systemctl --user stop hermes-tor.service >/dev/null 2>&1 || true
  STRAY="$STRAY hermes-tor.service"
fi

if [ -n "$STRAY" ]; then
  echo "--- Уложил спать посторонних:$STRAY"
fi

if [ "$TARGET_WAS_ACTIVE" != "1" ]; then
  if [ -n "$PREV_UNIT" ]; then
    echo "!!! ВНИМАНИЕ: прежний провайдер ($PREV_UNIT) будет остановлен."
    echo "!!! Активные сессии Hermes, работавшие через него (например, этот"
    echo "!!! Telegram-чат), отвалятся до следующего сообщения."
  fi
  echo "--- Буджу $UNIT ..."
  systemctl --user start "$UNIT"

  echo "--- Жду порт :$PORT ..."
  i=0
  ready=""
  # Playwright-провайдеры (qwenmode ~91с, chatgpt CF-warmup) холодными
  # в 90с не укладываются — окно с запасом; быстрые выйдут по первому пробу.
  while [ $i -lt 100 ]; do
    if [ "$NEEDS_TOR" = "1" ] && ! tor_ready; then
      sleep 2; i=$((i+1)); continue
    fi
    if [ -n "$HEALTH_URL" ]; then
      if curl -sf -m 3 "$HEALTH_URL" >/dev/null 2>&1; then ready=1; break; fi
    else
      if curl -s -o /dev/null -m 3 "http://127.0.0.1:$PORT/" ; then ready=1; break; fi
    fi
    sleep 2
    i=$((i+1))
  done

  if [ -z "$ready" ]; then
    echo "ERROR: $UNIT не поднялся за ~90с. Логи:" >&2
    echo "  journalctl --user -u $UNIT -n 50" >&2
    systemctl --user stop "$UNIT" >/dev/null 2>&1 || true
    if [ -n "$PREV_UNIT" ]; then
      echo "--- Откат: буджу прежнего ($PREV_UNIT)" >&2
      # prev may be tor-backed and we may have just slept Tor — wake it first
      systemctl --user start hermes-tor.service >/dev/null 2>&1 || true
      systemctl --user start "$PREV_UNIT"
    fi
    exit 1
  fi
else
  echo "--- $UNIT уже активен — просто перепривязываю конфиг"
fi

# ─── Hermes config ──────────────────────────────────────────────────────────
if ! "$HERMES" config set model.provider "$PROVIDER_NAME"; then
  echo "Failed to set model.provider" >&2
  exit 1
fi
if ! "$HERMES" config set model.default "$MODEL"; then
  echo "Failed to set model.default" >&2
  exit 1
fi
if ! "$HERMES" config set model.base_url "$BASE_URL"; then
  echo "Failed to set model.base_url" >&2
  exit 1
fi

# state для provider-restore после ребута
mkdir -p "$(dirname "$STATE")"
printf "%s %s\n" "$PROVIDER" "$MODEL" > "$STATE"

echo "---"
echo "Switched to $PROVIDER_NAME / $MODEL  (unit: $UNIT)"

#!/usr/bin/env bash
# version_watch.sh — вахта версий opencode CLI.
#
# Следит за каналами раздачи CLI и за эмулируемой форвардером формой:
#   v2 канал: https://opencode.ai/update/api/latest/cli/npm  (2.x, @opencode/cli)
#   npm v2:   https://registry.npmjs.org/@opencode%2fcli/latest
#   npm v1:   https://registry.npmjs.org/opencode-ai/latest   (старая линия)
#   emu:      версия в UPSTREAM_UA внутри forwarder.py
#
# Если что-то сменилось (или форма отстала от актуального CLI) — алерт в Telegram.
# Первый запуск только засеивает state, без алерта.
#
# Тест без отправки: VERSION_WATCH_DRYRUN=1 ./version_watch.sh
# Ставится таймером: hermes-version-watch.timer (каждые 6 часов).

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
STATE="$DIR/version_watch.state"
LOG="$DIR/logs/version_watch.log"
ENV_FILE="$HOME/.hermes/.env"
PY="$HOME/.hermes/hermes-agent/venv/bin/python3"
mkdir -p "$DIR/logs"

ts() { date -u "+%Y-%m-%d %H:%M:%S"; }
log() { echo "$(ts) $*" >> "$LOG"; }

jget_version() {
    "$PY" -c "import sys,json;print(json.load(sys.stdin).get('version',''))" 2>/dev/null
}

v2_cli=$(curl -fsSL --max-time 20 "https://opencode.ai/update/api/latest/cli/npm" 2>/dev/null | jget_version)
v2_npm=$(curl -fsSL --max-time 20 "https://registry.npmjs.org/@opencode%2fcli/latest" 2>/dev/null | jget_version)
v1_npm=$(curl -fsSL --max-time 20 "https://registry.npmjs.org/opencode-ai/latest" 2>/dev/null | jget_version)
emu=$(grep -oE 'opencode/latest/[0-9][0-9.]*' "$DIR/forwarder.py" 2>/dev/null | head -1 | sed 's#opencode/latest/##')

if [ -z "$v2_cli" ] && [ -z "$v2_npm" ] && [ -z "$v1_npm" ]; then
    log "[err] не удалось получить версии (сеть?)"
    exit 1
fi

now="v2_cli=${v2_cli:--} v2_npm=${v2_npm:--} v1_npm=${v1_npm:--} emu=${emu:--}"

# --- первый запуск: засеять состояние, без алерта ---
if [ ! -f "$STATE" ]; then
    echo "$now" > "$STATE"
    log "[seed] $now"
    exit 0
fi

prev=$(cat "$STATE" 2>/dev/null || true)
if [ "$now" = "$prev" ]; then
    log "[ok] $now"
    exit 0
fi

# --- собрать сообщение об изменениях ---
getfield() { echo "$1" | tr ' ' '\n' | grep -E "^$2=" | head -1 | cut -d= -f2-; }

lines="🐙 opencode version watch"
for k in v2_cli v2_npm v1_npm emu; do
    old=$(getfield "$prev" "$k"); new=$(getfield "$now" "$k")
    if [ "$old" != "$new" ]; then
        lines="$lines
• $k: ${old:--} → ${new:--}"
    fi
done
if [ -n "${emu:--}" ] && [ "$emu" != "-" ] && [ -n "${v2_cli:--}" ] && [ "$v2_cli" != "-" ] && [ "$emu" != "$v2_cli" ]; then
    lines="$lines
⚠️ форма форвардера отстаёт: emu=$emu, актуальный CLI v2=$v2_cli — пора переснять shape (forwarder.py: UPSTREAM_UA / _cli_session_headers)."
fi

if [ "${VERSION_WATCH_DRYRUN:-0}" = "1" ]; then
    echo "$lines"
    log "[dry] изменение: $prev -> $now"
    exit 0
fi

# --- отправка в Telegram ---
TOKEN=""
CHAT=""
if [ -f "$ENV_FILE" ]; then
    TOKEN=$(sed -nE 's/^TELEGRAM_BOT_TOKEN=[[:space:]]*"?([^"[:space:]]+)"?.*$/\1/p' "$ENV_FILE" | head -1)
    CHAT=$(sed -nE 's/^TELEGRAM_HOME_CHANNEL=[[:space:]]*"?([^"[:space:]]+)"?.*$/\1/p' "$ENV_FILE" | head -1)
fi
[ -z "$CHAT" ] && CHAT="7311515527"

if [ -n "$TOKEN" ]; then
    if curl -fsS --max-time 20 "https://api.telegram.org/bot${TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${CHAT}" \
            --data-urlencode "text=${lines}" >/dev/null 2>&1; then
        log "[alert] отправлен: $now"
    else
        log "[alert-fail] TG API/сеть; текст: $lines"
    fi
else
    log "[alert-fail] нет TELEGRAM_BOT_TOKEN в $ENV_FILE; текст: $lines"
fi

echo "$now" > "$STATE"
exit 0

#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Oracle Cloud Free Tier Guardian
# Защита от превышения лимитов → бан аккаунта
# ═══════════════════════════════════════════════════════════════

LOG="/var/log/oracle-guardian.log"
ALERT_LOG="/tmp/oracle-guardian-alerts.log"
STATE_FILE="/tmp/oracle-guardian-state.json"
SWITCH_STATE="$HOME/.config/hermes-switch.state"

# cron бежит без user bus env — указываем его явно, иначе systemctl --user не работает
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

# Менеджер форвардеров — ТОЛЬКО systemd-юниты. nohup-запуски запрещены:
# они создают сирот с PPID 1, которые отбирают порты у юнитов.
# Активный провайдер определяется по SWITCH_STATE (пишет switch.sh).

# ═══ ЛИМИТЫ ORACLE ALWAYS FREE (A1.Flex 1 OCPU / 6GB) ═══
MAX_RAM_MB=5800
RAM_WARN_MB=5000
RAM_CRITICAL_MB=5500
MAX_DISK_PERCENT=80
MAX_DISK_CRITICAL=90
MAX_NETWORK_MB_DAY=500
MAX_OPENFD=2000
IDLE_KILL_SECONDS=3600

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG"; }
alert() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ALERT: $1" >> "$ALERT_LOG"; echo "ALERT: $1"; }

# ═══ 1. MEMORY GUARD ═══
check_memory() {
    local used_mb=$(free -m | awk '/^Mem:/{print $3}')
    if [ "$used_mb" -gt "$RAM_CRITICAL_MB" ]; then
        alert "RAM CRITICAL: ${used_mb}MB used (> ${RAM_CRITICAL_MB}MB)"
        # unit stop (не pkill): иначе systemd с Restart=always тут же перезапустит
        systemctl --user stop hermes-kilo-forwarder.service >/dev/null 2>&1 || true
        pkill -f "kilo_forwarder.py" 2>/dev/null || true
        log "Stopped kilo_forwarder to free RAM"
        sleep 2
        local new_used=$(free -m | awk '/^Mem:/{print $3}')
        if [ "$new_used" -gt "$RAM_CRITICAL_MB" ]; then
            systemctl --user stop hermes-opencode-forwarder.service >/dev/null 2>&1 || true
            pkill -f "forwarder.py 9000" 2>/dev/null || true
            log "Stopped opencode_forwarder — RAM still critical"
        fi
    elif [ "$used_mb" -gt "$RAM_WARN_MB" ]; then
        log "RAM WARNING: ${used_mb}MB used"
    fi
}

# ═══ 2. DISK GUARD ═══
check_disk() {
    local usage=$(df -h / | awk 'NR==2{print $5}' | tr -d '%')
    if [ "$usage" -gt "$MAX_DISK_CRITICAL" ]; then
        alert "DISK CRITICAL: ${usage}% — cleaning up"
        find /tmp -name "*.log" -mtime +3 -delete 2>/dev/null
        find /home/ubuntu/.hermes/logs -name "*.log" -mtime +3 -delete 2>/dev/null
        journalctl --vacuum-time=2d 2>/dev/null
        find /home/ubuntu/.hermes/sessions -name "*.jsonl" -mtime +30 -delete 2>/dev/null
        log "Disk cleanup performed"
    elif [ "$usage" -gt "$MAX_DISK_PERCENT" ]; then
        log "DISK WARNING: ${usage}%"
    fi
}

# ═══ 3. NETWORK GUARD (outbound traffic) ═══
check_network() {
    # Суммируем TX по всем NIC; +0 страхует от пустого grep (иначе арифметика падает).
    local rx_tx=$(cat /proc/net/dev | grep -E "ens|enp|eth" | awk '{s+=$10} END{print s+0}')
    local tx_mb=$((rx_tx / 1048576))
    local today=$(date +%Y-%m-%d)
    # Бейзлайн пишется ОДИН раз в сутки (первый прогон дня), дальше только читается.
    # Иначе (перезапись каждый прогон) daily вырождается в дельту за 2 минуты,
    # а после смены ключа даты — в весь счётчик с загрузки (ложный троттлинг).
    local base_tx=""
    if [ -f "$STATE_FILE" ]; then
        base_tx=$(grep -o "\"base_${today}\":[0-9]*" "$STATE_FILE" | cut -d: -f2)
    fi
    if [ -z "$base_tx" ]; then
        base_tx=$tx_mb
        echo "{\"base_${today}\": ${base_tx}}" > "$STATE_FILE"
    fi
    local daily_tx=$((tx_mb - base_tx))
    if [ "$daily_tx" -lt 0 ]; then daily_tx=0; fi   # счётчики сброшены (ребут)
    if [ "$daily_tx" -gt "$MAX_NETWORK_MB_DAY" ]; then
        alert "NETWORK LIMIT: ${daily_tx}MB sent today (limit: ${MAX_NETWORK_MB_DAY}MB)"
        systemctl --user stop hermes-opencode-forwarder.service hermes-kilo-forwarder.service >/dev/null 2>&1 || true
        pkill -f "forwarder.py" 2>/dev/null || true
        throttle hermes-opencode-forwarder.service 3600
        throttle hermes-kilo-forwarder.service 3600
        log "Throttled forwarders (units stopped 1h) — network limit reached"
    fi
    # NB: STATE_FILE НЕ перезаписываем — там суточный бейзлайн (см. выше).
}

# ═══ 4. PROCESS GUARD ═══
check_processes() {
    local zombies=$(ps aux | awk '{if($8=="Z") print}' | wc -l)
    if [ "$zombies" -gt 0 ]; then
        log "WARNING: ${zombies} zombie processes found"
    fi
    local max_fd=$(cat /proc/sys/fs/file-nr | awk '{print $1}')
    if [ "$max_fd" -gt "$MAX_OPENFD" ]; then
        alert "FD LIMIT: ${max_fd} open files"
    fi
    local port9000=$(ss -tln | grep ":9000 " | wc -l)
    local port9001=$(ss -tln | grep ":9001 " | wc -l)
    if [ "$port9000" -gt 1 ]; then
        log "WARNING: Multiple on port 9000 — killing all, unit restarts if active (see check_services)"
        pkill -f "forwarder.py 9000" 2>/dev/null || true
        sleep 1
    fi
    if [ "$port9001" -gt 1 ]; then
        log "WARNING: Multiple on port 9001 — killing all, unit restarts if active (see check_services)"
        pkill -f "kilo_forwarder.py 9001" 2>/dev/null || true
        sleep 1
    fi
}

# ═══ 5. SERVICE HEALTH (только systemd-юниты, только АКТИВНЫЙ провайдер) ═══
# Спящий провайдер (уложен switch.sh ради RAM) НЕ трогаем — иначе сломаем sleep-политику.
active_provider_unit() {
    local prov=""
    [ -f "$SWITCH_STATE" ] && read -r prov _ < "$SWITCH_STATE"
    case "$prov" in
      opencode) echo "hermes-opencode-forwarder.service" ;;
      kilo)     echo "hermes-kilo-forwarder.service" ;;
      kiro)     echo "hermes-kiro-gateway.service" ;;
      qwenmode) echo "qwenmode.service" ;;
      chatgpt)  echo "chatgpt-guest.service" ;;
    esac
}
throttle() { echo "$(($(date +%s) + $2))" > "/tmp/oracle-guardian-throttle-$(basename "$1" .service)" 2>/dev/null; }
throttled() {
    local f="/tmp/oracle-guardian-throttle-$(basename "$1" .service)" exp now
    [ -f "$f" ] || return 1
    exp=$(cat "$f" 2>/dev/null); now=$(date +%s)
    if [ "${exp:-0}" -gt "$now" ]; then return 0; fi
    rm -f "$f"; return 1
}
check_services() {
    local unit
    unit=$(active_provider_unit)
    if [ -n "$unit" ] && systemctl --user cat "$unit" >/dev/null 2>&1; then
        if ! systemctl --user is-active --quiet "$unit" 2>/dev/null; then
            if throttled "$unit"; then
                log "$unit down but throttled — leaving asleep"
            else
                # порт может держать nohup-сирота: убить, затем стартовать юнит
                case "$unit" in
                  hermes-opencode-forwarder.service) pkill -f "forwarder.py 9000" 2>/dev/null || true; sleep 1 ;;
                  hermes-kilo-forwarder.service)     pkill -f "kilo_forwarder.py 9001" 2>/dev/null || true; sleep 1 ;;
                esac
                log "Active provider unit $unit down — starting"
                systemctl --user start "$unit" >/dev/null 2>&1 || log "Failed to start $unit"
            fi
        fi
    fi
    if ! pgrep -x "tor" > /dev/null; then
        log "Tor died — restarting"
        sudo systemctl restart tor 2>/dev/null || true
    fi
}

# ═══ MAIN ═══
main() {
    log "--- Guardian check started ---"
    check_memory
    check_disk
    check_network
    check_processes
    check_services
    log "--- Guardian check complete ---"
}

case "${1:-run}" in
    run) main ;;
    status)
        echo "=== Oracle Guardian Status ==="
        echo "Memory: $(free -m | awk '/^Mem:/{printf "%dMB used / %dMB total", $3, $2}')"
        echo "Disk: $(df -h / | awk 'NR==2{print $5}') used"
        echo "Network today: $(cat /proc/net/dev | grep -E 'ens|enp|eth' | awk '{printf "%.0f MB", $10/1048576}')"
        echo "Forwarders: $(pgrep -f 'forwarder.py' | wc -l) processes"
        echo "Last check: $(tail -1 /var/log/oracle-guardian.log 2>/dev/null || echo 'never')"
        ;;
    log) tail -20 /var/log/oracle-guardian.log 2>/dev/null ;;
    alerts) cat /tmp/oracle-guardian-alerts.log 2>/dev/null || echo "No alerts" ;;
    *) echo "Usage: $0 [run|status|log|alerts]" ;;
esac

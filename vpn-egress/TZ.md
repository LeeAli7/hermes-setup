# ТЗ: VPN-only egress для Hermes на внешнем устройстве

Версия: 2026-11-27  
Область: `vpn-egress/` в репозитории `hermes-setup`  
Статус: реализация для внешних Linux-устройств (VPS/домашний сервер/мини-ПК)

---

## 1. Зачем это нужно

На основном сервере Hermes использует несколько типов egress:

- VPNGate (OpenVPN в netns);
- direct (IP самого сервера);
- WARP;
- Tor;
- static6/Oracle IPv6.

На внешнем устройстве нужен **упрощённый и переносимый** вариант: только VPN,
без Oracle/static6, без direct-IP и без Tor. Причина — не раскрывать IP
устройства и не тащить на него инфраструктуру Oracle, которая привязана к
конкретному серверу.

Итоговая схема:

```
Hermes agent
  -> forwarder.py :9000
      -> egress pool :9200
          -> vpngate slot 0 -> Bun relay :9120 -> CONNECT proxy 10.200.0.2:8897 -> OpenVPN netns vgprobe -> Zen
          -> vpngate slot 1 -> Bun relay :9121 -> CONNECT proxy 10.201.0.2:8898 -> OpenVPN netns vgprobe1 -> Zen
```

Оба VPNGate-слота — обычные OpenVPN-туннели в отдельных network namespaces.
Один слот активный, второй заранее поднят как горячий резерв. При 429/таймауте
пул выдаёт следующий слот, а сгоревший ротируется в фоне.

---

## 2. Что входит в поставку

```
vpn-egress/
├── TZ.md                                  # этот документ
├── README.md                              # быстрый старт
├── install.sh                             # установщик
├── uninstall.sh                           # удаление
├── etc/
│   └── egress.env.example                 # шаблон конфигурации
├── bin/
│   ├── hermes-egress                      # CLI: status/doctor/rotate/probe/logs/restart
│   └── hermes-egress-run                  # внутренний dispatcher для systemd
├── lib/
│   ├── egress_pool.py                     # VPN-only пул (только VPNGate-слоты)
│   ├── vpngate_tunnel.py                  # супервизор OpenVPN + netns + proxy
│   ├── vpngate_recovery.py                # ротация слота после 429/таймаута
│   ├── netns_connect_proxy.py             # HTTP CONNECT proxy внутри netns
│   └── bun_relay.ts                       # Bun TLS relay (CLI-совместимый)
└── systemd/
    ├── system/
    │   └── hermes-vpngate-tunnel@.service
    └── user/
        ├── hermes-egress-pool.service
        ├── hermes-egress-vpngate-relay@.service
        ├── hermes-vpngate-recovery@.service
        ├── hermes-vpngate-recovery@.timer
        ├── hermes-egress-watchdog.service
        ├── hermes-egress-watchdog.timer
        └── hermes-opencode-forwarder.service.d/egress-vpn.conf
```

Чего в поставке **нет** и не должно быть:

- `static6_*`, `oracle4`, `tcp_proxy6.py`, `static6_discovery.py`;
- direct-источника (`127.0.0.1:9150`) в пуле;
- Tor-источника (`127.0.0.1:9130`);
- WARP (в этой версии; при необходимости добавляется отдельно);
- секретов, токенов и пользовательских конфигов Hermes.

---

## 3. Требования к устройству

| Параметр | Требование |
|---|---|
| ОС | Linux с systemd (Ubuntu 22.04+/Debian 12+ проверять в первую очередь) |
| Права | обычный пользователь + `sudo`; root-логин не нужен |
| TUN/TAP | `/dev/net/tun` должен существовать (в VPS включить TUN/TAP) |
| Пакеты | `openvpn`, `iptables`, `curl`, `python3`, `iproute2`, `ss` |
| Bun | устанавливается `install.sh` при `--yes` или вручную |
| Сеть | исходящий UDP/TCP к VPNGate-серверам; NAT не обязателен |
| Порты | локально `9000` (forwarder), `9120+slot` (relay), `9200` (pool) |
| RAM | от 1 ГБ; 2 слота добавляют ~100–200 МБ |
| CPU | 1 vCPU достаточно; VPN-туннели в простое почти не грузят CPU |

Важно: установщик запускается **от имени пользователя Hermes**, не от root.
Системные unit-ы он ставит через `sudo`.

---

## 4. Куда что устанавливается

| Что | Куда |
|---|---|
| Python/Bun файлы | `$HOME/.hermes/ippool/` (`HERMES_EGRESS_HOME`) |
| Конфигурация | `/etc/hermes-egress/egress.env` |
| Список источников пула | `/etc/hermes-egress/sources.json` |
| Системный unit туннеля | `/etc/systemd/system/hermes-vpngate-tunnel@.service` |
| Пользовательские unit-ы | `$HOME/.config/systemd/user/` |
| Drop-in forwarder | `$HOME/.config/systemd/user/hermes-opencode-forwarder.service.d/egress-vpn.conf` |
| CLI | `/usr/local/bin/hermes-egress`, `/usr/local/bin/hermes-egress-run` |
| Документация | `/usr/local/share/hermes-egress/` |
| Логи и состояние | journald + `vpngate_recovery*.log`, `*.json` |
| OVPN-конфиги | `$HOME/.hermes/ippool/vpngate*/vg_*.ovpn` |

Все runtime-файлы лежат вне git-репозитория и не коммитятся.

---

## 5. Установка

```bash
git clone https://github.com/LeeAli7/hermes-setup.git
cd hermes-setup/vpn-egress
./install.sh --yes
```

Что делает `install.sh`:

1. Проверяет ОС, `sudo`, `/dev/net/tun`, наличие пакетов.
2. При `--yes` доустанавливает `openvpn iptables curl python3 iproute2`.
3. Находит или устанавливает Bun (`~/.bun/bin/bun`).
4. Определяет физический интерфейс по default-route (или `--iface`).
5. Копирует `lib/*` в `$HOME/.hermes/ippool`.
6. Пишет `/etc/hermes-egress/egress.env` и `sources.json`.
7. Ставит system/user unit-ы и drop-in для forwarder.
8. Запускает туннели, ждёт HTTP 200 от `/zen/v1/models` на каждом relay
   (до 240 секунд), затем запускает pool и recovery-таймеры.
9. Делает `loginctl enable-linger`, чтобы user-сервисы жили после logout.
10. Перезапускает `hermes-opencode-forwarder.service`, если он установлен.

Полезные флаги:

```bash
./install.sh --slots 3          # три горячих VPNGate-слота
./install.sh --iface eth0       # если default-route определяется неверно
./install.sh --no-start         # только установить unit-ы
./install.sh --force            # перезаписать egress.env
./uninstall.sh                  # удалить unit-ы и конфиг, оставить Hermes
./uninstall.sh --purge          # плюс удалить $HOME/.hermes/ippool
```

---

## 6. Конфигурация

`/etc/hermes-egress/egress.env`:

| Переменная | Смысл | По умолчанию |
|---|---|---|
| `HERMES_EGRESS_HOME` | каталог с `lib/*` | `$HOME/.hermes/ippool` |
| `VPNGATE_SLOTS` | число VPNGate-слотов | `2` |
| `VPNGATE_RELAY_PORT_BASE` | порт Bun relay слота 0 | `9120` |
| `VPNGATE_PROXY_PORT_BASE` | CONNECT proxy слота 0 | `8897` |
| `VPNGATE_PHYS_IF` | физический NIC для OpenVPN | default-route NIC |
| `EGRESS_POOL_PORT` | порт pool | `9200` |
| `EGRESS_POOL_URL` | URL pool | `http://127.0.0.1:9200` |
| `BUN_BIN` | путь к Bun | `$HOME/.bun/bin/bun` |
| `HERMES_CONFIG` | путь к config.yaml (диагностика) | `$HOME/.hermes/config.yaml` |
| `HERMES_RELAY_DEBUG` | дампы запросов/заголовков | `0` |

`sources.json` генерируется установщиком и выглядит так:

```json
[
  {"name": "vpngate",  "relay": "http://127.0.0.1:9120", "priority": 0},
  {"name": "vpngate2", "relay": "http://127.0.0.1:9121", "priority": 0}
]
```

При `VPNGATE_SLOTS=3` добавляется `vpngate3` на `9122` и т.д.

---

## 7. Как работает VPNGate-слот

Один слот — это:

1. **netns** `vgprobe` (slot 0), `vgprobe1` (slot 1), …
2. **veth** `veth-vg`/`veth-vg-ns` (slot 0), `veth-vg1`/`veth-vg1-ns` (slot 1).
3. **OpenVPN** внутри netns; после подключения весь трафик netns идёт через
   `tun0`, host default route не меняется.
4. **CONNECT proxy** внутри netns на `10.20<slot>.0.2:8897+slot`.
5. **Bun relay** на хосте, слушает `127.0.0.1:9120+slot` и ходит в upstream
   через `HTTPS_PROXY=http://10.20<slot>.0.2:8897+slot`.
6. **Supervisor** `vpngate_tunnel.py`:
   - скачивает список VPNGate (`/api/iphone/`), кэширует `.ovpn`;
   - выбирает сервер, поднимает OpenVPN, проверяет exit IP через
     `api.ipify.org`;
   - каждые 20 секунд проверяет живость туннеля;
   - по `rotate.request` или после 3 неудачных проверок убивает OpenVPN и
     берёт следующий сервер;
   - пишет `vpngate_live*.json` с host/ip/country/exit/ts.

Слоты не должны выбирать один и тот же сервер. Для этого:

- `VPNGATE_OFFSET` по умолчанию `slot * 40`;
- `VPNGATE_PER_COUNTRY=8`, `VPNGATE_MIN_SPEED=0` — расширяют список кандидатов;
- новые серверы, которые не подключились, попадают в `failed.json` на час.

Ограничение: VPNGate-выходы общие. Свежий exit может быть уже сожжён
другими пользователями. Поэтому два слота — минимум, а не гарантия.

---

## 8. Как работает recovery

`vpngate_recovery.py` запускается user-таймером каждые 20 секунд для каждого
слота (`hermes-vpngate-recovery@N.timer`).

Логика:

1. Читает состояние своего source из pool (`/health`).
2. Реагирует только на `cooldown`/`recovering`/`suspect` с
   `last_status in (429, 0, 502, 503)`.
3. Ставит source в `recovering` на 480 секунд.
4. Пишет `vpngate_control<N>/rotate.request`.
5. Ждёт до 150 секунд, пока supervisor подключит **другой** host
   (`vpngate_live*.json.ts` изменился).
6. Ждёт открытия relay-порта.
7. Проверяет **только туннель** (live-файл + порт), не тратит chat-квоту.
8. При успехе ставит source `ok`; иначе — `cooldown` и следующий цикл.

Важно: recovery не проверяет, осталась ли у нового IP chat-квота. Это
сделано специально, чтобы не сжигать единственный запрос. Первый реальный
запрос агента и есть проверка квоты; при 429 пул переключится на другой слот.

`hermes-egress-watchdog.timer` каждую минуту проверяет, что все
`hermes-vpngate-recovery@N.timer` активны, и поднимает их.

---

## 9. Как работает pool

`egress_pool.py` — локальный HTTP-сервис на `127.0.0.1:9200`.

Основные endpoint-ы:

| Endpoint | Назначение |
|---|---|
| `GET /health` | состояние источников, cooldown, last_status |
| `POST /lease` | выдать source + relay для запроса |
| `POST /lease/<id>/result` | сообщить статус (200/429/502/0) |
| `POST /source/<name>/state` | ручное состояние (`ok`/`cooldown`/`recovering`/`new`) |
| `POST /reset` | сбросить состояния |

Алгоритм выдачи:

1. Все источники — VPNGate-слоты с priority 0.
2. Здоровые сортируются по `last_used` (round-robin), чтобы нагрузка
   распределялась между слотами.
3. Если источник в `cooldown`/`recovering`, он не выдаётся.
4. Если здоровых нет, пул делает один проход recheck по слотам.
5. После неудачного recheck — 503; forwarder ждёт восстановления в пределах
   своего retry window.

`POST /lease/<id>/result`:

- `200` → source `ok`, fail_streak=0;
- `429` → `cooldown` на `Retry-After` (минимум 30с);
- `0` (таймаут/conn) → `cooldown` **300с**, чтобы recovery успел ротировать
  медленный выход;
- `403/5xx` → `cooldown` 60с.

Фоновый prober (`EGRESS_POOL_PROBE_INTERVAL=45`) проверяет источники с
приоритетом **выше** текущего лучшего через `GET /zen/v1/models`. В
VPN-only сборке все приоритеты равны 0, поэтому prober не трогает соседние
слоты; их живость контролирует supervisor. Это сознательно: `/models` не
доказывает chat-квоту, а лишние запросы тратят лимит.

---

## 10. Интеграция с forwarder

Установщик кладёт drop-in:

`~/.config/systemd/user/hermes-opencode-forwarder.service.d/egress-vpn.conf`

```ini
Environment=FORWARDER_EGRESS_POOL=http://127.0.0.1:9200
Environment=FORWARDER_UPSTREAM_RELAY=http://127.0.0.1:9120
Environment=FORWARDER_DISABLE_TOR=1
Environment=FORWARDER_DIRECT_FALLBACK=0
Environment=FORWARDER_WARP_ROTATE=0
Environment=FORWARDER_429_ATTEMPTS=3
Environment=FORWARDER_WALL_FAST_ATTEMPTS=2
Environment=FORWARDER_429_BACKOFF=1
Environment=FORWARDER_429_MAX_BACKOFF=2
Environment=FORWARDER_RETRY_WINDOW=180
Environment=FORWARDER_LARGE_BODY=204800
Environment=FORWARDER_LARGE_BODY_TIMEOUT=60
Environment=FORWARDER_FREE_ZEN_SESSION_SOURCE=server
```

Что это значит:

- `EGRESS_POOL` — forwarder спрашивает lease у пула;
- `UPSTREAM_RELAY=9120` — аварийный relay, если pool недоступен;
- `DISABLE_TOR=1`, `DIRECT_FALLBACK=0` — запрет Tor и прямого IP;
- `429_ATTEMPTS=3` — один запрос может попробовать слот 0, слот 1 и ещё
  один источник;
- `RETRY_WINDOW=180` — окно, в котором forwarder ждёт восстановления
  VPNGate, а не сразу отдаёт 503;
- `LARGE_BODY=204800` / `LARGE_BODY_TIMEOUT=60` — тела >200 КБ через
  не-direct выход имеют 60-секундный read timeout; если выход завис,
  запрос уходит на другой слот, а не висит 300 секунд.

Forwarder должен быть версии из этого же репозитория (в нём есть
`FORWARDER_EGRESS_POOL` и обработка 429 с большим `Retry-After`).

---

## 11. Проверка после установки

```bash
hermes-egress status
hermes-egress doctor
```

Ожидаемо:

- pool `sources`: `vpngate ok`, `vpngate2 ok`;
- `/models` на 9120 и 9121 — HTTP 200;
- в `vpngate_live*.json` — разные host/ip;
- `hermes-egress doctor` не показывает MISSING.

Проверка, что трафик реально идёт через VPN (не через IP устройства):

```bash
curl -sS --proxy http://10.200.0.2:8897 https://api.ipify.org; echo
curl -sS --proxy http://10.201.0.2:8898 https://api.ipify.org; echo
ip -4 addr show | grep -w inet   # IP устройства не должен совпадать с exit
```

Проверка chat-линии (тратит один запрос free-квоты):

```bash
curl -sS -m 45 http://127.0.0.1:9000/zen/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer public' \
  -d '{"model":"deepseek-v4.1-flash-free","messages":[{"role":"user","content":"Reply with OK"}],"max_tokens":5,"stream":false}'
```

Ожидаемо: JSON с `choices` и HTTP 200.

---

## 12. Диагностика

| Симптом | Что смотреть |
|---|---|
| pool DOWN | `systemctl --user status hermes-egress-pool`; `journalctl --user -u hermes-egress-pool -n 100` |
| relay 502 `VPN Down` | `sudo journalctl -u hermes-vpngate-tunnel@0 -n 100` |
| `vpn-fail` подряд | VPNGate-серверы недоступны/блокируют UDP; попробуйте другой `VPNGATE_PHYS_IF`, проверьте firewall |
| `/models` 429 | IP слота сожжён; `hermes-egress rotate 0`, recovery поднимет новый |
| 429 на chat, но `/models` 200 | квота IP исчерпана; это нормально, пул переключит слот |
| forwarder 503 | `tail ~/projects/hermes/logs/forwarder.log`; смотрите `egress lease` и `Retry-After` |
| `vpngate list failed` | VPNGate API недоступен; положите `.ovpn` в `$HOME/.hermes/ippool/vpngate/vg_*.ovpn`, supervisor подхватит кэш |
| после reboot ничего не работает | `loginctl show-user $USER -p Linger`; `systemctl --user status ...`; `sudo systemctl status hermes-vpngate-tunnel@0` |

Логи (journald; отдельные файлы unit-ы не пишут):

```bash
journalctl --user -u hermes-egress-pool -f
journalctl --user -u "hermes-egress-vpngate-relay@0" -f
journalctl --user -u "hermes-vpngate-recovery@0" -f
sudo journalctl -u "hermes-vpngate-tunnel@0" -f
hermes-egress logs 0
```

Recovery дополнительно ведёт файл
`$HERMES_EGRESS_HOME/vpngate_recovery<N>.log` (его удобно смотреть, когда
systemd-юнит уже завершился).

---

## 13. Безопасность

- Bun relay слушает только `127.0.0.1`; pool тоже.
- CONNECT proxy слушает только veth-адрес `10.20<slot>.0.2`, не `0.0.0.0`.
- OpenVPN и netns создаются отдельно; host default route не меняется.
- `iptables` правила добавляются только для подсети `10.20<slot>.0.0/24`.
- `HERMES_RELAY_DEBUG=0` по умолчанию: relay не пишет тела запросов и
  заголовки в `/tmp`. Включайте только для отладки и потом удаляйте дампы.
- В репозитории нет токенов и приватных ключей. `egress.env` не содержит
  секретов, но всё равно имеет режим `0640`.

Ограничения:

- VPNGate — общий бесплатный сервис; скорость и доступность не гарантируются.
- Бесплатная квота Zen привязана к IP; свежий VPNGate-выход может быть уже
  сожжён. Два слота снижают вероятность простоя, но не убирают её.
- При полном выгорании всех VPN-выходов Hermes вернёт 503; Kilo можно
  оставить как аварийный fallback, но это отдельная настройка Hermes.

---

## 14. Обновление и удаление

Обновление:

```bash
cd hermes-setup && git pull
cd vpn-egress && ./install.sh --force
```

`--force` перезапишет `egress.env` (слоты/интерфейс сбросятся на текущие
значения по умолчанию). Если конфиг кастомизирован — сделайте backup.

Удаление:

```bash
./uninstall.sh          # unit-ы, /etc/hermes-egress, CLI
./uninstall.sh --purge  # плюс $HOME/.hermes/ippool
```

Hermes, forwarder и его конфиг не удаляются.

---

## 15. Критерии приёмки

1. На чистом устройстве `./install.sh --yes` завершается без ошибок.
2. `hermes-egress status` показывает 2+ source в состоянии `ok`.
3. `/zen/v1/models` на каждом relay возвращает 200.
4. `curl --proxy http://10.200.0.2:8897 https://api.ipify.org` возвращает
   IP, отличный от IP устройства.
5. Тестовый chat-запрос через `127.0.0.1:9000` возвращает 200.
6. `hermes-egress rotate 0` не ломает второй слот: пока слот 0 ротируется,
   слот 1 продолжает отвечать.
7. После `reboot` (при включённом linger) сервисы поднимаются сами.
8. В `git status` репозитория нет runtime-логов, `.ovpn`, state-файлов и
   секретов.

---

## 16. Что сознательно не сделано в v1

- Нет WARP: на внешнем устройстве он требует отдельного `warpctl`/WireGuard
  и часто блокируется. VPNGate-слоты проще и переносимее.
- Нет direct/Tor/Oracle: по требованию «только VPN».
- Нет chat-probe в recovery: чтобы не сжигать free-квоту; первый реальный
  запрос — проверка.
- Нет автоматического выбора страны: supervisor берёт кандидатов по
  скорости/доступности и разносит слоты offset-ом.

При необходимости WARP можно добавить как source `priority=10` в
`sources.json` и отдельный relay, не меняя VPNGate-часть.

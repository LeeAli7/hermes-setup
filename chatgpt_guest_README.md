# ChatGPT-Guest (codexer.guest)

OpenAI-совместимый API-сервер поверх **chatgpt.com в гостевом режиме** — без аккаунта,
без API-ключа, без токенов. Headless Chromium + перехват SSE.

```
Клиент (curl, Hermes, opencode, любой OpenAI SDK)
   -> :5003 /v1/chat/completions
   -> Playwright headless Chromium
   -> chatgpt.com (guest mode)
```

## Как это работает

1. Поднимается headless Chromium с постоянным профилем (`/tmp/chatgpt_guest_profile`).
   Cloudflare-челлендж решается автоматически (~10-30с на холодном профиле, быстрее
   на прогретом).
2. Страница загружает chatgpt.com в **гостевом режиме** — модель **gpt-5-6**
   (`plan_type: guest`), без входа в аккаунт.
3. Перед отправкой в страницу инжектится скрипт, который патчит `window.fetch` и
   перехватывает SSE-поток `backend-anon/f/conversation` в реальном времени.
4. Каждые 250мс сервер поллит буфер страницы, разбирает SSE-дельта-операции
   (`{"p":"/message/content/parts/0","o":"append","v":"текст"}`) и пересылает их клиенту
   как живые `chat.completion.chunk` — стриминг, не ожидание конца.
5. Формат ответа — стандартный OpenAI: `delta.content`, `finish_reason`, `[DONE]`,
   плюс non-stream режим с полным `message.content`.

## Быстрый старт

```bash
# 1. Установить сервис
cp chatgpt-guest.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now chatgpt-guest

# 2. Проверить
curl http://127.0.0.1:5003/health
# {"status":"ok","pages":1}

# 3. Спросить (non-stream)
curl http://127.0.0.1:5003/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5-6","messages":[{"role":"user","content":"Скажи привет"}]}'

# 4. Спросить (stream)
curl -N http://127.0.0.1:5003/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5-6","stream":true,"messages":[{"role":"user","content":"Скажи привет"}]}'
```

## Эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/v1/chat/completions` | OpenAI-совместимый чат (stream/non-stream) |
| GET  | `/v1/models` | `gpt-5-6` |
| GET  | `/health` | статус пула страниц |

## Конфигурация (env)

| Переменная | Default | Описание |
|------------|---------|----------|
| `CG_PORT` | `5003` | порт |
| `CG_POOL_SIZE` | `1` | число страниц Chromium |
| `CG_PROFILE_DIR` | `/tmp/chatgpt_guest_profile` | профиль браузера (прогретый = быстрее) |
| `CG_API_KEY` | пусто | если задан — требует `Authorization: Bearer <key>` |
| `CG_HEADLESS` | `true` | headless режим |
| `CG_CF_WAIT` | `150` | максимум ожидания Cloudflare-челленджа, сек |
| `CG_IDLE_TIMEOUT` | `60` | лимит тишины в генерации, сек |
| `CG_TOTAL_TIMEOUT` | `300` | жёсткий лимит генерации, сек |
| `CG_SSE_POLL_MS` | `250` | частота чтения буфера страницы |
| `CG_CHROME` | путь к chromium | бинарь браузера |
| `CG_UA` | Chrome/126 | user-agent браузера |
| `CG_PROXY` | пусто | прокси-сервер (например Tor) |

## Ограничения (честно)

- **Модель одна**: `gpt-5-6` (гостевой слот), на выбор не даётся.
- **Tool-calls (25.08)**: работают через текстовый протокол `<tool_call>` → нативный
  OpenAI `tool_calls` (stream и non-stream), поддержан `role:"tool"` контекст,
  `tool_choice` (форс с ретраями). Матрица прилежания гостя:
  механика/парсинг/stream/контекст/многошаговые циклы/forced — стабильно
  (проверено 25.08 живой матрицей T1–T6); **auto на live-data запросах** — гость
  любит искать сам через встроенный веб-поиск; при `tool_choice` форс действует
  **эскалация промпта**: попытка N говорит громче (`_forced_override`), а не
  повторяет тот же текст — одинаковые ретраи гостя не двигают. Мусорные
  search-карточки детектятся маркерами + структурой и вычищаются.
- **Авто-ретраи**: пустой/мусорный ответ (протухшая гостевая сессия) → до
  `CG_MAX_ATTEMPTS` (по умолч. 3) попыток со сбросом профиля между ними;
  клиент не видит фейлы до первого реального чанка. Дисклеймер-футер страницы
  вырезается широким набором якорей (`_TXT_STOP`) — текст дисклеймера у
  ChatGPT дрейфует, при очередном изменении правится там.
- **Мысли не показываются**: в SSE-потоке гостя нет reasoning-чанков, только текст.
  (Проверено на живом потоке — `cot_version` есть в метаданных, но контент reasoning
  не стримится.)
- **Лимиты гостя**: ChatGPT периодически режет гостевые сессии. Поведение на длинной
  дистанции — на уровне Qwen-гостя: после серии запросов возможна капча/блок IP,
  лечится ротацией профиля (`CG_PROFILE_DIR` на свежий) или прокси (`CG_PROXY`).
- Не хранит историю: каждый запрос — отправка в тот же тред страницы (контекст
  чата сохраняется на странице между запросами).

## Интеграция с Hermes

Провайдер (в конфиге Hermes):

```yaml
providers:
  chatgpt-guest:
    base_url: http://127.0.0.1:5003/v1
    api_key: x
```

## Принцип vs codexer

| | codexer | chatgpt-guest |
|---|---|---|
| Аккаунт | твой ChatGPT (OAuth) | не нужен вообще |
| Модель | gpt-5.5+ по квоте аккаунта | gpt-5-6 гостевая |
| Риск | 🔴 бан аккаунта | 🟡 блок IP/капча |
| Конвертация ответов | ❌ сырой passthrough | ✅ полная, stream+non-stream |
| Совместимость | только `/v1/responses` | любой OpenAI SDK |
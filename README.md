# hermes-setup

Run the Hermes AI agent on free-tier LLM providers (`opencode.ai`, `kilo.ai`)
through local proxy forwarders. This README is a step-by-step walkthrough:
an agent on a fresh machine should be able to follow it top to bottom
without guessing.

```
hermes-agent -> forwarder.py :9000 -> Tor :9050 -> opencode.ai
hermes-agent -> kilo_forwarder.py :9001 -> Tor :9050 -> api.kilo.ai
```

> **Changelog (read the entries, they explain WHY things look odd):**
> - **2026-09-27 — vision verification:** new section “Vision” below with
>   a reference test image (`vision-ref.png`) and a 3-question protocol
>   that PROVES pixel vision (defeats text-summary false positives).
>   Agent self-reports (“I can see…”) are worthless — the old stub message
>   literally claimed vision while blind. Verify, don't trust.
> - **2026-09-25 — vision fixes (2):** (a) `forwarder.py` used to *drop*
>   `image_url` blocks when converting chat→responses for muse models, so
>   vision returned text stubs. Fixed: images map to `input_image`
>   (user messages only — upstream 400s assistant images). (b) Hermes routed
>   photos to text-stub mode because `models.dev` doesn't know our
>   `opencode-free` alias (`unknown` → fallback). Forced native via
>   `model.supports_vision: true` in `config.yaml`. (c) Vision turns reason
>   silently for minutes; the agent's 60s stream-idle watchdog killed healthy
>   streams (subagents died with *"did not emit a terminal response"*).
>   Disabled via `hermes-gateway.service.d/watchdog.conf`
>   (`HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS=0`) — TTFB/stale/hard-1500s
>   backstops remain. **Never put this env in `hermes-gateway.service`
>   itself: Hermes regenerates that file and wipes custom lines.**
> - **2026-09-18 — 3-layer gate:** TLS ClientHello bytes (`tls_forge.py`),
>   body shape (`stream:true` + SDK fields + CLI-known tools), headers/IDs.
>   See “Why the forwarder looks the way it does”.
> - **2026-09-17 — ID formats:** `ses_<9hex>ffe<14alnum>`,
>   `msg_<9hex>001<14alnum>`, `Bearer public`, current CLI UA.

## Step 0 — Prereqs (Ubuntu/Debian)

```bash
sudo apt install tor python3-venv
pip install requests pysocks
sudo systemctl enable --now tor   # provides 127.0.0.1:9050 (SOCKS) + :9051 (control)
```

## Step 1 — Place the files

```bash
mkdir -p ~/projects/hermes/logs
cp forwarder.py kilo_forwarder.py tls_forge.py switch.sh \
   oracle-guardian.sh provider-restore.sh resource-limits.sh \
   refresh-free-models.sh ~/projects/hermes/
cp -r free-models ~/projects/hermes/
chmod +x ~/projects/hermes/*.sh
```

## Step 2 — User systemd units (NOT system units, NOT nohup)

```bash
mkdir -p ~/.config/systemd/user
cp hermes-opencode-forwarder.service hermes-kilo-forwarder.service \
   provider-restore.service hermes-refresh-models.service \
   hermes-refresh-models.timer ~/.config/systemd/user/
cp -r hermes-gateway.service.d ~/.config/systemd/user/   # watchdog off, see Changelog 25.09
systemctl --user daemon-reload
```

Rules that will save you hours (all learned the hard way):
- Units are **user** units (`systemctl --user`). Never add `User=` — the user
  manager rejects it (`216/GROUP`, silent crash-loop).
- Never start forwarders via `nohup` next to units — orphans steal the ports
  (`Address already in use` + endless restarts).
- Never edit `hermes-gateway.service` itself — Hermes regenerates it.
  Custom env goes in `hermes-gateway.service.d/*.conf`.
- Only ONE provider unit runs at a time (`switch.sh` enforces this for RAM).

## Step 3 — Start the forwarder

```bash
systemctl --user enable --now hermes-opencode-forwarder.service
systemctl --user enable --now provider-restore.service
curl http://127.0.0.1:9000/health   # expect: OK
tail ~/projects/hermes/logs/forwarder.log
```

## Step 4 — Guardian cron (as the user, NOT root)

```
*/2 * * * * ~/projects/hermes/oracle-guardian.sh run >> /tmp/oracle-guardian.log 2>&1
*/5 * * * * ~/projects/hermes/resource-limits.sh >> /tmp/resource-limits.log 2>&1
```

`oracle-guardian.sh` manages ONLY systemd units (never `nohup`), enforces
RAM/disk/network caps, and restarts a dead ACTIVE provider. Sleepers
(inactive providers) are left alone on purpose.

## Step 5 — Hermes config

Copy `config.yaml` to `~/.hermes/config.yaml` (merge with existing settings,
don't overwrite blindly). The load-bearing keys:

```yaml
model:
  provider: opencode-free      # NOT opencode-zen (needs OPENCODE_ZEN_API_KEY -> AuthError)
  default: muse-spark-1.3-contributor-free
  base_url: http://127.0.0.1:9000/zen/v1
  supports_vision: true        # models.dev doesn't know opencode-free;
                               # without this, photos degrade to text stubs
```

Restart the gateway afterwards (`systemctl --user restart hermes-gateway.service`;
sessions drop until the next message — normal).

## Step 6 — Switch provider/model and verify

```bash
./switch.sh --list opencode     # live free models (auto-refreshed into free-models/)
./switch.sh opencode mimo-v2.5-free
cat ~/.config/hermes-switch.state   # provider + model persist here

# Text check (expect real content, not an error):
curl -X POST http://127.0.0.1:9000/zen/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5-free","messages":[{"role":"user","content":"Say OK"}],"max_tokens":20}'

# Vision check (expect a description of the image, not a stub):
# post chat/completions with messages[0].content = [
#   {"type":"text","text":"Describe this image"},
#   {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}} ]
# The forwarder maps image_url -> input_image for the Responses API.
```

## Why the forwarder looks the way it does

opencode.ai free tier gates on request shape — anything not byte-level like
the official CLI gets `403 FreeTierError`. Verified layers (Sept 2026):

1. **`Authorization: Bearer public`** (literal). Hermes sends its own
   placeholder (401s) → forwarder overwrites it on every upstream request.
2. **Session/request ID format** (`x-opencode-session: ses_<9hex>ffe<14alnum>`,
   `x-opencode-request: msg_<9hex>001<14alnum>`), `x-opencode-client: cli`,
   `x-opencode-project: global`, current CLI `User-Agent`
   (override via `FORWARDER_UA`).
3. **TLS ClientHello bytes** — `tls_forge.py` replays the captured CLI hello
   byte-exact (fresh random + X25519 share patched in) with a from-scratch
   TLS 1.3 implementation (RFC 8448 vectors + OpenSSL interop). No stock
   stack passes. `FORWARDER_TLS_FORGE=0` falls back to `requests` (will 403).
4. **Body shape** — `/responses` needs `stream:true` + SDK fields
   (`max_output_tokens`/`store`/`include`) + CLI-known `tools` (auto-injected
   decoys `bash`/`edit`/`read` + `tool_choice:auto`); `/chat` needs
   `stream:true` + tools too. Non-stream clients get SSE assembled
   server-side. Reasoning models get `max_output_tokens` floored to 4096
   (small caps end `incomplete` with zero visible text).
5. **Rate limits (429/500)** — per-IP quotas, Tor `NEWNYM` rotation with
   backoff; exit change is verified, not assumed.
6. **Model routing** — `muse-spark-*` only work via `/v1/responses`;
   the forwarder converts Chat→Responses and back (images preserved as
   `input_image` on user messages; assistant images stay text — upstream
   400s them).

What does NOT matter (verified): Tor-vs-direct egress, exact header order,
exact `ses_`/`msg_` values (fresh valid-format IDs pass), JSON key order.

## Supported models (auto-refreshed into `free-models/*.txt` every 6h)

- **opencode**: `switch.sh --list opencode` (mimo, ling-flash, nemotron,
  muse-spark-1.2/1.3, …). `deepseek-v4-flash-free` is dead upstream.
- **kilo**: `switch.sh --list kilo` (19 `:free` + routers). Some are
  429-limited (`minimax-m3`, `inkling*`) — rotated through automatically.

## Vision: native pixels vs text stubs (+ how to PROVE which you have)

Two modes exist. Only one of them means the model actually sees:

| | `native` (want this) | `text` (stubs) |
|---|---|---|
| What happens | photo pixels attached inline to the main model request (`input_image`) | photo pre-analyzed by `vision_analyze` tool, model gets only a text description |
| Agent says | describes real content | `couldn't quite see it` / generic description |
| Gateway log | `Image routing: native (model supports vision)` | `Image routing: text … Pre-analyzing … via vision_analyze` |

Why `text` kicks in when it shouldn't: `models.dev` doesn't know our
`opencode-free` alias (`unknown` → fallback). Fix = `model.supports_vision:
true` in `~/.hermes/config.yaml` (already in the `config.yaml` template
here) + gateway restart. Second trap (fixed): old `forwarder.py` dropped
`image_url` blocks when converting chat→responses — this repo's version
maps them to `input_image` (user messages only; upstream 400s assistant
images, and Hermes strips them on replay anyway).

**Verification protocol** (run this, don't believe self-reports):
`vision-ref.png` in this repo is the reference: 736×737, red rectangle top,
blue rectangle bottom, microtext `KXQ-5193` in the middle, 7 green dots
bottom row. Ground truth: `KXQ-5193` / `7` / `red, blue`.
Designed so a text summary CANNOT pass: the auto-describer writes 2–4
sentences (~150 words) and explicitly skips decorative details — microtext
and exact counts don't survive it.

1. Send `vision-ref.png` to the agent, ask exactly:
   1) the exact code text in the middle;
   2) how many small green dots in the bottom row (exact number);
   3) colors of the two big rectangles, top to bottom.
2. Expect: `KXQ-5193` / `7` / `red, blue` (verified live 27.09.2026,
   terminal `response.completed`).
3. Cross-check logs for the same turn:
   - `gateway.log`: `Image routing: native` (not `text`);
   - `forwarder.log`: big `Body read` (~100+KB base64 inline) + `Response: 200`;
   - `errors.log`: no `terminal response` / `codex_stream_idle_kill`.
4. Verdict rule: correct microtext + count **and** all three log signs =
   sees pixels. Anything less = still on stubs, debug further.

## Files

| File | Description |
|------|-------------|
| `forwarder.py` | opencode proxy: CLI headers, body padding, Responses conversion, Tor rotation |
| `tls_forge.py` | minimal TLS 1.3 client replaying the CLI ClientHello byte-exact |
| `kilo_forwarder.py` | kilo proxy: transparent pass-through, Tor rotation |
| `switch.sh` | provider/model switcher (sleep/wake units, persists choice) |
| `refresh-free-models.sh` + `free-models/*.txt` | model-list auto-refresh (timer every 6h) + snapshots |
| `oracle-guardian.sh` | Oracle Free Tier guards (RAM/disk/net, unit watchdog) |
| `provider-restore.sh` / `provider-restore.service` | boot-time provider restore |
| `resource-limits.sh` | cron RAM/disk probe |
| `hermes-opencode-forwarder.service` | user systemd unit, port 9000 (`%h`-templated) |
| `hermes-kilo-forwarder.service` | user systemd unit, port 9001 (`%h`-templated) |
| `hermes-refresh-models.service` + `.timer` | 6h model-list refresh (`%h`-templated) |
| `hermes-gateway.service.d/watchdog.conf` | **drop-in**: disables the stream-idle killer (see Changelog 25.09) |
| `config.yaml` | Hermes config template (opencode-free, supports_vision) |
| `vision-ref.png` | reference vision test image + ground truth (see Vision above) |

## Troubleshooting (symptom → cause → fix)

- **`403 FreeTierError`** → gate tightened again. Bisect in order: body
  shape → headers → TLS (re-capture CLI hello: `strace -e trace=%network`
  on `~/.opencode/bin/opencode run`, compare ClientHello bytes).
- **Agent shows photo stubs (`couldn't quite see it`)** → vision routed to
  text mode. Check `supports_vision: true` in `~/.hermes/config.yaml`, then
  gateway restart. Then run the Vision verification protocol above
  (`vision-ref.png` + 3 questions) — a correct microtext/count answer plus
  `Image routing: native` in the log is the only acceptable proof.
- **`did not emit a terminal response` / idle kills** → `watchdog.conf`
  drop-in missing or not loaded (`systemctl --user cat hermes-gateway.service`
  must list it; env must show in `/proc/<gateway-pid>/environ`).
- **`Address already in use` + restart loops** → a `nohup` orphan holds the
  port: `pkill -f 'forwarder.py 9000'`, never use nohup next to units.
- **`216/GROUP` crash-loop** → stray `User=` line in a user unit. Remove it.
- **Vision `incomplete` with empty text** → token cap starved the reasoning
  phase; forwarder floors muse caps at 4096 automatically.
- `curl http://127.0.0.1:9000/health` → must print `OK`.
- `cat ~/.config/hermes-switch.state` → active provider/model.
- Rotate Tor IP + verify: `python3 -c "import sys; sys.path.insert(0,'~/projects/hermes'.replace('~','$HOME')); import forwarder; print(forwarder.renew_tor_ip())"`.

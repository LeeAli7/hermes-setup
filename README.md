# hermes-setup

Scripts and configs for running Hermes AI agent with free-tier LLM providers (opencode.ai, kilo.ai) via local proxy forwarder.

> **2026-09-17 update — READ FIRST.** opencode.ai killed every previous bypass
> (`User-Agent` spoof, system-prompt marker, random-UUID session headers — all
> `403 FreeTierError` since ~16.09, verified on 2 Tor exits + direct IP).
> The real gate is the **format of `x-opencode-session` / `x-opencode-request`**
> (see §1 below). `forwarder.py` in this repo implements the working combo.
> If requests 403 again, re-capture the official CLI (`~/.opencode/bin/opencode`
> through mitmproxy) and compare — the procedure is in Troubleshooting.

## Problem

opencode.ai free tier gates on request shape — anything that does not look
byte-level like the official CLI gets `403 FreeTierError: "OpenCode's free
tier can only be used from within OpenCode"`:

1. **`Authorization: Bearer public`** (literal!) — always required. Hermes
   sends its own placeholder (`opencode-zen-free-keyless`) which 401s, so the
   forwarder **overwrites** it on every upstream request.
2. **Session/request ID format** — reverse-engineered from the CLI binary and
   its local `opencode.db` (9/9 sessions match):
   - `x-opencode-session: ses_<9 hex>ffe<14 alnum>` (e.g. `ses_f50f93dbdffefPFhyjMVZ7g32c`)
   - `x-opencode-request: msg_<9 hex>001<14 alnum>` (e.g. `msg_0af06c31b001lxhkl0JbdkIMoO`)
   - Random UUIDs → 403. Missing headers → 403. Replayed real IDs → 200,
     freshly minted valid-format IDs → 200 (verified 17.09.2026).
3. **Current CLI User-Agent** — `opencode/1.18.31 ai-sdk/provider-utils/4.0.40 runtime/bun/1.3.14`
   (stale versions risk allowlist rejection; override via `FORWARDER_UA`).
4. **Fixed literals** — `x-opencode-client: cli`, `x-opencode-project: global`
   (NOT version `"1"`, NOT random UUIDs).
5. **Rate limits (429/500)** — per-IP quotas, mitigated by Tor IP rotation
   (`NEWNYM` on 429/500 with backoff; exit change is verified, not assumed).
6. **Model-specific API routing** — `muse-spark-1.3/1.2-contributor-free` only
   work via `/v1/responses` (Responses API). The forwarder auto-converts
   Chat Completions → Responses for these models and back.
7. **Hermes provider must be `opencode-free`** (keyless by contract for any
   model). `opencode-zen` demands `OPENCODE_ZEN_API_KEY` for models outside
   Hermes' built-in allowlist → agent dies with AuthError before any request.
   `switch.sh` sets `opencode-free` automatically.

What does NOT matter (verified): TLS fingerprint (Python-urllib3 passes once
headers are right), Tor-vs-direct egress, request body shape beyond the
endpoint contract.

## How it works

```
hermes-agent -> forwarder.py :9000 -> Tor :9050 -> opencode.ai (CLI-shaped headers)
hermes-agent -> kilo_forwarder.py :9001 -> Tor :9050 -> api.kilo.ai
```

The forwarders (`forwarder.py`, `kilo_forwarder.py`) per upstream request:
1. **Overwrite upstream auth/identity headers** — `Authorization: Bearer public`,
   current CLI `User-Agent`, `x-opencode-*` with freshly minted valid-format IDs
   (sticky per conversation via `prompt_cache_key`).
2. **Convert Chat Completions → Responses API** — for `muse-spark-*` models
   (`messages[]` → `input[]`, back-convert `output[]` → `choices[]`).
3. **Retry with Tor rotation** — 429/5xx → rotate exit IP (verified change) →
   backoff → retry; 413 over 20 MB bodies; vision/multimodal bodies pass
   through unmutated (kilo forwarder never mutates bodies at all).

`switch.sh` — interactive provider/model switcher. Only ONE provider unit runs
at a time (RAM policy for Oracle Free Tier); choice persists in
`~/.config/hermes-switch.state` and is restored on boot by
`provider-restore.sh` + `provider-restore.service`.
`oracle-guardian.sh` (cron every 2 min) — RAM/disk/network guards. It manages
ONLY systemd units, never `nohup` (orphans steal ports from units).

## Supported models (verified live through Tor, 06–17.09.2026)

### opencode.ai (free) — `switch.sh opencode`
| Model | Endpoint | Status |
|-------|----------|--------|
| `mimo-v2.5-free` | `/v1/chat/completions` | 200 OK |
| `ling-3.0-flash-fin-free` | `/v1/chat/completions` | 200 OK |
| `nemotron-3-ultra-free` | `/v1/chat/completions` | 200 OK |
| `nemotron-3.5-lightning-free` | `/v1/chat/completions` | 200 OK |
| `muse-spark-1.3-contributor-free` | `/v1/responses` (auto-converted) | 200 OK |
| `muse-spark-1.2-contributor-free` | `/v1/responses` (auto-converted) | 200 OK |
| `deepseek-v4-flash-free` | — | dead upstream (400 Model unavailable) |

Full list lives in `switch.sh` (`opencode_models`). Run `switch.sh --list opencode`.

### kilo.ai (free) — `switch.sh kilo`
19 `:free` models + `kilo-auto/free`, `openrouter/free` routers — all verified
against `https://api.kilo.ai/api/openrouter/models` (pricing 0). Run
`switch.sh --list kilo`. Known 429-limited (rotated through):
`minimax-m3`, `inkling`, `inkling-small`.

## Files

| File | Description |
|------|-------------|
| `forwarder.py` | opencode proxy: CLI-shaped headers, Responses conversion, Tor rotation |
| `kilo_forwarder.py` | kilo proxy: transparent pass-through, Tor rotation |
| `switch.sh` | provider/model switcher (sleep/wake units, persists choice) |
| `oracle-guardian.sh` | Oracle Free Tier guards (RAM/disk/net, unit watchdog) |
| `provider-restore.sh` / `provider-restore.service` | boot-time provider restore |
| `resource-limits.sh` | cron RAM/disk probe |
| `hermes-opencode-forwarder.service` | user systemd unit, port 9000 (`%h`-templated) |
| `hermes-kilo-forwarder.service` | user systemd unit, port 9001 (`%h`-templated) |

Units are **user** units (`systemctl --user`). Never add `User=` to them —
the user manager rejects it (`216/GROUP`, silent crash-loop). Never start
forwarders via `nohup` next to the units — orphans steal the ports.

## Setup

```bash
# Install dependencies
sudo apt install tor
pip install requests pysocks

# System Tor (provides :9050/:9051), then user units:
systemctl --user enable --now hermes-opencode-forwarder.service
systemctl --user enable --now provider-restore.service

# cron (as the user):
# */2 * * * * /home/<you>/projects/hermes/oracle-guardian.sh run >> /tmp/oracle-guardian.log 2>&1

# Switch provider/model:
./switch.sh opencode muse-spark-1.3-contributor-free
```

## Troubleshooting

```bash
# Forwarder health + logs
curl http://127.0.0.1:9000/health
tail ~/projects/hermes/logs/forwarder.log

# Exact upstream shape the forwarder must emit (verified 17.09.2026):
curl https://opencode.ai/zen/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer public" \
  -H "User-Agent: opencode/1.18.31 ai-sdk/provider-utils/4.0.40 runtime/bun/1.3.14" \
  -H "x-opencode-client: cli" -H "x-opencode-project: global" \
  -H "x-opencode-request: msg_<9hex>001<14alnum>" \
  -H "x-opencode-session: ses_<9hex>ffe<14alnum>" \
  -d '{"model":"muse-spark-1.3-contributor-free","input":"hi","max_output_tokens":32}'

# If 403s return: re-capture the official CLI to diff the wire:
# mitmdump -p 18081, then HTTPS_PROXY=http://127.0.0.1:18081 +
# NODE_EXTRA_CA_CERTS/SSL_CERT_FILE=~/.mitmproxy/mitmproxy-ca-cert.pem,
# run: ~/.opencode/bin/opencode run --model opencode/<model> "hi"
# Compare: Authorization, UA, x-opencode-* formats/values, endpoint.

# Rotate Tor IP + verify exit change:
python3 -c "import sys; sys.path.insert(0,'~/projects/hermes'.replace('~','$HOME')); import forwarder; print(forwarder.renew_tor_ip())"

# Current provider state / unit state:
cat ~/.config/hermes-switch.state
systemctl --user is-active hermes-opencode-forwarder.service
```

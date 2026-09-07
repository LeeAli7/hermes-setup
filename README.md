# hermes-setup

Scripts and configs for running Hermes AI agent with free-tier LLM providers (opencode.ai, kilo.ai) via local proxy forwarder.

## Problem

opencode.ai free tier enforces request-level protections that block anonymous/non-official clients:

1. **User-Agent whitelist** — only `opencode/...` User-Agent accepted
2. **System prompt marker** — response must contain "You are opencode"
3. **MissingSessionID** — requests without `x-opencode-session`, `x-opencode-client`, `x-opencode-request`, `x-opencode-project` headers are rejected
4. **Rate limits (429)** — per-IP quotas, mitigated by Tor IP rotation
5. **Model-specific API routing** — `muse-spark-1.3-contributor-free` only works via `/v1/responses` (Responses API), not `/v1/chat/completions` (Chat Completions API)

Without these headers, opencode.ai returns `MissingSessionID` or 403. The `muse-spark` models additionally return 500 if sent through the standard Chat Completions endpoint.

## How it works

```
hermes-agent -> forwarder.py :9000 -> opencode.ai (with spoofed headers + Responses API conversion)
```

The forwarder (`forwarder.py`) does three things:
1. **Injects official opencode headers** — session ID, client ID, request ID, project ID (extracted from reverse-engineering the opencode CLI binary)
2. **Converts Chat Completions → Responses API** — for `muse-spark-*` models, converts the OpenAI-format request to opencode's Responses API format and back
3. **Tor IP rotation** — rotates exit node on 429 to bypass per-IP quotas

## Supported models

### opencode.ai (free, via Responses API conversion)
| Model | Endpoint | Status |
|-------|----------|--------|
| `muse-spark-1.3-contributor-free` | `/v1/responses` | Working |
| `muse-spark-1.2-contributor-free` | `/v1/responses` | Working |
| `mimo-v2.5-free` | `/v1/chat/completions` | Working |

### opencode.ai (free, Chat Completions)
| Model | Status |
|-------|--------|
| `grok-4-1-free` | Working |
| `qwen3-coder-free` | Working |
| `glm-5-free` | Working |
| `minimax-m2.5-free` | Working |

### kilo.ai (free, no Tor needed)
| Model | Status |
|-------|--------|
| `Llama-3.3-70B-Instruct` | Working |
| `Llama-3.1-8B-Instruct` | Working |
| `Mistral-Small-3.1-24B-Instruct-2503` | Working |
| `DeepSeek-V3-0324` | Working |
| `Qwen3-235B-A22B` | Working |
| + 14 more models | See `switch.sh` |

## Files

| File | Description |
|------|-------------|
| `forwarder.py` | Local proxy: header injection, Responses API conversion, Tor rotation |
| `proxy_manager.py` | Tor service management, IP rotation |
| `switch.sh` | Interactive provider/model switcher |
| `config.yaml` | Hermes agent config (model, provider, base_url) |
| `hermes-opencode-forwarder.service` | systemd unit for forwarder |
| `provider-restore.sh` | Boot-time provider state restoration |

## Setup

```bash
# Install dependencies
sudo apt install tor
pip install requests pysocks

# Start Tor
sudo systemctl start tor

# Start forwarder
python3 forwarder.py 9000

# Or install as systemd service
sudo cp hermes-opencode-forwarder.service /etc/systemd/system/
sudo systemctl enable --now hermes-opencode-forwarder@ali
```

## Why these changes were made

### 1. MissingSessionID bypass (headers)
opencode.ai added session validation. Without `x-opencode-session` header, requests fail with `MissingSessionID`. We reverse-engineered the opencode CLI binary (`~/.opencode/bin/opencode`) and extracted the required headers:
- `x-opencode-session` — random UUID per request
- `x-opencode-client` — client version (always `1`)
- `x-opencode-request` — random UUID per request
- `x-opencode-project` — random UUID per request

### 2. Responses API conversion (muse-spark models)
`muse-spark-1.3-contributor-free` and `muse-spark-1.2-contributor-free` do not support the Chat Completions API (`/v1/chat/completions`). They only work through opencode's Responses API (`/v1/responses`). The forwarder automatically detects these models and converts:
- Request: `messages[]` → `input[]`
- Response: `output[].content[].text` → `choices[].message.content`

### 3. Tor rotation on 429
opencode.ai enforces per-IP quotas. When a 429 is received, the forwarder rotates the Tor exit node to get a fresh IP. This is done via `proxy_manager.py` which sends a `NEWNYM` signal to Tor's control port.

## Troubleshooting

```bash
# Check forwarder logs
tail -f /home/ali/projects/hermes/logs/forwarder.log

# Test direct to forwarder
curl http://127.0.0.1:9000/zen/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"muse-spark-1.3-contributor-free","messages":[{"role":"user","content":"hi"}]}'

# Rotate Tor IP manually
python3 -c "from proxy_manager import renew_tor_ip; renew_tor_ip()"

# Check current provider state
cat ~/.config/hermes-switch.state
```

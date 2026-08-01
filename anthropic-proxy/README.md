# Anthropic Proxy

Translates the **Anthropic Messages API** (`/v1/messages`) to the **OpenAI Chat Completions API** (`/v1/chat/completions`), enabling tools like Claude Code or Cline to use any OpenAI-compatible backend.

## How It Works

Anthropic clients send requests in the Anthropic format (`/v1/messages` with `content` blocks of type `text`, `tool_use`, `tool_result`, `thinking`). This proxy converts them to OpenAI format, forwards to an upstream OpenAI-compatible server, and translates the response back to Anthropic format.

```
Claude Code/Cline -> anthropic-proxy :5000 -> any OpenAI-compatible backend (GLMMode, QwenMode, etc.)
```

## Requirements

- Python 3.8+ (stdlib only — no pip dependencies)

## Installation

```bash
# 1. Clone
git clone https://github.com/LeeAli7/anthropic-proxy.git
cd anthropic-proxy

# 2. (Optional) Create venv — not required since there are no deps
python3 --version  # should be 3.8+
```

## Usage

```bash
# Default: port 5000, upstream http://127.0.0.1:9000/zen/v1
python anthropic_proxy.py

# With environment variables
UPSTREAM_BASE=http://127.0.0.1:5001/v1 PROXY_PORT=5000 python anthropic_proxy.py
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `UPSTREAM_BASE` | `http://127.0.0.1:9000/zen/v1` | Upstream OpenAI-compatible API base URL |
| `API_KEY` | `public` | API key sent to upstream |
| `PROXY_PORT` | `5000` | Port to listen on |
| `PROXY_HOST` | `0.0.0.0` | Host to bind to |
| `PROXY_LOG_FILE` | *(none)* | Optional log file path |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/messages` | Anthropic Messages API → upstream |
| POST | `/v1/complete` | Same as `/v1/messages` |
| POST | `/v1/chat/completions` | Passthrough to upstream |
| GET | `/v1/models` | Passthrough to upstream |
| GET | `/health` | Health check |

### Quick test

```bash
# Start with GLMMode as upstream
UPSTREAM_BASE=http://127.0.0.1:5001/v1 PROXY_PORT=5000 python anthropic_proxy.py &

# Test in another terminal
curl http://127.0.0.1:5000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: public" \
  -d '{
    "model": "glm-4.7",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Say hello"}]
  }'
```

### Using with Claude Code

```bash
# Set the proxy as the API endpoint
export ANTHROPIC_BASE_URL=http://127.0.0.1:5000
claude
```

## Linux Service (systemd)

Create `/etc/systemd/system/anthropic-proxy.service`:

```ini
[Unit]
Description=Anthropic Proxy
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/anthropic-proxy
Environment=UPSTREAM_BASE=http://127.0.0.1:5001/v1
Environment=PROXY_PORT=5000
ExecStart=/usr/bin/python3 anthropic_proxy.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now anthropic-proxy
```

## Translation Details

**Anthropic → OpenAI:**
- `system` → prepended as `{"role": "system"}`
- `tool_use` blocks → `tool_calls` array
- `tool_result` blocks → `{"role": "tool"}`
- `thinking` blocks preserved as `reasoning_content`

**OpenAI → Anthropic:**
- `reasoning_content` → `{"type": "thinking"}`
- `tool_calls` → `{"type": "tool_use"}`
- `content` → `{"type": "text"}`
- `stop_reason` mapping: `stop`→`end_turn`, `length`→`max_tokens`, `tool_calls`→`tool_use`

# QwenMode

OpenAI-compatible API server that routes requests through **chat.qwen.ai** (Qwen3.8-Max-Preview) using Playwright headless browser automation.

## How It Works

QwenMode launches a headless Chromium browser pointing to chat.qwen.ai, intercepts SSE responses from the `/api/v2/chat/completions` endpoint, parses them into OpenAI-compatible responses, and supports tool calls with robust JSON extraction including snake_case-to-camelCase mapping.

```
Client (opencode, curl, etc.) -> QwenMode :5002 -> Playwright -> chat.qwen.ai
```

## Requirements

- Python 3.10+
- Chromium (installed by Playwright)

## Installation

```bash
# 1. Clone
git clone https://github.com/LeeAli7/qwenmode.git
cd qwenmode

# 2. Create virtual environment (optional but recommended)
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install Playwright Chromium
playwright install chromium
```

## Usage

```bash
# Default port 5002
python qwenmode.py --server

# Custom port
python qwenmode.py --server 5002
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/chat/completions` | OpenAI-compatible chat completion |
| GET | `/v1/models` | List available models |
| GET | `/health` | Health check |
| GET | `/debug` | Browser page DOM diagnostics |
| GET | `/log` | Last request details |

### Quick test

```bash
curl http://127.0.0.1:5002/health
curl http://127.0.0.1:5002/v1/models

curl http://127.0.0.1:5002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen3.8-Max-Preview",
    "messages": [{"role": "user", "content": "Say hello"}]
  }'
```

### With tool calls

```bash
curl http://127.0.0.1:5002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen3.8-Max-Preview",
    "messages": [{"role": "user", "content": "Read file /etc/hostname"}],
    "tools": [{
      "function": {
        "name": "read",
        "description": "Read a file from disk",
        "parameters": {
          "type": "object",
          "properties": {
            "filePath": {"type": "string"}
          },
          "required": ["filePath"]
        }
      }
    }]
  }'
```

## Configuration

All settings are read from environment variables (`QWENMODE_*`), with `qwenmode.py` defaults:

| Variable | Default | Description |
|----------|---------|-------------|
| `QWENMODE_URL` | `https://chat.qwen.ai` | Target chat URL |
| `QWENMODE_API_KEY` | (empty) | If set, requests must send it via `Authorization: Bearer` |
| `QWENMODE_POOL_SIZE` | `1` | Number of concurrent browser pages |
| `QWENMODE_SSE_TIMEOUT` | `90` | SSE response wait timeout (seconds) |
| `QWENMODE_EMPTY_NO_SSE` | `30` | Abort as EMPTY if no SSE POST observed within Ns (silent dead send) |
| `QWENMODE_EMPTY_RETRY_BUDGET` | `180` | Total retry budget (s) for EMPTY/dead-send attempts; `0` disables retries |
| `QWENMODE_GUEST_ROTATE_EVERY` | `3` | Rotate to a fresh guest profile every N requests |
| `QWENMODE_GUEST_COOLDOWN` | `5` | Pause (s) before reusing a rotated page |
| `QWENMODE_NO_PAGE_TIMEOUT` | `20` | Max wait (s) for an idle page before failing |

## Guest Mode

By default QwenMode runs in **guest mode**: it rotates through fresh incognito guest profiles (default every 3 requests) so daily rate limits and auth prompts are avoided. No `cookies.json` is needed anymore. For an authenticated persistent session, set `QWENMODE_GUEST_MODE=0` and place a `cookies.json` export locally (file is git-ignored).

## opencode Integration

Add to `~/.config/opencode/opencode.json`:

```json
{
  "provider": {
    "qwenmode": {
      "name": "QwenMode",
      "npm": "@ai-sdk/openai-compatible",
      "env": ["QWENMODE_API_KEY"],
      "options": {
        "apiKey": "public",
        "baseURL": "http://127.0.0.1:5002/v1",
        "timeout": 300000
      },
      "models": {
        "Qwen3.8-Max-Preview": {
          "id": "Qwen3.8-Max-Preview",
          "name": "Qwen3.8-Max-Preview",
          "tool_call": true,
          "reasoning": true,
          "limit": { "context": 128000, "output": 4096 },
          "cost": { "input": 0, "output": 0 }
        }
      }
    }
  }
}
```

## Linux Service (systemd)

Create `/etc/systemd/system/qwenmode.service`:

```ini
[Unit]
Description=QwenMode API Server
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/qwenmode
ExecStart=/path/to/qwenmode/venv/bin/python qwenmode.py --server 5002
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now qwenmode
sudo systemctl status qwenmode
```

## Notes

- The browser profile directory is cleaned on each restart for a fresh session
- Guest mode rotates fresh incognito profiles to dodge daily rate limits and login prompts
- EMPTY/silent-dead-send detections abort fast and retry within `QWENMODE_EMPTY_RETRY_BUDGET`
- Quota/overload ("high demand") errors rotate to a fresh guest instead of retrying the same page
- Tool call responses support snake_case (`file_path`, `old_text`) and camelCase parameter names

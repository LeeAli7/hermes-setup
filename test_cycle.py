#!/usr/bin/env python3
"""Reproduce full pi/opencode cycle: write -> bash -> final answer."""
import json, time, urllib.request

URL = "http://127.0.0.1:5002/v1/chat/completions"
TOOLS = [
    {"type": "function", "function": {"name": "write", "description": "Write a file to disk", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "bash", "description": "Run a shell command", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
]

def call(messages, tools=None, label=""):
    body = {"model": "Qwen3.8-Max-Preview", "messages": messages}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=150) as r:
        data = json.loads(r.read())
    dt = time.time() - t0
    msg = data["choices"][0]["message"]
    print(f"--- {label} ({dt:.1f}s) ---")
    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            print(f"TOOL_CALL: {tc['function']['name']}({tc['function']['arguments']})")
    else:
        print(f"TEXT: {(msg.get('content') or '')[:200]!r}")
    return msg

# messages history accumulates like a real agent loop
history = [
    {"role": "system", "content": "Working directory: /tmp/oc_cycle. You are a coding agent."},
    {"role": "user", "content": "Создай файл calc.py, который умножает два числа из аргументов."},
]
m1 = call(history, TOOLS, "1. write calc.py")
history.append({"role": "assistant", "content": None, "tool_calls": m1.get("tool_calls")})
history.append({"role": "tool", "name": "write", "content": "OK: created calc.py (86 bytes)"})

m2 = call(history, TOOLS, "2. bash python3 calc.py 5 6")
history.append({"role": "assistant", "content": None, "tool_calls": m2.get("tool_calls")})
history.append({"role": "tool", "name": "bash", "content": "30.0"})

m3 = call(history, TOOLS, "3. final summary")
print("\nDONE. m3 tool_calls:", bool(m3.get("tool_calls")))

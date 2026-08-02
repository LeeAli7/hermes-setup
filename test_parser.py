#!/usr/bin/env python3
"""Unit tests for the tolerant tool-call parser."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qwenmode import _extract_all_tool_calls, _normalize_args

cases = [
    # (name, input_text, expected [(tool, args)...])
    ("canonical", '{"tool": "write", "arguments": {"path": "a.py", "content": "x"}}',
     [("write", {"path": "a.py", "content": "x"})]),
    ("renamed args key", '{"tool": "bash", "args": {"command": "ls"}}',
     [("bash", {"command": "ls"})]),
    ("renamed params key", '{"tool": "read", "params": {"path": "/etc/hostname"}}',
     [("read", {"path": "/etc/hostname"})]),
    ("arguments as json string", '{"tool": "bash", "arguments": "{\\"command\\": \\"ls -la\\"}"}',
     [("bash", {"command": "ls -la"})]),
    ("openai shape", '{"function": {"name": "write", "arguments": {"path": "x"}}}',
     [("write", {"path": "x"})]),
    ("bare name key", '{"name": "edit", "arguments": {"path": "x", "oldString": "a", "newString": "b"}}',
     [("edit", {"path": "x", "oldString": "a", "newString": "b"})]),
    ("tool_call wrapper", '{"tool_call": {"name": "bash", "arguments": {"command": "pwd"}}}',
     [("bash", {"command": "pwd"})]),
    ("tool as object", '{"tool": {"name": "write"}, "content": "hi"}',
     [("write", {})]),
    ("two calls", '{"tool": "write", "arguments": {"path": "a"}}\n{"tool": "bash", "arguments": {"command": "ls"}}',
     [("write", {"path": "a"}), ("bash", {"command": "ls"})]),
    ("code block", '```json\n{"tool": "bash", "arguments": {"command": "ls"}}\n```',
     [("bash", {"command": "ls"})]),
    ("array in block", '```json\n[{"tool": "read", "arguments": {"path": "a"}}, {"tool": "read", "arguments": {"path": "b"}}]\n```',
     [("read", {"path": "a"}), ("read", {"path": "b"})]),
    ("prefixed name", '{"tool": "functions.bash", "arguments": {"command": "ls"}}',
     [("bash", {"command": "ls"})]),
    ("marker prefix", '[Assistant tool call]\n{"tool": "write", "arguments": {"path": "a.py", "content": "print(1)"}}',
     [("write", {"path": "a.py", "content": "print(1)"})]),
    ("args as single list", '{"tool": "bash", "arguments": [{"command": "ls"}]}',
     [("bash", {"command": "ls"})]),
    ("explanation + call", 'Сначала прочитаю файл.\n{"tool": "read", "arguments": {"path": "x"}}.\nПотом отредактирую.',
     [("read", {"path": "x"})]),
    ("no tool call", "Просто текстовый ответ без тулов.",
     []),
    ("arguments string with nested json", '{"tool": "edit", "arguments": "{\\"path\\": \\"a\\", \\"edits\\": [{\\"oldText\\": \\"x\\", \\"newText\\": \\"y\\"}]}"}',
     [("edit", {"path": "a", "edits": [{"oldText": "x", "newText": "y"}]})]),
]

failures = 0
for label, text, expected in cases:
    got = _extract_all_tool_calls(text)
    if got != expected:
        failures += 1
        print(f"FAIL [{label}]:\n  got      {got}\n  expected {expected}")
    else:
        print(f"ok   [{label}] -> {[(n, list(a.keys())) for n, a in got]}")

# Normalization with client schema
schema = {"write": {"path", "content"}, "bash": {"command"}}
norm = _normalize_args("bash", {"cmd": "ls"}, schema.get("bash"))
assert norm == {"command": "ls"}, f"norm failed: {norm}"
norm2 = _normalize_args("write", {"filePath": "a", "data": "x"}, schema.get("write"))
assert norm2 == {"path": "a", "content": "x"}, f"norm2 failed: {norm2}"
print("ok   [normalize args with schema]")

print(f"\n{len(cases)} cases, {failures} failures")
sys.exit(1 if failures else 0)

#!/usr/bin/env python3
"""Test _repair_json on broken JSON with unescaped quotes in HTML content."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qwenmode import _repair_json, _extract_all_tool_calls

# Case 1: HTML with unescaped lang="ru" and CSS braces — EXACT user scenario
html = '''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Тест</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#F4F4F0;color:#101010}
</style>
</head>
<body>
<div class="cursor" data-x="a,b"></div>
</body>
</html>'''
broken = '{"tool": "write", "arguments": {"path": "/home/ali/designer-site/index.html", "content": "' + html + '"}}'

r = _repair_json(broken)
print("=== repair whole broken object ===")
if r:
    print(f"name={r.get('tool')!r}")
    args = r.get("arguments", {})
    print(f"path={args.get('path')!r}")
    content = args.get("content", "")
    print(f"content_len={len(content)} head={content[:60]!r}")
    assert r["tool"] == "write"
    assert args["path"] == "/home/ali/designer-site/index.html"
    assert 'lang="ru"' in content, "content lost quotes!"
    assert "{margin:0;padding:0" in content
    print("PASS: broken write extracted, content intact")
else:
    print("FAIL: repair returned None")
    sys.exit(1)

# Case 2: through _extract_all_tool_calls (whole text)
tcs = _extract_all_tool_calls(broken)
print("=== extract_all_tool_calls ===")
print(f"tool_calls={[(n, list(a.keys())) for n, a in tcs]}")
assert tcs and tcs[0][0] == "write"
print("PASS: extract found write")

# Case 3: valid JSON still works
valid = '{"tool": "bash", "arguments": {"command": "ls -la"}}'
tcs = _extract_all_tool_calls(valid)
assert tcs == [("bash", {"command": "ls -la"})]
print("PASS: valid JSON unaffected")

# Case 4: write with properly escaped content (what Qwen SHOULD emit)
escaped_html = html.replace('"', '\\"')
proper = '{"tool": "write", "arguments": {"path": "/x/index.html", "content": "' + escaped_html + '"}}'
tcs = _extract_all_tool_calls(proper)
assert tcs and tcs[0][0] == "write"
print("PASS: properly-escaped write extracted")

# Case 5: broken but small (lang="ru" only)
small = '{"tool": "write", "arguments": {"path": "a.html", "content": "<p lang="ru">привет</p>"}}'
r = _repair_json(small)
print("=== small broken ===", r["tool"] if r else None, (r or {}).get("arguments", {}).get("content", "")[:30])
assert r and r["tool"] == "write" and '<p lang="ru">' in r["arguments"]["content"]
print("PASS: small broken fixed")

print("\nALL PASS")

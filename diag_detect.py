#!/usr/bin/env python3
"""Replicate detect-incognito Chrome test: IndexedDB durability strict + write speed."""
import asyncio, os, json
os.environ.setdefault("QWENMODE_GUEST_MODE", "1")
from playwright.async_api import async_playwright

URL = "https://chat.qwen.ai"
_LAUNCH_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage", "--disable-gpu",
]

async def main():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/qwenmode_di1", headless=True,
        args=_LAUNCH_ARGS, ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        locale="en-US", timezone_id="Europe/Helsinki",
    )
    page = await ctx.new_page()
    await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(2)

    # Replicate the detect-incognito Chrome branch
    result = await page.evaluate("""async () => {
        const out = {};
        // a() helper
        function a() {
            var e = 0, t = parseInt("-1");
            try { t.toFixed(t); } catch (n) { e = n.message.length; }
            return e;
        }
        out.a = a();  // 51 for Chrome
        // Chrome branch: IndexedDB durability strict + write timing
        var t = "__di_" + Math.random().toString(36).slice(2),
            n = new Uint8Array(16384),
            s = indexedDB.open(t, 1);
        const p = new Promise((resolve) => {
            s.onupgradeneeded = function() { s.result.createObjectStore("s"); };
            s.onerror = function() { indexedDB.deleteDatabase(t); resolve({error: "onerror"}); };
            s.onsuccess = function() {
                var db = s.result, o = false;
                try {
                    var tx = db.transaction("s", "readwrite", {durability: "strict"});
                    o = "strict" === tx.durability;
                    tx.abort();
                } catch (c) { out.txErr = String(c); }
                if (!o) { db.close(); indexedDB.deleteDatabase(t); resolve({strict: false}); return; }
                var l = function(e) {
                    return new Promise(function(res, rej) {
                        var st = performance.now(), cnt = 0;
                        var r = function() {
                            if (15 !== cnt) {
                                var tr = db.transaction("s", "readwrite", {durability: e});
                                tr.objectStore("s").put(n, cnt);
                                cnt++;
                                tr.oncomplete = r;
                                tr.onerror = tr.onabort = function() { rej(tr.error); };
                            } else res(performance.now() - st);
                        };
                        r();
                    });
                };
                l("strict").then(function(dur) {
                    out.writeTimeMs = dur;
                    db.close();
                    indexedDB.deleteDatabase(t);
                    resolve({strict: true, writeTimeMs: dur});
                }).catch(function(e) {
                    out.writeErr = String(e);
                    db.close(); indexedDB.deleteDatabase(t);
                    resolve({strict: true, writeErr: String(e)});
                });
            };
        });
        const r = await p;
        out.result = r;
        return out;
    }""")
    print(json.dumps(result, ensure_ascii=False, indent=1)[:1200])
    await ctx.close()
    await p.stop()

asyncio.run(main())

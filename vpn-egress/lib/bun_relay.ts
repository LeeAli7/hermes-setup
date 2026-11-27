// Origin relay v2: Bun TLS + CLI-like trace linkage (models fetch then completion).
const UPSTREAM = "https://opencode.ai";
const MODELS = "https://models.opencode.ai/api.json";
const PORT = Number(process.env.RELAY_PORT || 9100);
const UA = "opencode/latest/2.3.0/cli";
const DEBUG = process.env.HERMES_RELAY_DEBUG === "1";

const hex = (n: number) =>
  Array.from({ length: n }, () => "0123456789abcdef"[Math.floor(Math.random() * 16)]).join("");

let lastWarm = 0;
let warmInFlight: Promise<void> | null = null;

async function warm(trace: string) {
  if (Date.now() - lastWarm < 0) return;
  if (warmInFlight) return warmInFlight;
  warmInFlight = (async () => {
    const span = hex(16);
    try {
      await fetch(MODELS, {
        headers: {
          "User-Agent": UA,
          b3: `${trace}-${span}-1-${hex(16)}`,
          traceparent: `00-${trace}-${span}-01`,
          Accept: "*/*",
          "Accept-Encoding": "gzip, deflate, br, zstd",
        },
      });
      lastWarm = Date.now();
      console.log("warm api.json ok trace=" + trace.slice(0, 8));
    } catch (e) {
      console.log("warm failed: " + String(e));
    } finally {
      warmInFlight = null;
    }
  })();
  return warmInFlight;
}

Bun.serve({
  port: PORT,
  hostname: "127.0.0.1",
  idleTimeout: 0,
  async fetch(req) {
    const url = new URL(req.url);
    if (!url.pathname.startsWith("/inference/") && !url.pathname.startsWith("/zen/")) {
      return new Response("relay: bad path\n", { status: 400 });
    }
    const target = UPSTREAM + url.pathname + url.search;
    if (DEBUG) {
      try { await Bun.write("/tmp/opencode/relay_in.log", JSON.stringify({path:url.pathname+url.search, method:req.method, headers:[...req.headers.entries()]})+"\n", {createPath:true}); } catch {}
    }
    const headers = new Headers();
    for (const [k, v] of req.headers.entries()) {
      const lk = k.toLowerCase();
      if (lk === "host" || lk === "connection" || lk === "content-length") continue;
      headers.set(k, v);
    }
    // CLI-like trace: models fetch and completion share one trace id.
    const incoming = headers.get("traceparent") || "";
    const m = incoming.match(/^00-([0-9a-f]{32})-/);
    const trace = m ? m[1] : hex(32);
    await warm(trace);
    const span = hex(16);
    headers.set("b3", `${trace}-${span}-1-${hex(16)}`);
    headers.set("traceparent", `00-${trace}-${span}-01`);
    const method = req.method.toUpperCase();
    const body = method === "GET" || method === "HEAD" ? undefined : await req.arrayBuffer();
    if (body && DEBUG) { try { await Bun.write("/tmp/opencode/relay_body_last.bin", body); } catch {} }
    if (DEBUG) try { await Bun.write("/tmp/opencode/relay_out.log", JSON.stringify({headers:[...headers.entries()], method, target, bodyLen: body ? body.byteLength : 0})+"\n", {createPath:true}); } catch {}
    let up;
    try {
      headers.set("connection", "close");
      up = await fetch(target, { method, headers, body, redirect: "manual", keepalive: false });
    } catch (e) {
      return new Response("relay upstream error: " + String(e), { status: 502 });
    }
    const outHeaders = new Headers();
    for (const [k, v] of up.headers.entries()) {
      const lk = k.toLowerCase();
      if (lk === "content-encoding" || lk === "content-length" || lk === "transfer-encoding") continue;
      outHeaders.set(k, v);
    }
    return new Response(up.body, { status: up.status, headers: outHeaders });
  },
});

console.log("bun relay v2 on 127.0.0.1:" + PORT);

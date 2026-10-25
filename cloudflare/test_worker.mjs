// Run: node cloudflare/test_worker.mjs   (also executed by backend/tests/test_cloudflare_worker.py)
import fs from "node:fs";
import assert from "node:assert/strict";

const src = fs.readFileSync(new URL("./maintenance-worker.js", import.meta.url), "utf8");
const worker = (await import("data:text/javascript;base64," + Buffer.from(src).toString("base64"))).default;

let seen;           // last request the Worker sent to the "origin"
let originImpl;     // what the fake origin does
globalThis.fetch = async (req) => { seen = req; return originImpl(req); };

const html = { Accept: "text/html,application/xhtml+xml" };
const call = (url = "https://wazibothq.com/", init = {}, env = {}) =>
  worker.fetch(new Request(url, init), env);

let passed = 0;
const t = async (name, fn) => { await fn(); passed++; console.log("ok -", name); };

await t("outage 502 from gateway + page load -> maintenance page (503)", async () => {
  originImpl = () => new Response("Bad Gateway", { status: 502 });
  const r = await call("https://wazibothq.com/site/x", { headers: html });
  assert.equal(r.status, 503);
  assert.match(await r.text(), /working on it/);
  assert.equal(r.headers.get("cache-control"), "no-store");
});

await t("outage on API/JSON call passes through untouched", async () => {
  originImpl = () => new Response("Bad Gateway", { status: 502 });
  const r = await call("https://wazibothq.com/orders", { headers: { Accept: "application/json" } });
  assert.equal(r.status, 502);
});

await t("deliberate app 503 (has X-WZ-App) is not replaced", async () => {
  originImpl = () => new Response("paused", { status: 503, headers: { "X-WZ-App": "1" } });
  const r = await call("https://wazibothq.com/site/x", { headers: html });
  assert.equal(await r.text(), "paused");
});

await t("origin unreachable (fetch throws): page -> maintenance, API -> JSON 503", async () => {
  originImpl = () => { throw new TypeError("network"); };
  const page = await call("https://wazibothq.com/", { headers: html });
  assert.equal(page.status, 503); assert.match(await page.text(), /technicians/);
  const api = await call("https://wazibothq.com/me", { headers: { Accept: "application/json" } });
  assert.equal(api.status, 503); assert.equal((await api.json()).detail, "Service temporarily unavailable");
});

await t("healthy responses and POSTs pass through; webhooks never replaced", async () => {
  originImpl = () => new Response("fine", { status: 200 });
  assert.equal(await (await call("https://wazibothq.com/", { headers: html })).text(), "fine");
  originImpl = () => new Response("down", { status: 502 });
  const r = await call("https://wazibothq.com/webhook", { method: "POST", body: "{}", headers: html });
  assert.equal(r.status, 502);
});

await t("websocket upgrade is not replaced", async () => {
  originImpl = () => new Response("down", { status: 502 });
  const r = await call("https://wazibothq.com/ws/chat/1", { headers: { ...html, Upgrade: "websocket" } });
  assert.equal(r.status, 502);
});

await t("edge secret: client-sent X-WZ-Edge stripped; ours added only when configured", async () => {
  originImpl = () => new Response("ok");
  await call("https://wazibothq.com/", { headers: { "X-WZ-Edge": "forged" } }, {});
  assert.equal(seen.headers.get("x-wz-edge"), null);
  await call("https://wazibothq.com/", { headers: { "X-WZ-Edge": "forged" } }, { EDGE_SHARED_SECRET: "s3cret" });
  assert.equal(seen.headers.get("x-wz-edge"), "s3cret");
});

await t("redirects are passed to the browser, not followed", async () => {
  originImpl = () => new Response(null, { status: 302, headers: { Location: "/x" } });
  const r = await call("https://wazibothq.com/a", { headers: html });
  assert.equal(r.status, 302); assert.equal(seen.redirect, "manual");
});

await t("?wz_maintenance_test=1 previews the page without touching the origin", async () => {
  seen = null;
  const r = await call("https://wazibothq.com/?wz_maintenance_test=1", { headers: html });
  assert.equal(r.status, 503); assert.equal(seen, null);
});

console.log(`\n${passed} worker tests passed`);

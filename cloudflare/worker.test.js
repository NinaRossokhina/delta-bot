// Tests of worker.js with stubbed Telegram and GitHub (no network). Run: node --test cloudflare/worker.test.js
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { tick, nowMsk } from "./worker.js";

const env = { TELEGRAM_BOT_TOKEN: "tg", GITHUB_TOKEN: "gh" };
const MIN = 60 * 1000;

// updates: Telegram result; queue: queue.json; runs: poll.yml runs (newest first)
function stub({ updates = [], queue = [], runs = [], tgOk = true, dispatchStatus = 204 } = {}) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url, method: init.method || "GET", headers: init.headers || {} });
    const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
    if (url.startsWith("https://api.telegram.org/"))
      return tgOk ? json({ ok: true, result: updates }) : json({ ok: false, description: "Unauthorized" }, 401);
    if (url.includes("/contents/queue.json")) return json(queue);
    if (url.includes("/runs?")) return json({ workflow_runs: runs });
    if (url.endsWith("/dispatches")) return new Response(null, { status: dispatchStatus });
    throw new Error("unexpected " + url);
  };
  return calls;
}
const dispatched = calls => calls.some(c => c.url.endsWith("/actions/workflows/poll.yml/dispatches") && c.method === "POST");
const ago = m => new Date(Date.now() - m * MIN).toISOString();

test("nothing to do: idle, no dispatch", async () => {
  const calls = stub({ queue: [{ msg: 1, at: "2999-01-01T09:00" }] });
  assert.equal(await tick(env), "idle");
  assert.ok(!dispatched(calls));
});

test("new message starts poll.yml on main", async () => {
  const calls = stub({ updates: [{ update_id: 5 }] });
  assert.equal(await tick(env), "new messages: started");
  const d = calls.find(c => c.url.endsWith("/dispatches"));
  assert.equal(d.headers.Authorization, "Bearer gh");
});

test("only peeks at Telegram: no offset and no allowed_updates", async () => {
  const calls = stub({ updates: [{ update_id: 5 }] });
  await tick(env);
  const tg = calls.find(c => c.url.startsWith("https://api.telegram.org/"));
  assert.ok(tg.url.includes("/getUpdates?"));
  assert.ok(!tg.url.includes("offset") && !tg.url.includes("allowed_updates"));
});

test("due post starts poll.yml, future post does not", async () => {
  const past = nowMsk(Date.now() - MIN), future = nowMsk(Date.now() + 10 * MIN);
  let calls = stub({ queue: [{ msg: 1, at: future }, { msg: 2, at: past }] });
  assert.equal(await tick(env), "post due: started");
  calls = stub({ queue: [{ msg: 1, at: future }] });
  assert.equal(await tick(env), "idle");
  assert.ok(!dispatched(calls));
});

test("Moscow time is UTC+3", () => {
  assert.equal(nowMsk(Date.parse("2026-10-01T21:59:30Z")), "2026-10-02T00:59");
});

test("no second run while one is queued or running", async () => {
  const calls = stub({ updates: [{}], runs: [{ status: "queued" }] });
  assert.equal(await tick(env), "new messages: already running");
  assert.ok(!dispatched(calls));
});

test("after a failed run waits a few minutes before retrying", async () => {
  let calls = stub({ updates: [{}], runs: [{ status: "completed", conclusion: "failure", updated_at: ago(2) }] });
  assert.equal(await tick(env), "new messages: last run failed, waiting");
  assert.ok(!dispatched(calls));
  calls = stub({ updates: [{}], runs: [{ status: "completed", conclusion: "failure", updated_at: ago(6) }] });
  assert.equal(await tick(env), "new messages: started");
});

test("errors are reported without the tokens", async () => {
  stub({ tgOk: false });
  const res = await tick(env);
  assert.match(res, /^error: Telegram: 401/);
  assert.ok(!res.includes("bottg"));
  stub({ updates: [{}], dispatchStatus: 403 });
  assert.match(await tick(env), /^error: dispatch failed 403/);
  assert.match(await tick({}), /secrets/);
});

test("a network error that mentions the token does not show it", async () => {
  globalThis.fetch = async url => { throw new Error(`fetch failed: ${url}`); };
  const res = await tick({ TELEGRAM_BOT_TOKEN: "123:secret", GITHUB_TOKEN: "ghp_x" });
  assert.match(res, /^error: fetch failed/);
  assert.ok(!res.includes("123:secret"));
});

test("opening the URL shows the result", async () => {
  stub();
  const r = await worker.fetch(new Request("https://x/"), env);
  assert.equal(await r.text(), "idle\n");
});

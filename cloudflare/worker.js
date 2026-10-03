// Delta bot "doorbell" for Cloudflare Workers (free plan).
// Every minute it checks whether Nina wrote to the bot / pressed a button, or a scheduled
// post is due, and if so starts the "Publish approved posts" workflow (poll.yml) right away.
// Every 5 minutes it also starts "Hot news" (hot.yml), the search for breaking AI news.
// Setup for beginners: cloudflare/README.md.
// Secrets (Settings -> Variables and Secrets): TELEGRAM_BOT_TOKEN, GITHUB_TOKEN.
// Trigger (Settings -> Trigger events -> Cron Triggers): * * * * *

const REPO = "NinaRossokhina/delta-bot";
const GH = { "Accept": "application/vnd.github+json", "User-Agent": "delta-bot-worker" };
const RETRY_AFTER_FAILURE_MIN = 5; // a failed poll run is retried no more often than this

async function github(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${REPO}${path}`, {
    ...init, headers: { ...GH, "Authorization": `Bearer ${env.GITHUB_TOKEN}`, ...(init.headers || {}) },
  });
}

// Only peeks: no offset, so nothing is confirmed and poll.py still gets every update.
// No allowed_updates either: Telegram would remember it and poll.py sets its own.
export async function hasUpdates(env) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getUpdates?timeout=0&limit=1`);
  const data = await r.json().catch(() => ({}));
  if (!data.ok) throw new Error(`Telegram: ${r.status} ${data.description || ""}`);
  return data.result.length > 0;
}

export function nowMsk(ms = Date.now()) {
  return new Date(ms + 3 * 3600 * 1000).toISOString().slice(0, 16); // "YYYY-MM-DDTHH:MM", as in queue.json
}

export async function postDue(env, now = nowMsk()) {
  const r = await github(env, "/contents/queue.json?ref=main", { headers: { "Accept": "application/vnd.github.raw+json" } });
  if (!r.ok) throw new Error(`queue.json: ${r.status}`);
  const queue = await r.json();
  return queue.some(item => item.at <= now);
}

// Why not to start poll.yml now: it is already queued/running, or the last run failed
// a moment ago (otherwise a crash on some update would restart it every minute).
export async function busyReason(env, ms = Date.now()) {
  const r = await github(env, "/actions/workflows/poll.yml/runs?per_page=5");
  if (!r.ok) throw new Error(`runs: ${r.status}`);
  const runs = (await r.json()).workflow_runs || [];
  if (runs.some(run => run.status !== "completed")) return "already running";
  const last = runs[0];
  if (last && last.conclusion === "failure" &&
      ms - Date.parse(last.updated_at) < RETRY_AFTER_FAILURE_MIN * 60 * 1000) return "last run failed, waiting";
  return null;
}

// Error texts never show the tokens, even if some error message happens to contain a URL.
export function redact(text, env) {
  let out = String(text);
  for (const secret of [env.TELEGRAM_BOT_TOKEN, env.GITHUB_TOKEN]) if (secret) out = out.split(secret).join("***");
  return out;
}

export async function tick(env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.GITHUB_TOKEN) return "error: secrets TELEGRAM_BOT_TOKEN / GITHUB_TOKEN are not set";
  try {
    const why = (await hasUpdates(env)) ? "new messages" : (await postDue(env)) ? "post due" : null;
    if (!why) return "idle";
    const busy = await busyReason(env);
    if (busy) return `${why}: ${busy}`;
    const r = await github(env, "/actions/workflows/poll.yml/dispatches", {
      method: "POST", body: JSON.stringify({ ref: "main" }),
    });
    return r.ok ? `${why}: started` : redact(`error: dispatch failed ${r.status} ${await r.text()}`, env);
  } catch (e) {
    return redact(`error: ${e.message}`, env);
  }
}

// Hot news (hot.yml) every HOT_EVERY_MIN minutes: GitHub's own schedule is often late by 10-20 minutes.
// Returns null when it is not the time.
export const HOT_EVERY_MIN = 5;

export async function hotTick(env, ms = Date.now()) {
  if (!env.GITHUB_TOKEN || new Date(ms).getUTCMinutes() % HOT_EVERY_MIN !== 0) return null;
  try {
    const r = await github(env, "/actions/workflows/hot.yml/dispatches", {
      method: "POST", body: JSON.stringify({ ref: "main" }),
    });
    return r.ok ? "hot news: started" : redact(`hot news: error ${r.status} ${await r.text()}`, env);
  } catch (e) {
    return redact(`hot news: error ${e.message}`, env);
  }
}

// The day's drafts (ai.yml) on time: GitHub's own schedule for them was hours late or skipped.
// UTC hour -> part: 16:00 UTC = 21:00 Yekaterinburg (19:00 Moscow), tomorrow's posts.
// "scheduled" makes ai.py skip drafts that are already there, so GitHub's late run adds nothing twice.
export const DAILY = { 16: "next" };

export async function dailyTick(env, ms = Date.now()) {
  const at = new Date(ms), part = DAILY[at.getUTCHours()];
  if (!env.GITHUB_TOKEN || !part || at.getUTCMinutes() !== 0) return null;
  try {
    const task = JSON.stringify({ kind: "daily", part, scheduled: true });
    const r = await github(env, "/actions/workflows/ai.yml/dispatches", {
      method: "POST", body: JSON.stringify({ ref: "main", inputs: { task } }),
    });
    return r.ok ? `drafts (${part}): started` : redact(`drafts (${part}): error ${r.status} ${await r.text()}`, env);
  } catch (e) {
    return redact(`drafts (${part}): error ${e.message}`, env);
  }
}

export default {
  async scheduled(event, env, ctx) {
    console.log(await tick(env));
    const hot = await hotTick(env, event.scheduledTime);
    if (hot) console.log(hot);
    const daily = await dailyTick(env, event.scheduledTime);
    if (daily) console.log(daily);
  },
  // Opening the worker's URL runs one check and shows the result (handy for testing).
  async fetch(request, env) {
    return new Response(await tick(env) + "\n", { headers: { "Content-Type": "text/plain; charset=utf-8" } });
  },
};

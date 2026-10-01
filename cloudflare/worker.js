// Delta bot "doorbell" for Cloudflare Workers (free plan).
// Every minute it checks whether Nina wrote to the bot / pressed a button, or a scheduled
// post is due, and if so starts the "Publish approved posts" GitHub workflow right away.
// Secrets (Settings -> Variables and Secrets): TELEGRAM_BOT_TOKEN, GITHUB_TOKEN.
// Trigger (Settings -> Triggers -> Cron Triggers): * * * * *

const REPO = "NinaRossokhina/delta-bot";
const GH = { "Accept": "application/vnd.github+json", "User-Agent": "delta-bot-worker" };

async function github(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${REPO}${path}`, {
    ...init, headers: { ...GH, "Authorization": `Bearer ${env.GITHUB_TOKEN}`, ...(init.headers || {}) },
  });
}

async function hasUpdates(env) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getUpdates?timeout=0&limit=1`);
  const data = await r.json();
  return data.ok && data.result.length > 0;
}

async function postDue(env) {
  const r = await github(env, "/contents/queue.json", { headers: { "Accept": "application/vnd.github.raw+json" } });
  if (!r.ok) return false;
  const queue = await r.json();
  const now = new Date(Date.now() + 3 * 3600 * 1000).toISOString().slice(0, 16); // Moscow time
  return queue.some(item => item.at <= now);
}

async function pollRunning(env) {
  for (const status of ["in_progress", "queued"]) {
    const r = await github(env, `/actions/workflows/poll.yml/runs?status=${status}&per_page=1`);
    if (r.ok && (await r.json()).total_count > 0) return true;
  }
  return false;
}

async function tick(env) {
  if (!(await hasUpdates(env)) && !(await postDue(env))) return "idle";
  if (await pollRunning(env)) return "already running";
  const r = await github(env, "/actions/workflows/poll.yml/dispatches", {
    method: "POST", body: JSON.stringify({ ref: "main" }),
  });
  return r.ok ? "started" : `dispatch failed: ${r.status} ${await r.text()}`;
}

export default {
  async scheduled(event, env, ctx) {
    console.log(await tick(env));
  },
  // Opening the worker's URL runs one check and shows the result (handy for testing).
  async fetch(request, env) {
    return new Response(await tick(env));
  },
};

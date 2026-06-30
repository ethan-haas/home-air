/**
 * Home_Air pinger — Cloudflare Worker.
 *
 * Cloudflare Cron Triggers ARE honored on schedule (unlike GitHub's throttled
 * scheduled Actions), so this fires the control loop reliably every 15 min by
 * calling GitHub's workflow_dispatch API. The GitHub token is an encrypted
 * Worker secret (GH_TOKEN), never in this code.
 *
 *   scheduled()  -> the cron trigger calls this every 15 min
 *   fetch()      -> visiting the worker URL triggers one run (manual test)
 */
export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },
  async fetch(request, env) {
    const r = await dispatch(env);
    const body = r.status === 204
      ? "OK — dispatched a control run.\n"
      : `GitHub returned ${r.status}: ${await r.text()}\n`;
    return new Response(body, { status: r.status === 204 ? 200 : 502 });
  },
};

async function dispatch(env) {
  const repo = env.REPO || "ethan-haas/home-air";
  const workflow = env.WORKFLOW || "control.yml";
  const ref = env.REF || "main";
  return fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        "Accept": "application/vnd.github+json",
        "Authorization": `Bearer ${env.GH_TOKEN}`,
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        // GitHub's API rejects requests with no User-Agent (403) — required.
        "User-Agent": "home-air-pinger",
      },
      body: JSON.stringify({ ref }),
    },
  );
}

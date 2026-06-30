# Home_Air pinger (Cloudflare Worker)

Fires the GitHub control loop **reliably every 15 min** via `workflow_dispatch`
(Cloudflare cron triggers are honored on schedule; GitHub's own scheduled
Actions are throttled to 30–90 min). The GitHub token lives as an encrypted
Worker secret — never in this code.

## One-time setup

### 1. GitHub token (fine-grained PAT)
- https://github.com/settings/tokens?type=beta → **Generate new token**
- Repository access → **Only select repositories** → `ethan-haas/home-air`
- Permissions → Repository → **Actions: Read and write**
- Generate, copy the `github_pat_…` value.

### 2. Deploy the Worker
```bash
npm install -g wrangler          # or use: npx wrangler ...
cd cloudflare-pinger
wrangler login                   # opens browser, authorize Cloudflare (free account)
wrangler secret put GH_TOKEN     # paste the github_pat_… token when prompted
wrangler deploy
```

`wrangler deploy` prints the Worker URL, e.g.
`https://home-air-pinger.<your-subdomain>.workers.dev`

### 3. Verify
- **Manual:** open the Worker URL in a browser → should say
  `OK — dispatched a control run.` and a run appears under the repo's Actions tab.
- **Cron:** `wrangler tail` streams logs; you'll see a scheduled invocation
  every 15 min. Or just watch the Actions tab — runs now arrive every 15 min.

## Notes
- The repo workflow has `concurrency: cancel-in-progress: false`, so the Worker's
  dispatch and GitHub's own (slow) schedule can't overlap — extra triggers queue.
- Change cadence: edit `crons` in `wrangler.toml` and `wrangler deploy` again.
- Rotate the token: re-run `wrangler secret put GH_TOKEN`.
- Cost: well within Cloudflare's free tier (cron + a few requests/day).

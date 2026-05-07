# Quote Spreadsheeter — Render Backend

Minimal proof-of-concept that wraps the `quote-spreadsheeter` skill behind an HTTP API. Users POST a plan list and uploaded files; the service calls the Claude API and returns a populated `.xlsx`.

## Project layout

```
quote-spreadsheeter-render/
├── app.py              Flask app — single endpoint
├── requirements.txt    Python deps
├── render.yaml         Render Blueprint (auto-config on deploy)
├── README.md           This file
└── skill/
    ├── SKILL.md        The skill body — used as the system prompt
    └── assets/
        └── Prost11_Medical_Comparison.xlsx
```

## What it does

`POST /api/spreadsheet-quotes` accepts:
- `plans` (form field): comma-separated plan names
- `documents` (form files): one or more carrier quote files + the census

It loads `skill/SKILL.md` as the system prompt, attaches the template + uploaded files as document blocks, calls Claude with code execution enabled, and returns the populated spreadsheet as a download.

`GET /` renders a basic HTML form for manual testing — replace with your real frontend later.

`GET /healthz` is a health check for Render's load balancer.

## Deploy to Render — first time

1. **Get the code on GitHub.** Create a new repo and push this folder. Render deploys from Git.

2. **Get a Claude API key.** Go to console.anthropic.com → API Keys → create one. Copy it.

3. **Connect Render to the repo.**
   - Sign in at render.com.
   - Click **New** → **Blueprint**.
   - Select your GitHub repo. Render reads `render.yaml` and previews the service it'll create.
   - Click **Apply**.

4. **Set environment variables** in the Render dashboard for the new service:
   - `ANTHROPIC_API_KEY` — paste your key
   - `SHARED_SECRET` — pick any random string (this gates access to the API; clients must send it as `X-Shared-Secret` header or `secret` form field). Leave unset to disable auth during early testing.

5. **Wait for the build.** First build takes 2–4 minutes. When it's green, click the URL Render gives you (`your-app.onrender.com`) and you'll see the test form.

## Test it

**Easiest:** open the URL in a browser, fill in the form, upload sample files, click Generate.

**With curl:**
```bash
curl -X POST https://your-app.onrender.com/api/spreadsheet-quotes \
  -H "X-Shared-Secret: your-secret-here" \
  -F "plans=HMO Gold, PPO Silver, HDHP" \
  -F "documents=@/path/to/quote1.pdf" \
  -F "documents=@/path/to/quote2.pdf" \
  -F "documents=@/path/to/census.xlsx" \
  --output result.xlsx
```

Expect 1–3 minutes per call. PDF-heavy runs are slower.

## Things to know

**Cost.** Each call uses Claude API credits. PDF processing is the main driver — a typical run with 2–3 quote PDFs and a census might cost $0.20–$0.80. For internal use this is negligible; for a public tool you'll want rate limiting.

**Cold starts on the free tier.** Render's free tier spins the service down after 15 min of inactivity; first request after that takes ~50s to wake. The `starter` plan ($7/mo) keeps it warm. The Blueprint defaults to `starter` — change to `free` in `render.yaml` if you want to test for free first.

**Timeouts.** Gunicorn is configured with a 300-second timeout. Render's HTTP layer also has a 300-second cap. If your runs ever exceed this you'll need to switch to an async pattern (job queue + polling).

**PII.** Census files contain employee names and dependents. Before processing real client data:
- Apply for Zero Data Retention with Anthropic (support@anthropic.com).
- Strip request body logging from any middleware you add later.
- Don't store uploaded files on disk — the current code holds them in memory only.

**Auth.** The `SHARED_SECRET` check is intentionally crude — fine for an internal tool behind a known URL, not for public exposure. For public use, swap in real auth (OAuth, JWT, or just put it behind a login on your existing site).

## Iterating on the skill

The skill body in `skill/SKILL.md` is loaded once at startup. To update it:
1. Edit the file.
2. Commit and push — Render auto-deploys on push to your main branch.
3. Or click **Manual Deploy** in the dashboard.

There's no separate "skill upload" step like in Claude.ai — the file in the repo IS the skill.

## Common issues

**"No spreadsheet was produced."** Claude responded but didn't generate a file. Check the `claude_response` field in the JSON error — it usually explains why (e.g., "I need to know which carrier is current"). Tighten the skill or improve the user prompt.

**504 Gateway Timeout.** Run took longer than 300 seconds. Reduce file size, or move to async.

**401 Unauthorized.** `SHARED_SECRET` is set in env but client didn't send the matching value.

**ImportError on deploy.** Render is using the wrong Python version. Set `PYTHON_VERSION=3.12` in env vars (also already in `render.yaml`).

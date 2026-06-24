# Monday → Google Calendar Sync

Cloud-hostable monday.com → Google Calendar mirror for business, personal, or multiple Monday board → Google Calendar syncs.

The intended production flow is:

```text
monday.com webhook
→ Cloudflare Worker
→ GitHub repository_dispatch
→ GitHub Actions sync job
→ Google Calendar
```

This avoids depending on a personal computer or a business VPS. Secrets live in GitHub Actions secrets and Cloudflare Worker secrets, not in source control.

## What syncs

The sync reads dated items and subitems from a configured monday.com board and mirrors them into a configured Google Calendar. Board/calendar names are environment-driven so additional business or personal syncs can be added without renaming the project.

Event shape:

- summary: monday item/subitem title
- description: board, group, status, parent/subitem status, Monday item ID, direct Monday link
- transparency: free/transparent
- color: based on status
  - Done: green
  - Working/in progress: yellow
  - Blocked/stuck/problem: red
  - Waiting/hold/later: orange
  - Unknown/no status: blue

Important behavior:

- If a parent item is marked `Done`, dated subitems inherit `Done` in Google Calendar even if the subitem's own status still says `Working on it`.

## Repository secrets for GitHub Actions

Set these in GitHub repository settings → Secrets and variables → Actions:

- `MONDAY_API_TOKEN`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `TELEGRAM_BOT_TOKEN` optional, for catch-up drift alerts
- `TELEGRAM_CHAT_ID` optional, for catch-up drift alerts

Repository variables:

- `MONDAY_BOARD_ID`
- `MONDAY_BOARD_NAME`
- `GOOGLE_CALENDAR_NAME`
- `GOOGLE_CALENDAR_TIMEZONE`

The workflow also runs every 15 minutes as a backup catch-up sync. If that scheduled catch-up creates, updates, or deletes events, it can send a Telegram alert so missed webhook coverage is visible.

## Cloudflare Worker secrets

Set these in Cloudflare Workers:

- `GITHUB_DISPATCH_TOKEN` — GitHub token allowed to create `repository_dispatch` events for this repo.
- optional `INBOUND_SECRET` — only useful if you control the inbound request header/query. monday.com webhooks do not natively sign requests, so the webhook path should also be hard to guess.

## Cloudflare Worker vars

Configured in `worker/wrangler.toml`:

- `GITHUB_OWNER=JayRemedy`
- `GITHUB_REPO=monday-gcal-sync`
- `MONDAY_BOARD_IDS=1234567890` — comma-separated if monday.com subitem webhooks report a separate subitems board ID
- `WEBHOOK_PATH=/monday-gcal-example`

Before production, change `WEBHOOK_PATH` to a generated hard-to-guess path.

## Deploy Worker

Install Wrangler and log in:

```bash
npm install -g wrangler
wrangler login
```

Then from `worker/`:

```bash
wrangler secret put GITHUB_DISPATCH_TOKEN
wrangler deploy
```

Use the deployed Worker URL plus `WEBHOOK_PATH` as the monday.com webhook URL.

## Local test

Run syntax check:

```bash
python3 -m py_compile scripts/sync_monday_gcal.py
```

Run a sync with env vars loaded:

```bash
python3 scripts/sync_monday_gcal.py
```

## Security notes

Do not commit:

- `.env`
- monday API token
- Google client secret
- Google refresh token
- Cloudflare secrets
- live webhook setup JSON
- logs

The business VPS is intentionally not part of this design, so personal Google credentials do not need to be placed there.

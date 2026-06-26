# Monday → Google Calendar Sync

Cloud-hostable monday.com → Google Calendar mirror for business, personal, or multiple Monday board → Google Calendar syncs.

The intended production flow is:

```text
monday.com webhook
→ Cloudflare Worker
→ GitHub repository_dispatch
→ GitHub Actions sync job
→ Google Calendar

Google Calendar event move
→ Google Calendar push notification
→ Cloudflare Worker
→ GitHub repository_dispatch
→ GitHub Actions reverse sync job
→ monday.com due date
```

This avoids depending on a personal computer or a business VPS. Secrets live in GitHub Actions secrets and Cloudflare Worker secrets, not in source control.

## What syncs

The sync reads dated items and subitems from a configured monday.com board and mirrors them into a configured Google Calendar. Board/calendar names are environment-driven so additional business or personal syncs can be added without renaming the project.

Event shape:

- summary: monday item/subitem title
- description: board, group, status, parent/subitem status, Monday Text/Notes content when present, user-entered Monday URL/link columns, Monday item ID, direct Monday link
- transparency: free/transparent
- color: based on status
  - Done: basil
  - Working/in progress: blueberry
  - Blank/no status: graphite
  - Blocked/stuck/problem: tomato
  - Not Started: graphite
  - Waiting/hold/later: banana
  - Project/unknown: blueberry

Google Calendar color controls for reverse status sync:

- blueberry → `Working on it`
- basil → `Done`
- tomato → `Stuck`
- graphite → `Not Started`

Important behavior:

- If a parent item is marked `Done`, dated subitems inherit `Done` in Google Calendar even if the subitem's own status still says `Working on it`.
- If a script-owned Google Calendar mirror event is moved to another day/time, the reverse sync updates that Monday item's date column. If the event color is changed to a command color, the reverse sync updates that Monday item's status. It does not update titles, owners, notes, or non-mirrored calendar events.
- If a dated Monday item/subitem has a blank human `Text` field, `scripts/suggest_monday_task_descriptions.py` can prepare an approval queue and then write approved wording back to Monday. The next Monday → Google sync carries that text into the Calendar event description.
- If a dated Monday item/subitem has a user-entered `URL`/link column value, the next Monday → Google sync includes it in the Calendar event description before the Monday item link.

## Repository secrets for GitHub Actions

Set these in GitHub repository settings → Secrets and variables → Actions:

- `MONDAY_API_TOKEN`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `TELEGRAM_BOT_TOKEN` optional, for catch-up drift alerts
- `TELEGRAM_CHAT_ID` optional, for catch-up drift alerts
- `GOOGLE_WEBHOOK_URL` optional, for registering Calendar move notifications. Use the deployed Worker URL plus `GOOGLE_WEBHOOK_PATH`.
- `GOOGLE_CHANNEL_TOKEN` optional, shared token for Google Calendar push notifications.

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
- optional `GOOGLE_WEBHOOK_PATH=/google-gcal-example` — path for Google Calendar event-change notifications

Before production, change `WEBHOOK_PATH` and `GOOGLE_WEBHOOK_PATH` to generated hard-to-guess paths.

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

## Register Google Calendar move notifications

After deploying the Worker with `GOOGLE_WEBHOOK_PATH`, set these GitHub Actions secrets:

- `GOOGLE_WEBHOOK_URL` — deployed Worker URL plus the configured `GOOGLE_WEBHOOK_PATH`
- optional `GOOGLE_CHANNEL_TOKEN` — same value as the Worker secret if you want token checking

Then run the manual **Register Google Calendar watch** workflow. Google push channels expire, so re-run this workflow periodically or after changing the webhook URL. The reverse sync still only writes date/time changes for events carrying this repo's private Monday metadata.

## Local test

Run syntax check:

```bash
python3 -m py_compile scripts/sync_monday_gcal.py scripts/sync_gcal_monday.py scripts/register_google_calendar_watch.py
python3 -m unittest discover -s tests
```

Run a sync with env vars loaded:

```bash
python3 scripts/sync_monday_gcal.py
```

Find dated Monday tasks with blank Text and prepare/apply approved suggestions:

```bash
python3 scripts/suggest_monday_task_descriptions.py --limit 5
python3 scripts/suggest_monday_task_descriptions.py --apply ITEM_OR_SUBITEM_ID
python3 scripts/suggest_monday_task_descriptions.py --apply ITEM_OR_SUBITEM_ID --text "Approved custom wording"
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

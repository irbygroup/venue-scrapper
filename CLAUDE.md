# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Eventective CRM lead management for Yellowhammer Hospitality (3 venues: The Hallet-Irby House, Oak & Fountain, The Courtyard on Dauphin).

**`api.py` + `app/`** — FastAPI + async Playwright HTTP API (`localhost:5050`, all routes at `/eventective/*`)

## Running locally

```bash
# Create venv and install deps
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium

# Run the API (dev)
.venv/bin/uvicorn api:app --port 5050 --log-level info
```

## Production (vm-mind365)

```bash
# Deploy (build image, sync OpenClaw skill, restart service, health check)
sudo /root/gitops-mind365/tools/deploy-venue-scrapper.sh

# Logs
sudo journalctl -u venue-scrapper -f

# Container shell
docker exec -it venue-scrapper bash

# Connect to venue-scrapper database
psql -U venue_scrapper -d venue_scrapper -h 127.0.0.1
```

## Architecture

### Module layout

`api.py` is a thin entrypoint (~30 lines) that wires together the `app/` package:

```
api.py                  # app creation, lifespan, router wiring
app/
  config.py             # DATABASE_URL, get_config, _cfg, lazy accessors, constants
  db.py                 # get_db, init_db, get_meta, set_meta, upsert_*
  browser.py            # BrowserManager class (on_error callback injection)
  state.py              # global `bm` holder + get_bm() accessor
  utils.py              # days_until_event, classify_thread, compute_urgency, etc.
  email.py              # send_email, notify_error
  sync.py               # run_sync()
  lead_market.py        # run_check_lead_market() — claim free leads from Lead Market
  models.py             # LoginRequest, ReplyRequest
  fub.py                # All FUB logic (_fub_*, ACTIVITY_LABELS, fub_sync_state)
  routes/
    auth.py             # POST /auth/login, GET /auth/status
    sync.py             # POST /sync
    lead_market.py      # POST /check-lead-market
    leads.py            # GET /leads, GET /leads/{id}, POST /leads/{id}/reply
    status.py           # GET /status
    email.py            # POST /notify_error, GET /daily_report
    fub.py              # POST /fub-sync, POST /fub-export-new, GET /fub-sync/status
    fub_webhook.py      # POST /fub-webhook, GET /fub-webhook/ensure (+ root /fub alias)
```

**Dependency graph (no cycles):** `config` <- `db`, `email`, `browser`; `browser` <- `state`; `config`+`db`+`email` <- `fub`; `state`+`config`+`db`+`utils`+`fub` <- `sync`; routes import from all.

### Browser — persistent context, two pages

A single `BrowserManager` instance lives for the lifetime of the process. It holds one `BrowserContext` with **two pages**:
- `sync_page` — stays on the Eventective inbox, makes all JSON API calls via `page.evaluate(fetch(...))`
- `reply_page` — navigates to individual lead message URLs to interact with the Angular reply UI

All Playwright calls go through the same browser context (shared cookies, consistent TLS/fingerprint). On startup, cookies are loaded from the `config` table in the database; on login, new cookies are saved back to the same table.

`BrowserManager` accepts an `on_error` callback (injected as `notify_error` during lifespan startup) to stay decoupled from the email module.

**Chromium launch flags required for Docker:** `--no-sandbox --disable-setuid-sandbox --disable-dev-shm-usage`

### Smart incremental sync (`POST /eventective/sync`)

The Eventective inbox sidebar is sorted by `LastActivityDttm DESC`. Sync fetches batches of 20 via the `getmessagesforinbox` API. The moment it encounters a lead whose `LastActivityDttm <= last_sync_time`, it stops — everything below is stale. New/changed leads are fetched individually via `geteventdetails`. Last sync time stored in `sync_meta` table.

### Reply flow (`POST /eventective/leads/{id}/reply`)

Navigates `reply_page` to the lead's message URL, types character-by-character (`textarea.type()` not `.fill()` — needed to trigger Angular's change detection so the send button renders), then finds the send button by walking up from the textarea to its `.send-message-wrapper` parent.

### Session / auto-login

`ensure_session()` checks the session, then calls `do_login()` if expired. `do_login()` navigates to `/signin` — if already redirected to the dashboard (valid cookies), returns `True` immediately. Otherwise fills `#Email`/`#Password` and submits.

### Database

PostgreSQL 15 on vm-mind365 (database: `venue_scrapper`, role: `venue_scrapper`). Connection via `DATABASE_URL` env var. Tables:
- `config` — key/value configuration (credentials, URLs, cookies, FUB keys)
- `eventective_leads` — merged inbox metadata + full contact/event details per lead, with FUB tracking fields
- `eventective_lead_activities` — full message thread for each lead, with FUB tracking fields
- `sync_meta` — `last_sync_time` key/value

All config (credentials, URLs, batch sizes) is stored in the `config` table and read at request-time. Only env var needed: `DATABASE_URL`.

### FUB tracking fields

Both `eventective_leads` and `eventective_lead_activities` have:
- `fub_exported` (0/1) — whether the record has been exported to Follow Up Boss
- `fub_exported_date` — when it was exported
- `fub_people_id` — the FUB person ID it was linked to

`eventective_leads` also has:
- `fub_lead_stage` — current FUB stage, updated via webhook (`POST /fub-webhook`)

## API endpoints

See **[API.md](API.md)** for full endpoint documentation with request/response examples.

**IMPORTANT: Always update API.md when endpoints change.**

## Schema management

PostgreSQL schema: `schema_pg.sql` — manually maintained DDL, executed by `init_db()` on startup.

**IMPORTANT: After any schema change, update `schema_pg.sql` (add idempotent migration at bottom).**

## Container

Built from `mcr.microsoft.com/playwright/python:v1.50.0-noble`. Uses `DATABASE_URL` env var to connect to PostgreSQL (no local data volume needed). Port `5050` bound to `127.0.0.1` only.

## OpenClaw skill

`openclaw/workspace-events/SKILL.md` — auto-symlinked to `/root/.openclaw/workspace-events/SKILL.md` on deploy. Teaches OpenClaw to call the API endpoints via curl and follow Yellowhammer sales rules (no pricing in messages, always push for phone call at 251-422-9114).

## Sales rules (baked into the skill)

- Never quote pricing — always push for a phone call or tour
- Always mention all 3 venues regardless of which one they inquired about
- Goal: get them on the phone with Veronica Miller at 251-422-9114
- Use assumptive close ("When are you free?") not permission-seeking ("Would you like to call?")

## Drip campaign system

AI-powered automated follow-up sequences using LLM-generated messages (Gemini 2.5 Flash via LiteLLM proxy).

### Three sequences

| Sequence | Trigger | Steps | Timing |
|----------|---------|-------|--------|
| `new_lead` | New inquiry arrives | 4 | Immediate, +1d, +3d, +7d |
| `unanswered_reply` | They replied, we replied, silence | 4 | +1d, +3d, +7d, +14d |
| `long_term_nurture` | After Seq 1 or 2 completes | 6 | +30d, +60d, +120d, +180d, +180d, +180d |

### Prompt files

All LLM prompts live in `prompts/` as flat `.txt` files — edit without touching Python:
- `prompts/system_base.txt` — base system prompt (sales rules, output format)
- `prompts/{sequence}/step_{N}.txt` — stage-specific instructions

### FUB stage gate

Before sending any drip message, checks `fub_lead_stage`:
- NULL, "YH | Hot Lead", "YH | Long Term Nurture" → OK to send
- Anything else → skip (Veronica is managing in FUB)

### Config keys

| Key | Default | Description |
|-----|---------|-------------|
| `drip_auto_send` | `false` | Set to `true` to auto-send (otherwise messages go to `pending_review`) |
| `drip_seq1_daily_cap` | `0` | Max daily sends for Seq 1 (0 = unlimited) |
| `drip_seq2_daily_cap` | `25` | Max daily sends for Seq 2 |
| `drip_seq3_daily_cap` | `25` | Max daily sends for Seq 3 |
| `litellm_base_url` | `https://litellm.build365.app` | LiteLLM proxy URL |
| `litellm_api_key` | — | LiteLLM API key |
| `litellm_model` | `openrouter/google/gemini-2.5-flash` | Model to use |

### Key modules

- `app/llm.py` — LLM generation (loads flat-file prompts, structured JSON context)
- `app/drip.py` — State machine, scheduler, batch sending, backfill
- `app/routes/drip.py` — API endpoints (process, status, pause, resume, cancel, send)

### Cron (all times Central, Mon-Sat only)

| Schedule | Endpoint | Purpose |
|----------|----------|---------|
| `:22/:52` every 2h, 8am-6pm | `POST /check-lead-market` | Claim free leads from Lead Market + trigger sync |
| `:32` every 2h, 8am-6pm | `POST /sync` | Sync new leads from Eventective |
| `:42` every 2h, 8am-6pm | `POST /drip/process` | Process due campaigns (10 min after sync) |
| `7:00am` daily | `GET /daily_report` | Email daily summary |
| `:15` every hour | `GET /fub-webhook/ensure` | Ensure FUB webhook exists |

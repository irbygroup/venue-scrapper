# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Eventective CRM lead management for Yellowhammer Hospitality (3 venues: The Hallet-Irby House, Oak & Fountain, The Courtyard on Dauphin). Two components:

1. **`api.py`** — FastAPI + async Playwright HTTP API (`localhost:5050`, all routes at `/eventective/*`)
2. **`scrape_leads.py`** — CLI scraper (full/incremental modes), used manually for recovery only

## Running locally

```bash
# Create venv and install deps
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium

# Run the API (dev)
.venv/bin/uvicorn api:app --port 5050 --log-level info

# Run incremental sync (CLI scraper)
PYTHONUNBUFFERED=1 .venv/bin/python scrape_leads.py incremental --db leads.db
```

## Production (vm-mind365)

```bash
# Deploy (build image, sync OpenClaw skill, restart service, health check)
sudo /root/gitops-mind365/tools/deploy-venue-scrapper.sh

# Logs
sudo journalctl -u venue-scrapper -f

# Container shell
docker exec -it venue-scrapper bash

# Re-copy leads.db or cookies.json from Mac
scp leads.db cookies.json mind365:/opt/venue-scrapper/data/
```

## Architecture

### `api.py` — persistent browser, two pages

A single `BrowserManager` instance lives for the lifetime of the process. It holds one `BrowserContext` with **two pages**:
- `sync_page` — stays on the Eventective inbox, makes all JSON API calls via `page.evaluate(fetch(...))`
- `reply_page` — navigates to individual lead message URLs to interact with the Angular reply UI

All Playwright calls go through the same browser context (shared cookies, consistent TLS/fingerprint). On startup, cookies are loaded from `/data/cookies.json`; on login, new cookies are saved back.

**Chromium launch flags required for Docker:** `--no-sandbox --disable-setuid-sandbox --disable-dev-shm-usage`

### Smart incremental sync (`POST /eventective/sync`)

The Eventective inbox sidebar is sorted by `LastActivityDttm DESC`. Sync fetches batches of 20 via the `getmessagesforinbox` API. The moment it encounters a lead whose `LastActivityDttm <= last_sync_time`, it stops — everything below is stale. New/changed leads are fetched individually via `geteventdetails`. Last sync time stored in `sync_meta` SQLite table.

### Reply flow (`POST /eventective/leads/{id}/reply`)

Navigates `reply_page` to the lead's message URL, types character-by-character (`textarea.type()` not `.fill()` — needed to trigger Angular's change detection so the send button renders), then finds the send button by walking up from the textarea to its `.send-message-wrapper` parent.

### Session / auto-login

`ensure_session()` checks the session, then calls `do_login()` if expired. `do_login()` navigates to `/signin` — if already redirected to the dashboard (valid cookies), returns `True` immediately. Otherwise fills `#Email`/`#Password` and submits.

### Database

SQLite at `leads.db` (local) or `/data/leads.db` (container). Three tables:
- `leads` — inbox metadata, `LastActivityDttm`, `DetailScrapedAt`
- `lead_details` — full contact/event info per lead
- `activities` — full message thread for each lead
- `sync_meta` — `last_sync_time` key/value

## Container

Built from `mcr.microsoft.com/playwright/python:v1.50.0-noble`. Data volume at `/data` (leads.db + cookies.json). Env vars: `EVENTECTIVE_EMAIL`, `EVENTECTIVE_PASSWORD`, `DB_PATH`, `COOKIES_PATH`. Port `5050` bound to `127.0.0.1` only.

## OpenClaw skill

`openclaw/workspace-events/SKILL.md` — auto-symlinked to `/root/.openclaw/workspace-events/SKILL.md` on deploy. Teaches OpenClaw to call the API endpoints via curl and follow Yellowhammer sales rules (no pricing in messages, always push for phone call at 251-422-9114).

## Sales rules (baked into the skill)

- Never quote pricing — always push for a phone call or tour
- Always mention all 3 venues regardless of which one they inquired about
- Goal: get them on the phone with Veronica Miller at 251-422-9114
- Use assumptive close ("When are you free?") not permission-seeking ("Would you like to call?")

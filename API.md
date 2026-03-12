# API Reference

Base URL: `http://localhost:5050/eventective`

## Authentication

### `POST /eventective/auth/login`

Login to Eventective. Uses credentials from DB config table by default.

**Body** (optional):
```json
{"email": "...", "password": "..."}
```

**Response:**
```json
{"success": true, "message": "Logged in, cookies saved"}
```

### `GET /eventective/auth/status`

Check if the browser session is still valid.

**Response:**
```json
{"authenticated": true, "has_cookies": true}
```

## Sync

### `POST /eventective/sync`

Incremental sync — fetches new/changed leads from Eventective inbox API, stores details + activities in SQLite. Stops when it reaches leads older than the last sync time.

**Query params:**
- `limit` (int, optional) — max leads to scan

**Response:**
```json
{
  "duration_seconds": 1.1,
  "batches_fetched": 1,
  "leads_scanned": 20,
  "stop_reason": "reached_stale|end_of_inbox|api_error|limit_reached",
  "new_leads": [...],
  "replied_to_us": [...],
  "read_no_reply": [...],
  "other_updates": [...],
  "summary": {
    "new_leads": 0,
    "replied_to_us": 0,
    "read_no_reply": 1,
    "other_updates": 0,
    "no_change": 19
  }
}
```

Each lead in the result arrays contains:
```json
{
  "event_id": "EGZKF1OT",
  "name": "...", "phone": "...", "email": "...",
  "venue": "...", "event_type": "...", "event_date": "2026-04-18",
  "days_until_event": 36, "guests": 30, "budget": "Under $500",
  "notes": "...", "source": null, "received_at": "...",
  "urgency": "HIGH|MEDIUM|LOW", "urgency_reasons": ["..."],
  "we_replied": true, "they_replied_to_us": false,
  "last_their_message": "...", "last_our_message": "...",
  "thread_length": 5
}
```

## Leads

### `GET /eventective/leads`

List leads from the database with filtering.

**Query params:**
| Param | Type | Description |
|-------|------|-------------|
| `since` | string | Time window, e.g. `24h`, `7d` |
| `unreplied` | bool | Only leads we haven't replied to |
| `replied_to_us` | bool | Only leads where they replied to us |
| `venue` | string | Filter by venue name (partial match) |
| `upcoming_days` | int | Events within N days from now |
| `urgency` | string | `HIGH`, `MEDIUM`, or `LOW` |
| `status` | string | Lead status, e.g. `Prospect`, `Booked` |
| `limit` | int | Max results (default 50) |
| `offset` | int | Pagination offset (default 0) |

**Response:**
```json
{
  "count": 5,
  "leads": [
    {
      "event_id": "EGZKF1OT",
      "name": "...", "phone": "...", "venue": "...",
      "event_type": "BabyShwr", "event_date": "2026-04-18",
      "days_until_event": 36, "guests": 30, "budget": "Under $500",
      "status": "Prospect",
      "we_replied": true, "they_replied_to_us": false,
      "thread_length": 5, "urgency": "MEDIUM",
      "last_activity_at": "2026-03-12T07:58:57.407"
    }
  ]
}
```

### `GET /eventective/leads/{event_id}`

Full detail for a single lead including contact info, event details, thread signals, and full message thread.

**Response:**
```json
{
  "event_id": "EGZKF1OT",
  "contact": {
    "name": "...", "phone": "...", "email": "...",
    "location": "...", "contact_pref": "..."
  },
  "event": {
    "type": "BabyShwr", "date": "2026-04-18",
    "days_until": 36, "guests": 30, "budget": "Under $500",
    "duration": "3.0", "date_flexible": false,
    "notes": "...", "food": "Self-provided"
  },
  "meta": {
    "status": "Prospect", "source": null,
    "venue": "Oak & Fountain",
    "received_at": "...", "last_activity": "..."
  },
  "urgency": "MEDIUM",
  "urgency_reasons": ["read our message, no reply in 11h"],
  "thread_signals": {
    "we_replied": true, "they_replied_to_us": false,
    "hours_since_our_msg": 11.2,
    "last_our_message": "...", "last_their_message": null,
    "our_message_count": 1, "their_message_count": 0
  },
  "thread": [
    {"from": "Courtney", "type": "inquiry", "text": "...", "at": "..."},
    {"from": "Yellowhammer", "type": "our_reply", "text": "...", "at": "..."}
  ]
}
```

### `POST /eventective/leads/{event_id}/reply`

Send a reply via Playwright DOM interaction. Types character-by-character to trigger Angular change detection.

**Body:**
```json
{"message": "Your reply text here"}
```

**Response:**
```json
{
  "success": true,
  "event_id": "EGZKF1OT",
  "message_sent": "Your reply text here",
  "sent_at": "2026-03-12T...",
  "thread_length": 6
}
```

**Errors:**
- `409` — Reply already in progress
- `401` — Session expired and auto-login failed
- `404` — Reply box not found (lead may be closed)

## Dashboard

### `GET /eventective/status`

Dashboard overview with action items, watched leads, upcoming events, and 30-day performance stats.

**Response:**
```json
{
  "as_of": "2026-03-12T19:16:04...",
  "session": {"authenticated": true},
  "last_sync": "2026-03-12T19:13:53...",
  "total_leads_in_db": 3553,
  "action_required": [
    {
      "event_id": "...", "name": "...", "venue": "...",
      "reason": "replied to us — awaiting your response",
      "urgency": "HIGH", "their_last_message": "..."
    }
  ],
  "watching": [
    {"event_id": "...", "name": "...", "venue": "...", "reason": "...", "urgency": "MEDIUM"}
  ],
  "upcoming_events": [
    {"name": "...", "venue": "...", "date": "2026-03-21", "days_away": 8, "status": "waiting on lead"}
  ],
  "stats_30d": {
    "leads_received": 42,
    "response_rate_pct": 95.2,
    "avg_response_minutes": 15,
    "first_responder_rate_pct": 78.6
  }
}
```

## Email & Notifications

### `POST /eventective/notify_error`

Send an error notification email via SendGrid.

**Query params:**
- `subject` (string) — Error subject line
- `detail` (string) — Error detail/stacktrace

**Response:**
```json
{"status_code": 202, "success": true}
```

### `GET /eventective/daily_report`

Generate and email a daily summary of all Eventective activity in the last 24 hours. Runs automatically via cron at 7 AM Central (12:00 UTC).

**Response:**
```json
{
  "report_sent": true,
  "email_result": {"status_code": 202, "success": true},
  "summary": {
    "new_leads": 1,
    "active_leads": 3,
    "our_replies_24h": 5,
    "their_replies_24h": 2,
    "total_activities_24h": 15
  }
}
```

The email includes:
- Summary stats table (new leads, active leads, our/their replies, total activities)
- New lead cards with contact info, event details, and notes
- Activity log grouped by lead with message text

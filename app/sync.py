import asyncio
import random
from datetime import datetime, timezone
from typing import Optional

from psycopg2.extras import RealDictCursor

from app import state as state_mod
from app.config import BASE_BODY, batch_size, inbox_url
from app.db import get_db, get_meta, set_meta, upsert_inbox_lead, upsert_lead_details, upsert_activities
from app.utils import days_until_event, classify_thread, classify_change, compute_urgency
from app.fub import _fub_incremental_export


async def run_sync(limit: Optional[int] = None) -> dict:
    """
    Smart incremental sync.
    - Fetches batches of 20 from the API (sorted by LastActivityDttm DESC)
    - Stops at the first lead whose LastActivityDttm <= last_sync_time
    - Fetches full details only for leads with new activity
    - Auto-login if session is expired
    """
    bm = state_mod.get_bm()
    started_at = datetime.now(timezone.utc)

    # Ensure we have a valid session (ensure_session sends error email if login fails)
    if not await bm.ensure_session():
        return {"error": "login_failed", "message": "Could not authenticate with Eventective"}

    # Make sure sync_page is on inbox
    if "myeventective" not in bm.sync_page.url:
        await bm.sync_page.goto(inbox_url(), wait_until="domcontentloaded")
        await bm.sync_page.wait_for_load_state("networkidle", timeout=15000)

    last_sync_raw = get_meta("last_sync_time") or "2020-01-01T00:00:00"
    # Strip timezone offset for string comparison with naive Eventective timestamps
    last_sync = last_sync_raw.replace("+00:00", "").replace("Z", "")
    con = get_db()
    cur = con.cursor(cursor_factory=RealDictCursor)

    needs_fetch = []   # list of (event_id, lead_dict)
    batches_checked = 0
    total_scanned   = 0
    stop_reason     = "limit_reached"

    api_start = 1
    while True:
        if limit and total_scanned >= limit:
            break

        body = {**BASE_BODY, "StartIndex": api_start, "EndIndex": api_start + batch_size() - 1}
        batch = await bm.fetch(
            "/api/v1/salesandcatering/getmessagesforinbox?showFlagged=false&showUnread=false",
            method="POST", body=body
        )

        if not batch or isinstance(batch, dict) and "__error" in batch:
            stop_reason = "api_error"
            break

        batches_checked += 1

        for lead in batch:
            total_scanned += 1
            last_activity = lead.get("LastActivityDttm") or ""

            # Sorted DESC — first stale lead means everything below is stale
            if last_activity <= last_sync:
                stop_reason = "reached_stale"
                break

            # Check against DB
            cur.execute(
                'SELECT "LastActivityDttm", "DetailScrapedAt" FROM eventective_leads WHERE "EventId"=%s',
                (lead["EventId"],)
            )
            row = cur.fetchone()

            if row is None or not row["DetailScrapedAt"] or last_activity > (row["LastActivityDttm"] or ""):
                needs_fetch.append(lead)
        else:
            # All leads in batch were fresh — continue if more pages
            if len(batch) < batch_size():
                stop_reason = "end_of_inbox"
                break
            api_start += batch_size()
            await asyncio.sleep(0.3)
            continue
        break  # broke out of inner loop (stale found or limit)

    # Fetch full details for changed leads
    results = {
        "new_leads":       [],
        "replied_to_us":   [],
        "read_no_reply":   [],
        "other_updates":   [],
    }

    for lead in needs_fetch:
        event_id = lead["EventId"]
        await asyncio.sleep(random.uniform(0.3, 0.8))

        detail = await bm.fetch(f"/api/v1/salesandcatering/geteventdetails?id={event_id}")
        if not detail or isinstance(detail, dict) and "__error" in detail:
            continue

        upsert_inbox_lead(cur, lead)
        upsert_lead_details(cur, event_id, detail)
        upsert_activities(cur, event_id, detail.get("Activities") or [])
        cur.execute(
            'UPDATE eventective_leads SET "DetailScrapedAt"=%s WHERE "EventId"=%s',
            (datetime.now(timezone.utc).isoformat(), event_id)
        )
        con.commit()

        # Re-read fresh activities
        cur.execute(
            'SELECT * FROM eventective_lead_activities WHERE "EventId"=%s ORDER BY "DateTime"', (event_id,)
        )
        acts = [dict(r) for r in cur.fetchall()]
        signals = classify_thread(acts)
        change  = classify_change(event_id, signals, cur)

        event_date = detail.get("DatePossible1") or lead.get("EventDate")
        d_until    = days_until_event(event_date)
        urgency, urgency_reasons = compute_urgency(
            d_until, signals["they_replied_to_us"], signals["hours_since_our_msg"]
        )

        entry = {
            "event_id":        event_id,
            "name":            detail.get("RequestorName") or lead.get("PlannerName"),
            "phone":           detail.get("RequestorPhone"),
            "email":           detail.get("RequestorEmailAddress"),
            "venue":           lead.get("ProviderName"),
            "event_type":      detail.get("EventType") or lead.get("EventType"),
            "event_date":      event_date.split("T")[0] if event_date else None,
            "days_until_event":d_until,
            "guests":          detail.get("AttendeeCount") or lead.get("AttendeeCount"),
            "budget":          detail.get("BudgetValue"),
            "notes":           detail.get("InformationRequested"),
            "source":          lead.get("Source"),
            "received_at":     lead.get("EmailSentDttm"),
            "urgency":         urgency,
            "urgency_reasons": urgency_reasons,
            "we_replied":      signals["we_replied"],
            "they_replied_to_us": signals["they_replied_to_us"],
            "last_their_message": signals["last_their_message"],
            "last_our_message":   signals["last_our_message"],
            "thread_length":   len(acts),
        }

        results[change].append(entry) if change in results else results["other_updates"].append(entry)

    set_meta("last_sync_time", started_at.isoformat())
    con.close()

    # Auto-export new leads and activities to FUB
    if needs_fetch:
        asyncio.create_task(_fub_incremental_export())

    # Drip campaign hooks — enroll all synced leads (create_campaign skips duplicates)
    try:
        from app.drip import drip_post_sync_new_leads, drip_post_sync_replies
        all_synced_ids = [e["event_id"] for cat in ("new_leads", "read_no_reply", "other_updates") for e in results[cat]]
        if all_synced_ids:
            asyncio.create_task(drip_post_sync_new_leads(all_synced_ids))
        if results["replied_to_us"]:
            asyncio.create_task(drip_post_sync_replies(
                [e["event_id"] for e in results["replied_to_us"]]
            ))
    except Exception as e:
        print(f"Drip post-sync hook error (non-fatal): {e}")

    duration = (datetime.now(timezone.utc) - started_at).total_seconds()
    return {
        "duration_seconds": round(duration, 1),
        "batches_fetched":  batches_checked,
        "leads_scanned":    total_scanned,
        "stop_reason":      stop_reason,
        "new_leads":        results["new_leads"],
        "replied_to_us":    results["replied_to_us"],
        "read_no_reply":    results["read_no_reply"],
        "other_updates":    results["other_updates"],
        "summary": {
            "new_leads":     len(results["new_leads"]),
            "replied_to_us": len(results["replied_to_us"]),
            "read_no_reply": len(results["read_no_reply"]),
            "other_updates": len(results["other_updates"]),
            "no_change":     total_scanned - sum(
                len(v) for v in results.values()
            ),
        },
    }

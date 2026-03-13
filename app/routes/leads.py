import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from psycopg2.extras import RealDictCursor

from app import state as state_mod
from app.config import inbox_url
from app.db import get_db, upsert_activities
from app.email import notify_error
from app.models import ReplyRequest
from app.utils import days_until_event, classify_thread, compute_urgency, build_lead_detail

router = APIRouter()


@router.get("/leads")
async def list_leads(
    since: Optional[str]   = None,      # e.g. "24h", "7d"
    unreplied: Optional[bool] = None,
    replied_to_us: Optional[bool] = None,
    venue: Optional[str]   = None,
    upcoming_days: Optional[int] = None,
    urgency: Optional[str] = None,
    status: Optional[str]  = None,
    limit: int = 50,
    offset: int = 0,
):
    con = get_db()
    cur = con.cursor(cursor_factory=RealDictCursor)
    wheres = []
    params = []

    if since:
        unit = since[-1]
        n    = int(since[:-1])
        secs = n * 3600 if unit == "h" else n * 86400
        cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - secs, tz=timezone.utc
        ).isoformat()
        wheres.append('"LastActivityDttm" >= %s')
        params.append(cutoff)

    if venue:
        wheres.append('LOWER("ProviderName") LIKE %s')
        params.append(f"%{venue.lower()}%")

    if status:
        wheres.append('LOWER("LeadStatus") = %s')
        params.append(status.lower())

    if upcoming_days is not None:
        cutoff_date = datetime.now(timezone.utc).isoformat()
        far_date    = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + upcoming_days * 86400, tz=timezone.utc
        ).isoformat()
        wheres.append('("DatePossible1" >= %s AND "DatePossible1" <= %s)')
        params.extend([cutoff_date, far_date])

    where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""

    cur.execute(f"""
        SELECT "EventId", "PlannerName", "LastActivityDttm", "EventDate",
               "AttendeeCount", "EventType", "ProviderName", "LeadStatus",
               "Source", "EmailSentDttm",
               "RequestorName", "RequestorPhone", "RequestorEmailAddress",
               "BudgetValue", "InformationRequested", "DatePossible1", "DateFlexible"
        FROM eventective_leads
        {where_sql}
        ORDER BY "LastActivityDttm" DESC
        LIMIT %s OFFSET %s
    """, params + [limit, offset])
    rows = cur.fetchall()

    leads_out = []
    for r in rows:
        event_date = r["DatePossible1"] or r["EventDate"]
        d_until    = days_until_event(event_date)

        # Quick thread signals from DB
        cur.execute(
            'SELECT "ActivityTypeCd", "DateTime", "ResponseText", "Sender" FROM eventective_lead_activities WHERE "EventId"=%s ORDER BY "DateTime"',
            (r["EventId"],)
        )
        acts = cur.fetchall()
        acts_dicts = [dict(a) for a in acts]
        signals    = classify_thread(acts_dicts)
        urg, _     = compute_urgency(d_until, signals["they_replied_to_us"], signals["hours_since_our_msg"])

        # Filter by urgency/replied flags if requested
        if urgency and urg != urgency.upper():
            continue
        if unreplied is True and signals["we_replied"]:
            continue
        if replied_to_us is True and not signals["they_replied_to_us"]:
            continue

        leads_out.append({
            "event_id":         r["EventId"],
            "name":             r["RequestorName"] or r["PlannerName"],
            "phone":            r["RequestorPhone"],
            "venue":            r["ProviderName"],
            "event_type":       r["EventType"],
            "event_date":       event_date.split("T")[0] if event_date else None,
            "days_until_event": d_until,
            "guests":           r["AttendeeCount"],
            "budget":           r["BudgetValue"],
            "status":           r["LeadStatus"],
            "we_replied":       signals["we_replied"],
            "they_replied_to_us": signals["they_replied_to_us"],
            "thread_length":    len(acts_dicts),
            "urgency":          urg,
            "last_activity_at": r["LastActivityDttm"],
        })

    con.close()
    return {"count": len(leads_out), "leads": leads_out}


@router.get("/leads/{event_id}")
async def get_lead(event_id: str):
    con = get_db()
    cur = con.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        'SELECT * FROM eventective_leads WHERE "EventId"=%s', (event_id,)
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Lead {event_id} not found")

    cur.execute(
        'SELECT * FROM eventective_lead_activities WHERE "EventId"=%s ORDER BY "DateTime"', (event_id,)
    )
    acts = [dict(a) for a in cur.fetchall()]

    con.close()
    row_dict = dict(row)
    return build_lead_detail(row_dict, row_dict, acts)


@router.post("/leads/{event_id}/reply")
async def send_reply(event_id: str, req: ReplyRequest):
    """Send a message via Playwright DOM interaction."""
    bm = state_mod.get_bm()
    if bm.reply_lock.locked():
        raise HTTPException(status_code=409, detail="Reply already in progress")

    async with bm.reply_lock:
        # Ensure session first
        if not await bm.ensure_session():
            raise HTTPException(status_code=401, detail="Session expired and auto-login failed")

        page = bm.reply_page
        # Ensure reply_page has visited the inbox first (cold page needs domain context)
        if "eventective.com" not in page.url:
            await page.goto(inbox_url(), wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle", timeout=15000)
            await asyncio.sleep(1)

        msg_url = f"https://www.eventective.com/myeventective/#/crm/Event/Messages/{event_id}"
        await page.goto(msg_url, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=15000)
        await asyncio.sleep(2)

        textarea = page.locator('textarea[placeholder="Enter your reply here"]')
        try:
            await textarea.wait_for(state="visible", timeout=10000)
        except Exception:
            notify_error("Reply failed — no reply box", f"Could not find reply textarea for lead {event_id}. Lead may be closed.")
            raise HTTPException(status_code=404, detail="Reply box not found — lead may be closed")

        await textarea.click()
        await asyncio.sleep(0.3)
        await textarea.type(req.message, delay=20)  # type char-by-char to trigger Angular events
        await asyncio.sleep(0.8)

        sent = await page.evaluate("""
            () => {
                // Send button lives in the direct parent (.send-message-wrapper) of the textarea
                const textarea = document.querySelector('textarea[placeholder="Enter your reply here"]');
                if (!textarea) return 'no_textarea';
                const parent = textarea.parentElement;
                const btn = Array.from(parent.querySelectorAll('a[href="javascript:void(0)"]'))
                    .find(a => a.textContent.trim() === '');
                if (!btn) return 'no_button';
                btn.click();
                return 'clicked';
            }
        """)

        if sent != 'clicked':
            notify_error("Reply failed — send button", f"Send button not found for lead {event_id}: {sent}")
            raise HTTPException(status_code=500, detail=f"Send button not found: {sent}")

        await asyncio.sleep(2)

        # Verify by fetching thread
        detail = await bm.fetch(f"/api/v1/salesandcatering/geteventdetails?id={event_id}")
        our_msgs = [a for a in (detail.get("Activities") or []) if a.get("ActivityTypeCd") == "provplnr"]
        sent_at  = our_msgs[-1].get("DateTime") if our_msgs else None

        # Update DB
        con = get_db()
        cur = con.cursor(cursor_factory=RealDictCursor)
        upsert_activities(cur, event_id, detail.get("Activities") or [])
        con.commit()
        con.close()

        return {
            "success":      True,
            "event_id":     event_id,
            "message_sent": req.message,
            "sent_at":      sent_at,
            "thread_length": len(detail.get("Activities") or []),
        }

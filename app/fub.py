import asyncio
import traceback
from base64 import b64encode
from datetime import datetime, timezone

import httpx
import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import DATABASE_URL, _cfg
from app.email import notify_error

ACTIVITY_LABELS = {
    "LeadReceived": "Lead received",
    "LeadPurchased": "Lead purchased",
    "ReferralReceived": "Referral received",
    "ReferralViewed": "Referral viewed",
    "ReadMsgs": "Read our reply",
    "EmailViewed": "Email address viewed",
    "PhoneViewed": "Phone number viewed",
    "DetClick": "Viewed Eventective listing",
    "UrlClick": "Clicked URL",
    "PPhoneClic": "Clicked to call",
    "PEmailClic": "Clicked to email",
    "PStatArchi": "Moved to Archived",
    "PStatLost": "Moved to Lost",
    "PStatQual": "Moved to Qualified",
    "PStatBook": "Moved to Booked",
    "pStatTent": "Moved to Tentative",
    "NoInterest": "Marked no interest",
    "PAddNote": "Note added",
    "AttendChg": "Attendee count changed",
    "NameChg": "Event name changed",
    "TimeChg": "Start time changed",
    "DuratChg": "Duration changed",
    "DateChg": "Date changed",
    "FlexOnChg": "Flexibility turned on",
    "FlexOffChg": "Flexibility turned off",
}

fub_sync_state = {
    "asc": {"running": False, "progress": {}, "errors": []},
    "desc": {"running": False, "progress": {}, "errors": []},
    "incremental": {"running": False, "progress": {}, "errors": []},
}


def _fub_headers():
    api_key = _cfg("fub_api_key")
    token = b64encode(f"{api_key}:".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "X-System": _cfg("fub_system_header", "IRBY-GROUP-FUB-API"),
        "X-System-Key": _cfg("fub_system_key"),
    }


async def _fub_request(client: httpx.AsyncClient, method: str, url: str, **kwargs):
    """Make a FUB API request with automatic 429 retry."""
    kwargs["headers"] = _fub_headers()
    for attempt in range(5):
        resp = await client.request(method, url, **kwargs)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "2"))
            print(f"[fub-sync] rate limited, waiting {retry_after}s (attempt {attempt+1})")
            await asyncio.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def _parse_name(full_name: str):
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


async def _fub_search_person(client: httpx.AsyncClient, phone: str, email: str):
    """Search FUB for existing person by phone first, then email."""
    base = _cfg("fub_api_base_url", "https://api.followupboss.com/v1")

    if phone:
        resp = await _fub_request(client, "GET", f"{base}/people", params={"phone": phone, "limit": 1})
        people = resp.json().get("people", [])
        if people:
            return people[0]["id"]

    if email:
        resp = await _fub_request(client, "GET", f"{base}/people", params={"email": email, "limit": 1})
        people = resp.json().get("people", [])
        if people:
            return people[0]["id"]

    return None


def _fub_stage(lead: dict, mode: str) -> str:
    if mode == "incremental":
        return "YH | Hot Lead"
    try:
        sent = datetime.fromisoformat(lead["EmailSentDttm"].replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - sent).days
        return "YH | Hot Lead" if age_days <= 14 else "YH | Long Term Nurture"
    except Exception:
        return "YH | Long Term Nurture"


async def _fub_create_or_update_person(client: httpx.AsyncClient, lead: dict, fub_person_id, mode: str):
    """Create or update a FUB person and register the inquiry event."""
    base = _cfg("fub_api_base_url", "https://api.followupboss.com/v1")

    first, last = _parse_name(lead["RequestorName"] or "")
    stage = _fub_stage(lead, mode)
    event_id = lead["EventId"]
    emails = [{"value": lead["RequestorEmailAddress"]}] if lead.get("RequestorEmailAddress") else []
    phones = [{"value": lead["RequestorPhone"]}] if lead.get("RequestorPhone") else []

    if mode == "backfill" and not fub_person_id:
        # ── Backfill, new person: POST /people with createdAt to backdate ──
        create_body = {
            "firstName": first,
            "lastName": last,
            "emails": emails,
            "phones": phones,
            "tags": ["Eventective"],
            "stage": stage,
            "source": "Eventective.com",
            "contacted": False,
            "customPrimaryVenueInterest": lead.get("ProviderName", ""),
            "createdAt": lead.get("EmailSentDttm") or "",
        }
        resp = await _fub_request(client, "POST", f"{base}/people", json=create_body)
        person_id = resp.json()["id"]

        # Register the inquiry event with occurredAt (historical, no workflows)
        event_body = {
            "source": "Eventective.com",
            "system": _cfg("fub_system_header", "IRBY-GROUP-FUB-API"),
            "type": "Inquiry",
            "message": lead.get("InformationRequested") or "",
            "occurredAt": lead.get("EmailSentDttm") or "",
            "contacted": False,
            "sourceUrl": f"https://www.eventective.com/myeventective/#/crm/Event/Messages/{event_id}",
            "campaign": {"source": "Eventective.com"},
            "person": {"id": person_id},
        }
        await _fub_request(client, "POST", f"{base}/events", json=event_body)

    else:
        # ── Incremental (new leads) or existing person: POST /events ──
        person = {
            "firstName": first,
            "lastName": last,
            "emails": emails,
            "phones": phones,
            "tags": ["Eventective"],
            "stage": stage,
            "customPrimaryVenueInterest": lead.get("ProviderName", ""),
        }
        if fub_person_id:
            person["id"] = fub_person_id

        event_body = {
            "source": "Eventective.com",
            "system": _cfg("fub_system_header", "IRBY-GROUP-FUB-API"),
            "type": "Inquiry",
            "message": lead.get("InformationRequested") or "",
            "contacted": False,
            "sourceUrl": f"https://www.eventective.com/myeventective/#/crm/Event/Messages/{event_id}",
            "campaign": {"source": "Eventective.com"},
            "person": person,
        }
        # Historical occurredAt for backfill existing people; omit for incremental (triggers workflows)
        if mode == "backfill":
            event_body["occurredAt"] = lead.get("EmailSentDttm") or ""

        resp = await _fub_request(client, "POST", f"{base}/events", json=event_body)

        person_id = None
        if resp.status_code in (200, 201):
            result = resp.json()
            person_id = result.get("id") or result.get("person", {}).get("id")
        if not person_id:
            person_id = await _fub_search_person(client, lead.get("RequestorPhone") or "", lead.get("RequestorEmailAddress") or "")
        if not person_id:
            raise ValueError(f"Could not resolve FUB person after events POST for {event_id}")

    # PUT /v1/people to ensure name, stage, contacted, venue are set
    update_body = {
        "firstName": first,
        "lastName": last,
        "stage": stage,
        "contacted": False,
        "customPrimaryVenueInterest": lead.get("ProviderName", ""),
    }
    try:
        await _fub_request(client, "PUT", f"{base}/people/{person_id}", json=update_body)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            # Ghost person — create via POST /people as fallback
            print(f"[fub-sync] person {person_id} is a ghost (404), creating via /people for {event_id}")
            create_body = {
                "firstName": first, "lastName": last,
                "emails": emails, "phones": phones,
                "tags": ["Eventective"], "stage": stage,
                "source": "Eventective.com", "contacted": False,
                "customPrimaryVenueInterest": lead.get("ProviderName", ""),
                "createdAt": lead.get("EmailSentDttm") or "",
            }
            resp2 = await _fub_request(client, "POST", f"{base}/people", json=create_body)
            person_id = resp2.json()["id"]
        else:
            raise

    return person_id


async def _fub_create_note(client: httpx.AsyncClient, person_id: int, body_text: str, subject: str = ""):
    """POST /v1/notes"""
    base = _cfg("fub_api_base_url", "https://api.followupboss.com/v1")

    payload = {
        "personId": person_id,
        "body": body_text,
    }
    if subject:
        payload["subject"] = subject

    resp = await _fub_request(client, "POST", f"{base}/notes", json=payload)
    return resp.json()


async def _fub_export_lead(client: httpx.AsyncClient, lead: dict, mode: str):
    """Export a single Eventective lead + activities to FUB."""
    event_id = lead["EventId"]
    phone = lead.get("RequestorPhone") or ""
    email_addr = lead.get("RequestorEmailAddress") or ""

    # Step 1: search for existing person
    fub_person_id = await _fub_search_person(client, phone, email_addr)

    # Step 2: create/update via events POST
    fub_person_id = await _fub_create_or_update_person(client, lead, fub_person_id, mode)

    if not fub_person_id:
        raise ValueError(f"No person ID returned from FUB for {event_id}")

    # Step 3: lead details note
    details_parts = [f"[Eventective Lead Details]"]
    field_map = [
        ("Venue", "ProviderName"), ("Event", "EventName"), ("Type", "EventType"),
        ("Date", "EventDate"), ("Attendees", "AttendeeCount"), ("Budget", "BudgetValue"),
        ("Duration", "Duration"), ("Location", "DirectLeadLocation"),
        ("Info Requested", "InformationRequested"), ("Services", "ServicesRequested"),
        ("Contact Pref", "RequestorContactPref"), ("Lead Status", "LeadStatus"),
    ]
    for label, key in field_map:
        val = lead.get(key)
        if val is not None and val != "":
            details_parts.append(f"{label}: {val}")

    food_parts = []
    if lead.get("VenueProvidesFood"): food_parts.append("Venue")
    if lead.get("CatererProvidesFood"): food_parts.append("Caterer")
    if lead.get("SelfProvidesFood"): food_parts.append("Self")
    if food_parts:
        details_parts.append(f"Food provided by: {', '.join(food_parts)}")

    details_parts.append(f"Eventective ID: {event_id}")
    details_parts.append(f"Lead received: {lead.get('EmailSentDttm', 'N/A')}")
    await _fub_create_note(client, fub_person_id, "\n".join(details_parts), subject="Eventective Lead Details")

    # Step 4 & 5: activities
    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        'SELECT * FROM eventective_lead_activities WHERE "EventId"=%s ORDER BY "DateTime" ASC',
        (event_id,)
    )
    activities = cur.fetchall()
    con.close()

    messages = []
    timeline_lines = []

    for act in activities:
        atype = act["ActivityTypeCd"]
        dt_long = act["DateTimeLong"] or act["DateTime"] or ""
        dt_iso = act["DateTime"] or ""

        if atype in ("provplnr", "plnrprov"):
            direction = "Outbound" if atype == "provplnr" else "Inbound"
            sender = act["Sender"] or ""
            recipient = act["Recipient"] or ""
            text = act["ResponseText"] or ""
            messages.append(f"[Eventective {direction}] {sender} → {recipient} ({dt_long}):\n{text}")
        elif atype == "ResponseRank":
            timeline_lines.append(f"{dt_long} - {act['ResponseText'] or 'Response ranked'}")
        elif atype in ACTIVITY_LABELS:
            timeline_lines.append(f"{dt_long} - {ACTIVITY_LABELS[atype]}")

    # Create message notes
    for note_body in messages:
        await _fub_create_note(client, fub_person_id, note_body, subject="Eventective Message")

    # Create timeline note
    if timeline_lines:
        timeline_body = f"[Eventective Timeline - {event_id}]\n" + "\n".join(timeline_lines)
        await _fub_create_note(client, fub_person_id, timeline_body, subject="Eventective Timeline")

    # Step 6: mark exported + set local stage (webhook doesn't fire on initial creation)
    now_str = datetime.now(timezone.utc).isoformat()
    stage = _fub_stage(lead, mode)
    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor()
    cur.execute(
        'UPDATE eventective_leads SET fub_exported=1, fub_exported_date=%s, fub_people_id=%s, fub_lead_stage=%s WHERE "EventId"=%s',
        (now_str, str(fub_person_id), stage, event_id)
    )
    cur.execute(
        'UPDATE eventective_lead_activities SET fub_exported=1, fub_exported_date=%s, fub_people_id=%s WHERE "EventId"=%s',
        (now_str, str(fub_person_id), event_id)
    )
    con.commit()
    con.close()

    return fub_person_id


async def _fub_export_new_activities(client: httpx.AsyncClient, state: dict):
    """Export only new (fub_exported=0) activities on already-exported leads."""
    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """SELECT DISTINCT el."EventId", el.fub_people_id
           FROM eventective_leads el
           JOIN eventective_lead_activities ela ON ela."EventId" = el."EventId"
           WHERE el.fub_exported=1 AND el.fub_people_id IS NOT NULL AND ela.fub_exported=0"""
    )
    rows = cur.fetchall()
    con.close()

    for row in rows:
        event_id = row["EventId"]
        fub_people_id = int(row["fub_people_id"])
        try:
            con2 = psycopg2.connect(DATABASE_URL)
            cur2 = con2.cursor(cursor_factory=RealDictCursor)
            cur2.execute(
                'SELECT * FROM eventective_lead_activities WHERE "EventId"=%s AND fub_exported=0 ORDER BY "DateTime" ASC',
                (event_id,)
            )
            activities = cur2.fetchall()
            con2.close()

            messages = []
            timeline_lines = []
            for act in activities:
                atype = act["ActivityTypeCd"]
                dt_long = act["DateTimeLong"] or act["DateTime"] or ""
                if atype in ("provplnr", "plnrprov"):
                    direction = "Outbound" if atype == "provplnr" else "Inbound"
                    sender = act["Sender"] or ""
                    recipient = act["Recipient"] or ""
                    text = act["ResponseText"] or ""
                    messages.append(f"[Eventective {direction}] {sender} → {recipient} ({dt_long}):\n{text}")
                elif atype == "ResponseRank":
                    timeline_lines.append(f"{dt_long} - {act['ResponseText'] or 'Response ranked'}")
                elif atype in ACTIVITY_LABELS:
                    timeline_lines.append(f"{dt_long} - {ACTIVITY_LABELS[atype]}")

            for note_body in messages:
                await _fub_create_note(client, fub_people_id, note_body, subject="Eventective Message")

            if timeline_lines:
                timeline_body = f"[Eventective Timeline - {event_id}]\n" + "\n".join(timeline_lines)
                await _fub_create_note(client, fub_people_id, timeline_body, subject="Eventective Timeline")

            now_str = datetime.now(timezone.utc).isoformat()
            con3 = psycopg2.connect(DATABASE_URL)
            cur3 = con3.cursor()
            cur3.execute(
                'UPDATE eventective_lead_activities SET fub_exported=1, fub_exported_date=%s, fub_people_id=%s WHERE "EventId"=%s AND fub_exported=0',
                (now_str, str(fub_people_id), event_id)
            )
            con3.commit()
            con3.close()

            state["progress"]["activities_exported"] = state["progress"].get("activities_exported", 0) + len(activities)
            print(f"[fub-incremental] exported {len(activities)} new activities for {event_id} → FUB person {fub_people_id}")
        except Exception as e:
            err_msg = f"activities {event_id}: {e}"
            state["errors"].append(err_msg)
            print(f"[fub-incremental] FAILED {err_msg}")
            traceback.print_exc()


async def _fub_incremental_export():
    """Export new leads and new activities on existing leads to FUB."""
    state = fub_sync_state["incremental"]
    if state["running"]:
        print("[fub-incremental] already running, skipping")
        return
    state["running"] = True
    state["errors"] = []
    state["progress"] = {"exported": 0, "failed": 0, "total": 0, "activities_exported": 0, "current_event_id": None}

    try:
        # Pass A — new leads (fub_exported=0)
        con = psycopg2.connect(DATABASE_URL)
        cur = con.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            'SELECT * FROM eventective_leads WHERE fub_exported=0 ORDER BY "EmailSentDttm" ASC'
        )
        leads = cur.fetchall()
        con.close()

        state["progress"]["total"] = len(leads)

        async with httpx.AsyncClient(timeout=30.0) as client:
            for lead in leads:
                eid = lead["EventId"]
                state["progress"]["current_event_id"] = eid
                try:
                    pid = await _fub_export_lead(client, dict(lead), mode="incremental")
                    state["progress"]["exported"] += 1
                    print(f"[fub-incremental] exported new lead {eid} → FUB person {pid}")
                except Exception as e:
                    state["progress"]["failed"] += 1
                    err_msg = f"{eid}: {e}"
                    state["errors"].append(err_msg)
                    print(f"[fub-incremental] FAILED {err_msg}")
                    traceback.print_exc()

            # Pass B — new activities on already-exported leads
            await _fub_export_new_activities(client, state)
    finally:
        state["running"] = False
        state["progress"]["current_event_id"] = None
        failed = state["progress"].get("failed", 0)
        if failed > 0:
            notify_error(
                f"FUB incremental export completed with {failed} error(s)",
                "\n".join(state["errors"][-20:])
            )


async def _fub_sync_task(mode: str, limit: int = 0, order: str = "asc"):
    """Background task: export unexported leads to FUB."""
    state = fub_sync_state[order]
    state["running"] = True
    state["errors"] = []
    state["progress"] = {"exported": 0, "failed": 0, "total": 0, "current_event_id": None}
    tag = f"fub-sync-{order}"

    try:
        con = psycopg2.connect(DATABASE_URL)
        cur = con.cursor(cursor_factory=RealDictCursor)
        direction = "DESC" if order == "desc" else "ASC"
        query = f'SELECT * FROM eventective_leads WHERE fub_exported=0 ORDER BY "EmailSentDttm" {direction}'
        if limit > 0:
            query += f" LIMIT {limit}"
        cur.execute(query)
        leads = cur.fetchall()
        con.close()

        state["progress"]["total"] = len(leads)

        async with httpx.AsyncClient(timeout=30.0) as client:
            for lead in leads:
                # Re-check fub_exported in case the other direction already got this lead
                con2 = psycopg2.connect(DATABASE_URL)
                cur2 = con2.cursor()
                cur2.execute('SELECT fub_exported FROM eventective_leads WHERE "EventId"=%s', (lead["EventId"],))
                already = cur2.fetchone()
                con2.close()
                if already and already[0] == 1:
                    state["progress"]["total"] -= 1
                    continue

                eid = lead["EventId"]
                state["progress"]["current_event_id"] = eid
                try:
                    pid = await _fub_export_lead(client, dict(lead), mode)
                    state["progress"]["exported"] += 1
                    print(f"[{tag}] exported {eid} → FUB person {pid}")
                except Exception as e:
                    state["progress"]["failed"] += 1
                    err_msg = f"{eid}: {e}"
                    state["errors"].append(err_msg)
                    print(f"[{tag}] FAILED {err_msg}")
                    traceback.print_exc()
    finally:
        state["running"] = False
        state["progress"]["current_event_id"] = None
        failed = state["progress"].get("failed", 0)
        if failed > 0:
            notify_error(
                f"FUB sync ({order}) completed with {failed} error(s)",
                "\n".join(state["errors"][-20:])
            )

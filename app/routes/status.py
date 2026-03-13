from datetime import datetime, timezone

from fastapi import APIRouter
from psycopg2.extras import RealDictCursor

from app import state as state_mod
from app.db import get_db, get_meta
from app.utils import days_until_event, classify_thread, compute_urgency

router = APIRouter()


@router.get("/status")
async def status():
    bm = state_mod.get_bm()
    con = get_db()
    cur = con.cursor(cursor_factory=RealDictCursor)
    now  = datetime.now(timezone.utc)

    action_required = []
    watching        = []
    upcoming        = []

    # Recent leads (last 30 days)
    cur.execute("""
        SELECT "EventId", "PlannerName", "LastActivityDttm", "EventDate",
               "AttendeeCount", "EventType", "ProviderName", "LeadStatus", "EmailSentDttm",
               "RequestorName", "DatePossible1", "BudgetValue"
        FROM eventective_leads
        WHERE "LastActivityDttm" >= %s
        ORDER BY "LastActivityDttm" DESC
    """, ((datetime.fromtimestamp(now.timestamp() - 30*86400, tz=timezone.utc)).isoformat(),))
    recent = cur.fetchall()

    leads_30d = 0
    response_times = []
    first_responder_count = 0

    cur.execute('SELECT COUNT(*) as c FROM eventective_leads')
    all_leads = cur.fetchone()["c"]

    for r in recent:
        event_id   = r["EventId"]
        event_date = r["DatePossible1"] or r["EventDate"]
        d_until    = days_until_event(event_date)

        cur.execute(
            'SELECT * FROM eventective_lead_activities WHERE "EventId"=%s ORDER BY "DateTime"', (event_id,)
        )
        acts = [dict(a) for a in cur.fetchall()]
        signals = classify_thread(acts)
        urg, reasons = compute_urgency(d_until, signals["they_replied_to_us"], signals["hours_since_our_msg"])

        # Count leads received in last 30d
        received_acts = [a for a in acts if a["ActivityTypeCd"] in ("LeadReceived", "ReferralReceived")]
        if received_acts:
            leads_30d += 1

        # Response time stats
        lead_recv = next((a for a in acts if a["ActivityTypeCd"] in ("LeadReceived", "ReferralReceived")), None)
        our_first = next((a for a in acts if a["ActivityTypeCd"] == "provplnr"), None)
        if lead_recv and our_first and lead_recv["DateTime"] and our_first["DateTime"]:
            try:
                t1 = datetime.fromisoformat(lead_recv["DateTime"]).replace(tzinfo=timezone.utc)
                t2 = datetime.fromisoformat(our_first["DateTime"]).replace(tzinfo=timezone.utc)
                response_times.append((t2 - t1).total_seconds() / 60)
            except Exception:
                pass

        # First responder
        rank_acts = [a for a in acts if a["ActivityTypeCd"] == "ResponseRank"]
        if any("first business" in (a["ResponseText"] or "").lower() for a in rank_acts):
            first_responder_count += 1

        # Classify for action/watching
        name  = r["RequestorName"] or r["PlannerName"]
        venue = r["ProviderName"]

        if signals["they_replied_to_us"] and signals["we_replied"]:
            # Check if we already replied AFTER their last reply
            their_last = max((a["DateTime"] for a in acts if a["ActivityTypeCd"] == "plnrprov"), default=None)
            our_last   = max((a["DateTime"] for a in acts if a["ActivityTypeCd"] == "provplnr"), default=None)
            if their_last and our_last and their_last > our_last:
                action_required.append({
                    "event_id": event_id, "name": name, "venue": venue,
                    "reason": f"replied to us — awaiting your response", "urgency": "HIGH",
                    "their_last_message": signals["last_their_message"],
                })
        elif urg in ("HIGH", "MEDIUM") and d_until is not None:
            watching.append({
                "event_id": event_id, "name": name, "venue": venue,
                "reason":   ", ".join(reasons) or urg,
                "urgency":  urg,
            })

        # Upcoming events
        if d_until is not None and 0 <= d_until <= 60:
            upcoming.append({
                "name":     name,
                "venue":    venue,
                "date":     event_date.split("T")[0] if event_date else None,
                "days_away": d_until,
                "status":   "needs reply" if signals["they_replied_to_us"] else
                            "waiting on lead" if signals["we_replied"] else "no response yet",
            })

    upcoming.sort(key=lambda x: x["days_away"])
    watching.sort(key=lambda x: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}[x["urgency"]])

    session_valid = await bm.check_session()

    last_sync = get_meta("last_sync_time")
    avg_resp  = round(sum(response_times) / len(response_times), 0) if response_times else None
    resp_rate = round(len(response_times) / leads_30d * 100, 1) if leads_30d else None
    fr_rate   = round(first_responder_count / leads_30d * 100, 1) if leads_30d else None

    con.close()
    return {
        "as_of": now.isoformat(),
        "session": {
            "authenticated": session_valid,
        },
        "last_sync": last_sync,
        "total_leads_in_db": all_leads,
        "action_required": action_required,
        "watching":        watching[:10],
        "upcoming_events": upcoming[:10],
        "stats_30d": {
            "leads_received":       leads_30d,
            "response_rate_pct":    resp_rate,
            "avg_response_minutes": avg_resp,
            "first_responder_rate_pct": fr_rate,
        },
    }

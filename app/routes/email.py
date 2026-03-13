from datetime import datetime, timezone

from fastapi import APIRouter
from psycopg2.extras import RealDictCursor

from app.db import get_db, get_meta
from app.email import send_email

router = APIRouter()


@router.post("/notify_error")
async def api_notify_error(subject: str = "API Error", detail: str = ""):
    """Send an error notification email."""
    result = send_email(
        f"[Venue Scrapper] {subject}",
        f"""
        <h2 style="color:#c0392b;">⚠️ Venue Scrapper Error</h2>
        <p><strong>Time:</strong> {datetime.now(timezone.utc).isoformat()}</p>
        <p><strong>Subject:</strong> {subject}</p>
        <p><strong>Detail:</strong></p>
        <pre style="background:#f8f8f8;padding:12px;border-radius:4px;">{detail}</pre>
        """
    )
    return result


@router.get("/daily_report")
async def daily_report():
    """Generate and email a daily summary of all Eventective activity in the last 24 hours."""
    con = get_db()
    cur = con.cursor(cursor_factory=RealDictCursor)
    now = datetime.now(timezone.utc)
    cutoff = datetime.fromtimestamp(now.timestamp() - 86400, tz=timezone.utc).isoformat()

    # New leads (received in last 24h)
    cur.execute("""
        SELECT "EventId", "RequestorName", "PlannerName", "ProviderName", "EventType",
               "DatePossible1", "EventDate", "AttendeeCount", "BudgetValue",
               "RequestorPhone", "RequestorEmailAddress", "InformationRequested",
               "EmailSentDttm"
        FROM eventective_leads
        WHERE "EmailSentDttm" >= %s
        ORDER BY "EmailSentDttm" DESC
    """, (cutoff,))
    new_leads = cur.fetchall()

    # Leads with new activity in last 24h (excluding brand new ones)
    new_lead_ids = {r["EventId"] for r in new_leads}
    cur.execute("""
        SELECT DISTINCT el."EventId", el."RequestorName", el."PlannerName",
               el."ProviderName", el."EventType", el."LastActivityDttm",
               el."LeadStatus", el."DatePossible1", el."EventDate"
        FROM eventective_leads el
        WHERE el."LastActivityDttm" >= %s
        ORDER BY el."LastActivityDttm" DESC
    """, (cutoff,))
    active_leads = cur.fetchall()
    active_leads = [r for r in active_leads if r["EventId"] not in new_lead_ids]

    # All activities in last 24h grouped by lead
    cur.execute("""
        SELECT a."EventId", a."DateTime", a."ActivityTypeCd", a."Sender",
               a."Recipient", a."ResponseText",
               el."RequestorName", el."PlannerName", el."ProviderName"
        FROM eventective_lead_activities a
        JOIN eventective_leads el ON el."EventId" = a."EventId"
        WHERE a."DateTime" >= %s
        ORDER BY a."DateTime" DESC
    """, (cutoff,))
    recent_activities = cur.fetchall()

    # Stats
    cur.execute('SELECT COUNT(*) as c FROM eventective_leads')
    total_leads_db = cur.fetchone()["c"]
    last_sync = get_meta("last_sync_time")
    con.close()

    # Group activities by lead
    activities_by_lead = {}
    for a in recent_activities:
        eid = a["EventId"]
        if eid not in activities_by_lead:
            activities_by_lead[eid] = {
                "name": a["RequestorName"] or a["PlannerName"],
                "venue": a["ProviderName"],
                "activities": []
            }
        activities_by_lead[eid]["activities"].append(dict(a))

    type_labels = {
        "LeadReceived": "📩 Lead Received",
        "ReferralReceived": "📩 Referral Received",
        "provplnr": "📤 Our Reply",
        "plnrprov": "📥 Their Reply",
        "ReadMsgs": "👁️ Read",
        "ResponseRank": "🏆 Response Rank",
        "NoInterest": "❌ No Interest",
    }

    # Build HTML
    html_parts = []
    html_parts.append(f"""
    <div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;">
    <h1 style="color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:8px;">
        📊 Eventective Daily Report
    </h1>
    <p style="color:#7f8c8d;">
        {now.strftime('%B %d, %Y')} &middot;
        Total leads in DB: <strong>{total_leads_db}</strong> &middot;
        Last sync: {last_sync or 'never'}
    </p>
    """)

    # Summary counts
    our_replies = len([a for a in recent_activities if a["ActivityTypeCd"] == "provplnr"])
    their_replies = len([a for a in recent_activities if a["ActivityTypeCd"] == "plnrprov"])
    html_parts.append(f"""
    <table style="width:100%;border-collapse:collapse;margin:16px 0;">
        <tr style="background:#3498db;color:white;">
            <th style="padding:8px;text-align:center;">New Leads</th>
            <th style="padding:8px;text-align:center;">Active Leads</th>
            <th style="padding:8px;text-align:center;">Our Replies</th>
            <th style="padding:8px;text-align:center;">Their Replies</th>
            <th style="padding:8px;text-align:center;">Total Activities</th>
        </tr>
        <tr style="text-align:center;font-size:24px;font-weight:bold;">
            <td style="padding:12px;">{len(new_leads)}</td>
            <td style="padding:12px;">{len(active_leads)}</td>
            <td style="padding:12px;">{our_replies}</td>
            <td style="padding:12px;">{their_replies}</td>
            <td style="padding:12px;">{len(recent_activities)}</td>
        </tr>
    </table>
    """)

    # New leads section
    if new_leads:
        html_parts.append('<h2 style="color:#27ae60;">🆕 New Leads</h2>')
        for lead in new_leads:
            name = lead["RequestorName"] or lead["PlannerName"]
            event_date = (lead["DatePossible1"] or lead["EventDate"] or "").split("T")[0]
            html_parts.append(f"""
            <div style="background:#f0f9f0;border-left:4px solid #27ae60;padding:12px;margin:8px 0;border-radius:4px;">
                <strong>{name}</strong> — {lead["ProviderName"]}<br>
                <span style="color:#555;">
                    {lead["EventType"] or "Event"} &middot;
                    {event_date or "No date"} &middot;
                    {lead["AttendeeCount"] or "?"} guests &middot;
                    Budget: {lead["BudgetValue"] or "Not specified"}
                </span><br>
                {f'<span>📞 {lead["RequestorPhone"]}</span> &middot; ' if lead["RequestorPhone"] else ''}
                {f'<span>✉️ {lead["RequestorEmailAddress"]}</span>' if lead["RequestorEmailAddress"] else ''}
                {f'<br><em style="color:#888;">"{lead["InformationRequested"][:200]}"</em>' if lead["InformationRequested"] else ''}
            </div>
            """)
    else:
        html_parts.append('<h2 style="color:#27ae60;">🆕 New Leads</h2><p style="color:#999;">None in the last 24 hours.</p>')

    # Active leads with activity
    if activities_by_lead:
        html_parts.append('<h2 style="color:#2980b9;">💬 Lead Activity</h2>')
        for eid, info in activities_by_lead.items():
            html_parts.append(f"""
            <div style="background:#f0f4f8;border-left:4px solid #2980b9;padding:12px;margin:8px 0;border-radius:4px;">
                <strong>{info["name"]}</strong> — {info["venue"]}
                <span style="color:#888;font-size:12px;">({eid})</span>
            """)
            for act in info["activities"]:
                label = type_labels.get(act["ActivityTypeCd"], act["ActivityTypeCd"])
                text = act["ResponseText"] or ""
                if len(text) > 300:
                    text = text[:300] + "..."
                time_str = (act["DateTime"] or "").replace("T", " ").split(".")[0]
                html_parts.append(f"""
                <div style="margin:6px 0 6px 16px;padding:6px;background:white;border-radius:3px;">
                    <span style="font-size:12px;color:#888;">{time_str}</span>
                    <strong>{label}</strong>
                    {f' — {act["Sender"]}' if act["Sender"] else ''}
                    {f'<br><span style="color:#333;">{text}</span>' if text else ''}
                </div>
                """)
            html_parts.append('</div>')
    else:
        html_parts.append('<h2 style="color:#2980b9;">💬 Lead Activity</h2><p style="color:#999;">No activity in the last 24 hours.</p>')

    html_parts.append("""
    <hr style="border:none;border-top:1px solid #ddd;margin:24px 0;">
    <p style="color:#aaa;font-size:12px;">
        Generated by Venue Scrapper &middot; Yellowhammer Hospitality
    </p>
    </div>
    """)

    html_body = "\n".join(html_parts)

    # Send it
    result = send_email(
        f"Eventective Daily Report — {now.strftime('%b %d, %Y')}",
        html_body
    )

    return {
        "report_sent": result.get("success", False),
        "email_result": result,
        "summary": {
            "new_leads": len(new_leads),
            "active_leads": len(active_leads),
            "our_replies_24h": our_replies,
            "their_replies_24h": their_replies,
            "total_activities_24h": len(recent_activities),
        }
    }

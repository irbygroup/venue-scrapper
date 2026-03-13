import os
from datetime import datetime, timezone
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import DATABASE_URL


def get_db():
    con = psycopg2.connect(DATABASE_URL)
    return con


def init_db():
    con = get_db()
    cur = con.cursor()
    schema_path = os.path.join(os.path.dirname(__file__), "..", "schema_pg.sql")
    cur.execute(open(schema_path).read())
    con.commit()
    con.close()


def get_meta(key: str) -> Optional[str]:
    con = get_db()
    cur = con.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT value FROM sync_meta WHERE key=%s", (key,))
    row = cur.fetchone()
    con.close()
    return row["value"] if row else None


def set_meta(key: str, value: str):
    con = get_db()
    cur = con.cursor()
    cur.execute("INSERT INTO sync_meta VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
    con.commit()
    con.close()


def upsert_inbox_lead(cur, lead: dict):
    cur.execute("""
        INSERT INTO eventective_leads (
            "EventId", "RequestGuid", "RequestProviderNum", "ProviderNum", "ProviderName",
            "EmailSentDttm", "IsFlagged", "PurchasedLead", "DirectLead", "EventDate",
            "AttendeeCount", "PlannerName", "PlannerStatusCd", "LastActivityDttm",
            "LastActivity", "LastActivityType", "LastActivityIsAutoResponse",
            "LastActivitySender", "AvatarMediaNum", "IsRead", "UnreadCount",
            "LeadStatus", "IsAvailable", "DateAvailableType", "EventType",
            "EventNum", "GmtOffsetHours", "Source"
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT("EventId") DO UPDATE SET
            "RequestGuid"=excluded."RequestGuid", "RequestProviderNum"=excluded."RequestProviderNum",
            "ProviderNum"=excluded."ProviderNum", "ProviderName"=excluded."ProviderName",
            "EmailSentDttm"=excluded."EmailSentDttm", "IsFlagged"=excluded."IsFlagged",
            "PurchasedLead"=excluded."PurchasedLead", "DirectLead"=excluded."DirectLead",
            "EventDate"=excluded."EventDate", "AttendeeCount"=excluded."AttendeeCount",
            "PlannerName"=excluded."PlannerName", "PlannerStatusCd"=excluded."PlannerStatusCd",
            "LastActivityDttm"=excluded."LastActivityDttm", "LastActivity"=excluded."LastActivity",
            "LastActivityType"=excluded."LastActivityType",
            "LastActivityIsAutoResponse"=excluded."LastActivityIsAutoResponse",
            "LastActivitySender"=excluded."LastActivitySender", "AvatarMediaNum"=excluded."AvatarMediaNum",
            "IsRead"=excluded."IsRead", "UnreadCount"=excluded."UnreadCount",
            "LeadStatus"=excluded."LeadStatus", "IsAvailable"=excluded."IsAvailable",
            "DateAvailableType"=excluded."DateAvailableType", "EventType"=excluded."EventType",
            "EventNum"=excluded."EventNum", "GmtOffsetHours"=excluded."GmtOffsetHours",
            "Source"=excluded."Source"
    """, (
        lead.get("EventId"), lead.get("RequestGuid"), lead.get("RequestProviderNum"),
        lead.get("ProviderNum"), lead.get("ProviderName"), lead.get("EmailSentDttm"),
        lead.get("IsFlagged"), lead.get("PurchasedLead"), lead.get("DirectLead"),
        lead.get("EventDate"), lead.get("AttendeeCount"), lead.get("PlannerName"),
        lead.get("PlannerStatusCd"), lead.get("LastActivityDttm"), lead.get("LastActivity"),
        lead.get("LastActivityType"), lead.get("LastActivityIsAutoResponse"),
        lead.get("LastActivitySender"), lead.get("AvatarMediaNum"), lead.get("IsRead"),
        lead.get("UnreadCount"), lead.get("LeadStatus"), lead.get("IsAvailable"),
        lead.get("DateAvailableType"), lead.get("EventType"), lead.get("EventNum"),
        lead.get("GmtOffsetHours"), lead.get("Source"),
    ))


def upsert_lead_details(cur, event_id: str, d: dict):
    """Update the detail columns on the merged eventective_leads row."""
    cur.execute("""
        UPDATE eventective_leads SET
            "ProviderNum"=%s, "ProviderNameFull"=%s, "ProviderEmailGeneric"=%s,
            "RequestorName"=%s, "RequestorEmailAddress"=%s, "RequestorPhone"=%s,
            "RequestorContactPref"=%s, "EventName"=%s, "EventType"=%s,
            "AttendeeCount"=%s, "DatePossible1"=%s, "DateAvailable"=%s,
            "DateAvailableType"=%s, "DateFlexible"=%s, "Duration"=%s,
            "TimePossible1"=%s, "LeadStatus"=%s, "EmailSentDttm"=%s,
            "DirectLead"=%s, "PurchasedLead"=%s, "BudgetValue"=%s,
            "DirectLeadLocation"=%s, "InformationRequested"=%s, "ServicesRequested"=%s,
            "FoodRequired"=%s, "VenueProvidesFood"=%s, "CatererProvidesFood"=%s,
            "SelfProvidesFood"=%s, "IsFlagged"=%s, "IsRead"=%s, "IsEmailReguser"=%s,
            "PhoneViewed"=%s, "PhoneViewedDttm"=%s, "EmailViewed"=%s, "EmailViewedDttm"=%s,
            "ConfirmReceivedDttm"=%s, "IsStripeEnabled"=%s, "IsSquareEnabled"=%s,
            "Source"=%s, "ScrapedAt"=%s
        WHERE "EventId"=%s
    """, (
        d.get("ProviderNum"), d.get("ProviderNameFull"), d.get("ProviderEmailGeneric"),
        d.get("RequestorName"), d.get("RequestorEmailAddress"), d.get("RequestorPhone"),
        d.get("RequestorContactPref"), d.get("EventName"), d.get("EventType"),
        d.get("AttendeeCount"), d.get("DatePossible1"), d.get("DateAvailable"),
        d.get("DateAvailableType"), d.get("DateFlexible"), d.get("Duration"),
        d.get("TimePossible1"), d.get("LeadStatus"), d.get("EmailSentDttm"),
        d.get("DirectLead"), d.get("PurchasedLead"), d.get("BudgetValue"),
        d.get("DirectLeadLocation"), d.get("InformationRequested"), d.get("ServicesRequested"),
        d.get("FoodRequired"), d.get("VenueProvidesFood"), d.get("CatererProvidesFood"),
        d.get("SelfProvidesFood"), d.get("IsFlagged"), d.get("IsRead"),
        d.get("IsEmailReguser"), d.get("PhoneViewed"), d.get("PhoneViewedDttm"),
        d.get("EmailViewed"), d.get("EmailViewedDttm"), d.get("ConfirmReceivedDttm"),
        d.get("IsStripeEnabled"), d.get("IsSquareEnabled"), d.get("Source"),
        datetime.now(timezone.utc).isoformat(),
        event_id,
    ))


def upsert_activities(cur, event_id: str, activities: list) -> int:
    inserted = 0
    for a in activities:
        try:
            cur.execute("""
                INSERT INTO eventective_lead_activities
                ("EventId", "DateTime", "DateTimeLong", "ActivityTypeCd", "Sender", "Recipient",
                 "ResponseText", "IsRead", "ResponseNum", "HasAttachments", "IsAutoResponse",
                 "EventDocumentNum", "EventPaymentNum", "PaymentAmount", "ReguserNum", "ActionNum")
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT ("EventId", "DateTime", "ActivityTypeCd", "ResponseNum") DO NOTHING
            """, (
                event_id, a.get("DateTime"), a.get("DateTimeLong"), a.get("ActivityTypeCd"),
                a.get("Sender"), a.get("Recipient"), a.get("ResponseText"), a.get("IsRead"),
                a.get("ResponseNum"), a.get("HasAttachments"), a.get("IsAutoResponse"),
                a.get("EventDocumentNum"), a.get("EventPaymentNum"), a.get("PaymentAmount"),
                a.get("ReguserNum"), a.get("ActionNum"),
            ))
            inserted += cur.rowcount
        except Exception:
            pass
    return inserted

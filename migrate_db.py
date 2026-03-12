"""
Migration: Merge leads + lead_details into eventective_leads,
rename activities to eventective_lead_activities,
add FUB tracking fields.
"""
import sqlite3
import sys

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "leads.db"

con = sqlite3.connect(DB_PATH)
con.execute("PRAGMA journal_mode=WAL")

print(f"Migrating {DB_PATH}...")

# ── 1. Create new merged table ──────────────────────────────────────────────
con.executescript("""
    CREATE TABLE IF NOT EXISTS eventective_leads (
        -- PK
        EventId                 TEXT PRIMARY KEY,

        -- Inbox metadata (from leads)
        RequestGuid             TEXT,
        RequestProviderNum      INTEGER,
        ProviderNum             INTEGER,
        ProviderName            TEXT,
        EmailSentDttm           TEXT,
        IsFlagged               INTEGER,
        PurchasedLead           INTEGER,
        DirectLead              INTEGER,
        EventDate               TEXT,
        AttendeeCount           INTEGER,
        PlannerName             TEXT,
        PlannerStatusCd         TEXT,
        LastActivityDttm        TEXT,
        LastActivity            TEXT,
        LastActivityType        TEXT,
        LastActivityIsAutoResponse INTEGER,
        LastActivitySender      TEXT,
        AvatarMediaNum          INTEGER,
        IsRead                  INTEGER,
        UnreadCount             INTEGER,
        LeadStatus              TEXT,
        IsAvailable             INTEGER,
        DateAvailableType       TEXT,
        EventType               TEXT,
        EventNum                INTEGER,
        GmtOffsetHours          REAL,
        Source                  TEXT,
        DetailScrapedAt         TEXT,

        -- Detail fields (from lead_details)
        ProviderNameFull        TEXT,
        ProviderEmailGeneric    TEXT,
        RequestorName           TEXT,
        RequestorEmailAddress   TEXT,
        RequestorPhone          TEXT,
        RequestorContactPref    TEXT,
        EventName               TEXT,
        DatePossible1           TEXT,
        DateAvailable           INTEGER,
        DateFlexible            INTEGER,
        Duration                TEXT,
        TimePossible1           TEXT,
        BudgetValue             REAL,
        DirectLeadLocation      TEXT,
        InformationRequested    TEXT,
        ServicesRequested       TEXT,
        FoodRequired            INTEGER,
        VenueProvidesFood       INTEGER,
        CatererProvidesFood     INTEGER,
        SelfProvidesFood        INTEGER,
        IsEmailReguser          INTEGER,
        PhoneViewed             INTEGER,
        PhoneViewedDttm         TEXT,
        EmailViewed             INTEGER,
        EmailViewedDttm         TEXT,
        ConfirmReceivedDttm     TEXT,
        IsStripeEnabled         INTEGER,
        IsSquareEnabled         INTEGER,
        ScrapedAt               TEXT,

        -- FUB tracking
        fub_exported            INTEGER DEFAULT 0,
        fub_exported_date       TEXT,
        fub_people_id           TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_el_lastactivity ON eventective_leads(LastActivityDttm);
    CREATE INDEX IF NOT EXISTS idx_el_fub_exported ON eventective_leads(fub_exported);

    CREATE TABLE IF NOT EXISTS eventective_lead_activities (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        EventId             TEXT NOT NULL,
        DateTime            TEXT,
        DateTimeLong        TEXT,
        ActivityTypeCd      TEXT,
        Sender              TEXT,
        Recipient           TEXT,
        ResponseText        TEXT,
        IsRead              INTEGER,
        ResponseNum         INTEGER,
        HasAttachments      INTEGER,
        IsAutoResponse      INTEGER,
        EventDocumentNum    INTEGER,
        EventPaymentNum     INTEGER,
        PaymentAmount       REAL,
        ReguserNum          INTEGER,
        ActionNum           INTEGER,

        -- FUB tracking
        fub_exported            INTEGER DEFAULT 0,
        fub_exported_date       TEXT,
        fub_people_id           TEXT,

        UNIQUE(EventId, DateTime, ActivityTypeCd, ResponseNum)
    );

    CREATE INDEX IF NOT EXISTS idx_ela_eventid ON eventective_lead_activities(EventId);
    CREATE INDEX IF NOT EXISTS idx_ela_fub_exported ON eventective_lead_activities(fub_exported);
""")

# ── 2. Migrate data ─────────────────────────────────────────────────────────

# Check if old tables exist
old_tables = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
).fetchall()]

if "leads" in old_tables and "lead_details" in old_tables:
    # Merge leads + lead_details into eventective_leads
    # For overlapping columns, prefer lead_details values (more detailed) with leads as fallback
    count = con.execute("""
        INSERT OR IGNORE INTO eventective_leads
        SELECT
            l.EventId,
            l.RequestGuid, l.RequestProviderNum,
            COALESCE(ld.ProviderNum, l.ProviderNum),
            l.ProviderName,
            COALESCE(ld.EmailSentDttm, l.EmailSentDttm),
            COALESCE(ld.IsFlagged, l.IsFlagged),
            COALESCE(ld.PurchasedLead, l.PurchasedLead),
            COALESCE(ld.DirectLead, l.DirectLead),
            l.EventDate,
            COALESCE(ld.AttendeeCount, l.AttendeeCount),
            l.PlannerName, l.PlannerStatusCd,
            l.LastActivityDttm, l.LastActivity, l.LastActivityType,
            l.LastActivityIsAutoResponse, l.LastActivitySender,
            l.AvatarMediaNum,
            COALESCE(ld.IsRead, l.IsRead),
            l.UnreadCount,
            COALESCE(ld.LeadStatus, l.LeadStatus),
            l.IsAvailable,
            COALESCE(ld.DateAvailableType, l.DateAvailableType),
            COALESCE(ld.EventType, l.EventType),
            l.EventNum, l.GmtOffsetHours,
            COALESCE(ld.Source, l.Source),
            l.DetailScrapedAt,
            -- lead_details only fields
            ld.ProviderNameFull, ld.ProviderEmailGeneric,
            ld.RequestorName, ld.RequestorEmailAddress, ld.RequestorPhone,
            ld.RequestorContactPref, ld.EventName,
            ld.DatePossible1, ld.DateAvailable, ld.DateFlexible,
            ld.Duration, ld.TimePossible1, ld.BudgetValue,
            ld.DirectLeadLocation, ld.InformationRequested, ld.ServicesRequested,
            ld.FoodRequired, ld.VenueProvidesFood, ld.CatererProvidesFood,
            ld.SelfProvidesFood, ld.IsEmailReguser,
            ld.PhoneViewed, ld.PhoneViewedDttm,
            ld.EmailViewed, ld.EmailViewedDttm,
            ld.ConfirmReceivedDttm, ld.IsStripeEnabled, ld.IsSquareEnabled,
            ld.ScrapedAt,
            -- FUB defaults
            0, NULL, NULL
        FROM leads l
        LEFT JOIN lead_details ld ON ld.EventId = l.EventId
    """).rowcount
    print(f"  Migrated {count} leads -> eventective_leads")

if "activities" in old_tables:
    count = con.execute("""
        INSERT OR IGNORE INTO eventective_lead_activities
        (EventId, DateTime, DateTimeLong, ActivityTypeCd, Sender, Recipient,
         ResponseText, IsRead, ResponseNum, HasAttachments, IsAutoResponse,
         EventDocumentNum, EventPaymentNum, PaymentAmount, ReguserNum, ActionNum,
         fub_exported, fub_exported_date, fub_people_id)
        SELECT
            EventId, DateTime, DateTimeLong, ActivityTypeCd, Sender, Recipient,
            ResponseText, IsRead, ResponseNum, HasAttachments, IsAutoResponse,
            EventDocumentNum, EventPaymentNum, PaymentAmount, ReguserNum, ActionNum,
            0, NULL, NULL
        FROM activities
    """).rowcount
    print(f"  Migrated {count} activities -> eventective_lead_activities")

con.commit()

# ── 3. Drop old tables ──────────────────────────────────────────────────────
if "leads" in old_tables:
    con.execute("DROP TABLE leads")
    print("  Dropped old 'leads' table")
if "lead_details" in old_tables:
    con.execute("DROP TABLE lead_details")
    print("  Dropped old 'lead_details' table")
if "activities" in old_tables:
    con.execute("DROP TABLE activities")
    print("  Dropped old 'activities' table")

con.commit()

# ── 4. Verify ────────────────────────────────────────────────────────────────
el = con.execute("SELECT COUNT(*) FROM eventective_leads").fetchone()[0]
ea = con.execute("SELECT COUNT(*) FROM eventective_lead_activities").fetchone()[0]
print(f"\nDone! eventective_leads: {el}, eventective_lead_activities: {ea}")
con.close()

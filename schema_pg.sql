-- PostgreSQL schema for venue-scrapper
-- Reference DDL for fresh installs and migration verification
-- CamelCase columns are double-quoted to preserve case in PostgreSQL

CREATE TABLE IF NOT EXISTS config (
    name  TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS eventective_leads (
    -- PK
    "EventId"                 TEXT PRIMARY KEY,

    -- Inbox metadata (from leads)
    "RequestGuid"             TEXT,
    "RequestProviderNum"      INTEGER,
    "ProviderNum"             INTEGER,
    "ProviderName"            TEXT,
    "EmailSentDttm"           TEXT,
    "IsFlagged"               INTEGER,
    "PurchasedLead"           INTEGER,
    "DirectLead"              INTEGER,
    "EventDate"               TEXT,
    "AttendeeCount"           INTEGER,
    "PlannerName"             TEXT,
    "PlannerStatusCd"         TEXT,
    "LastActivityDttm"        TEXT,
    "LastActivity"            TEXT,
    "LastActivityType"        TEXT,
    "LastActivityIsAutoResponse" INTEGER,
    "LastActivitySender"      TEXT,
    "AvatarMediaNum"          INTEGER,
    "IsRead"                  INTEGER,
    "UnreadCount"             INTEGER,
    "LeadStatus"              TEXT,
    "IsAvailable"             INTEGER,
    "DateAvailableType"       TEXT,
    "EventType"               TEXT,
    "EventNum"                INTEGER,
    "GmtOffsetHours"          DOUBLE PRECISION,
    "Source"                  TEXT,
    "DetailScrapedAt"         TEXT,

    -- Detail fields (from lead_details)
    "ProviderNameFull"        TEXT,
    "ProviderEmailGeneric"    TEXT,
    "RequestorName"           TEXT,
    "RequestorEmailAddress"   TEXT,
    "RequestorPhone"          TEXT,
    "RequestorContactPref"    TEXT,
    "EventName"               TEXT,
    "DatePossible1"           TEXT,
    "DateAvailable"           INTEGER,
    "DateFlexible"            INTEGER,
    "Duration"                TEXT,
    "TimePossible1"           TEXT,
    "BudgetValue"             TEXT,
    "DirectLeadLocation"      TEXT,
    "InformationRequested"    TEXT,
    "ServicesRequested"       TEXT,
    "FoodRequired"            INTEGER,
    "VenueProvidesFood"       INTEGER,
    "CatererProvidesFood"     INTEGER,
    "SelfProvidesFood"        INTEGER,
    "IsEmailReguser"          INTEGER,
    "PhoneViewed"             INTEGER,
    "PhoneViewedDttm"         TEXT,
    "EmailViewed"             INTEGER,
    "EmailViewedDttm"         TEXT,
    "ConfirmReceivedDttm"     TEXT,
    "IsStripeEnabled"         INTEGER,
    "IsSquareEnabled"         INTEGER,
    "ScrapedAt"               TEXT,

    -- FUB tracking
    fub_exported            INTEGER DEFAULT 0,
    fub_exported_date       TEXT,
    fub_people_id           TEXT,
    fub_lead_stage          TEXT
);

CREATE INDEX IF NOT EXISTS idx_el_lastactivity ON eventective_leads("LastActivityDttm");
CREATE INDEX IF NOT EXISTS idx_el_fub_exported ON eventective_leads(fub_exported);

CREATE TABLE IF NOT EXISTS eventective_lead_activities (
    id                  SERIAL PRIMARY KEY,
    "EventId"             TEXT NOT NULL,
    "DateTime"            TEXT,
    "DateTimeLong"        TEXT,
    "ActivityTypeCd"      TEXT,
    "Sender"              TEXT,
    "Recipient"           TEXT,
    "ResponseText"        TEXT,
    "IsRead"              INTEGER,
    "ResponseNum"         INTEGER,
    "HasAttachments"      INTEGER,
    "IsAutoResponse"      INTEGER,
    "EventDocumentNum"    INTEGER,
    "EventPaymentNum"     INTEGER,
    "PaymentAmount"       DOUBLE PRECISION,
    "ReguserNum"          INTEGER,
    "ActionNum"           INTEGER,

    -- FUB tracking
    fub_exported            INTEGER DEFAULT 0,
    fub_exported_date       TEXT,
    fub_people_id           TEXT,

    UNIQUE("EventId", "DateTime", "ActivityTypeCd", "ResponseNum")
);

CREATE INDEX IF NOT EXISTS idx_ela_eventid ON eventective_lead_activities("EventId");
CREATE INDEX IF NOT EXISTS idx_ela_fub_exported ON eventective_lead_activities(fub_exported);

-- Drip campaign state
CREATE TABLE IF NOT EXISTS drip_campaigns (
    "EventId"          TEXT PRIMARY KEY,
    sequence           TEXT NOT NULL,
    current_step       INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'active',
    last_outbound_at   TEXT,
    last_inbound_at    TEXT,
    next_scheduled_at  TEXT,
    cancel_reason      TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dc_status_next ON drip_campaigns(status, next_scheduled_at);
CREATE INDEX IF NOT EXISTS idx_dc_sequence ON drip_campaigns(sequence);

-- Drip message log
CREATE TABLE IF NOT EXISTS drip_messages (
    id                 SERIAL PRIMARY KEY,
    "EventId"          TEXT NOT NULL,
    sequence           TEXT NOT NULL,
    step               INTEGER NOT NULL,
    message            TEXT NOT NULL,
    next_step          TEXT,
    next_step_reason   TEXT,
    tone_notes         TEXT,
    model              TEXT,
    sent_at            TEXT,
    result             TEXT,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dm_eventid ON drip_messages("EventId");
CREATE INDEX IF NOT EXISTS idx_dm_result ON drip_messages(result);

-- Drip config seeds
INSERT INTO config (name, value) VALUES ('drip_auto_send', 'false') ON CONFLICT DO NOTHING;
INSERT INTO config (name, value) VALUES ('drip_seq1_daily_cap', '0') ON CONFLICT DO NOTHING;
INSERT INTO config (name, value) VALUES ('drip_seq2_daily_cap', '25') ON CONFLICT DO NOTHING;
INSERT INTO config (name, value) VALUES ('drip_seq3_daily_cap', '25') ON CONFLICT DO NOTHING;
INSERT INTO config (name, value) VALUES ('litellm_base_url', 'https://litellm.build365.app') ON CONFLICT DO NOTHING;
INSERT INTO config (name, value) VALUES ('litellm_api_key', 'sk-W8WhFDtFrC8aqjZw7_Cxdg') ON CONFLICT DO NOTHING;
INSERT INTO config (name, value) VALUES ('litellm_model', 'openrouter/google/gemini-3-flash-preview') ON CONFLICT DO NOTHING;
INSERT INTO config (name, value) VALUES ('litellm_fallback_1', 'openrouter/openai/gpt-5.4-mini') ON CONFLICT DO NOTHING;
INSERT INTO config (name, value) VALUES ('litellm_fallback_2', 'openrouter/google/gemini-3.1-pro-preview') ON CONFLICT DO NOTHING;

-- Migrations (idempotent)
DO $$ BEGIN
    ALTER TABLE eventective_leads ADD COLUMN fub_lead_stage TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

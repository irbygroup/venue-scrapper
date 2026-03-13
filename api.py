"""
Eventective Lead Management API
FastAPI + async Playwright (persistent browser context) + SQLite
"""

import asyncio
import json
import os
import random
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import traceback
from base64 import b64encode

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks, APIRouter
from pydantic import BaseModel
from playwright.async_api import async_playwright, Page, BrowserContext
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, Content, HtmlContent

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = os.getenv("DB_PATH", "leads.db")


def get_config(key: str, default: str = "") -> str:
    """Read a config value from the config table, falling back to default."""
    try:
        con = sqlite3.connect(DB_PATH)
        row = con.execute("SELECT value FROM config WHERE name=?", (key,)).fetchone()
        con.close()
        return row[0] if row else default
    except Exception:
        return default


def _cfg(key: str, default: str = "") -> str:
    """Lazy config accessor — used after init_db has run."""
    return get_config(key, default)


# These are read at request-time via _cfg() so DB changes take effect without restart
def cookies_path(): return _cfg("eventective_cookies_path", "cookies.json")
def email():        return _cfg("eventective_email", "info@rentyellowhammer.com")
def password():     return _cfg("eventective_password", "IrbyWins1!")
def inbox_url():    return _cfg("eventective_inbox_url", "https://www.eventective.com/myeventective/#/crm/Event/Inbox")
def signin_url():   return _cfg("eventective_signin_url", "https://www.eventective.com/signin")
def batch_size():   return int(_cfg("eventective_batch_size", "20"))

BASE_BODY = {
    "SearchString": "",
    "ProvNum": None,
    "EventType": "All",
    "Stage": "All",
    "Stages": ["Prospect", "Qualified", "Tentative", "Booked", "Lost", "Deleted"],
    "Sources": ["Referral", "Lead", "Widget", "Self"],
}

BROWSER_OPTS = {
    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "viewport": {"width": 1470, "height": 956},
    "screen":   {"width": 1470, "height": 956},
    "device_scale_factor": 2.0,
    "locale": "en-US",
    "timezone_id": "America/Chicago",
    "extra_http_headers": {
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    },
}

INIT_SCRIPT = """
    Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
    Object.defineProperty(navigator, 'plugins',  { get: () => [1, 2, 3] });
    Object.defineProperty(navigator, 'languages',{ get: () => ['en-US', 'en'] });
    try { delete navigator.__proto__.webdriver; } catch(e) {}
"""


# ── Browser Manager ───────────────────────────────────────────────────────────

class BrowserManager:
    def __init__(self):
        self.pw        = None
        self.browser   = None
        self.context:    Optional[BrowserContext] = None
        self.sync_page:  Optional[Page] = None   # all API fetches + sync
        self.reply_page: Optional[Page] = None   # DOM interactions for replies
        self.sync_lock  = asyncio.Lock()
        self.reply_lock = asyncio.Lock()

    async def start(self):
        self.pw      = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
        self.context = await self.browser.new_context(**BROWSER_OPTS)
        await self.context.add_init_script(INIT_SCRIPT)

        # Load cookies from DB config table
        cookie_json = get_config("eventective_cookies")
        if cookie_json:
            try:
                await self.context.add_cookies(json.loads(cookie_json))
            except Exception:
                pass

        self.sync_page  = await self.context.new_page()
        self.reply_page = await self.context.new_page()

        # Land sync_page on inbox so session cookies are active
        await self.sync_page.goto(inbox_url(), wait_until="domcontentloaded")
        await self.sync_page.wait_for_load_state("networkidle", timeout=15000)

    async def save_cookies(self):
        cookies = await self.context.cookies()
        con = sqlite3.connect(DB_PATH)
        con.execute(
            "INSERT OR REPLACE INTO config (name, value) VALUES (?, ?)",
            ("eventective_cookies", json.dumps(cookies))
        )
        con.commit()
        con.close()

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()

    async def fetch(self, url: str, method: str = "GET", body=None):
        """All Eventective API calls go through the browser's own fetch."""
        return await self.sync_page.evaluate("""
            async ([url, method, body]) => {
                const r = await fetch(url, {
                    method,
                    headers: {
                        'Accept': 'application/json, text/plain, */*',
                        'Content-Type': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                    body: body ? JSON.stringify(body) : undefined,
                });
                if (!r.ok) return { __error: r.status };
                return r.json();
            }
        """, [url, method, body])

    async def check_session(self) -> bool:
        try:
            result = await self.fetch("/api/v1/salesandcatering/getunreadtotals")
            return isinstance(result, dict) and "__error" not in result
        except Exception:
            return False

    async def ensure_session(self) -> bool:
        """Check session, auto-login if expired."""
        if await self.check_session():
            return True
        print("Session expired — auto-logging in...")
        ok = await self.do_login(email(), password())
        if ok:
            print("Auto-login successful.")
        else:
            notify_error("Auto-login failed", "Session expired and automatic re-login failed. Manual intervention may be required.")
        return ok

    async def do_login(self, email: str, password: str) -> bool:
        page = self.sync_page
        await page.goto(signin_url(), wait_until="domcontentloaded")
        await asyncio.sleep(1)

        # If already redirected to dashboard (valid session), we're done
        if "myeventective" in page.url:
            await self.save_cookies()
            return True

        await page.locator("#Email").fill(email)
        await asyncio.sleep(random.uniform(0.3, 0.6))
        await page.locator("#Password").fill(password)
        await asyncio.sleep(random.uniform(0.4, 0.8))
        await page.locator('button[type="submit"]').first.click()
        try:
            await page.wait_for_url("**/myeventective/**", timeout=20000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            await self.save_cookies()
            return True
        except Exception:
            return False


# ── App lifecycle ─────────────────────────────────────────────────────────────

bm: Optional[BrowserManager] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bm
    init_db()
    bm = BrowserManager()
    await bm.start()
    print("Browser ready.")
    yield
    await bm.close()


app = FastAPI(title="Venue Scrapper API", lifespan=lifespan)
router = APIRouter(prefix="/eventective")


# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = get_db()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS config (
            name  TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS eventective_leads (
            EventId                 TEXT PRIMARY KEY,
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
            fub_exported            INTEGER DEFAULT 0,
            fub_exported_date       TEXT,
            fub_people_id           TEXT,
            UNIQUE(EventId, DateTime, ActivityTypeCd, ResponseNum)
        );
        CREATE INDEX IF NOT EXISTS idx_ela_eventid ON eventective_lead_activities(EventId);
        CREATE INDEX IF NOT EXISTS idx_ela_fub_exported ON eventective_lead_activities(fub_exported);
    """)
    con.commit()
    con.close()


def get_meta(key: str) -> Optional[str]:
    con = get_db()
    row = con.execute("SELECT value FROM sync_meta WHERE key=?", (key,)).fetchone()
    con.close()
    return row["value"] if row else None


def set_meta(key: str, value: str):
    con = get_db()
    con.execute("INSERT OR REPLACE INTO sync_meta VALUES (?,?)", (key, value))
    con.commit()
    con.close()


def upsert_inbox_lead(con, lead: dict):
    con.execute("""
        INSERT INTO eventective_leads (
            EventId, RequestGuid, RequestProviderNum, ProviderNum, ProviderName,
            EmailSentDttm, IsFlagged, PurchasedLead, DirectLead, EventDate,
            AttendeeCount, PlannerName, PlannerStatusCd, LastActivityDttm,
            LastActivity, LastActivityType, LastActivityIsAutoResponse,
            LastActivitySender, AvatarMediaNum, IsRead, UnreadCount,
            LeadStatus, IsAvailable, DateAvailableType, EventType,
            EventNum, GmtOffsetHours, Source
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(EventId) DO UPDATE SET
            RequestGuid=excluded.RequestGuid, RequestProviderNum=excluded.RequestProviderNum,
            ProviderNum=excluded.ProviderNum, ProviderName=excluded.ProviderName,
            EmailSentDttm=excluded.EmailSentDttm, IsFlagged=excluded.IsFlagged,
            PurchasedLead=excluded.PurchasedLead, DirectLead=excluded.DirectLead,
            EventDate=excluded.EventDate, AttendeeCount=excluded.AttendeeCount,
            PlannerName=excluded.PlannerName, PlannerStatusCd=excluded.PlannerStatusCd,
            LastActivityDttm=excluded.LastActivityDttm, LastActivity=excluded.LastActivity,
            LastActivityType=excluded.LastActivityType,
            LastActivityIsAutoResponse=excluded.LastActivityIsAutoResponse,
            LastActivitySender=excluded.LastActivitySender, AvatarMediaNum=excluded.AvatarMediaNum,
            IsRead=excluded.IsRead, UnreadCount=excluded.UnreadCount,
            LeadStatus=excluded.LeadStatus, IsAvailable=excluded.IsAvailable,
            DateAvailableType=excluded.DateAvailableType, EventType=excluded.EventType,
            EventNum=excluded.EventNum, GmtOffsetHours=excluded.GmtOffsetHours,
            Source=excluded.Source
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


def upsert_lead_details(con, event_id: str, d: dict):
    """Update the detail columns on the merged eventective_leads row."""
    con.execute("""
        UPDATE eventective_leads SET
            ProviderNum=?, ProviderNameFull=?, ProviderEmailGeneric=?,
            RequestorName=?, RequestorEmailAddress=?, RequestorPhone=?,
            RequestorContactPref=?, EventName=?, EventType=?,
            AttendeeCount=?, DatePossible1=?, DateAvailable=?,
            DateAvailableType=?, DateFlexible=?, Duration=?,
            TimePossible1=?, LeadStatus=?, EmailSentDttm=?,
            DirectLead=?, PurchasedLead=?, BudgetValue=?,
            DirectLeadLocation=?, InformationRequested=?, ServicesRequested=?,
            FoodRequired=?, VenueProvidesFood=?, CatererProvidesFood=?,
            SelfProvidesFood=?, IsFlagged=?, IsRead=?, IsEmailReguser=?,
            PhoneViewed=?, PhoneViewedDttm=?, EmailViewed=?, EmailViewedDttm=?,
            ConfirmReceivedDttm=?, IsStripeEnabled=?, IsSquareEnabled=?,
            Source=?, ScrapedAt=?
        WHERE EventId=?
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


def upsert_activities(con, event_id: str, activities: list) -> int:
    inserted = 0
    for a in activities:
        try:
            con.execute("""
                INSERT OR IGNORE INTO eventective_lead_activities
                (EventId, DateTime, DateTimeLong, ActivityTypeCd, Sender, Recipient,
                 ResponseText, IsRead, ResponseNum, HasAttachments, IsAutoResponse,
                 EventDocumentNum, EventPaymentNum, PaymentAmount, ReguserNum, ActionNum)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                event_id, a.get("DateTime"), a.get("DateTimeLong"), a.get("ActivityTypeCd"),
                a.get("Sender"), a.get("Recipient"), a.get("ResponseText"), a.get("IsRead"),
                a.get("ResponseNum"), a.get("HasAttachments"), a.get("IsAutoResponse"),
                a.get("EventDocumentNum"), a.get("EventPaymentNum"), a.get("PaymentAmount"),
                a.get("ReguserNum"), a.get("ActionNum"),
            ))
            inserted += con.execute("SELECT changes()").fetchone()[0]
        except Exception:
            pass
    return inserted


# ── Utility ───────────────────────────────────────────────────────────────────

def days_until_event(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        event_dt = datetime.fromisoformat(date_str.replace("Z", "")).replace(tzinfo=timezone.utc)
        delta = (event_dt - datetime.now(timezone.utc)).days
        return max(0, delta)
    except Exception:
        return None


def classify_thread(activities: list) -> dict:
    """Classify activities into useful signals."""
    our_messages   = [a for a in activities if a["ActivityTypeCd"] == "provplnr"]
    their_messages = [a for a in activities if a["ActivityTypeCd"] == "plnrprov"]

    we_replied = len(our_messages) > 0

    first_our_time = our_messages[0]["DateTime"] if our_messages else None
    they_replied_to_us = any(
        a["DateTime"] > first_our_time
        for a in their_messages
        if first_our_time and a["DateTime"]
    )

    hours_since_our_msg = None
    last_our_msg_text   = None
    last_their_msg_text = None
    if our_messages:
        last_our = max(our_messages, key=lambda a: a["DateTime"] or "")
        last_our_msg_text = last_our["ResponseText"]
        try:
            dt = datetime.fromisoformat(last_our["DateTime"]).replace(tzinfo=timezone.utc)
            hours_since_our_msg = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except Exception:
            pass

    if their_messages:
        last_theirs = max(their_messages, key=lambda a: a["DateTime"] or "")
        last_their_msg_text = last_theirs["ResponseText"]

    return {
        "we_replied":           we_replied,
        "they_replied_to_us":   they_replied_to_us,
        "hours_since_our_msg":  hours_since_our_msg,
        "last_our_message":     last_our_msg_text,
        "last_their_message":   last_their_msg_text,
        "our_message_count":    len(our_messages),
        "their_message_count":  len(their_messages),
    }


def compute_urgency(days_until: Optional[int], they_replied: bool,
                    hours_since_our: Optional[float]) -> tuple[str, list]:
    reasons = []
    if they_replied:
        reasons.append("they replied — awaiting your response")
        return "HIGH", reasons
    if days_until is not None and days_until <= 10:
        reasons.append(f"event in {days_until} days")
        return "HIGH", reasons
    if days_until is not None and days_until <= 30:
        reasons.append(f"event in {days_until} days")
        urgency = "MEDIUM"
    else:
        urgency = "LOW"
    if hours_since_our and hours_since_our > 4:
        reasons.append(f"read our message, no reply in {hours_since_our:.0f}h")
        urgency = max(urgency, "MEDIUM", key=lambda x: {"HIGH": 2, "MEDIUM": 1, "LOW": 0}[x])
    return urgency, reasons


def build_thread_view(activities: list) -> list:
    """Clean thread for display — skip noise."""
    skip = {"ResponseRank", "LeadPurchased", "ReferralViewed"}
    type_map = {
        "LeadReceived":    "inquiry",
        "ReferralReceived":"inquiry",
        "provplnr":        "our_reply",
        "plnrprov":        "their_reply",
        "ReadMsgs":        "read_receipt",
        "NoInterest":      "no_interest",
    }
    return [
        {
            "from":  a["Sender"] or "system",
            "type":  type_map.get(a["ActivityTypeCd"], a["ActivityTypeCd"]),
            "text":  a["ResponseText"],
            "at":    a["DateTime"],
        }
        for a in activities
        if a["ActivityTypeCd"] not in skip
    ]


def build_lead_detail(l_row, ld_row, activities: list) -> dict:
    """Assemble full lead detail from DB rows + activities."""
    event_date  = (ld_row["DatePossible1"] if ld_row else None) or l_row["EventDate"]
    d_until     = days_until_event(event_date)
    signals     = classify_thread(activities)
    urgency, urgency_reasons = compute_urgency(
        d_until, signals["they_replied_to_us"], signals["hours_since_our_msg"]
    )

    contact = {}
    event   = {}
    meta    = {}

    if ld_row:
        contact = {
            "name":         ld_row["RequestorName"],
            "phone":        ld_row["RequestorPhone"],
            "email":        ld_row["RequestorEmailAddress"],
            "location":     ld_row["DirectLeadLocation"],
            "contact_pref": ld_row["RequestorContactPref"],
        }
        event = {
            "type":          ld_row["EventType"] or l_row["EventType"],
            "date":          event_date.split("T")[0] if event_date else None,
            "days_until":    d_until,
            "guests":        ld_row["AttendeeCount"] or l_row["AttendeeCount"],
            "budget":        ld_row["BudgetValue"],
            "duration":      ld_row["Duration"],
            "date_flexible": bool(ld_row["DateFlexible"]),
            "notes":         ld_row["InformationRequested"],
            "food":          (
                "Not served"      if not ld_row["FoodRequired"]      else
                "Provided by venue"    if ld_row["VenueProvidesFood"]     else
                "Outside caterer" if ld_row["CatererProvidesFood"]   else
                "Self-provided"
            ),
        }
    else:
        contact = {"name": l_row["PlannerName"]}
        event   = {
            "type": l_row["EventType"], "date": l_row["EventDate"],
            "days_until": d_until, "guests": l_row["AttendeeCount"],
        }

    meta = {
        "status":         l_row["LeadStatus"],
        "source":         l_row["Source"],
        "venue":          l_row["ProviderName"],
        "received_at":    l_row["EmailSentDttm"],
        "last_activity":  l_row["LastActivityDttm"],
    }

    return {
        "event_id":        l_row["EventId"],
        "contact":         contact,
        "event":           event,
        "meta":            meta,
        "urgency":         urgency,
        "urgency_reasons": urgency_reasons,
        "thread_signals":  signals,
        "thread":          build_thread_view(activities),
    }


def classify_change(event_id: str, signals: dict, con) -> str:
    """Classify what changed: new_lead | replied_to_us | read_no_reply | updated"""
    row = con.execute(
        "SELECT DetailScrapedAt FROM eventective_leads WHERE EventId=?", (event_id,)
    ).fetchone()
    if not row or not row["DetailScrapedAt"]:
        return "new_lead"
    if signals["they_replied_to_us"]:
        return "replied_to_us"
    if signals["we_replied"] and not signals["they_replied_to_us"]:
        return "read_no_reply"
    return "updated"


# ── Sync logic ────────────────────────────────────────────────────────────────

async def run_sync(limit: Optional[int] = None) -> dict:
    """
    Smart incremental sync.
    - Fetches batches of 20 from the API (sorted by LastActivityDttm DESC)
    - Stops at the first lead whose LastActivityDttm <= last_sync_time
    - Fetches full details only for leads with new activity
    - Auto-login if session is expired
    """
    started_at = datetime.now(timezone.utc)

    # Ensure we have a valid session (ensure_session sends error email if login fails)
    if not await bm.ensure_session():
        return {"error": "login_failed", "message": "Could not authenticate with Eventective"}

    # Make sure sync_page is on inbox
    if "myeventective" not in bm.sync_page.url:
        await bm.sync_page.goto(inbox_url(), wait_until="domcontentloaded")
        await bm.sync_page.wait_for_load_state("networkidle", timeout=15000)

    last_sync = get_meta("last_sync_time") or "2020-01-01T00:00:00"
    con = get_db()

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
            row = con.execute(
                "SELECT LastActivityDttm, DetailScrapedAt FROM eventective_leads WHERE EventId=?",
                (lead["EventId"],)
            ).fetchone()

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

        upsert_inbox_lead(con, lead)
        upsert_lead_details(con, event_id, detail)
        upsert_activities(con, event_id, detail.get("Activities") or [])
        con.execute(
            "UPDATE eventective_leads SET DetailScrapedAt=? WHERE EventId=?",
            (datetime.now(timezone.utc).isoformat(), event_id)
        )
        con.commit()

        # Re-read fresh activities
        acts = [dict(r) for r in con.execute(
            "SELECT * FROM eventective_lead_activities WHERE EventId=? ORDER BY DateTime", (event_id,)
        ).fetchall()]
        signals = classify_thread(acts)
        change  = classify_change(event_id, signals, con)

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


# ── Request models ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email:    Optional[str] = None
    password: Optional[str] = None


class ReplyRequest(BaseModel):
    message: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/auth/login")
async def auth_login(req: LoginRequest = LoginRequest()):
    _email    = req.email    or email()
    _password = req.password or password()
    ok = await bm.do_login(_email, _password)
    if not ok:
        notify_error("Login failed", f"Manual login attempt failed for {_email}")
        raise HTTPException(status_code=401, detail="Login failed")
    # Navigate sync_page back to inbox
    await bm.sync_page.goto(inbox_url(), wait_until="domcontentloaded")
    await bm.sync_page.wait_for_load_state("networkidle", timeout=15000)
    return {"success": True, "message": "Logged in, cookies saved"}


@router.get("/auth/status")
async def auth_status():
    valid = await bm.check_session()
    has_cookies = bool(get_config("eventective_cookies"))
    return {
        "authenticated": valid,
        "has_cookies":   has_cookies,
    }


@router.post("/sync")
async def sync(limit: Optional[int] = None):
    if bm.sync_lock.locked():
        raise HTTPException(status_code=409, detail="Sync already in progress")
    async with bm.sync_lock:
        return await run_sync(limit=limit)


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
    wheres = []
    params = []

    if since:
        unit = since[-1]
        n    = int(since[:-1])
        secs = n * 3600 if unit == "h" else n * 86400
        cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - secs, tz=timezone.utc
        ).isoformat()
        wheres.append("LastActivityDttm >= ?")
        params.append(cutoff)

    if venue:
        wheres.append("LOWER(ProviderName) LIKE ?")
        params.append(f"%{venue.lower()}%")

    if status:
        wheres.append("LOWER(LeadStatus) = ?")
        params.append(status.lower())

    if upcoming_days is not None:
        cutoff_date = datetime.now(timezone.utc).isoformat()
        far_date    = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + upcoming_days * 86400, tz=timezone.utc
        ).isoformat()
        wheres.append("(DatePossible1 >= ? AND DatePossible1 <= ?)")
        params.extend([cutoff_date, far_date])

    where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""

    rows = con.execute(f"""
        SELECT EventId, PlannerName, LastActivityDttm, EventDate,
               AttendeeCount, EventType, ProviderName, LeadStatus,
               Source, EmailSentDttm,
               RequestorName, RequestorPhone, RequestorEmailAddress,
               BudgetValue, InformationRequested, DatePossible1, DateFlexible
        FROM eventective_leads
        {where_sql}
        ORDER BY LastActivityDttm DESC
        LIMIT ? OFFSET ?
    """, params + [limit, offset]).fetchall()

    leads_out = []
    for r in rows:
        event_date = r["DatePossible1"] or r["EventDate"]
        d_until    = days_until_event(event_date)

        # Quick thread signals from DB
        acts = con.execute(
            "SELECT ActivityTypeCd, DateTime, ResponseText, Sender FROM eventective_lead_activities WHERE EventId=? ORDER BY DateTime",
            (r["EventId"],)
        ).fetchall()
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

    row = con.execute(
        "SELECT * FROM eventective_leads WHERE EventId=?", (event_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Lead {event_id} not found")

    acts = [dict(a) for a in con.execute(
        "SELECT * FROM eventective_lead_activities WHERE EventId=? ORDER BY DateTime", (event_id,)
    ).fetchall()]

    con.close()
    row_dict = dict(row)
    return build_lead_detail(row_dict, row_dict, acts)


@router.post("/leads/{event_id}/reply")
async def send_reply(event_id: str, req: ReplyRequest):
    """Send a message via Playwright DOM interaction."""
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
        upsert_activities(con, event_id, detail.get("Activities") or [])
        con.commit()
        con.close()

        return {
            "success":      True,
            "event_id":     event_id,
            "message_sent": req.message,
            "sent_at":      sent_at,
            "thread_length": len(detail.get("Activities") or []),
        }


@router.get("/status")
async def status():
    con  = get_db()
    now  = datetime.now(timezone.utc)

    action_required = []
    watching        = []
    upcoming        = []

    # Recent leads (last 30 days)
    recent = con.execute("""
        SELECT EventId, PlannerName, LastActivityDttm, EventDate,
               AttendeeCount, EventType, ProviderName, LeadStatus, EmailSentDttm,
               RequestorName, DatePossible1, BudgetValue
        FROM eventective_leads
        WHERE LastActivityDttm >= ?
        ORDER BY LastActivityDttm DESC
    """, ((datetime.fromtimestamp(now.timestamp() - 30*86400, tz=timezone.utc)).isoformat(),)).fetchall()

    leads_30d = 0
    response_times = []
    first_responder_count = 0

    all_leads = con.execute("SELECT COUNT(*) as c FROM eventective_leads").fetchone()["c"]

    for r in recent:
        event_id   = r["EventId"]
        event_date = r["DatePossible1"] or r["EventDate"]
        d_until    = days_until_event(event_date)

        acts = [dict(a) for a in con.execute(
            "SELECT * FROM eventective_lead_activities WHERE EventId=? ORDER BY DateTime", (event_id,)
        ).fetchall()]
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

# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str) -> dict:
    """Send an email via SendGrid using config table values."""
    api_key = get_config("sendgrid_api_key")
    if not api_key:
        return {"error": "sendgrid_api_key not configured"}

    from_email = Email(get_config("email_from", "it@irbygroup.com"),
                       get_config("email_from_name", "Venue Scrapper"))
    to_email = To(get_config("email_to", "jared@irbygroup.com"))
    message = Mail(from_email=from_email, to_emails=to_email,
                   subject=subject, html_content=HtmlContent(html_body))

    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        return {"status_code": response.status_code, "success": response.status_code in (200, 201, 202)}
    except Exception as e:
        return {"error": str(e)}


def notify_error(subject: str, detail: str):
    """Fire-and-forget error notification email."""
    html = f"""
    <h2 style="color:#c0392b;">⚠️ Venue Scrapper Error</h2>
    <p><strong>Time:</strong> {datetime.now(timezone.utc).isoformat()}</p>
    <p><strong>Error:</strong></p>
    <pre style="background:#f8f8f8;padding:12px;border-radius:4px;">{detail}</pre>
    """
    send_email(f"[Venue Scrapper] {subject}", html)


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
    now = datetime.now(timezone.utc)
    cutoff = datetime.fromtimestamp(now.timestamp() - 86400, tz=timezone.utc).isoformat()

    # New leads (received in last 24h)
    new_leads = con.execute("""
        SELECT EventId, RequestorName, PlannerName, ProviderName, EventType,
               DatePossible1, EventDate, AttendeeCount, BudgetValue,
               RequestorPhone, RequestorEmailAddress, InformationRequested,
               EmailSentDttm
        FROM eventective_leads
        WHERE EmailSentDttm >= ?
        ORDER BY EmailSentDttm DESC
    """, (cutoff,)).fetchall()

    # Leads with new activity in last 24h (excluding brand new ones)
    new_lead_ids = {r["EventId"] for r in new_leads}
    active_leads = con.execute("""
        SELECT DISTINCT el.EventId, el.RequestorName, el.PlannerName,
               el.ProviderName, el.EventType, el.LastActivityDttm,
               el.LeadStatus, el.DatePossible1, el.EventDate
        FROM eventective_leads el
        WHERE el.LastActivityDttm >= ?
        ORDER BY el.LastActivityDttm DESC
    """, (cutoff,)).fetchall()
    active_leads = [r for r in active_leads if r["EventId"] not in new_lead_ids]

    # All activities in last 24h grouped by lead
    recent_activities = con.execute("""
        SELECT a.EventId, a.DateTime, a.ActivityTypeCd, a.Sender,
               a.Recipient, a.ResponseText,
               el.RequestorName, el.PlannerName, el.ProviderName
        FROM eventective_lead_activities a
        JOIN eventective_leads el ON el.EventId = a.EventId
        WHERE a.DateTime >= ?
        ORDER BY a.DateTime DESC
    """, (cutoff,)).fetchall()

    # Stats
    total_leads_db = con.execute("SELECT COUNT(*) as c FROM eventective_leads").fetchone()["c"]
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


# ── FUB Sync ─────────────────────────────────────────────────────────────────

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
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    activities = con.execute(
        "SELECT * FROM eventective_lead_activities WHERE EventId=? ORDER BY DateTime ASC",
        (event_id,)
    ).fetchall()
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

    # Step 6: mark exported
    now_str = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE eventective_leads SET fub_exported=1, fub_exported_date=?, fub_people_id=? WHERE EventId=?",
        (now_str, str(fub_person_id), event_id)
    )
    con.execute(
        "UPDATE eventective_lead_activities SET fub_exported=1, fub_exported_date=?, fub_people_id=? WHERE EventId=?",
        (now_str, str(fub_person_id), event_id)
    )
    con.commit()
    con.close()

    return fub_person_id


async def _fub_export_new_activities(client: httpx.AsyncClient, state: dict):
    """Export only new (fub_exported=0) activities on already-exported leads."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT DISTINCT el.EventId, el.fub_people_id
           FROM eventective_leads el
           JOIN eventective_lead_activities ela ON ela.EventId = el.EventId
           WHERE el.fub_exported=1 AND el.fub_people_id IS NOT NULL AND ela.fub_exported=0"""
    ).fetchall()
    con.close()

    for row in rows:
        event_id = row["EventId"]
        fub_people_id = int(row["fub_people_id"])
        try:
            con2 = sqlite3.connect(DB_PATH)
            con2.row_factory = sqlite3.Row
            activities = con2.execute(
                "SELECT * FROM eventective_lead_activities WHERE EventId=? AND fub_exported=0 ORDER BY DateTime ASC",
                (event_id,)
            ).fetchall()
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
            con3 = sqlite3.connect(DB_PATH)
            con3.execute(
                "UPDATE eventective_lead_activities SET fub_exported=1, fub_exported_date=?, fub_people_id=? WHERE EventId=? AND fub_exported=0",
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
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        leads = con.execute(
            "SELECT * FROM eventective_leads WHERE fub_exported=0 ORDER BY EmailSentDttm ASC"
        ).fetchall()
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
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        direction = "DESC" if order == "desc" else "ASC"
        query = f"SELECT * FROM eventective_leads WHERE fub_exported=0 ORDER BY EmailSentDttm {direction}"
        if limit > 0:
            query += f" LIMIT {limit}"
        leads = con.execute(query).fetchall()
        con.close()

        state["progress"]["total"] = len(leads)

        async with httpx.AsyncClient(timeout=30.0) as client:
            for lead in leads:
                # Re-check fub_exported in case the other direction already got this lead
                con2 = sqlite3.connect(DB_PATH)
                already = con2.execute("SELECT fub_exported FROM eventective_leads WHERE EventId=?", (lead["EventId"],)).fetchone()
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


@router.post("/fub-sync")
async def fub_sync(background_tasks: BackgroundTasks, mode: str = "backfill", limit: int = 0, order: str = "asc"):
    state = fub_sync_state[order]
    if state["running"]:
        raise HTTPException(409, f"FUB sync ({order}) already running")
    background_tasks.add_task(_fub_sync_task, mode, limit, order)
    return {"status": "started", "mode": mode, "limit": limit or "unlimited", "order": order}


@router.post("/fub-export-new")
async def fub_export_new(background_tasks: BackgroundTasks):
    state = fub_sync_state["incremental"]
    if state["running"]:
        raise HTTPException(409, "FUB incremental export already running")
    background_tasks.add_task(_fub_incremental_export)
    return {"status": "started", "mode": "incremental"}


@router.get("/fub-sync/status")
async def fub_sync_status():
    return {
        "asc": {
            "running": fub_sync_state["asc"]["running"],
            "progress": fub_sync_state["asc"]["progress"],
            "errors": fub_sync_state["asc"]["errors"][-20:],
        },
        "desc": {
            "running": fub_sync_state["desc"]["running"],
            "progress": fub_sync_state["desc"]["progress"],
            "errors": fub_sync_state["desc"]["errors"][-20:],
        },
        "incremental": {
            "running": fub_sync_state["incremental"]["running"],
            "progress": fub_sync_state["incremental"]["progress"],
            "errors": fub_sync_state["incremental"]["errors"][-20:],
        },
    }


app.include_router(router)

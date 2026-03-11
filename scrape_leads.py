"""
Eventective lead scraper.

Modes:
  full        — scroll entire sidebar, skip already-scraped, click & scrape the rest
  incremental — scroll first 100 sidebar leads, click & scrape new/changed ones only

Browser behavior (both modes):
  - Real DOM clicks on .message-wrapper sidebar items by index
  - Sidebar (.eve-infinite-scroll-inbox) scrolled to load batches of 20
  - Sidebar stays rendered across lead clicks (SPA — no back navigation needed)
  - All fetch() calls run from within the browser page context

Session:
  - cookies.json persists between runs
  - Headed login fires automatically when cookies are missing/expired
  - All scraping is headless
"""

import json
import sqlite3
import time
import random
import argparse
import os
from datetime import datetime
from playwright.sync_api import sync_playwright

# ── Credentials ───────────────────────────────────────────────────────────────
EMAIL        = "info@rentyellowhammer.com"
PASSWORD     = "IrbyWins1!"
SIGNIN_URL   = "https://www.eventective.com/signin"
INBOX_URL    = "https://www.eventective.com/myeventective/#/crm/Event/Inbox"
COOKIES_FILE = "cookies.json"
PAGE_SIZE    = 20

BASE_BODY = {
    "SearchString": "",
    "ProvNum": None,
    "EventType": "All",
    "Stage": "All",
    "Stages": ["Prospect", "Qualified", "Tentative", "Booked", "Lost", "Deleted"],
    "Sources": ["Referral", "Lead", "Widget", "Self"],
}


# ── Cookie management ─────────────────────────────────────────────────────────

def save_cookies(cookies):
    with open(COOKIES_FILE, "w") as f:
        json.dump(cookies, f, indent=2)
    print(f"  Cookies saved → {COOKIES_FILE}")


def load_cookies():
    if not os.path.exists(COOKIES_FILE):
        return None
    with open(COOKIES_FILE) as f:
        return json.load(f)


# ── Headed login ──────────────────────────────────────────────────────────────

def browser_login(p):
    print("  [headed] Opening browser for login...")
    browser = p.chromium.launch(headless=False, slow_mo=60)
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        extra_http_headers={
            "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        },
    )
    page = context.new_page()
    print("  [headed] Loading signin page...")
    page.goto(SIGNIN_URL, wait_until="domcontentloaded")
    time.sleep(random.uniform(1.0, 1.8))

    print("  [headed] Typing credentials...")
    page.locator('input[type="email"], #Email').first.click()
    time.sleep(random.uniform(0.3, 0.6))
    page.keyboard.type(EMAIL, delay=random.randint(50, 100))
    time.sleep(random.uniform(0.3, 0.7))
    page.locator('input[type="password"], #Password').first.click()
    time.sleep(random.uniform(0.2, 0.5))
    page.keyboard.type(PASSWORD, delay=random.randint(50, 100))
    time.sleep(random.uniform(0.6, 1.2))

    print("  [headed] Submitting...")
    page.locator('button[type="submit"]').first.click()
    page.wait_for_url("**/myeventective/**", timeout=30000)
    page.wait_for_load_state("networkidle", timeout=20000)
    time.sleep(2)

    cookies = context.cookies()
    browser.close()
    print(f"  [headed] Login successful — {len(cookies)} cookies.")
    return cookies


# ── Session setup ─────────────────────────────────────────────────────────────

def validate_session(p, cookies):
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    )
    context.add_cookies(cookies)
    page = context.new_page()
    page.goto(INBOX_URL, wait_until="domcontentloaded")
    time.sleep(1)
    status = page.evaluate("""
        async () => {
            const r = await fetch('/api/v1/salesandcatering/getunreadtotals',
                { headers: { 'Accept': 'application/json' } });
            return r.status;
        }
    """)
    browser.close()
    return status == 200


def get_headless_page(p):
    cookies = load_cookies()
    if cookies:
        print("Loaded saved cookies — validating...")
        if validate_session(p, cookies):
            print("Session valid. Running headlessly.\n")
        else:
            print("Session expired. Re-logging in...")
            cookies = browser_login(p)
            save_cookies(cookies)
    else:
        print("No saved cookies. Logging in...")
        cookies = browser_login(p)
        save_cookies(cookies)

    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        extra_http_headers={
            "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        },
    )
    context.add_cookies(cookies)
    page = context.new_page()

    # Land on inbox so the sidebar renders and referer is correct
    page.goto(INBOX_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=15000)
    time.sleep(1.5)
    return browser, page


# ── Browser / sidebar helpers ─────────────────────────────────────────────────

def get_inbox_batch_via_api(page, start_index):
    """Fetch one page of inbox leads via fetch() inside the browser."""
    body = {**BASE_BODY, "StartIndex": start_index, "EndIndex": start_index + PAGE_SIZE - 1}
    return page.evaluate("""
        async (body) => {
            const r = await fetch(
                '/api/v1/salesandcatering/getmessagesforinbox?showFlagged=false&showUnread=false',
                {
                    method: 'POST',
                    headers: {
                        'Accept': 'application/json, text/plain, */*',
                        'Content-Type': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                    body: JSON.stringify(body),
                }
            );
            return r.json();
        }
    """, body)


def fetch_lead_details_in_browser(page, event_id):
    """Fetch full lead details via fetch() from within the current page."""
    return page.evaluate("""
        async (id) => {
            const r = await fetch(
                `/api/v1/salesandcatering/geteventdetails?id=${id}`,
                { headers: { 'Accept': 'application/json, text/plain, */*' } }
            );
            return r.json();
        }
    """, event_id)


def scroll_sidebar(page):
    """Scroll the sidebar all the way to the bottom to trigger infinite scroll load."""
    result = page.evaluate("""
        () => {
            const sidebar = document.querySelector('.eve-infinite-scroll-inbox');
            if (!sidebar) return 'not found';
            sidebar.scrollTop = sidebar.scrollHeight;
            sidebar.dispatchEvent(new Event('scroll', { bubbles: true }));
            return 'scrolled to bottom: ' + sidebar.scrollTop + '/' + sidebar.scrollHeight;
        }
    """)
    print(f"    [scroll: {result}]", flush=True)
    time.sleep(random.uniform(2.5, 3.5))  # give Angular time to render new items
    return 'scrolled' in str(result)


def wait_for_sidebar_count(page, expected_min):
    """Wait until at least expected_min .message-wrapper elements are in the DOM."""
    for attempt in range(30):  # up to 15 seconds
        count = page.evaluate("""
            () => document.querySelectorAll('.message-wrapper').length
        """)
        if count >= expected_min:
            return True
        time.sleep(0.5)
    count = page.evaluate("() => document.querySelectorAll('.message-wrapper').length")
    print(f"    [sidebar has {count} items, expected {expected_min}]")
    return False


def click_lead_by_dom_index(page, dom_index):
    """
    Click the Nth .message-wrapper in the sidebar (0-based).
    Scrolls it into view before clicking.
    """
    return page.evaluate("""
        (index) => {
            const wrappers = document.querySelectorAll('.message-wrapper');
            if (index < wrappers.length) {
                wrappers[index].scrollIntoView({ behavior: 'smooth', block: 'center' });
                wrappers[index].click();
                return true;
            }
            return false;
        }
    """, dom_index)


def wait_for_lead_panel(page, event_id):
    """Wait for the right panel to load this lead's messages."""
    page.wait_for_url(f"**{event_id}**", timeout=12000)
    page.wait_for_load_state("networkidle", timeout=12000)
    time.sleep(random.uniform(0.5, 1.0))


# ── Database setup ────────────────────────────────────────────────────────────

def init_db(db_path):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
            EventId             TEXT PRIMARY KEY,
            RequestGuid         TEXT,
            RequestProviderNum  INTEGER,
            ProviderNum         INTEGER,
            ProviderName        TEXT,
            EmailSentDttm       TEXT,
            IsFlagged           INTEGER,
            PurchasedLead       INTEGER,
            DirectLead          INTEGER,
            EventDate           TEXT,
            AttendeeCount       INTEGER,
            PlannerName         TEXT,
            PlannerStatusCd     TEXT,
            LastActivityDttm    TEXT,
            LastActivity        TEXT,
            LastActivityType    TEXT,
            LastActivityIsAutoResponse INTEGER,
            LastActivitySender  TEXT,
            AvatarMediaNum      INTEGER,
            IsRead              INTEGER,
            UnreadCount         INTEGER,
            LeadStatus          TEXT,
            IsAvailable         INTEGER,
            DateAvailableType   TEXT,
            EventType           TEXT,
            EventNum            INTEGER,
            GmtOffsetHours      REAL,
            Source              TEXT,
            DetailScrapedAt     TEXT
        );

        CREATE TABLE IF NOT EXISTS lead_details (
            EventId                 TEXT PRIMARY KEY,
            ProviderNum             INTEGER,
            ProviderNameFull        TEXT,
            ProviderEmailGeneric    TEXT,
            RequestorName           TEXT,
            RequestorEmailAddress   TEXT,
            RequestorPhone          TEXT,
            RequestorContactPref    TEXT,
            EventName               TEXT,
            EventType               TEXT,
            AttendeeCount           INTEGER,
            DatePossible1           TEXT,
            DateAvailable           INTEGER,
            DateAvailableType       TEXT,
            DateFlexible            INTEGER,
            Duration                TEXT,
            TimePossible1           TEXT,
            LeadStatus              TEXT,
            EmailSentDttm           TEXT,
            DirectLead              INTEGER,
            PurchasedLead           INTEGER,
            BudgetValue             REAL,
            DirectLeadLocation      TEXT,
            InformationRequested    TEXT,
            ServicesRequested       TEXT,
            FoodRequired            INTEGER,
            VenueProvidesFood       INTEGER,
            CatererProvidesFood     INTEGER,
            SelfProvidesFood        INTEGER,
            IsFlagged               INTEGER,
            IsRead                  INTEGER,
            IsEmailReguser          INTEGER,
            PhoneViewed             INTEGER,
            PhoneViewedDttm         TEXT,
            EmailViewed             INTEGER,
            EmailViewedDttm         TEXT,
            ConfirmReceivedDttm     TEXT,
            IsStripeEnabled         INTEGER,
            IsSquareEnabled         INTEGER,
            Source                  TEXT,
            ScrapedAt               TEXT
        );

        CREATE TABLE IF NOT EXISTS activities (
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
            UNIQUE(EventId, DateTime, ActivityTypeCd, ResponseNum)
        );

        CREATE INDEX IF NOT EXISTS idx_activities_eventid ON activities(EventId);
        CREATE INDEX IF NOT EXISTS idx_leads_lastactivity ON leads(LastActivityDttm);
    """)
    con.commit()
    return con


# ── DB upsert helpers ─────────────────────────────────────────────────────────

def upsert_inbox_lead(cur, lead):
    cur.execute("""
        INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(EventId) DO UPDATE SET
            RequestGuid=excluded.RequestGuid,
            RequestProviderNum=excluded.RequestProviderNum,
            ProviderNum=excluded.ProviderNum,
            ProviderName=excluded.ProviderName,
            EmailSentDttm=excluded.EmailSentDttm,
            IsFlagged=excluded.IsFlagged,
            PurchasedLead=excluded.PurchasedLead,
            DirectLead=excluded.DirectLead,
            EventDate=excluded.EventDate,
            AttendeeCount=excluded.AttendeeCount,
            PlannerName=excluded.PlannerName,
            PlannerStatusCd=excluded.PlannerStatusCd,
            LastActivityDttm=excluded.LastActivityDttm,
            LastActivity=excluded.LastActivity,
            LastActivityType=excluded.LastActivityType,
            LastActivityIsAutoResponse=excluded.LastActivityIsAutoResponse,
            LastActivitySender=excluded.LastActivitySender,
            AvatarMediaNum=excluded.AvatarMediaNum,
            IsRead=excluded.IsRead,
            UnreadCount=excluded.UnreadCount,
            LeadStatus=excluded.LeadStatus,
            IsAvailable=excluded.IsAvailable,
            DateAvailableType=excluded.DateAvailableType,
            EventType=excluded.EventType,
            EventNum=excluded.EventNum,
            GmtOffsetHours=excluded.GmtOffsetHours,
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
        lead.get("GmtOffsetHours"), lead.get("Source"), None,
    ))


def upsert_lead_details(cur, event_id, d):
    cur.execute("""
        INSERT INTO lead_details VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(EventId) DO UPDATE SET
            ProviderNum=excluded.ProviderNum, ProviderNameFull=excluded.ProviderNameFull,
            ProviderEmailGeneric=excluded.ProviderEmailGeneric, RequestorName=excluded.RequestorName,
            RequestorEmailAddress=excluded.RequestorEmailAddress, RequestorPhone=excluded.RequestorPhone,
            RequestorContactPref=excluded.RequestorContactPref, EventName=excluded.EventName,
            EventType=excluded.EventType, AttendeeCount=excluded.AttendeeCount,
            DatePossible1=excluded.DatePossible1, DateAvailable=excluded.DateAvailable,
            DateAvailableType=excluded.DateAvailableType, DateFlexible=excluded.DateFlexible,
            Duration=excluded.Duration, TimePossible1=excluded.TimePossible1,
            LeadStatus=excluded.LeadStatus, EmailSentDttm=excluded.EmailSentDttm,
            DirectLead=excluded.DirectLead, PurchasedLead=excluded.PurchasedLead,
            BudgetValue=excluded.BudgetValue, DirectLeadLocation=excluded.DirectLeadLocation,
            InformationRequested=excluded.InformationRequested, ServicesRequested=excluded.ServicesRequested,
            FoodRequired=excluded.FoodRequired, VenueProvidesFood=excluded.VenueProvidesFood,
            CatererProvidesFood=excluded.CatererProvidesFood, SelfProvidesFood=excluded.SelfProvidesFood,
            IsFlagged=excluded.IsFlagged, IsRead=excluded.IsRead, IsEmailReguser=excluded.IsEmailReguser,
            PhoneViewed=excluded.PhoneViewed, PhoneViewedDttm=excluded.PhoneViewedDttm,
            EmailViewed=excluded.EmailViewed, EmailViewedDttm=excluded.EmailViewedDttm,
            ConfirmReceivedDttm=excluded.ConfirmReceivedDttm, IsStripeEnabled=excluded.IsStripeEnabled,
            IsSquareEnabled=excluded.IsSquareEnabled, Source=excluded.Source, ScrapedAt=excluded.ScrapedAt
    """, (
        event_id, d.get("ProviderNum"), d.get("ProviderNameFull"), d.get("ProviderEmailGeneric"),
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
        datetime.utcnow().isoformat(),
    ))


def upsert_activities(cur, event_id, activities):
    inserted = 0
    for a in activities:
        try:
            cur.execute("""
                INSERT OR IGNORE INTO activities
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
            inserted += cur.rowcount
        except sqlite3.IntegrityError:
            pass
    return inserted


def mark_detail_scraped(cur, event_id):
    cur.execute("UPDATE leads SET DetailScrapedAt=? WHERE EventId=?",
                (datetime.utcnow().isoformat(), event_id))


# ── Core: process one batch of sidebar leads ──────────────────────────────────

def process_batch(page, con, batch, dom_start, needs_scrape, position_offset, total):
    """
    For each lead in batch:
      - If in needs_scrape: click it by DOM index, wait for panel, fetch & save
      - Otherwise: skip (no click)
    dom_start: the DOM index of the first item in this batch
    """
    cur = con.cursor()
    scraped = 0
    skipped = 0

    for i, lead in enumerate(batch):
        event_id = lead["EventId"]
        name = lead.get("PlannerName", "?")
        position = position_offset + i + 1
        dom_index = dom_start + i

        upsert_inbox_lead(cur, lead)
        con.commit()

        if event_id not in needs_scrape:
            print(f"  [{position}/{total}] {event_id} {name} — already done, skip")
            skipped += 1
            continue

        # Random human-like delay
        wait = random.uniform(2, 10)
        print(f"  [{position}/{total}] {event_id} {name}... wait {wait:.1f}s", end=" ", flush=True)
        time.sleep(wait)

        # Click the lead in the sidebar by its DOM index
        if not click_lead_by_dom_index(page, dom_index):
            print(f"SKIP (wrapper[{dom_index}] not in DOM — expected {dom_index+1} wrappers)")
            skipped += 1
            continue

        # Wait for panel to load
        try:
            wait_for_lead_panel(page, event_id)
        except Exception:
            print("SKIP (panel load timeout) — navigating back to inbox", flush=True)
            skipped += 1
            try:
                page.goto(INBOX_URL, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_load_state("networkidle", timeout=10000)
                time.sleep(2)
            except Exception:
                pass
            continue

        # Fetch full details from this page context
        try:
            d = fetch_lead_details_in_browser(page, event_id)
        except Exception as e:
            print(f"ERROR: {e}")
            skipped += 1
            continue

        upsert_lead_details(cur, event_id, d)
        new_act = upsert_activities(cur, event_id, d.get("Activities") or [])
        mark_detail_scraped(cur, event_id)
        con.commit()
        print(f"saved ({new_act} new activities)")
        scraped += 1

    return scraped, skipped


# ── FULL mode ─────────────────────────────────────────────────────────────────

def run_full(db_path):
    con = init_db(db_path)
    cur = con.cursor()

    with sync_playwright() as p:
        browser, page = get_headless_page(p)

        # Pre-load the full inbox list so we know what needs scraping
        print("=" * 60)
        print("FULL MODE — pre-loading inbox list...")
        print("=" * 60)
        all_leads = {}
        api_start = 1
        while True:
            batch = get_inbox_batch_via_api(page, api_start)
            if not batch:
                break
            for lead in batch:
                all_leads[lead["EventId"]] = lead
            if len(batch) < PAGE_SIZE:
                break
            api_start += PAGE_SIZE
            time.sleep(0.4)

        total = len(all_leads)
        needs_scrape = {
            eid: lead for eid, lead in all_leads.items()
            if not cur.execute(
                "SELECT DetailScrapedAt FROM leads WHERE EventId=? AND DetailScrapedAt IS NOT NULL",
                (eid,)).fetchone()
        }
        print(f"Total: {total} | Already done: {total - len(needs_scrape)} | To scrape: {len(needs_scrape)}")

        if not needs_scrape:
            print("Nothing to do.")
            browser.close()
            con.close()
            return

        print("\nScrolling sidebar and scraping...\n")

        total_scraped = 0
        total_skipped = 0
        dom_count = 0     # how many .message-wrapper items are currently in DOM
        api_pos = 1       # current API pagination position

        while True:
            batch_num = (api_pos - 1) // PAGE_SIZE + 1
            print(f"--- Batch {batch_num} (sidebar positions {api_pos}–{api_pos + PAGE_SIZE - 1}) ---")

            # Ensure this batch is loaded in the DOM
            expected_dom = dom_count + PAGE_SIZE
            if not wait_for_sidebar_count(page, min(expected_dom, total)):
                print("  Sidebar didn't load expected items — stopping.")
                break

            batch = get_inbox_batch_via_api(page, api_pos)
            if not batch:
                break

            s, sk = process_batch(page, con, batch, dom_count, needs_scrape, api_pos - 1, total)
            total_scraped += s
            total_skipped += sk
            dom_count += len(batch)

            if len(batch) < PAGE_SIZE:
                print("\nReached end of sidebar.")
                break

            # Scroll sidebar to load next batch
            scroll_sidebar(page)
            api_pos += PAGE_SIZE

        browser.close()

    print(f"\nDone. Scraped: {total_scraped} | Skipped (done): {total_skipped}")
    con.close()


# ── INCREMENTAL mode ──────────────────────────────────────────────────────────

def run_incremental(db_path):
    con = init_db(db_path)
    cur = con.cursor()

    with sync_playwright() as p:
        browser, page = get_headless_page(p)

        print("=" * 60)
        print("INCREMENTAL — checking first 100 sidebar leads...")
        print("=" * 60)

        # Load first 100 from API to determine what changed
        inbox_leads = {}
        api_start = 1
        while api_start <= 100:
            batch = get_inbox_batch_via_api(page, api_start)
            if not batch:
                break
            for lead in batch:
                inbox_leads[lead["EventId"]] = lead
            if len(batch) < PAGE_SIZE:
                break
            api_start += PAGE_SIZE
            time.sleep(0.4)

        total = len(inbox_leads)

        # Determine which need scraping
        needs_scrape = {}
        for event_id, lead in inbox_leads.items():
            last_activity = lead.get("LastActivityDttm")
            row = cur.execute(
                "SELECT LastActivityDttm, DetailScrapedAt FROM leads WHERE EventId=?", (event_id,)
            ).fetchone()
            if row is None:
                needs_scrape[event_id] = (lead, "NEW lead")
            elif not row[1]:
                needs_scrape[event_id] = (lead, "never scraped")
            elif last_activity and last_activity > (row[0] or ""):
                needs_scrape[event_id] = (lead, f"new activity")

        if not needs_scrape:
            print("\nAll up to date. Nothing to do.")
            browser.close()
            con.close()
            return

        print(f"\n{len(needs_scrape)}/{total} leads need updating:")
        for eid, (lead, reason) in needs_scrape.items():
            print(f"  {eid}  {lead.get('PlannerName','?')} — {reason}")
        print()

        to_scrape = {eid: data[0] for eid, data in needs_scrape.items()}

        total_scraped = 0
        total_skipped = 0
        dom_count = 0
        api_pos = 1

        while api_pos <= 100:
            batch_num = (api_pos - 1) // PAGE_SIZE + 1
            print(f"--- Batch {batch_num} (sidebar positions {api_pos}–{api_pos + PAGE_SIZE - 1}) ---")

            expected_dom = dom_count + PAGE_SIZE
            wait_for_sidebar_count(page, min(expected_dom, total))

            batch = get_inbox_batch_via_api(page, api_pos)
            if not batch:
                break

            s, sk = process_batch(page, con, batch, dom_count, to_scrape, api_pos - 1, total)
            total_scraped += s
            total_skipped += sk
            dom_count += len(batch)

            if len(batch) < PAGE_SIZE or api_pos + PAGE_SIZE > 100:
                break

            scroll_sidebar(page)
            api_pos += PAGE_SIZE

        browser.close()

    print(f"\nDone. Scraped: {total_scraped} | Unchanged: {total_skipped}")
    con.close()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["full", "incremental"],
                        nargs="?", default="incremental")
    parser.add_argument("--db", default="leads.db")
    args = parser.parse_args()

    if args.mode == "full":
        run_full(args.db)
    else:
        run_incremental(args.db)

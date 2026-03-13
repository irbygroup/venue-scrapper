import os
import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL")


def get_config(key: str, default: str = "") -> str:
    """Read a config value from the config table, falling back to default."""
    try:
        con = psycopg2.connect(DATABASE_URL)
        cur = con.cursor()
        cur.execute("SELECT value FROM config WHERE name=%s", (key,))
        row = cur.fetchone()
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

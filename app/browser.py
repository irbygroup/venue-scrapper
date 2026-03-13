import asyncio
import json
import random
from typing import Optional, Callable

import psycopg2
from playwright.async_api import async_playwright, Page, BrowserContext

from app.config import (
    DATABASE_URL, get_config, BROWSER_OPTS, INIT_SCRIPT,
    inbox_url, signin_url, email, password,
)


class BrowserManager:
    def __init__(self, on_error: Optional[Callable[[str, str], None]] = None):
        self.pw        = None
        self.browser   = None
        self.context:    Optional[BrowserContext] = None
        self.sync_page:  Optional[Page] = None   # all API fetches + sync
        self.reply_page: Optional[Page] = None   # DOM interactions for replies
        self.sync_lock  = asyncio.Lock()
        self.reply_lock = asyncio.Lock()
        self._on_error  = on_error

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
        con = psycopg2.connect(DATABASE_URL)
        cur = con.cursor()
        cur.execute(
            "INSERT INTO config (name, value) VALUES (%s, %s) ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
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
            if self._on_error:
                self._on_error("Auto-login failed", "Session expired and automatic re-login failed. Manual intervention may be required.")
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

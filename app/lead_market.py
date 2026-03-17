import asyncio
import random
import traceback
from datetime import datetime, timezone

from app import state as state_mod
from app.config import inbox_url
from app.email import notify_error
from app.sync import run_sync


async def run_check_lead_market() -> dict:
    """
    Navigate to Lead Market, move all free leads to inbox,
    then trigger sync so existing pipeline handles them.
    """
    bm = state_mod.get_bm()
    started_at = datetime.now(timezone.utc)

    try:
        return await _do_check_lead_market(bm, started_at)
    except Exception as e:
        detail = f"{e}\n\n{traceback.format_exc()}"
        print(f"Lead Market check failed: {detail}")
        notify_error("Lead Market check failed", detail)
        return {"error": "lead_market_failed", "message": str(e)}


async def _do_check_lead_market(bm, started_at) -> dict:
    if not await bm.ensure_session():
        return {"error": "login_failed", "message": "Could not authenticate with Eventective"}

    page = bm.reply_page

    # Cold-page bootstrap: ensure reply_page has domain context
    if "eventective.com" not in page.url:
        await page.goto(inbox_url(), wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=15000)
        await asyncio.sleep(1)

    # Click "Lead Market" nav link (mimic real user)
    lead_market_link = page.locator('a[href="#/crm/Event/LeadMarket"]').first
    await lead_market_link.click()
    await page.wait_for_load_state("networkidle", timeout=15000)
    await asyncio.sleep(random.uniform(1.5, 2.5))

    moved = []
    skipped = []

    print(f"Lead Market: page URL after nav = {page.url}")

    # Wait for Angular to render lead rows (or confirm page is empty)
    try:
        await page.wait_for_selector("div.sc-table-row", timeout=8000)
    except Exception:
        # Might be a dialog blocking, or genuinely empty
        pass

    # Dismiss the Lead Market help/intro dialog if visible (Bootstrap modal)
    try:
        intro_dialog = page.locator("div.modal.show:has-text('Lead Market')")
        if await intro_dialog.is_visible(timeout=1000):
            cont_btn = intro_dialog.locator("button:has-text('Continue')")
            if await cont_btn.is_visible(timeout=1000):
                await cont_btn.click()
                print("Lead Market: dismissed intro dialog")
                await asyncio.sleep(random.uniform(1.0, 2.0))
    except Exception:
        pass

    # Debug: snapshot visible text to understand page state
    page_text = await page.evaluate("document.body?.innerText?.substring(0, 500) || 'empty'")
    print(f"Lead Market: page text preview = {page_text[:300]}")

    # Process leads one at a time — DOM changes after each move
    while True:
        rows = await page.query_selector_all("div.sc-table-row")
        print(f"Lead Market: found {len(rows)} lead rows")
        if not rows:
            break

        # Find the first free lead that hasn't been processed
        found_free = False
        for row in rows:
            row_text = await row.text_content() or ""

            if "Free" not in row_text:
                # Extract lead info for skip log
                skipped.append(_extract_lead_info(row_text))
                continue

            found_free = True
            lead_info = _extract_lead_info(row_text)

            # Click the row to select it
            await row.click()
            await asyncio.sleep(random.uniform(1.0, 2.0))

            # Click "To Inbox" button in detail panel
            to_inbox_btn = page.locator("div.lead-details-action-btn:has(i.fa-inbox)")
            try:
                await to_inbox_btn.wait_for(state="visible", timeout=5000)
            except Exception:
                # Fallback: find by text
                to_inbox_btn = page.locator("div.lead-details-action-btn", has_text="To Inbox")
                await to_inbox_btn.wait_for(state="visible", timeout=5000)

            await asyncio.sleep(random.uniform(0.5, 1.0))
            await to_inbox_btn.click()
            await asyncio.sleep(random.uniform(0.8, 1.5))

            # Handle confirmation dialog
            dialog = page.locator("#sc-confirm-lead-inbox")
            try:
                await dialog.wait_for(state="visible", timeout=3000)
                yes_btn = dialog.locator("button.sc-dark-btn")
                await asyncio.sleep(random.uniform(0.3, 0.6))
                await yes_btn.click()
                await asyncio.sleep(random.uniform(1.0, 2.0))
            except Exception:
                # Dialog might not appear if "Don't show again" was checked
                await asyncio.sleep(random.uniform(0.5, 1.0))

            # Handle Purchase Lead Credit dialog (paid lead snuck through)
            try:
                purchase_dialog = page.locator("#sc-purchase-lead-credit, div:has(> h3:text('Purchase Lead Credit'))")
                if await purchase_dialog.is_visible(timeout=500):
                    close_btn = purchase_dialog.locator("button.btn-close, button:has-text('Clear')")
                    await close_btn.click()
                    await asyncio.sleep(0.5)
                    skipped.append({**lead_info, "reason": "requires_purchase"})
                    continue
            except Exception:
                pass

            moved.append(lead_info)
            print(f"Lead Market: moved lead to inbox — {lead_info.get('summary', 'unknown')}")

            # Wait for DOM to update (row removal)
            await asyncio.sleep(random.uniform(1.0, 1.5))
            break  # Re-query rows from the top

        if not found_free:
            break

    # Trigger sync to pick up newly moved leads
    sync_result = None
    if moved:
        print(f"Lead Market: {len(moved)} leads moved, triggering sync...")
        await asyncio.sleep(random.uniform(1.0, 2.0))
        if not bm.sync_lock.locked():
            async with bm.sync_lock:
                sync_result = await run_sync()

    duration = (datetime.now(timezone.utc) - started_at).total_seconds()
    return {
        "duration_seconds": round(duration, 1),
        "leads_moved": len(moved),
        "leads_skipped": len(skipped),
        "moved": moved,
        "skipped": skipped,
        "sync_triggered": sync_result is not None,
        "sync_new_leads": len(sync_result.get("new_leads", [])) if sync_result else 0,
        "debug_url": page.url,
    }


def _extract_lead_info(row_text: str) -> dict:
    """Extract readable info from a lead row's text content."""
    parts = [p.strip() for p in row_text.split("\n") if p.strip()]
    return {
        "summary": " | ".join(parts[:4]) if parts else "unknown",
        "raw_parts": parts[:6],
    }

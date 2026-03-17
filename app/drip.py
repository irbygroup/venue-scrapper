"""
Drip campaign state machine, scheduler, and batch sending.
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from psycopg2.extras import RealDictCursor

from app.config import _cfg
from app.db import get_db
from app.email import notify_error
from app.llm import generate_reply_for_lead, generate_all_seq1

log = logging.getLogger("drip")

# ── Sequence definitions ─────────────────────────────────────────────────────

# Delays in days from previous step
SEQUENCE_STEPS = {
    "new_lead":          [0, 1, 3, 7],
    "unanswered_reply":  [1, 3, 7, 14],
    "long_term_nurture": [30, 60, 120, 180, 180, 180],
}

SEQUENCE_TRANSITIONS = {
    "new_lead":          "long_term_nurture",
    "unanswered_reply":  "long_term_nurture",
    "long_term_nurture": None,
}

ALLOWED_FUB_STAGES = {None, "", "YH | Hot Lead", "YH | Long Term Nurture"}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _schedule_next(step_delay_days: int) -> str:
    """Compute next_scheduled_at from now + delay days."""
    dt = datetime.now(timezone.utc) + timedelta(days=step_delay_days)
    return dt.isoformat()


def _daily_cap(sequence: str) -> int:
    """Read daily cap from config. 0 = unlimited."""
    key = f"drip_{sequence.replace('long_term_nurture', 'seq3').replace('unanswered_reply', 'seq2').replace('new_lead', 'seq1')}_daily_cap"
    # Normalize key
    key_map = {
        "new_lead": "drip_seq1_daily_cap",
        "unanswered_reply": "drip_seq2_daily_cap",
        "long_term_nurture": "drip_seq3_daily_cap",
    }
    return int(_cfg(key_map.get(sequence, "drip_seq3_daily_cap"), "0"))


# ── Disqualification checks ─────────────────────────────────────────────────

def check_disqualified(cur, event_id: str) -> tuple[Optional[str], bool]:
    """
    Check if a lead should be cancelled or skipped.
    Returns (cancel_reason, skip_only).
    - cancel_reason set + skip_only=False → cancel the campaign
    - cancel_reason set + skip_only=True → skip this cycle but don't cancel
    - cancel_reason=None → OK to proceed
    """
    cur.execute(
        'SELECT "LeadStatus", fub_lead_stage FROM eventective_leads WHERE "EventId"=%s',
        (event_id,),
    )
    row = cur.fetchone()
    if not row:
        return "lead_not_found", False

    status = row["LeadStatus"]
    if status in ("Deleted", "Lost", "Booked"):
        return f"lead_status_{status.lower()}", False

    # Check for NoInterest activity
    cur.execute(
        'SELECT 1 FROM eventective_lead_activities WHERE "EventId"=%s AND "ActivityTypeCd"=%s LIMIT 1',
        (event_id, "NoInterest"),
    )
    if cur.fetchone():
        return "no_interest_activity", False

    # FUB stage gate — skip but don't cancel (Veronica might move them back)
    fub_stage = row.get("fub_lead_stage")
    if fub_stage and fub_stage not in ALLOWED_FUB_STAGES:
        return f"fub_stage_manual:{fub_stage}", True

    return None, False


def get_daily_send_counts(cur) -> dict:
    """Count messages sent today per sequence."""
    today = _today_str()
    cur.execute(
        """SELECT sequence, count(*) as cnt
           FROM drip_messages
           WHERE sent_at LIKE %s AND result = 'success'
           GROUP BY sequence""",
        (f"{today}%",),
    )
    return {row["sequence"]: row["cnt"] for row in cur.fetchall()}


# ── Campaign CRUD ────────────────────────────────────────────────────────────

def create_campaign(cur, event_id: str, sequence: str, immediate: bool = False, start_step: int = 0):
    """
    Create a drip campaign. Skips if an active/paused campaign already exists.
    Overwrites completed/cancelled/transitioned campaigns.
    start_step allows enrolling mid-sequence (e.g., lead is 2 days old, skip to step 2).
    """
    # Check for existing campaign
    cur.execute('SELECT status FROM drip_campaigns WHERE "EventId"=%s', (event_id,))
    existing = cur.fetchone()
    if existing and existing["status"] in ("active", "paused"):
        log.debug(f"Skipping create for {event_id}: already {existing['status']}")
        return False

    steps = SEQUENCE_STEPS[sequence]
    if start_step >= len(steps):
        start_step = 0
    delay = steps[start_step] if not immediate else 0
    now = _now_iso()

    if existing:
        cur.execute(
            """UPDATE drip_campaigns
               SET sequence=%s, current_step=%s, status='active',
                   last_outbound_at=NULL, last_inbound_at=NULL,
                   next_scheduled_at=%s, cancel_reason=NULL,
                   created_at=%s, updated_at=%s
               WHERE "EventId"=%s""",
            (sequence, start_step, _schedule_next(delay), now, now, event_id),
        )
    else:
        cur.execute(
            """INSERT INTO drip_campaigns
               ("EventId", sequence, current_step, status, next_scheduled_at, created_at, updated_at)
               VALUES (%s, %s, %s, 'active', %s, %s, %s)""",
            (event_id, sequence, start_step, _schedule_next(delay), now, now),
        )

    log.info(f"Created campaign: {event_id} → {sequence} step {start_step} (immediate={immediate})")
    return True


def advance_campaign(cur, event_id: str):
    """Advance to the next step or transition/complete."""
    cur.execute(
        'SELECT sequence, current_step FROM drip_campaigns WHERE "EventId"=%s',
        (event_id,),
    )
    row = cur.fetchone()
    if not row:
        return

    sequence = row["sequence"]
    current_step = row["current_step"]
    steps = SEQUENCE_STEPS[sequence]
    next_step = current_step + 1

    now = _now_iso()

    if next_step >= len(steps):
        # Past last step — transition or complete
        next_seq = SEQUENCE_TRANSITIONS[sequence]
        if next_seq:
            log.info(f"Transitioning {event_id}: {sequence} → {next_seq}")
            cur.execute(
                """UPDATE drip_campaigns
                   SET status='transitioned', updated_at=%s
                   WHERE "EventId"=%s""",
                (now, event_id),
            )
            create_campaign(cur, event_id, next_seq)
        else:
            log.info(f"Campaign completed: {event_id} ({sequence})")
            cur.execute(
                """UPDATE drip_campaigns
                   SET status='completed', updated_at=%s
                   WHERE "EventId"=%s""",
                (now, event_id),
            )
    else:
        delay = steps[next_step]
        cur.execute(
            """UPDATE drip_campaigns
               SET current_step=%s, next_scheduled_at=%s, updated_at=%s
               WHERE "EventId"=%s""",
            (next_step, _schedule_next(delay), now, event_id),
        )


def handle_lead_reply(cur, event_id: str):
    """
    Handle when a lead replies. Transition/restart campaigns as needed.
    Called from post-sync hook.
    """
    now = _now_iso()

    cur.execute(
        'SELECT sequence, status FROM drip_campaigns WHERE "EventId"=%s',
        (event_id,),
    )
    row = cur.fetchone()

    if not row or row["status"] not in ("active", "paused"):
        # No active campaign — create unanswered_reply
        create_campaign(cur, event_id, "unanswered_reply")
        return

    sequence = row["sequence"]

    if sequence == "new_lead":
        # They engaged during new_lead → transition to unanswered_reply
        cur.execute(
            """UPDATE drip_campaigns
               SET status='transitioned', cancel_reason='lead_replied', updated_at=%s
               WHERE "EventId"=%s""",
            (now, event_id),
        )
        create_campaign(cur, event_id, "unanswered_reply")

    elif sequence == "unanswered_reply":
        # They replied again during unanswered_reply → restart from step 0
        steps = SEQUENCE_STEPS["unanswered_reply"]
        cur.execute(
            """UPDATE drip_campaigns
               SET current_step=0, status='active', last_inbound_at=%s,
                   next_scheduled_at=%s, updated_at=%s
               WHERE "EventId"=%s""",
            (now, _schedule_next(steps[0]), now, event_id),
        )

    elif sequence == "long_term_nurture":
        # They re-engaged from nurture → transition to unanswered_reply
        cur.execute(
            """UPDATE drip_campaigns
               SET status='transitioned', cancel_reason='lead_replied', updated_at=%s
               WHERE "EventId"=%s""",
            (now, event_id),
        )
        create_campaign(cur, event_id, "unanswered_reply")

    log.info(f"Handled reply for {event_id} (was {sequence})")


# ── Batch sending ────────────────────────────────────────────────────────────

async def send_batch(messages: list[dict]) -> dict:
    """
    Send a batch of messages directly via Playwright with human-like delays.
    Each item: {event_id, message_text, drip_message_id}
    Calls _do_send_reply directly (no HTTP self-call).
    """
    if not messages:
        return {"sent": 0, "failed": 0}

    from app.routes.leads import _do_send_reply
    from app import state as state_mod

    bm = state_mod.get_bm()
    sent = 0
    failed = 0

    async with bm.reply_lock:
        for i, msg in enumerate(messages):
            if i > 0:
                delay = random.uniform(5, 10)
                log.info(f"Waiting {delay:.1f}s before next send...")
                await asyncio.sleep(delay)

            event_id = msg["event_id"]

            # Re-sync lead from Eventective before sending to catch manual replies
            try:
                detail = await bm.fetch(f"/api/v1/salesandcatering/geteventdetails?id={event_id}")
                if detail and not (isinstance(detail, dict) and "__error" in detail):
                    from app.db import upsert_lead_details, upsert_activities
                    con_sync = get_db()
                    cur_sync = con_sync.cursor()
                    upsert_lead_details(cur_sync, event_id, detail)
                    upsert_activities(cur_sync, event_id, detail.get("Activities") or [])
                    con_sync.commit()
                    con_sync.close()
            except Exception as e:
                log.warning(f"Pre-send sync failed for {event_id} (sending anyway): {e}")

            # Check if someone already replied manually since we generated
            con_chk = get_db()
            cur_chk = con_chk.cursor()
            cur_chk.execute(
                """SELECT count(*) as cnt FROM eventective_lead_activities
                   WHERE "EventId" = %s AND "ActivityTypeCd" = 'provplnr'
                   AND "DateTime" > (SELECT created_at FROM drip_campaigns WHERE "EventId" = %s)""",
                (event_id, event_id),
            )
            manual_replies = cur_chk.fetchone()[0]
            con_chk.close()

            if manual_replies > 0:
                log.info(f"Skipping drip for {event_id}: manual reply detected since enrollment")
                con_upd = get_db()
                cur_upd = con_upd.cursor()
                cur_upd.execute(
                    "UPDATE drip_messages SET sent_at=%s, result=%s WHERE id=%s",
                    (_now_iso(), "skipped:manual_reply", msg["drip_message_id"]),
                )
                con_upd.commit()
                con_upd.close()
                continue

            try:
                reply_result = await _do_send_reply(event_id, msg["message_text"])
                if reply_result.get("success"):
                    result = "success"
                    sent += 1
                    log.info(f"Sent drip message to {event_id}")
                else:
                    result = f"failed:{reply_result.get('error', 'unknown')}"
                    failed += 1
                    log.error(f"Send failed for {event_id}: {reply_result.get('error')}")
            except Exception as e:
                result = f"failed:{e}"
                failed += 1
                log.error(f"Send error for {event_id}: {e}")

            # Update drip_messages row
            con = get_db()
            cur = con.cursor()
            cur.execute(
                "UPDATE drip_messages SET sent_at=%s, result=%s WHERE id=%s",
                (_now_iso(), result, msg["drip_message_id"]),
            )
            con.commit()
            con.close()

    return {"sent": sent, "failed": failed}


# ── Main scheduler ───────────────────────────────────────────────────────────

async def process_due_campaigns() -> dict:
    """
    Process all due drip campaigns. Called every 15 minutes by cron.

    Phase 1: Generate messages for all due campaigns
    Phase 2: Batch send if drip_auto_send=true
    Phase 3: Backfill Sequence 3
    """
    con = get_db()
    cur = con.cursor(cursor_factory=RealDictCursor)

    auto_send = _cfg("drip_auto_send", "false") == "true"
    daily_counts = get_daily_send_counts(cur)
    now = _now_iso()

    # Query due campaigns
    cur.execute(
        """SELECT "EventId", sequence, current_step, status
           FROM drip_campaigns
           WHERE status = 'active' AND next_scheduled_at <= %s
           ORDER BY next_scheduled_at ASC""",
        (now,),
    )
    due = cur.fetchall()

    generated = []
    cancelled = 0
    skipped = 0
    errors = 0

    llm_failures = 0
    LLM_FAIL_THRESHOLD = 3  # notify after this many consecutive LLM failures

    for campaign in due:
        event_id = campaign["EventId"]
        sequence = campaign["sequence"]
        step = campaign["current_step"]

        # Check daily cap
        cap = _daily_cap(sequence)
        if cap > 0 and daily_counts.get(sequence, 0) >= cap:
            skipped += 1
            continue

        # Check disqualification
        cancel_reason, skip_only = check_disqualified(cur, event_id)
        if cancel_reason and not skip_only:
            cur.execute(
                """UPDATE drip_campaigns
                   SET status='cancelled', cancel_reason=%s, updated_at=%s
                   WHERE "EventId"=%s""",
                (cancel_reason, now, event_id),
            )
            con.commit()
            cancelled += 1
            continue
        elif cancel_reason and skip_only:
            skipped += 1
            continue

        # If LLM is down, skip remaining campaigns (they'll retry next run)
        if llm_failures >= LLM_FAIL_THRESHOLD:
            skipped += 1
            continue

        # Check for pre-generated or previously failed message to retry
        cur.execute(
            """SELECT id, message, result FROM drip_messages
               WHERE "EventId"=%s AND sequence=%s AND step=%s AND (result='scheduled' OR result LIKE 'failed:%%')
               ORDER BY CASE WHEN result='scheduled' THEN 0 ELSE 1 END, id LIMIT 1""",
            (event_id, sequence, step),
        )
        pre_gen = cur.fetchone()

        if pre_gen:
            # Use pre-generated message
            msg_id = pre_gen["id"]
            message_text = pre_gen["message"]
            next_step_val = "reply_now"
        else:
            # Generate via LLM
            try:
                result = await generate_reply_for_lead(event_id, sequence, step)
                llm_failures = 0  # reset on success
            except Exception as e:
                log.error(f"LLM generation failed for {event_id}: {e}")
                errors += 1
                llm_failures += 1
                if llm_failures == LLM_FAIL_THRESHOLD:
                    notify_error(
                        "Drip LLM is down — skipping remaining campaigns",
                        f"Failed {LLM_FAIL_THRESHOLD} consecutive LLM calls. "
                        f"Last error: {e}\n\n"
                        f"Campaigns will auto-retry on next drip/process run."
                    )
                continue

            next_step_val = result.get("next_step", "reply_now")

            # Handle model recommendations
            if next_step_val == "dont_contact":
                cur.execute(
                    """UPDATE drip_campaigns
                       SET status='cancelled', cancel_reason=%s, updated_at=%s
                       WHERE "EventId"=%s""",
                    (f"model_dont_contact:{result.get('next_step_reason', '')}", now, event_id),
                )
                con.commit()
                cancelled += 1
                continue

            if next_step_val == "skip":
                advance_campaign(cur, event_id)
                con.commit()
                skipped += 1
                continue

            if next_step_val == "nurture":
                cur.execute(
                    """UPDATE drip_campaigns
                       SET status='transitioned', cancel_reason='model_nurture', updated_at=%s
                       WHERE "EventId"=%s""",
                    (now, event_id),
                )
                create_campaign(cur, event_id, "long_term_nurture")
                con.commit()
                skipped += 1
                continue

            message_text = result["proposed_reply"]

            # Store the generated message
            cur.execute(
                """INSERT INTO drip_messages
                   ("EventId", sequence, step, message, next_step, next_step_reason, tone_notes, model, result, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    event_id, sequence, step, message_text,
                    result.get("next_step"), result.get("next_step_reason"),
                    result.get("tone_notes"), result.get("model"),
                    "pending_review" if not auto_send else "pending_send",
                    now,
                ),
            )
            msg_id = cur.fetchone()["id"]
            con.commit()

        if auto_send:
            generated.append({
                "event_id": event_id,
                "message_text": message_text,
                "drip_message_id": msg_id,
                "sequence": sequence,
            })
        else:
            # Mark as pending_review
            if pre_gen:
                cur.execute(
                    "UPDATE drip_messages SET result='pending_review' WHERE id=%s",
                    (msg_id,),
                )
                con.commit()

        daily_counts[sequence] = daily_counts.get(sequence, 0) + 1

    # Phase 2: Batch send
    send_result = {"sent": 0, "failed": 0}
    if auto_send and generated:
        send_result = await send_batch(generated)

        # Advance campaigns for successfully sent messages
        failed_sends = []
        for msg in generated:
            con2 = get_db()
            cur2 = con2.cursor(cursor_factory=RealDictCursor)
            # Check if this message was sent successfully
            cur2.execute("SELECT result FROM drip_messages WHERE id=%s", (msg["drip_message_id"],))
            dm_row = cur2.fetchone()
            if dm_row and dm_row["result"] == "success":
                cur2.execute(
                    """UPDATE drip_campaigns SET last_outbound_at=%s, updated_at=%s
                       WHERE "EventId"=%s""",
                    (_now_iso(), _now_iso(), msg["event_id"]),
                )
                advance_campaign(cur2, msg["event_id"])
            elif dm_row and dm_row["result"].startswith("failed"):
                failed_sends.append(f"{msg['event_id']}: {dm_row['result'][:200]}")
            con2.commit()
            con2.close()

        if failed_sends:
            notify_error(
                f"Drip send failed for {len(failed_sends)} message(s)",
                "\n".join(failed_sends) + "\n\nMessages will auto-retry on next drip/process run."
            )

    # Phase 3: Backfill Sequence 3
    backfilled = 0
    seq3_cap = _daily_cap("long_term_nurture")
    seq3_sent = daily_counts.get("long_term_nurture", 0)
    if seq3_cap == 0 or seq3_sent < seq3_cap:
        remaining = (seq3_cap - seq3_sent) if seq3_cap > 0 else 25  # default batch
        backfilled = backfill_seq3(cur, remaining)
        con.commit()

    con.close()

    summary = {
        "due_campaigns": len(due),
        "generated": len(generated),
        "sent": send_result["sent"],
        "send_failed": send_result["failed"],
        "cancelled": cancelled,
        "skipped": skipped,
        "errors": errors,
        "backfilled_seq3": backfilled,
        "auto_send": auto_send,
    }
    log.info(f"Drip process complete: {summary}")

    # Send error email if there were failures
    if send_result["failed"] > 0 or errors > 0:
        notify_error(
            "Drip process had failures",
            f"Send failures: {send_result['failed']}, LLM errors: {errors}, "
            f"Cancelled: {cancelled}, Total due: {len(due)}"
        )

    return summary


def _classify_lead_for_backfill(cur, event_id: str) -> tuple[str, int]:
    """
    Determine the right sequence AND starting step for an existing lead.
    Returns: (sequence, starting_step)

    Logic:
    - Never contacted, <=14d old → new_lead step 0
    - Never contacted, >14d old → long_term_nurture step 0
    - We replied, they didn't, <=1d ago → new_lead step 1 (+1d nudge)
    - We replied, they didn't, 1-3d ago → new_lead step 2 (+3d value-add)
    - We replied, they didn't, 3-7d ago → new_lead step 3 (+7d last push)
    - We replied, they didn't, >7d ago → long_term_nurture step 0
    - They replied to us → unanswered_reply step 0
    """
    cur.execute(
        """SELECT count(*) as cnt FROM eventective_lead_activities
           WHERE "EventId" = %s AND "ActivityTypeCd" = 'provplnr'""",
        (event_id,),
    )
    our_count = cur.fetchone()["cnt"]

    cur.execute(
        """SELECT count(*) as cnt FROM eventective_lead_activities
           WHERE "EventId" = %s AND "ActivityTypeCd" = 'plnrprov'""",
        (event_id,),
    )
    their_count = cur.fetchone()["cnt"]

    # Get lead age
    cur.execute(
        'SELECT "EmailSentDttm" FROM eventective_leads WHERE "EventId" = %s',
        (event_id,),
    )
    row = cur.fetchone()
    age_days = 999
    if row and row["EmailSentDttm"]:
        try:
            sent = datetime.fromisoformat(row["EmailSentDttm"].replace("Z", "")).replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - sent).days
        except Exception:
            pass

    if their_count > 0:
        # They replied — unanswered_reply follow-up
        return "unanswered_reply", 0

    if our_count == 0:
        # Never contacted
        if age_days <= 14:
            return "new_lead", 0
        return "long_term_nurture", 0

    # We replied, they didn't — pick step based on how recently
    # Get days since our last reply
    cur.execute(
        """SELECT max("DateTime") as last_reply FROM eventective_lead_activities
           WHERE "EventId" = %s AND "ActivityTypeCd" = 'provplnr'""",
        (event_id,),
    )
    last_reply_row = cur.fetchone()
    days_since_reply = 999
    if last_reply_row and last_reply_row["last_reply"]:
        try:
            lr = datetime.fromisoformat(last_reply_row["last_reply"].replace("Z", "")).replace(tzinfo=timezone.utc)
            days_since_reply = (datetime.now(timezone.utc) - lr).days
        except Exception:
            pass

    if days_since_reply <= 1:
        return "new_lead", 1   # +1d nudge
    elif days_since_reply <= 3:
        return "new_lead", 2   # +3d value-add
    elif days_since_reply <= 7:
        return "new_lead", 3   # +7d last push
    else:
        return "long_term_nurture", 0


def backfill_seq3(cur, daily_remaining: int) -> int:
    """
    Enroll eligible leads into the appropriate drip sequence.
    Classifies each lead based on thread state rather than dumping all into Seq 3.
    Priority: never-contacted first, then newest-first.
    """
    if daily_remaining <= 0:
        return 0

    # Find leads NOT already in drip_campaigns
    cur.execute(
        """SELECT el."EventId", el."LastActivityType", el."LeadStatus"
           FROM eventective_leads el
           LEFT JOIN drip_campaigns dc ON dc."EventId" = el."EventId"
           WHERE dc."EventId" IS NULL
             AND el."LeadStatus" = 'Prospect'
           ORDER BY
             -- Priority 1: never contacted (NULL or LeadPurchased) first
             CASE WHEN el."LastActivityType" IS NULL OR el."LastActivityType" = 'LeadPurchased'
                  THEN 0 ELSE 1 END,
             -- Within each group: newest first
             el."EmailSentDttm" DESC
           LIMIT %s""",
        (daily_remaining,),
    )
    rows = cur.fetchall()

    created = 0
    for row in rows:
        event_id = row["EventId"]

        # Quick disqualification check
        cancel_reason, _ = check_disqualified(cur, event_id)
        if cancel_reason:
            continue

        sequence, start_step = _classify_lead_for_backfill(cur, event_id)
        if create_campaign(cur, event_id, sequence, start_step=start_step):
            created += 1
            log.debug(f"Backfill: {event_id} → {sequence} step {start_step}")

    if created:
        log.info(f"Backfilled {created} leads into drip campaigns")
    return created


# ── Post-sync hooks (called from app/sync.py) ───────────────────────────────

async def drip_post_sync_new_leads(event_ids: list[str]):
    """Create Seq 1 campaigns + pre-generate all messages for truly new leads only."""
    con = get_db()
    cur = con.cursor(cursor_factory=RealDictCursor)

    for event_id in event_ids:
        try:
            if not create_campaign(cur, event_id, "new_lead", immediate=True):
                continue
            con.commit()

            # Pre-generate all 4 Sequence 1 messages
            results = await generate_all_seq1(event_id)
            now = _now_iso()
            for step, result in enumerate(results):
                cur.execute(
                    """INSERT INTO drip_messages
                       ("EventId", sequence, step, message, next_step, next_step_reason,
                        tone_notes, model, result, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        event_id, "new_lead", step, result["proposed_reply"],
                        result.get("next_step"), result.get("next_step_reason"),
                        result.get("tone_notes"), result.get("model"),
                        "scheduled", now,
                    ),
                )
            con.commit()
            log.info(f"Pre-generated 4 Seq 1 messages for {event_id}")

            # Send step 0 immediately
            if _cfg("drip_auto_send", "false") == "true" and results[0].get("next_step") != "skip":
                step0_msg = cur.execute(
                    """SELECT id, message FROM drip_messages
                       WHERE "EventId"=%s AND sequence='new_lead' AND step=0 AND result='scheduled'
                       ORDER BY id LIMIT 1""",
                    (event_id,),
                )
                step0 = cur.fetchone()
                if step0:
                    send_result = await send_batch([{
                        "event_id": event_id,
                        "message_text": step0["message"],
                        "drip_message_id": step0["id"],
                        "sequence": "new_lead",
                    }])
                    if send_result["sent"] > 0:
                        cur.execute(
                            """UPDATE drip_campaigns SET last_outbound_at=%s, updated_at=%s
                               WHERE "EventId"=%s""",
                            (_now_iso(), _now_iso(), event_id),
                        )
                        advance_campaign(cur, event_id)
                        con.commit()
                        log.info(f"Sent immediate step 0 for {event_id}")

        except Exception as e:
            log.error(f"Failed to create drip for new lead {event_id}: {e}")
            con.rollback()

    con.close()


async def drip_post_sync_replies(event_ids: list[str]):
    """Handle reply transitions for leads that replied to us."""
    con = get_db()
    cur = con.cursor(cursor_factory=RealDictCursor)

    for event_id in event_ids:
        try:
            handle_lead_reply(cur, event_id)
            con.commit()
        except Exception as e:
            log.error(f"Failed to handle drip reply for {event_id}: {e}")
            con.rollback()

    con.close()

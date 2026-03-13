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

def create_campaign(cur, event_id: str, sequence: str, immediate: bool = False):
    """
    Create a drip campaign. Skips if an active/paused campaign already exists.
    Overwrites completed/cancelled/transitioned campaigns.
    """
    # Check for existing campaign
    cur.execute('SELECT status FROM drip_campaigns WHERE "EventId"=%s', (event_id,))
    existing = cur.fetchone()
    if existing and existing["status"] in ("active", "paused"):
        log.debug(f"Skipping create for {event_id}: already {existing['status']}")
        return False

    steps = SEQUENCE_STEPS[sequence]
    delay = steps[0] if not immediate else 0
    now = _now_iso()

    if existing:
        # Overwrite completed/cancelled/transitioned
        cur.execute(
            """UPDATE drip_campaigns
               SET sequence=%s, current_step=0, status='active',
                   last_outbound_at=NULL, last_inbound_at=NULL,
                   next_scheduled_at=%s, cancel_reason=NULL,
                   created_at=%s, updated_at=%s
               WHERE "EventId"=%s""",
            (sequence, _schedule_next(delay), now, now, event_id),
        )
    else:
        cur.execute(
            """INSERT INTO drip_campaigns
               ("EventId", sequence, current_step, status, next_scheduled_at, created_at, updated_at)
               VALUES (%s, %s, 0, 'active', %s, %s, %s)""",
            (event_id, sequence, _schedule_next(delay), now, now),
        )

    log.info(f"Created campaign: {event_id} → {sequence} (immediate={immediate})")
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
    Send a batch of messages via the reply endpoint with human-like delays.
    Each item: {event_id, message_text, drip_message_id}
    """
    if not messages:
        return {"sent": 0, "failed": 0}

    sent = 0
    failed = 0
    api_base = "http://localhost:5050/eventective"

    async with httpx.AsyncClient(timeout=60.0) as client:
        for i, msg in enumerate(messages):
            if i > 0:
                delay = random.uniform(5, 10)
                log.info(f"Waiting {delay:.1f}s before next send...")
                await asyncio.sleep(delay)

            try:
                resp = await client.post(
                    f"{api_base}/leads/{msg['event_id']}/reply",
                    json={"message": msg["message_text"]},
                )
                if resp.status_code == 200:
                    result = "success"
                    sent += 1
                    log.info(f"Sent drip message to {msg['event_id']}")
                else:
                    result = f"failed:{resp.status_code}"
                    failed += 1
                    log.error(f"Send failed for {msg['event_id']}: {resp.status_code}")
            except Exception as e:
                result = f"failed:{e}"
                failed += 1
                log.error(f"Send error for {msg['event_id']}: {e}")

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

        # Check for pre-generated message (Seq 1)
        cur.execute(
            """SELECT id, message FROM drip_messages
               WHERE "EventId"=%s AND sequence=%s AND step=%s AND result='scheduled'
               ORDER BY id LIMIT 1""",
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
            except Exception as e:
                log.error(f"LLM generation failed for {event_id}: {e}")
                errors += 1
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
            con2.commit()
            con2.close()

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
    return summary


def backfill_seq3(cur, daily_remaining: int) -> int:
    """
    Enroll eligible leads into Sequence 3 (long-term nurture).
    Priority: never-contacted first, then newest-first among replied leads.
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

        if create_campaign(cur, event_id, "long_term_nurture"):
            created += 1

    if created:
        log.info(f"Backfilled {created} leads into long_term_nurture")
    return created


# ── Post-sync hooks (called from app/sync.py) ───────────────────────────────

async def drip_post_sync_new_leads(event_ids: list[str]):
    """Create Seq 1 campaigns + pre-generate all messages for new leads."""
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

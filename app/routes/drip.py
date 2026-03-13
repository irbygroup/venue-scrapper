"""Drip campaign management endpoints."""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from psycopg2.extras import RealDictCursor

from app.config import _cfg
from app.db import get_db
from app.drip import process_due_campaigns, SEQUENCE_STEPS

router = APIRouter()
log = logging.getLogger("drip-routes")

_drip_lock = asyncio.Lock()


class CancelRequest(BaseModel):
    reason: str


@router.post("/drip/process")
async def run_drip_process():
    """Run the drip scheduler. Called by cron every 15 minutes."""
    if _drip_lock.locked():
        return {"status": "skipped", "reason": "already running"}

    async with _drip_lock:
        summary = await process_due_campaigns()
        return {"status": "ok", **summary}


@router.get("/drip/status")
async def drip_status():
    """Dashboard overview of drip campaigns."""
    con = get_db()
    cur = con.cursor(cursor_factory=RealDictCursor)

    # Counts by sequence + status
    cur.execute(
        """SELECT sequence, status, count(*) as cnt
           FROM drip_campaigns
           GROUP BY sequence, status
           ORDER BY sequence, status"""
    )
    by_seq = {}
    for row in cur.fetchall():
        seq = row["sequence"]
        if seq not in by_seq:
            by_seq[seq] = {}
        by_seq[seq][row["status"]] = row["cnt"]

    # Today's send counts
    from app.drip import get_daily_send_counts, _today_str
    daily = get_daily_send_counts(cur)

    # Pending review count
    cur.execute("SELECT count(*) as cnt FROM drip_messages WHERE result = 'pending_review'")
    pending = cur.fetchone()["cnt"]

    # Scheduled (pre-generated) count
    cur.execute("SELECT count(*) as cnt FROM drip_messages WHERE result = 'scheduled'")
    scheduled = cur.fetchone()["cnt"]

    # Next due campaigns
    cur.execute(
        """SELECT "EventId", sequence, current_step, next_scheduled_at
           FROM drip_campaigns
           WHERE status = 'active'
           ORDER BY next_scheduled_at ASC
           LIMIT 5"""
    )
    next_due = [dict(r) for r in cur.fetchall()]

    con.close()

    return {
        "campaigns_by_sequence": by_seq,
        "today_sent": daily,
        "pending_review": pending,
        "scheduled": scheduled,
        "next_due": next_due,
        "auto_send": _cfg("drip_auto_send", "false") == "true",
        "daily_caps": {
            "new_lead": int(_cfg("drip_seq1_daily_cap", "0")),
            "unanswered_reply": int(_cfg("drip_seq2_daily_cap", "25")),
            "long_term_nurture": int(_cfg("drip_seq3_daily_cap", "25")),
        },
    }


@router.get("/drip/{event_id}")
async def drip_detail(event_id: str):
    """Campaign state + message history for a specific lead."""
    con = get_db()
    cur = con.cursor(cursor_factory=RealDictCursor)

    cur.execute('SELECT * FROM drip_campaigns WHERE "EventId"=%s', (event_id,))
    campaign = cur.fetchone()

    cur.execute(
        'SELECT * FROM drip_messages WHERE "EventId"=%s ORDER BY created_at',
        (event_id,),
    )
    messages = [dict(r) for r in cur.fetchall()]

    con.close()

    if not campaign and not messages:
        raise HTTPException(status_code=404, detail=f"No drip data for {event_id}")

    return {
        "campaign": dict(campaign) if campaign else None,
        "messages": messages,
    }


@router.post("/drip/{event_id}/pause")
async def drip_pause(event_id: str):
    """Manually pause a lead's drip campaign."""
    con = get_db()
    cur = con.cursor()
    cur.execute(
        """UPDATE drip_campaigns SET status='paused', updated_at=now()::text
           WHERE "EventId"=%s AND status='active'""",
        (event_id,),
    )
    if cur.rowcount == 0:
        con.close()
        raise HTTPException(status_code=404, detail="No active campaign found")
    con.commit()
    con.close()
    return {"status": "paused", "event_id": event_id}


@router.post("/drip/{event_id}/resume")
async def drip_resume(event_id: str):
    """Resume a paused drip campaign."""
    con = get_db()
    cur = con.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        'SELECT sequence, current_step FROM drip_campaigns WHERE "EventId"=%s AND status=%s',
        (event_id, "paused"),
    )
    row = cur.fetchone()
    if not row:
        con.close()
        raise HTTPException(status_code=404, detail="No paused campaign found")

    # Recalculate next_scheduled_at from now
    from app.drip import _schedule_next, _now_iso
    steps = SEQUENCE_STEPS[row["sequence"]]
    step = row["current_step"]
    delay = steps[step] if step < len(steps) else 1

    cur.execute(
        """UPDATE drip_campaigns
           SET status='active', next_scheduled_at=%s, updated_at=%s
           WHERE "EventId"=%s""",
        (_schedule_next(delay), _now_iso(), event_id),
    )
    con.commit()
    con.close()
    return {"status": "resumed", "event_id": event_id}


@router.post("/drip/{event_id}/cancel")
async def drip_cancel(event_id: str, req: CancelRequest):
    """Cancel a drip campaign with a reason."""
    con = get_db()
    cur = con.cursor()

    from app.drip import _now_iso
    cur.execute(
        """UPDATE drip_campaigns
           SET status='cancelled', cancel_reason=%s, updated_at=%s
           WHERE "EventId"=%s AND status IN ('active', 'paused')""",
        (req.reason, _now_iso(), event_id),
    )
    if cur.rowcount == 0:
        con.close()
        raise HTTPException(status_code=404, detail="No active/paused campaign found")
    con.commit()
    con.close()
    return {"status": "cancelled", "event_id": event_id, "reason": req.reason}


@router.post("/drip/{event_id}/send")
async def drip_send(event_id: str):
    """Approve and send the next pending_review message for a lead."""
    con = get_db()
    cur = con.cursor(cursor_factory=RealDictCursor)

    cur.execute(
        """SELECT id, message, sequence, step FROM drip_messages
           WHERE "EventId"=%s AND result='pending_review'
           ORDER BY id LIMIT 1""",
        (event_id,),
    )
    msg = cur.fetchone()
    if not msg:
        con.close()
        raise HTTPException(status_code=404, detail="No pending_review message found")

    con.close()

    # Send via the batch sender (single message)
    from app.drip import send_batch, advance_campaign, _now_iso
    result = await send_batch([{
        "event_id": event_id,
        "message_text": msg["message"],
        "drip_message_id": msg["id"],
    }])

    # Advance campaign if sent successfully
    if result["sent"] > 0:
        con2 = get_db()
        cur2 = con2.cursor(cursor_factory=RealDictCursor)
        cur2.execute(
            """UPDATE drip_campaigns SET last_outbound_at=%s, updated_at=%s
               WHERE "EventId"=%s""",
            (_now_iso(), _now_iso(), event_id),
        )
        advance_campaign(cur2, event_id)
        con2.commit()
        con2.close()

    return {
        "status": "sent" if result["sent"] > 0 else "failed",
        "event_id": event_id,
        "message_id": msg["id"],
    }

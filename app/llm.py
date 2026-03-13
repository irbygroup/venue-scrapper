"""
LLM reply generation via LiteLLM proxy.
Loads prompts from flat files in prompts/ directory.
"""

import json
import logging
import os
from datetime import date
from pathlib import Path

import httpx
from psycopg2.extras import RealDictCursor

from app.config import _cfg
from app.db import get_db
from app.utils import build_lead_detail

log = logging.getLogger("llm")

PROMPTS_DIR = Path(os.path.dirname(os.path.dirname(__file__))) / "prompts"


def _load_prompt(path: str) -> str:
    """Load a prompt file relative to PROMPTS_DIR."""
    return (PROMPTS_DIR / path).read_text().strip()


def _inject_vars(text: str) -> str:
    """Replace template variables with config values."""
    text = text.replace("{{today}}", date.today().isoformat())
    text = text.replace("{{agent_name}}", _cfg("agent_name", "Veronica Miller"))
    text = text.replace("{{agent_phone}}", _cfg("agent_phone", "(251) 357-1185"))
    return text


def _build_system_prompt(sequence: str, step: int) -> str:
    """Build full system prompt from base + stage-specific file."""
    base = _inject_vars(_load_prompt("system_base.txt"))

    stage_file = f"{sequence}/step_{step}.txt"
    try:
        stage = _inject_vars(_load_prompt(stage_file))
    except FileNotFoundError:
        log.warning(f"Prompt file not found: {stage_file}, using base only")
        stage = ""

    return f"{base}\n\n{stage}" if stage else base


def _build_user_context(lead_detail: dict, sequence: str, step: int) -> str:
    """Build structured JSON user message from lead detail."""
    today = date.today().isoformat()

    # Extract competitive info from thread if available
    competitive_info = None
    for msg in lead_detail.get("thread", []):
        if msg.get("type") == "ResponseRank":
            competitive_info = msg.get("text")
            break

    context = {
        "today": today,
        "lead": {
            "event_id": lead_detail["event_id"],
            "contact": lead_detail["contact"],
            "event": lead_detail["event"],
            "meta": lead_detail["meta"],
            "urgency": lead_detail["urgency"],
            "urgency_reasons": lead_detail["urgency_reasons"],
        },
        "thread_summary": {
            "our_message_count": lead_detail["thread_signals"]["our_message_count"],
            "their_message_count": lead_detail["thread_signals"]["their_message_count"],
            "we_replied": lead_detail["thread_signals"]["we_replied"],
            "they_replied_to_us": lead_detail["thread_signals"]["they_replied_to_us"],
            "last_our_message": lead_detail["thread_signals"]["last_our_message"],
            "last_their_message": lead_detail["thread_signals"]["last_their_message"],
        },
        "thread": [
            msg for msg in lead_detail["thread"]
            if msg.get("type") not in ("read_receipt",)
        ],
        "drip_context": {
            "sequence": sequence,
            "step": step,
        },
    }

    if competitive_info:
        context["competitive_info"] = competitive_info

    return json.dumps(context)


def _fetch_lead_detail(event_id: str) -> dict:
    """Fetch full lead detail from DB (same as GET /leads/{event_id})."""
    con = get_db()
    cur = con.cursor(cursor_factory=RealDictCursor)

    cur.execute('SELECT * FROM eventective_leads WHERE "EventId"=%s', (event_id,))
    row = cur.fetchone()
    if not row:
        con.close()
        raise ValueError(f"Lead {event_id} not found")

    cur.execute(
        'SELECT * FROM eventective_lead_activities WHERE "EventId"=%s ORDER BY "DateTime"',
        (event_id,),
    )
    acts = [dict(a) for a in cur.fetchall()]
    con.close()

    row_dict = dict(row)
    return build_lead_detail(row_dict, row_dict, acts)


def _parse_llm_response(content: str) -> dict:
    """Parse model response — handle JSON, markdown-wrapped JSON, or plain text."""
    content = content.strip()

    # Strip markdown code fences if present
    if content.startswith("```"):
        lines = content.split("\n")
        # Remove first and last lines (fences)
        lines = [l for l in lines if not l.strip().startswith("```")]
        content = "\n".join(lines).strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Fallback: treat as plain text reply
        return {
            "proposed_reply": content,
            "next_step": "reply_now",
            "next_step_reason": "model returned plain text, defaulting to reply_now",
            "tone_notes": "",
        }


async def generate_reply_for_lead(event_id: str, sequence: str, step: int) -> dict:
    """
    Generate a single reply for a lead at a given sequence/step.
    Returns dict with: proposed_reply, next_step, next_step_reason, tone_notes, model
    """
    base_url = _cfg("litellm_base_url", "https://litellm.build365.app")
    api_key = _cfg("litellm_api_key")
    model = _cfg("litellm_model", "openrouter/google/gemini-2.5-flash")

    lead_detail = _fetch_lead_detail(event_id)
    system_prompt = _build_system_prompt(sequence, step)
    user_content = _build_user_context(lead_detail, sequence, step)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 400,
            },
        )
        resp.raise_for_status()

    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    result = _parse_llm_response(content)
    result["model"] = model

    log.info(
        f"Generated reply for {event_id} ({sequence}/step_{step}): "
        f"next_step={result.get('next_step')}"
    )
    return result


async def generate_all_seq1(event_id: str) -> list[dict]:
    """
    Pre-generate all 4 Sequence 1 messages for a new lead.
    Each step sees the previous generated replies as context for variety.
    """
    results = []
    for step in range(4):
        result = await generate_reply_for_lead(event_id, "new_lead", step)
        results.append(result)
    return results

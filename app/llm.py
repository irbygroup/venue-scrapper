"""
LLM reply generation via LiteLLM proxy.
Loads prompts from flat files in prompts/ directory.
Supports model fallback chain: primary → fallback_1 → fallback_2.
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

    # Put last messages front and center so the model sees them immediately
    signals = lead_detail["thread_signals"]
    last_exchange = {}
    if signals.get("last_our_message"):
        last_exchange["OUR_LAST_MESSAGE"] = signals["last_our_message"]
    if signals.get("last_their_message"):
        last_exchange["THEIR_LAST_MESSAGE"] = signals["last_their_message"]

    context = {
        "today": today,
        "IMPORTANT_last_exchange": last_exchange,
        "lead": {
            "event_id": lead_detail["event_id"],
            "contact": lead_detail["contact"],
            "event": lead_detail["event"],
            "meta": lead_detail["meta"],
            "urgency": lead_detail["urgency"],
            "urgency_reasons": lead_detail["urgency_reasons"],
        },
        "thread_summary": {
            "our_message_count": signals["our_message_count"],
            "their_message_count": signals["their_message_count"],
            "we_replied": signals["we_replied"],
            "they_replied_to_us": signals["they_replied_to_us"],
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
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # Models sometimes return JSON with raw newlines in string values — collapse and retry
        if content.startswith("{"):
            collapsed = " ".join(content.split())
            try:
                parsed = json.loads(collapsed)
            except json.JSONDecodeError:
                parsed = None
        else:
            parsed = None

    if parsed is not None and isinstance(parsed, dict):
        # Guard against double-nested JSON — if proposed_reply is itself JSON, unwrap
        reply = parsed.get("proposed_reply", "")
        if isinstance(reply, str) and reply.startswith("{"):
            try:
                inner = json.loads(reply)
                if "proposed_reply" in inner:
                    parsed = inner
            except json.JSONDecodeError:
                pass

        # Ensure proposed_reply exists and is a string
        if "proposed_reply" not in parsed or not isinstance(parsed.get("proposed_reply"), str):
            log.warning(f"LLM JSON missing proposed_reply field: {content[:200]}")
            parsed["proposed_reply"] = ""
            parsed["next_step"] = "skip"
            parsed["next_step_reason"] = "proposed_reply missing or not a string"

        return parsed

    # Fallback: plain text — but flag for review instead of auto-sending
    log.warning(f"LLM returned non-JSON response, flagging for review: {content[:200]}")
    return {
        "proposed_reply": content,
        "next_step": "skip",
        "next_step_reason": "model returned plain text (non-JSON) — skipping to be safe",
        "tone_notes": "",
    }


def _get_model_chain() -> list[str]:
    """
    Return ordered list of models to try.
    Config keys: litellm_model (primary), litellm_fallback_1, litellm_fallback_2.
    """
    primary = _cfg("litellm_model", "openrouter/google/gemini-2.5-flash")
    chain = [primary]
    for i in range(1, 10):
        fb = _cfg(f"litellm_fallback_{i}")
        if fb:
            chain.append(fb)
        else:
            break
    return chain


# JSON Schema for structured output — forces models to return valid JSON
REPLY_SCHEMA = {
    "name": "drip_reply",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "proposed_reply": {
                "type": "string",
                "description": "The message to send to the lead. Empty string if next_step is not reply_now.",
            },
            "next_step": {
                "type": "string",
                "enum": ["reply_now", "nurture", "dont_contact", "skip"],
            },
            "next_step_reason": {
                "type": "string",
                "description": "Brief explanation of why this next_step was chosen.",
            },
            "tone_notes": {
                "type": "string",
                "description": "Notes about the tone and approach used.",
            },
        },
        "required": ["proposed_reply", "next_step", "next_step_reason", "tone_notes"],
        "additionalProperties": False,
    },
}


async def _call_llm(client: httpx.AsyncClient, base_url: str, api_key: str,
                     model: str, messages: list[dict]) -> dict:
    """Make a single LLM API call. Returns raw response dict. Raises on failure."""
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 400,
        "response_format": {"type": "json_schema", "json_schema": REPLY_SCHEMA},
        # Suppress reasoning/thinking tokens from appearing in the response
        "reasoning": {"exclude": True},
    }

    resp = await client.post(
        f"{base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
    )
    resp.raise_for_status()
    return resp.json()


async def generate_reply_for_lead(event_id: str, sequence: str, step: int) -> dict:
    """
    Generate a single reply for a lead at a given sequence/step.
    Tries each model in the fallback chain until one succeeds.
    Returns dict with: proposed_reply, next_step, next_step_reason, tone_notes, model
    """
    base_url = _cfg("litellm_base_url", "https://litellm.build365.app")
    api_key = _cfg("litellm_api_key")
    model_chain = _get_model_chain()

    lead_detail = _fetch_lead_detail(event_id)
    system_prompt = _build_system_prompt(sequence, step)
    user_content = _build_user_context(lead_detail, sequence, step)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    last_error = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i, model in enumerate(model_chain):
            try:
                label = "primary" if i == 0 else f"fallback_{i}"
                log.info(f"Trying {label} model: {model} for {event_id}")

                data = await _call_llm(client, base_url, api_key, model, messages)
                content = data["choices"][0]["message"]["content"]
                result = _parse_llm_response(content)
                result["model"] = model

                if i > 0:
                    log.warning(f"Used {label} model {model} for {event_id} (primary failed)")

                log.info(
                    f"Generated reply for {event_id} ({sequence}/step_{step}): "
                    f"next_step={result.get('next_step')} model={model}"
                )
                return result

            except Exception as e:
                last_error = e
                label = "primary" if i == 0 else f"fallback_{i}"
                remaining = len(model_chain) - i - 1
                log.warning(
                    f"{label} model {model} failed for {event_id}: {e}"
                    f"{f' — trying {remaining} more fallback(s)' if remaining else ' — no more fallbacks'}"
                )

    # All models exhausted
    raise RuntimeError(
        f"All {len(model_chain)} models failed for {event_id}. "
        f"Chain: {model_chain}. Last error: {last_error}"
    )


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

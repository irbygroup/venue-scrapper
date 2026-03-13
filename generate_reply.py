"""
Generate reply suggestions for Eventective leads using LiteLLM proxy.

Usage:
    python generate_reply.py <event_id>
    python generate_reply.py EGX343UT
"""

import sys
import json
import httpx

# ── Config ────────────────────────────────────────────────────────────────────

LITELLM_BASE = "https://litellm.build365.app"
LITELLM_KEY = "sk-W8WhFDtFrC8aqjZw7_Cxdg"
MODEL = "openrouter/google/gemini-2.5-flash"

API_BASE = "http://localhost:5050/eventective"
NUM_ITERATIONS = 5

SYSTEM_PROMPT = """\
You are a reply-writing assistant for Yellowhammer Hospitality, which manages three \
wedding/event venues in Mobile, Alabama:
- The Hallet-Irby House
- Oak & Fountain
- The Courtyard on Dauphin

SALES RULES (you MUST follow these):
1. NEVER quote specific pricing — always push for a phone call or tour instead.
2. Always mention all 3 venues regardless of which one they inquired about.
3. Goal: get them on the phone with Veronica Miller at 251-422-9114.
4. Use assumptive close ("When are you free for a quick call?") not permission-seeking \
("Would you like to call?").
5. Keep the tone warm, professional, and concise — no more than 3-4 sentences.
6. Address their specific needs/concerns from the conversation thread.
7. Do NOT repeat information they've already been told.
8. Sign off as "Veronica Miller, Yellowhammer Hospitality" (never Sharon).

IMPORTANT: Read the full conversation thread carefully. Your reply must:
- Acknowledge what they said in their most recent message
- Advance the conversation toward a phone call
- Not rehash old ground or repeat previous messages
"""

ITERATION_PROMPT = """\
Here are the previous reply suggestions. Write a NEW reply that takes a DIFFERENT angle \
or approach. Do not repeat the same structure or talking points. Try a fresh strategy \
to re-engage this lead.

Previous suggestions:
{previous}

Write one new reply suggestion (just the message text, no commentary):\
"""


def fetch_lead(event_id: str) -> dict:
    """Fetch full lead details from the local API."""
    r = httpx.get(f"{API_BASE}/leads/{event_id}", timeout=10)
    r.raise_for_status()
    return r.json()


def format_lead_context(lead: dict) -> str:
    """Format lead data into a context string for the LLM."""
    c = lead["contact"]
    e = lead["event"]
    m = lead["meta"]
    ts = lead["thread_signals"]

    lines = [
        f"LEAD: {c['name']} — {e['type']} at {m['venue']}",
        f"Event date: {e['date']} ({e['days_until']} days away) | Guests: {e['guests']} | Budget: {e['budget']}",
        f"Duration: {e['duration']}h | Food: {e['food']} | Date flexible: {e['date_flexible']}",
        f"Location: {c['location']} | Contact pref: {c['contact_pref']}",
        f"Notes: {e['notes']}",
        f"",
        f"THREAD STATUS: We've sent {ts['our_message_count']} messages, they've sent {ts['their_message_count']}.",
        f"Urgency: {lead['urgency']} — {', '.join(lead['urgency_reasons'])}",
        f"",
        f"CONVERSATION THREAD:",
    ]

    for msg in lead["thread"]:
        if msg["type"] == "read_receipt":
            continue
        prefix = ">>> THEM" if msg["from"] != "Yellowhammer" else "<<< US"
        lines.append(f"  {prefix} ({msg['at'][:10]}): {msg['text']}")

    return "\n".join(lines)


def build_messages(lead_context: str, previous_replies: list[str]) -> list[dict]:
    """Build the messages array for the LLM call."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": lead_context},
    ]

    if previous_replies:
        numbered = "\n".join(f"{i+1}. {r}" for i, r in enumerate(previous_replies))
        messages.append({"role": "user", "content": ITERATION_PROMPT.format(previous=numbered)})
    else:
        messages.append({"role": "user", "content": "Write one reply suggestion for this lead (just the message text, no commentary):"})

    return messages


def generate_reply(lead_context: str, previous_replies: list[str]) -> tuple[str, list[dict]]:
    """Call LiteLLM to generate one reply suggestion. Returns (reply, messages_sent)."""
    messages = build_messages(lead_context, previous_replies)
    payload = {"model": MODEL, "messages": messages, "temperature": 0.9, "max_tokens": 300}

    r = httpx.post(
        f"{LITELLM_BASE}/v1/chat/completions",
        headers={"Authorization": f"Bearer {LITELLM_KEY}"},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    reply = r.json()["choices"][0]["message"]["content"].strip()
    return reply, messages


def run_lead(event_id: str, dump: bool = False) -> str:
    """Generate replies for a lead. Returns markdown dump if dump=True."""
    lead = fetch_lead(event_id)
    contact = lead["contact"]["name"]
    venue = lead["meta"]["venue"]
    print(f"Lead: {contact} — {lead['event']['type']} at {venue}")
    print(f"Urgency: {lead['urgency']} | Thread: {lead['thread_signals']['our_message_count']} us / {lead['thread_signals']['their_message_count']} them")
    print(f"Generating {NUM_ITERATIONS} reply suggestions using {MODEL}...\n")

    lead_context = format_lead_context(lead)
    replies = []
    md_parts = []

    if dump:
        md_parts.append(f"## Lead: {contact} ({event_id}) — {lead['event']['type']} at {venue}\n")

    for i in range(NUM_ITERATIONS):
        print(f"--- Suggestion {i+1} ---")
        reply, messages = generate_reply(lead_context, replies)
        replies.append(reply)
        print(reply)
        print()

        if dump:
            md_parts.append(f"### Iteration {i+1}\n")
            md_parts.append(f"**Request payload:**\n```json\n{json.dumps({'model': MODEL, 'messages': messages, 'temperature': 0.9, 'max_tokens': 300}, indent=2)}\n```\n")
            md_parts.append(f"**Response:**\n> {reply}\n")

    print("=" * 60)
    print(f"Generated {len(replies)} suggestions for {contact} ({event_id})\n")
    return "\n".join(md_parts)


def main():
    if len(sys.argv) < 2:
        print("Usage: python generate_reply.py <event_id> [event_id2 ...] [--dump prompts.md]")
        sys.exit(1)

    dump = "--dump" in sys.argv
    dump_file = None
    args = [a for a in sys.argv[1:] if a != "--dump"]
    if dump and args and not args[-1].startswith("-"):
        # Check if last arg looks like a filename
        if args[-1].endswith(".md"):
            dump_file = args.pop()
        else:
            dump_file = "prompts_dump.md"

    if not dump_file and dump:
        dump_file = "prompts_dump.md"

    event_ids = args
    all_md = [f"# Generate Reply — Prompt Dump\n\nModel: `{MODEL}`\nLiteLLM proxy: `{LITELLM_BASE}`\n"]

    for eid in event_ids:
        print(f"\nFetching lead {eid}...")
        md = run_lead(eid, dump=dump)
        if dump:
            all_md.append(md)

    if dump and dump_file:
        with open(dump_file, "w") as f:
            f.write("\n".join(all_md))
        print(f"Prompt dump written to {dump_file}")


if __name__ == "__main__":
    main()

import base64
import hashlib
import hmac
import json
import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import _cfg
from app.db import get_db
from app.fub import _fub_headers, _fub_request

router = APIRouter()
log = logging.getLogger("fub-webhook")


@router.post("/fub-webhook")
async def fub_webhook(request: Request, token: str = ""):
    # 1. Token check
    expected_token = _cfg("fub_webhook_token")
    if not expected_token or token != expected_token:
        return JSONResponse({"status": "unauthorized"}, status_code=401)

    # 2. HMAC signature check
    raw_body = await request.body()
    fub_signature = request.headers.get("FUB-Signature", "")
    system_key = _cfg("fub_system_key")

    body_b64 = base64.b64encode(raw_body).decode()
    computed = hmac.new(
        system_key.encode(), body_b64.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed, fub_signature):
        log.warning(f"HMAC mismatch: computed={computed[:16]}... header={fub_signature[:16]}...")
        return JSONResponse({"status": "invalid_signature"}, status_code=403)

    # 3. Parse payload
    payload = json.loads(raw_body)
    event = payload.get("event", "")
    if event != "peopleStageUpdated":
        return {"status": "ignored", "event": event}

    resource_ids = payload.get("resourceIds", [])
    if not resource_ids:
        return {"status": "ignored", "reason": "no resourceIds"}

    person_id = str(resource_ids[0])
    event_id = payload.get("eventId", "")
    new_stage = payload.get("data", {}).get("stage", "")

    # 4. DB lookup — does this person exist in our leads?
    con = get_db()
    cur = con.cursor()
    cur.execute(
        'SELECT "EventId" FROM eventective_leads WHERE fub_people_id = %s LIMIT 1',
        (person_id,),
    )
    row = cur.fetchone()
    if not row:
        con.close()
        return {"status": "ignored", "reason": "person not in eventective_leads"}

    # 5. Source verification — confirm this person came from Eventective
    uri = payload.get("uri", "")
    skip_update = False
    if uri:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await _fub_request(client, "GET", f"https://api.followupboss.com{uri}")
                person_data = resp.json()
                source = (person_data.get("source") or "").lower()
                if "eventective" not in source:
                    skip_update = True
        except Exception:
            # FUB API call failed — proceed anyway since we matched by fub_people_id
            pass

    if skip_update:
        con.close()
        return {"status": "ignored", "reason": "source is not eventective"}

    # 6. Update fub_lead_stage
    cur.execute(
        "UPDATE eventective_leads SET fub_lead_stage = %s WHERE fub_people_id = %s",
        (new_stage, person_id),
    )
    con.commit()
    con.close()

    log.info(f"Updated fub_lead_stage={new_stage} for fub_people_id={person_id}")
    return {"status": "updated", "event_id": event_id, "stage": new_stage}


@router.get("/fub-webhook/ensure")
async def fub_webhook_ensure():
    webhook_token = _cfg("fub_webhook_token")
    if not webhook_token:
        return {"status": "error", "reason": "fub_webhook_token not configured"}

    target_url = f"https://hooks.build365.app/fub?token={webhook_token}"
    base = _cfg("fub_api_base_url", "https://api.followupboss.com/v1")

    async with httpx.AsyncClient(timeout=15.0) as client:
        # List existing webhooks
        resp = await _fub_request(client, "GET", f"{base}/webhooks")
        webhooks = resp.json().get("webhooks", [])

        for wh in webhooks:
            if wh.get("url") == target_url:
                wh_id = wh.get("id")
                if wh.get("status") != "Active":
                    # Reactivate paused/disabled webhook
                    await _fub_request(
                        client, "PUT", f"{base}/webhooks/{wh_id}",
                        json={"status": "Active"},
                    )
                    return {"status": "reactivated", "webhook_id": wh_id}
                return {"status": "exists", "webhook_id": wh_id}

        # Create new webhook
        create_body = {
            "event": "peopleStageUpdated",
            "url": target_url,
        }
        resp = await _fub_request(client, "POST", f"{base}/webhooks", json=create_body)
        result = resp.json()
        return {"status": "created", "webhook_id": result.get("id")}

from fastapi import APIRouter, HTTPException

from app import state as state_mod
from app.lead_market import run_check_lead_market

router = APIRouter()


@router.post("/check-lead-market")
async def check_lead_market():
    bm = state_mod.get_bm()
    if bm.reply_lock.locked():
        raise HTTPException(status_code=409, detail="Reply page in use (reply or drip send in progress)")
    async with bm.reply_lock:
        return await run_check_lead_market()

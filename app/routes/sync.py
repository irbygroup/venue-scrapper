from typing import Optional

from fastapi import APIRouter, HTTPException

from app import state as state_mod
from app.sync import run_sync

router = APIRouter()


@router.post("/sync")
async def sync(limit: Optional[int] = None):
    bm = state_mod.get_bm()
    if bm.sync_lock.locked():
        raise HTTPException(status_code=409, detail="Sync already in progress")
    async with bm.sync_lock:
        return await run_sync(limit=limit)

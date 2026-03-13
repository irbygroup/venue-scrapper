from fastapi import APIRouter, HTTPException, BackgroundTasks

from app.fub import fub_sync_state, _fub_sync_task, _fub_incremental_export

router = APIRouter()


@router.post("/fub-sync")
async def fub_sync(background_tasks: BackgroundTasks, mode: str = "backfill", limit: int = 0, order: str = "asc"):
    state = fub_sync_state[order]
    if state["running"]:
        raise HTTPException(409, f"FUB sync ({order}) already running")
    background_tasks.add_task(_fub_sync_task, mode, limit, order)
    return {"status": "started", "mode": mode, "limit": limit or "unlimited", "order": order}


@router.post("/fub-export-new")
async def fub_export_new(background_tasks: BackgroundTasks):
    state = fub_sync_state["incremental"]
    if state["running"]:
        raise HTTPException(409, "FUB incremental export already running")
    background_tasks.add_task(_fub_incremental_export)
    return {"status": "started", "mode": "incremental"}


@router.get("/fub-sync/status")
async def fub_sync_status():
    return {
        "asc": {
            "running": fub_sync_state["asc"]["running"],
            "progress": fub_sync_state["asc"]["progress"],
            "errors": fub_sync_state["asc"]["errors"][-20:],
        },
        "desc": {
            "running": fub_sync_state["desc"]["running"],
            "progress": fub_sync_state["desc"]["progress"],
            "errors": fub_sync_state["desc"]["errors"][-20:],
        },
        "incremental": {
            "running": fub_sync_state["incremental"]["running"],
            "progress": fub_sync_state["incremental"]["progress"],
            "errors": fub_sync_state["incremental"]["errors"][-20:],
        },
    }

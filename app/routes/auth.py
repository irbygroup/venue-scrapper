from fastapi import APIRouter, HTTPException

from app import state as state_mod
from app.config import get_config, email, password, inbox_url
from app.email import notify_error
from app.models import LoginRequest

router = APIRouter()


@router.post("/auth/login")
async def auth_login(req: LoginRequest = LoginRequest()):
    bm = state_mod.get_bm()
    _email    = req.email    or email()
    _password = req.password or password()
    ok = await bm.do_login(_email, _password)
    if not ok:
        notify_error("Login failed", f"Manual login attempt failed for {_email}")
        raise HTTPException(status_code=401, detail="Login failed")
    # Navigate sync_page back to inbox
    await bm.sync_page.goto(inbox_url(), wait_until="domcontentloaded")
    await bm.sync_page.wait_for_load_state("networkidle", timeout=15000)
    return {"success": True, "message": "Logged in, cookies saved"}


@router.get("/auth/status")
async def auth_status():
    bm = state_mod.get_bm()
    valid = await bm.check_session()
    has_cookies = bool(get_config("eventective_cookies"))
    return {
        "authenticated": valid,
        "has_cookies":   has_cookies,
    }

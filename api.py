"""
Eventective Lead Management API
FastAPI + async Playwright (persistent browser context) + PostgreSQL
v3.0 — modular layout (see app/ package)
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, APIRouter, Request

from app import state as state_mod
from app.browser import BrowserManager
from app.db import init_db
from app.email import notify_error
from app.routes import auth, sync, leads, status, email, fub, fub_webhook, drip, lead_market


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    state_mod.bm = BrowserManager(on_error=notify_error)
    await state_mod.bm.start()
    print("Browser ready.")
    yield
    await state_mod.bm.close()


app = FastAPI(title="Venue Scrapper API", lifespan=lifespan)
router = APIRouter(prefix="/eventective")

router.include_router(auth.router)
router.include_router(sync.router)
router.include_router(leads.router)
router.include_router(status.router)
router.include_router(email.router)
router.include_router(fub.router)
router.include_router(fub_webhook.router)
router.include_router(drip.router)
router.include_router(lead_market.router)

app.include_router(router)


@app.post("/fub")
async def fub_webhook_root_alias(request: Request, token: str = ""):
    return await fub_webhook.fub_webhook(request, token)

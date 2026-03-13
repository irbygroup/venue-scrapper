"""
Eventective Lead Management API
FastAPI + async Playwright (persistent browser context) + PostgreSQL
v3.0 — modular layout (see app/ package)
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, APIRouter

from app import state as state_mod
from app.browser import BrowserManager
from app.db import init_db
from app.email import notify_error
from app.routes import auth, sync, leads, status, email, fub


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

app.include_router(router)

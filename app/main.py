"""Main FastAPI application for Chitragupta."""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .storage import ensure_dirs
from .db import init_db
from .routers import (
    auth_router,
    cases_router,
    evidence_router,
    ledger_router,
    admin_router,
    agent_router,
)
from .routers import vault_router
from .vault.consumer import start_poller, stop_poller
from .config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown."""
    # Startup
    ensure_dirs()
    init_db()
    if get_settings().vault_autosync:
        start_poller()
    yield
    # Shutdown
    stop_poller()


app = FastAPI(title="Chitragupta", lifespan=lifespan)

# CORS — allow all origins for dev; tighten in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth_router.router)
app.include_router(cases_router.router)
app.include_router(evidence_router.router)        # /cases/{case_id}/evidence
app.include_router(evidence_router.evidence_router)  # /evidence/{file_id}/...
app.include_router(ledger_router.router)
app.include_router(admin_router.router)
app.include_router(vault_router.router)
app.include_router(agent_router.router)           # Phase 2: agent endpoints


@app.get("/health")
def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}
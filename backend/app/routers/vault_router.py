"""Vault router: admin endpoints for vault sync and status."""

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user, require_roles
from app.db import get_db
from app.vault.consumer import run_vault_sync, vault_status

router = APIRouter(prefix="/vault", tags=["vault"])


@router.post("/sync")
def sync_vault(
    user: dict = Depends(require_roles("admin")),
    conn=Depends(get_db),
) -> dict:
    """Process pending events and sync vault notes."""
    processed = run_vault_sync()
    return {"processed": processed}


@router.get("/status")
def get_vault_status(
    user: dict = Depends(require_roles("admin")),
    conn=Depends(get_db),
) -> dict:
    """Get vault status including offset, pending events, and per-case counts."""
    from app.vault.writer import get_writer
    
    writer = get_writer()
    return vault_status(conn, writer)
"""Ledger router."""

from fastapi import APIRouter, Depends, Query
import sqlite3

from ..auth import get_current_user
from ..db import get_db
from ..ledger import list_entries, verify_chain
from ..models import ChainStatus, LedgerEntryOut

router = APIRouter(prefix="/ledger", tags=["ledger"])


@router.get("", response_model=list[LedgerEntryOut])
def get_ledger(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[LedgerEntryOut]:
    """List ledger entries with pagination."""
    rows = list_entries(conn, limit=limit, offset=offset)
    return [
        LedgerEntryOut(
            entry_no=row["entry_no"],
            file_id=row["file_id"],
            case_id=row["case_id"],
            file_sha256=row["file_sha256"],
            uploader_id=row["uploader_id"],
            created_at=row["created_at"],
            prev_entry_hash=row["prev_entry_hash"],
            entry_hash=row["entry_hash"],
        )
        for row in rows
    ]


@router.get("/verify", response_model=ChainStatus)
def verify_ledger(
    user: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> ChainStatus:
    """Verify the integrity of the ledger chain."""
    return verify_chain(conn)
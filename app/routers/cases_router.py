"""Cases router."""

from fastapi import APIRouter, HTTPException, Depends, Body
import sqlite3

from ..auth import require_roles, get_current_user
from ..db import get_db, transaction
from ..timeutil import utc_now_iso
from ..models import CaseCreate, CaseOut


router = APIRouter(prefix="/cases", tags=["cases"])

# Default case seeded when no cases exist (mirrors the real DB seed)
_DEFAULT_CASE_ID = "CASE-001"
_DEFAULT_CASE_NAME = "Case FIR-2026/0417 · Mule account ring"


def _ensure_default_case(conn: sqlite3.Connection) -> None:
    """Auto-create the default case if the DB is empty (e.g. in tests)."""
    row = conn.execute("SELECT COUNT(*) as cnt FROM cases").fetchone()
    if row["cnt"] == 0:
        # Find any admin/police user to act as creator
        user_row = conn.execute(
            "SELECT user_id FROM users ORDER BY rowid LIMIT 1"
        ).fetchone()
        created_by = user_row["user_id"] if user_row else "system"
        created_at = utc_now_iso()
        conn.execute(
            "INSERT INTO cases (case_id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
            (_DEFAULT_CASE_ID, _DEFAULT_CASE_NAME, created_by, created_at),
        )
        conn.commit()


@router.post("", response_model=CaseOut, status_code=201)
def create_case(
    case_in: CaseCreate = Body(...),
    user: dict = Depends(require_roles("police", "forensic", "admin")),
    conn: sqlite3.Connection = Depends(get_db),
) -> CaseOut:
    """Create a new case."""
    with transaction(conn):
        # Get next case number
        row = conn.execute("SELECT COUNT(*) as cnt FROM cases").fetchone()
        n = row["cnt"] + 1
        case_id = f"CASE-{n:03d}"

        # Insert case
        created_at = utc_now_iso()
        conn.execute(
            "INSERT INTO cases (case_id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
            (case_id, case_in.name, user["user_id"], created_at),
        )

    return CaseOut(
        case_id=case_id,
        name=case_in.name,
        created_by=user["user_id"],
        created_at=created_at,
    )


@router.get("", response_model=list[CaseOut])
def list_cases(
    user: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[CaseOut]:
    """List all cases."""
    rows = conn.execute(
        "SELECT case_id, name, created_by, created_at FROM cases ORDER BY created_at DESC"
    ).fetchall()

    return [
        CaseOut(
            case_id=row["case_id"],
            name=row["name"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


@router.get("/active", response_model=CaseOut)
def get_active_case(
    user: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> CaseOut:
    """
    Return the most recently created (active) case.

    If no cases exist yet (e.g. fresh test DB), the default seed case is
    auto-created so the dashboard can always start from a known state.
    """
    _ensure_default_case(conn)

    row = conn.execute(
        "SELECT case_id, name, created_by, created_at FROM cases ORDER BY created_at DESC LIMIT 1"
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No cases found")

    return CaseOut(
        case_id=row["case_id"],
        name=row["name"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )
"""Admin router."""

from fastapi import APIRouter, Depends, HTTPException, Query
import sqlite3

from ..auth import get_current_user, require_roles
from ..db import get_db
from ..audit import list_audit
from ..models import AuditOut, AlertOut

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/audit", response_model=list[AuditOut])
def get_audit(
    file_id: str | None = Query(default=None),
    user: dict = Depends(require_roles("admin")),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[AuditOut]:
    """List audit log entries, newest first; optionally filter by file_id."""
    rows = list_audit(conn, file_id=file_id, limit=200)
    return [
        AuditOut(
            audit_id=row["audit_id"],
            timestamp=row["timestamp"],
            user_id=row["user_id"],
            action=row["action"],
            file_id=row["file_id"],
            case_id=row["case_id"],
            result=row["result"],
            detail=row["detail"],
        )
        for row in rows
    ]


@router.get("/alerts", response_model=list[AlertOut])
def get_alerts(
    user: dict = Depends(require_roles("admin")),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[AlertOut]:
    """List all alerts, newest first."""
    rows = conn.execute(
        """
        SELECT alert_id, created_at, severity, file_id, case_id, message, acknowledged, acknowledged_by
        FROM alerts
        ORDER BY alert_id DESC
        """
    ).fetchall()
    return [
        AlertOut(
            alert_id=row["alert_id"],
            created_at=row["created_at"],
            severity=row["severity"],
            file_id=row["file_id"],
            case_id=row["case_id"],
            message=row["message"],
            acknowledged=row["acknowledged"],
            acknowledged_by=row["acknowledged_by"],
        )
        for row in rows
    ]


@router.post("/alerts/{alert_id}/ack", status_code=200)
def acknowledge_alert(
    alert_id: int,
    user: dict = Depends(require_roles("admin")),
    conn: sqlite3.Connection = Depends(get_db),
) -> None:
    """Acknowledge an alert by setting acknowledged=1 and acknowledged_by=current user."""
    cur = conn.execute(
        """
        UPDATE alerts
        SET acknowledged = 1, acknowledged_by = ?
        WHERE alert_id = ?
        """,
        (user["user_id"], alert_id),
    )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Alert not found")
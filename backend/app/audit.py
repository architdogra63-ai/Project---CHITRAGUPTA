"""Audit logging utilities."""

import sqlite3
import sys

from .timeutil import utc_now_iso


def write_audit(
    conn: sqlite3.Connection,
    user_id: str | None,
    action: str,
    result: str,
    file_id: str | None = None,
    case_id: str | None = None,
    detail: str | None = None,
) -> None:
    """Insert an audit log entry; never raises on normal failure."""
    try:
        conn.execute(
            """
            INSERT INTO audit_log (timestamp, user_id, action, file_id, case_id, result, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (utc_now_iso(), user_id, action, file_id, case_id, result, detail),
        )
    except sqlite3.Error as e:
        print(f"Warning: audit write failed: {e}", file=sys.stderr)


def list_audit(
    conn: sqlite3.Connection, file_id: str | None = None, limit: int = 200
) -> list[dict]:
    """Return audit rows, newest first; optionally filter by file_id."""
    if file_id is not None:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE file_id = ? ORDER BY audit_id DESC LIMIT ?",
            (file_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY audit_id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]
"""Database connection and initialization."""

import contextlib
import sqlite3
from pathlib import Path

from .config import get_settings


def get_connection() -> sqlite3.Connection:
    """Open a connection to the SQLite database with configured pragmas."""
    conn = sqlite3.connect(
        get_settings().db_path,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    """Initialize the database schema from schema.sql."""
    schema_path = Path(__file__).with_name("schema.sql")
    sql = schema_path.read_text()
    with get_connection() as conn:
        conn.executescript(sql)


def get_db():
    """FastAPI dependency that yields a database connection."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection):
    """Context manager for a database transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
#!/usr/bin/env python3
"""Seed demo users into the database idempotently.

Creates four demo users with bcrypt-hashed passwords:
- admin_1 (admin)
- officer_17 (police)
- forensic_2 (forensic)
- judge_1 (judge)

All use the password "ChangeMe123!"

WARNING: These are demo credentials. Change passwords immediately in production.
"""

import sys
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.auth import hash_password
from app.config import get_settings
from app.db import get_connection, init_db


USERS = [
    ("admin_1", "admin"),
    ("officer_17", "police"),
    ("forensic_2", "forensic"),
    ("judge_1", "judge"),
]

DEMO_PASSWORD = "ChangeMe123!"


def seed_users(conn: sqlite3.Connection) -> int:
    """Insert demo users idempotently. Returns number of newly inserted users."""
    password_hash = hash_password(DEMO_PASSWORD)
    created_at = datetime.utcnow().isoformat()

    inserted = 0
    for username, role in USERS:
        user_id = uuid.uuid4().hex
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO users (user_id, username, password_hash, role, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, username, password_hash, role, created_at),
        )
        if cursor.rowcount > 0:
            inserted += 1
            print(f"Created user: {username} ({role}) with user_id={user_id}")
        else:
            print(f"User already exists: {username} ({role})")

    return inserted


def main() -> None:
    print("=" * 60)
    print("WARNING: Seeding DEMO credentials - NOT for production use!")
    print("All users have password: ChangeMe123!")
    print("Change passwords immediately after first login in production.")
    print("=" * 60)

    settings = get_settings()
    init_db()  # Initialize database schema
    conn = get_connection()
    try:
        inserted = seed_users(conn)
        print(f"\nDone. {inserted} new user(s) inserted.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
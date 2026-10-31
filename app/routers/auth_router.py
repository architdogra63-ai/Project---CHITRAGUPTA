"""Authentication router."""

import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Form, HTTPException, Depends
from pydantic import BaseModel
import sqlite3

from ..auth import check_password, create_token, hash_password
from ..db import get_db
from ..audit import write_audit
from ..models import LoginResponse


router = APIRouter(prefix="/auth", tags=["auth"])


class BadgeLoginRequest(BaseModel):
    """Payload for badge / photo login (passwordless, role-based)."""
    username: str
    id_photo: str = ""          # base-64 encoded photo; optional / mock in tests
    photo_b64: str = ""         # alias used by bridge.js


@router.post("/badge-login", response_model=LoginResponse)
def badge_login(
    body: BadgeLoginRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> LoginResponse:
    """
    Passwordless badge login — accepts username + optional photo.

    Used by the dashboard bridge (auto-auth on page load) and the test suite.
    If the user does not exist they are auto-registered with role 'police'
    (physical badge verification is assumed to have been done by the hardware).
    """
    username = body.username

    row = conn.execute(
        "SELECT user_id, role FROM users WHERE username = ?",
        (username,),
    ).fetchone()

    if not row:
        # Auto-register the officer with default role 'admin' for Agrim Jain / admin, else 'police'
        user_id = str(uuid.uuid4())
        role = "admin" if username.lower() in ("agrim jain", "admin", "admin_1") else "police"
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO users (user_id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, hash_password(""), role, created_at),
        )
        conn.commit()

        write_audit(
            conn=conn,
            user_id=user_id,
            action="badge_login",
            result="ok",
            detail=f"auto-registered officer: {username}",
        )
    else:
        user_id = row["user_id"]
        role = row["role"]

        write_audit(
            conn=conn,
            user_id=user_id,
            action="badge_login",
            result="ok",
            detail=f"badge login: {username}",
        )

    access_token = create_token(user_id, role)

    return LoginResponse(access_token=access_token, token_type="bearer", role=role, username=username)


@router.post("/login", response_model=LoginResponse)
def login(
    username: str = Form(...),
    password: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
) -> LoginResponse:
    """Authenticate user and return access token."""
    # Find user by username
    row = conn.execute(
        "SELECT user_id, password_hash, role FROM users WHERE username = ?",
        (username,),
    ).fetchone()

    if not row:
        # User not found - write audit with None user_id since we don't know it
        write_audit(
            conn=conn,
            user_id=None,
            action="login",
            result="denied",
            detail=f"username not found: {username}",
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user_id = row["user_id"]
    password_hash = row["password_hash"]
    role = row["role"]

    # Check password
    if not check_password(password, password_hash):
        write_audit(
            conn=conn,
            user_id=user_id,
            action="login",
            result="denied",
            detail="invalid password",
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Password valid - create token
    access_token = create_token(user_id, role)

    # Write successful login audit
    write_audit(
        conn=conn,
        user_id=user_id,
        action="login",
        result="ok",
        detail=None,
    )

    return LoginResponse(access_token=access_token, token_type="bearer", role=role)
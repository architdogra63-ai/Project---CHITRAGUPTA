"""Authentication router."""

from fastapi import APIRouter, Form, HTTPException, Depends
import sqlite3

from ..auth import check_password, create_token
from ..db import get_db
from ..audit import write_audit
from ..models import LoginResponse


router = APIRouter(prefix="/auth", tags=["auth"])


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
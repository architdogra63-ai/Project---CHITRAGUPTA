"""Authentication utilities."""

import sqlite3
from datetime import datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer

from .config import get_settings
from .db import get_db
from .audit import write_audit


def hash_password(plain: str) -> str:
    """Return bcrypt hash of plain password."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def check_password(plain: str, hashed: str) -> bool:
    """Check plain password against bcrypt hash."""
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_token(user_id: str, role: str) -> str:
    """Create HS256 JWT with sub, role, exp."""
    settings = get_settings()
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": user_id, "role": role, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme), conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Validate JWT and return current user; raises 401 on failure."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = payload.get("sub")
    role = payload.get("role")
    if not user_id or not role:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    row = conn.execute(
        "SELECT user_id, role FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    return {"user_id": row["user_id"], "role": row["role"]}


def require_roles(*roles: str):
    """Return dependency that enforces role membership; writes audit on deny."""

    def checker(
        user: dict = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_db)
    ) -> dict:
        if user["role"] not in roles:
            write_audit(
                conn=conn,
                user_id=user["user_id"],
                action="access_check",
                result="denied",
                detail=f"required roles: {', '.join(roles)}",
            )
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return checker
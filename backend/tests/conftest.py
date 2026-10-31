"""Pytest configuration and fixtures for Chitragupta backend tests."""

import base64
import os
import tempfile
from pathlib import Path
from typing import Callable, Dict

import pytest
from fastapi.testclient import TestClient
import sqlite3

# Import from app
from app.config import get_settings
from app.db import init_db, get_connection
from app.auth import hash_password
from app import main
from scripts.seed_users import USERS, DEMO_PASSWORD, seed_users


@pytest.fixture(scope="function")
def test_env(tmp_path: Path) -> Dict[str, str]:
    """Create a temporary test environment with isolated DB and storage.

    Sets up environment variables for a fresh database and storage directory
    with random MASTER_KEY and JWT_SECRET.
    """
    # Create temp directories
    db_path = tmp_path / "test.db"
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    vault_template_graph = tmp_path / "graph.json"
    vault_template_graph.write_text('{"groups": [], "filters": {"orphan": true}}')

    # Generate random keys
    master_key = base64.b64encode(os.urandom(32)).decode()
    jwt_secret = base64.b64encode(os.urandom(48)).decode()

    # Set environment variables
    env_vars = {
        "DB_PATH": str(db_path),
        "STORAGE_DIR": str(storage_dir),
        "MASTER_KEY": master_key,
        "JWT_SECRET": jwt_secret,
        "MAX_UPLOAD_MB": "1",
        "JWT_EXPIRE_MINUTES": "60",
        "VAULT_DIR": str(vault_dir),
        "VAULT_TEMPLATE_GRAPH": str(vault_template_graph),
        "VAULT_AUTOSYNC": "0",
        "ALLOW_DEMO_IMPORT": "1",
    }

    # Apply to os.environ
    old_env = {}
    for key, value in env_vars.items():
        old_env[key] = os.environ.get(key)
        os.environ[key] = value

    yield env_vars

    # Restore old environment
    for key, old_value in old_env.items():
        if old_value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old_value


@pytest.fixture(scope="function")
def test_db(test_env: Dict[str, str]) -> sqlite3.Connection:
    """Initialize database and seed users, return connection."""
    # Re-initialize settings with new env vars
    # get_settings() reads from env each time, so no caching issues

    # Initialize database schema
    init_db()

    # Get connection and seed users
    conn = get_connection()
    try:
        seed_users(conn)
    finally:
        conn.close()

    # Return a fresh connection for tests
    return get_connection()


@pytest.fixture(scope="function")
def client(test_db: sqlite3.Connection) -> TestClient:
    """Create a TestClient with the test app."""
    # The app's startup event will have already run init_db() and ensure_dirs()
    # but we need to make sure the app uses our test settings.
    # Since get_settings() reads from env on each call, it will pick up test_env.
    return TestClient(main.app)


@pytest.fixture(scope="function")
def login(client: TestClient) -> Callable[[str], Dict[str, str]]:
    """Return a helper function to log in a user and get auth headers.

    Usage:
        headers = login("admin_1")
        response = client.get("/cases", headers=headers)
    """
    def _login(username: str) -> Dict[str, str]:
        response = client.post(
            "/auth/login",
            data={"username": username, "password": DEMO_PASSWORD},
        )
        assert response.status_code == 200, f"Login failed for {username}: {response.text}"
        token = response.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    return _login


@pytest.fixture(scope="function")
def admin_headers(login: Callable[[str], Dict[str, str]]) -> Dict[str, str]:
    """Auth headers for admin_1 user."""
    return login("admin_1")


@pytest.fixture(scope="function")
def officer_headers(login: Callable[[str], Dict[str, str]]) -> Dict[str, str]:
    """Auth headers for officer_17 user."""
    return login("officer_17")


@pytest.fixture(scope="function")
def forensic_headers(login: Callable[[str], Dict[str, str]]) -> Dict[str, str]:
    """Auth headers for forensic_2 user."""
    return login("forensic_2")


@pytest.fixture(scope="function")
def judge_headers(login: Callable[[str], Dict[str, str]]) -> Dict[str, str]:
    """Auth headers for judge_1 user."""
    return login("judge_1")
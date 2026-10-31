"""Application configuration."""

import base64
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """Application settings loaded from environment."""
    db_path: str
    storage_dir: Path
    master_key: bytes
    jwt_secret: str
    jwt_expire_minutes: int
    max_upload_bytes: int
    vault_dir: Path
    vault_template_graph: Path
    vault_autosync: bool
    vault_poll_seconds: float
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: int
    investigation_max_travel_minutes: int
    allow_demo_import: bool


def get_settings() -> Settings:
    """Load settings from environment variables (no caching)."""
    load_dotenv(override=False)

    master_key_b64 = os.getenv("MASTER_KEY")
    if not master_key_b64:
        raise RuntimeError("MASTER_KEY environment variable is required")
    try:
        master_key = base64.b64decode(master_key_b64)
    except Exception as e:
        raise ValueError("MASTER_KEY must be valid base64") from e
    if len(master_key) != 32:
        raise ValueError("MASTER_KEY must be exactly 32 bytes after base64 decoding")

    jwt_secret = os.getenv("JWT_SECRET")
    if not jwt_secret:
        raise RuntimeError("JWT_SECRET environment variable is required")

    db_path = os.getenv("DB_PATH", "./chitragupta.db")
    storage_dir = Path(os.getenv("STORAGE_DIR", "./storage"))
    jwt_expire_minutes = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))
    max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "100"))
    max_upload_bytes = max_upload_mb * 1024 * 1024

    vault_dir = Path(os.getenv("VAULT_DIR", "./vault"))
    vault_template_graph = Path(os.getenv("VAULT_TEMPLATE_GRAPH", "./vault_template/graph.json"))
    vault_autosync = os.getenv("VAULT_AUTOSYNC", "1") != "0"
    vault_poll_seconds = float(os.getenv("VAULT_POLL_SECONDS", "2"))

    llm_base_url = os.getenv("LLM_BASE_URL", "http://100.88.109.124:11434/v1")
    llm_api_key = os.getenv("LLM_API_KEY", "ollama")
    llm_model = os.getenv("LLM_MODEL", "")
    llm_timeout_seconds = int(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
    investigation_max_travel_minutes = int(os.getenv("INVESTIGATION_MAX_TRAVEL_MINUTES", "15"))
    allow_demo_import = os.getenv("ALLOW_DEMO_IMPORT", "0") == "1"

    return Settings(
        db_path=db_path,
        storage_dir=storage_dir,
        master_key=master_key,
        jwt_secret=jwt_secret,
        jwt_expire_minutes=jwt_expire_minutes,
        max_upload_bytes=max_upload_bytes,
        vault_dir=vault_dir,
        vault_template_graph=vault_template_graph,
        vault_autosync=vault_autosync,
        vault_poll_seconds=vault_poll_seconds,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        llm_timeout_seconds=llm_timeout_seconds,
        investigation_max_travel_minutes=investigation_max_travel_minutes,
        allow_demo_import=allow_demo_import,
    )
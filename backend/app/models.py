from pydantic import BaseModel, Field
from typing import Literal, Optional


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = Field(default="bearer")
    role: str


class CaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CaseOut(BaseModel):
    case_id: str
    name: str
    created_by: str
    created_at: str


class EvidenceOut(BaseModel):
    seq: int
    file_id: str
    case_id: str
    original_filename: str
    file_type: str
    content_type: Optional[str] = None
    size_bytes: int
    sha256: str
    stored_path: Optional[str] = None
    status: str
    ledger_entry_no: Optional[int] = None
    uploaded_by: str
    uploaded_at: str
    last_verified_at: Optional[str] = None
    tamper_reason: Optional[str] = None


class UploadResult(BaseModel):
    file_id: str
    case_id: str
    sha256: str
    ledger_entry_no: int
    status: str


class VerifyResult(BaseModel):
    file_id: str
    status: Literal["verified", "tampered", "ledger_compromised"]
    reason: Optional[str] = None
    expected_sha256: Optional[str] = None
    actual_sha256: Optional[str] = None
    ledger_entry_no: Optional[int] = None
    chain_ok: bool
    checked_at: str


class LedgerEntryOut(BaseModel):
    entry_no: int
    file_id: str
    case_id: str
    file_sha256: str
    uploader_id: str
    created_at: str
    prev_entry_hash: str
    entry_hash: str


class ChainStatus(BaseModel):
    ok: bool
    entries_checked: int
    broken_at: Optional[int] = None
    reason: Optional[str] = None


class AuditOut(BaseModel):
    audit_id: int
    timestamp: str
    user_id: Optional[str] = None
    action: str
    file_id: Optional[str] = None
    case_id: Optional[str] = None
    result: str
    detail: Optional[str] = None


class AlertOut(BaseModel):
    alert_id: int
    created_at: str
    severity: str
    file_id: Optional[str] = None
    case_id: Optional[str] = None
    message: str
    acknowledged: int
    acknowledged_by: Optional[str] = None
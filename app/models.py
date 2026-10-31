from pydantic import BaseModel, Field
from typing import Any, Literal, Optional


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = Field(default="bearer")
    role: str
    username: Optional[str] = None


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


# ---------------------------------------------------------------------------
# Agent / Phase-2 models
# ---------------------------------------------------------------------------

class AnalysisRunOut(BaseModel):
    run_id: str
    case_id: str
    status: str
    started_at: str
    completed_at: Optional[str] = None
    started_by: str
    error_detail: Optional[str] = None


class AnalysisStartResponse(BaseModel):
    run_id: str
    case_id: str
    status: str
    message: str


class ExtractionOut(BaseModel):
    extraction_id: int
    run_id: str
    file_id: str
    source_ref: str
    extracted_text: str
    entities_json: Optional[str] = None
    timestamps_json: Optional[str] = None
    created_at: str


class ClaimOut(BaseModel):
    claim_id: str
    claim_type: str
    text: str
    when: Optional[str] = None
    source_file_id: str
    source_ref: str
    confidence: Optional[float] = None
    entities: list[str] = Field(default_factory=list)
    status: str = "active"


class FlagOut(BaseModel):
    flag_id: str
    conflict_type: str
    claim_a: str
    claim_b: str
    description: str
    entities: list[str] = Field(default_factory=list)
    skeptic_verdict: Optional[str] = None
    reviewer_decision: Optional[str] = None


class GraphNodeOut(BaseModel):
    id: str
    label: str
    type: str
    claim_count: int = 0
    evidence_count: int = 0
    degree: int = 0
    suspiciousness: float = 0.0
    cluster: int = 0


class GraphEdgeOut(BaseModel):
    source: str
    target: str
    weight: int
    claims: list[str] = Field(default_factory=list)


class InvestigationLead(BaseModel):
    entity_id: str
    entity_label: str
    entity_type: str
    suspiciousness_score: float
    degree: int
    claim_count: int
    suggestion: str
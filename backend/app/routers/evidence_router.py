"""Evidence router."""

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
import sqlite3

from ..auth import get_current_user, require_roles
from ..config import get_settings
from ..custody import (
    EvidenceNotFoundError,
    FileTamperedError,
    LedgerCompromisedError,
    QuarantinedError,
    UnsupportedFileError,
    get_verified_bytes,
    seal_file,
    verify_file,
)
from ..db import get_db
from ..models import EvidenceOut, UploadResult, VerifyResult
from ..storage import UploadTooLargeError, save_upload_to_tmp
from ..audit import write_audit


router = APIRouter(prefix="/cases", tags=["evidence"])


@router.post("/{case_id}/evidence", response_model=UploadResult, status_code=201)
def upload_evidence(
    case_id: str,
    upload: UploadFile = File(...),
    user: dict = Depends(require_roles("police", "forensic", "admin")),
    conn: sqlite3.Connection = Depends(get_db),
) -> UploadResult:
    """Upload evidence file for a case."""
    # Check case exists
    case_row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if not case_row:
        raise HTTPException(status_code=404, detail="Case not found")

    settings = get_settings()

    # Save upload to tmp and compute hash/size
    try:
        tmp_path, sha256, size = save_upload_to_tmp(upload, settings.max_upload_bytes)
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))

    original_filename = upload.filename or "unknown"

    # Seal file (scan, encrypt, ledger)
    try:
        result = seal_file(
            conn=conn,
            tmp_path=tmp_path,
            sha256=sha256,
            size_bytes=size,
            original_filename=original_filename,
            case_id=case_id,
            user_id=user["user_id"],
        )
    except UnsupportedFileError as e:
        raise HTTPException(status_code=415, detail=str(e))
    except QuarantinedError as e:
        raise HTTPException(
            status_code=422,
            detail={"status": "quarantined", "reason": e.reason},
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")

    return UploadResult(
        file_id=result["file_id"],
        case_id=result["case_id"],
        sha256=result["sha256"],
        ledger_entry_no=result["ledger_entry_no"],
        status=result["status"],
    )


@router.get("/{case_id}/evidence", response_model=list[EvidenceOut])
def list_evidence(
    case_id: str,
    user: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[EvidenceOut]:
    """List all evidence files for a case."""
    # Check case exists
    case_row = conn.execute("SELECT case_id FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if not case_row:
        raise HTTPException(status_code=404, detail="Case not found")

    rows = conn.execute(
        """
        SELECT seq, file_id, case_id, original_filename, file_type, content_type,
               size_bytes, sha256, stored_path, status, ledger_entry_no,
               uploaded_by, uploaded_at, last_verified_at, tamper_reason
        FROM evidence_files
        WHERE case_id = ?
        ORDER BY seq
        """,
        (case_id,),
    ).fetchall()

    return [
        EvidenceOut(
            seq=row["seq"],
            file_id=row["file_id"],
            case_id=row["case_id"],
            original_filename=row["original_filename"],
            file_type=row["file_type"],
            content_type=row["content_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            stored_path=row["stored_path"],
            status=row["status"],
            ledger_entry_no=row["ledger_entry_no"],
            uploaded_by=row["uploaded_by"],
            uploaded_at=row["uploaded_at"],
            last_verified_at=row["last_verified_at"],
            tamper_reason=row["tamper_reason"],
        )
        for row in rows
    ]


# Separate router for evidence by file_id (not nested under cases)
evidence_router = APIRouter(prefix="/evidence", tags=["evidence"])


@evidence_router.get("/{file_id}/verify", response_model=VerifyResult)
def verify_evidence(
    file_id: str,
    user: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> VerifyResult:
    """Verify integrity of an evidence file."""
    try:
        result = verify_file(conn, file_id, user["user_id"])
    except EvidenceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except LedgerCompromisedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return result


@evidence_router.get("/{file_id}/download")
def download_evidence(
    file_id: str,
    user: dict = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """Download verified evidence file."""
    # Get original filename for Content-Disposition
    row = conn.execute(
        "SELECT original_filename FROM evidence_files WHERE file_id = ?", (file_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Evidence file not found")

    original_filename = row["original_filename"]

    try:
        plaintext = get_verified_bytes(conn, file_id, user["user_id"])
    except EvidenceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileTamperedError as e:
        raise HTTPException(status_code=423, detail=str(e))
    except LedgerCompromisedError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Write audit log for successful download
    write_audit(
        conn=conn,
        user_id=user["user_id"],
        action="download",
        result="ok",
        file_id=file_id,
        case_id=None,
        detail=None,
    )

    return Response(
        content=plaintext,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{original_filename}"'},
    )
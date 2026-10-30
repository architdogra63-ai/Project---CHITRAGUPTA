"""
Extraction Agent — reads evidence files and extracts structured data.

Supports: csv, xlsx, pdf, images (png/jpg/jpeg/webp), mp4 (via frame description).
For LLM-based extraction, calls the configured endpoint with appropriate prompts.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..custody import get_verified_bytes
from ..db import get_connection
from .. import llm as llm_client

logger = logging.getLogger(__name__)


@dataclass
class RawExtraction:
    """Single extracted unit from an evidence file."""
    file_id: str
    source_ref: str          # e.g. "row 14", "page 3 paragraph 2", "frame 00:01:30"
    text: str                # extracted text
    entities_raw: list[str] = field(default_factory=list)   # raw entity mentions
    timestamps: list[str] = field(default_factory=list)     # extracted timestamps


# ---------------------------------------------------------------------------
# File-type handlers
# ---------------------------------------------------------------------------

def _extract_csv(content: bytes, file_id: str) -> list[RawExtraction]:
    """Parse CSV and emit one extraction per data row (max 500 rows)."""
    extractions: list[RawExtraction] = []
    try:
        text = content.decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
        for i, row in enumerate(reader, start=2):  # row 1 is header
            if i > 501:
                break
            row_text = " | ".join(f"{k}: {v}" for k, v in row.items() if v)
            if not row_text.strip():
                continue
            extractions.append(RawExtraction(
                file_id=file_id,
                source_ref=f"row {i}",
                text=row_text,
            ))
    except Exception as exc:
        logger.warning("CSV extraction error for %s: %s", file_id, exc)
        extractions.append(RawExtraction(
            file_id=file_id,
            source_ref="file",
            text=f"[CSV parse error: {exc}]",
        ))
    return extractions


def _extract_xlsx(content: bytes, file_id: str) -> list[RawExtraction]:
    """Parse XLSX and emit one extraction per non-empty row."""
    try:
        import openpyxl  # type: ignore
    except ImportError:
        return [RawExtraction(
            file_id=file_id,
            source_ref="file",
            text="[openpyxl not installed; cannot parse xlsx]",
        )]

    extractions: list[RawExtraction] = []
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
                if i > 1001:
                    break
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if not cells:
                    continue
                extractions.append(RawExtraction(
                    file_id=file_id,
                    source_ref=f"sheet '{sheet_name}' row {i}",
                    text=" | ".join(cells),
                ))
        wb.close()
    except Exception as exc:
        logger.warning("XLSX extraction error for %s: %s", file_id, exc)
        extractions.append(RawExtraction(
            file_id=file_id,
            source_ref="file",
            text=f"[XLSX parse error: {exc}]",
        ))
    return extractions


async def _extract_pdf(content: bytes, file_id: str) -> list[RawExtraction]:
    """Extract text from PDF using LLM prompt (sends raw bytes as base64)."""
    import base64
    b64 = base64.b64encode(content).decode()
    # Send as a text extraction request; for large PDFs we chunk to first ~50k chars of base64
    b64_chunk = b64[:60000]
    prompt = (
        "You are a forensic document analyst. The following is a PDF file encoded in base64. "
        "Extract ALL factual claims, entities (people, organizations, accounts, phone numbers, "
        "places, dates, amounts) and events. "
        "Return a JSON array where each element has: "
        '{\"source_ref\": \"page N paragraph M\", \"text\": \"...\", '
        '\"entities_raw\": [...], \"timestamps\": [...]}. '
        "Return ONLY the JSON array, no other text.\n\n"
        f"Base64 PDF (first 60k chars): {b64_chunk}"
    )
    try:
        data = await llm_client.chat_json(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        if not isinstance(data, list):
            data = [data]
        extractions = []
        for item in data:
            if not isinstance(item, dict):
                continue
            extractions.append(RawExtraction(
                file_id=file_id,
                source_ref=str(item.get("source_ref", "unknown")),
                text=str(item.get("text", "")),
                entities_raw=list(item.get("entities_raw", [])),
                timestamps=list(item.get("timestamps", [])),
            ))
        return extractions
    except llm_client.LLMNotConfiguredError:
        logger.info("LLM not configured; skipping PDF extraction for %s", file_id)
        return [RawExtraction(
            file_id=file_id,
            source_ref="file",
            text="[PDF extraction requires LLM configuration]",
        )]
    except Exception as exc:
        logger.warning("PDF LLM extraction failed for %s: %s", file_id, exc)
        return [RawExtraction(
            file_id=file_id,
            source_ref="file",
            text=f"[PDF extraction error: {exc}]",
        )]


async def _extract_image(content: bytes, file_id: str, file_type: str) -> list[RawExtraction]:
    """Extract text/entities from an image using LLM vision (base64 data URI)."""
    import base64
    mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
    mime = mime_map.get(file_type, "image/png")
    b64 = base64.b64encode(content).decode()
    data_uri = f"data:{mime};base64,{b64}"

    # Vision-capable models accept image_url in content
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "You are a forensic analyst examining an image. "
                        "Extract all text visible in the image (OCR), any entities "
                        "(people, accounts, phone numbers, places, dates, amounts), "
                        "and describe any significant visual elements. "
                        "Return JSON: {\"text\": \"...\", \"entities_raw\": [...], \"timestamps\": [...]}. "
                        "Return ONLY the JSON object."
                    ),
                },
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }
    ]
    try:
        data = await llm_client.chat_json(messages, temperature=0.1)
        return [RawExtraction(
            file_id=file_id,
            source_ref="image",
            text=str(data.get("text", "")),
            entities_raw=list(data.get("entities_raw", [])),
            timestamps=list(data.get("timestamps", [])),
        )]
    except llm_client.LLMNotConfiguredError:
        return [RawExtraction(
            file_id=file_id,
            source_ref="image",
            text="[Image extraction requires LLM configuration]",
        )]
    except Exception as exc:
        logger.warning("Image LLM extraction failed for %s: %s", file_id, exc)
        return [RawExtraction(
            file_id=file_id,
            source_ref="image",
            text=f"[Image extraction error: {exc}]",
        )]


async def _extract_video(content: bytes, file_id: str) -> list[RawExtraction]:
    """Extract keyframe descriptions from video using ffmpeg + LLM."""
    import base64

    extractions: list[RawExtraction] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        vid_path = Path(tmpdir) / f"{file_id}.mp4"
        vid_path.write_bytes(content)
        frames_dir = Path(tmpdir) / "frames"
        frames_dir.mkdir()

        # Extract 1 frame per 30 seconds, max 10 frames
        try:
            subprocess.run(
                [
                    "ffmpeg", "-i", str(vid_path),
                    "-vf", "fps=1/30",
                    "-frames:v", "10",
                    str(frames_dir / "frame_%04d.jpg"),
                ],
                capture_output=True,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("ffmpeg unavailable or timeout for %s: %s", file_id, exc)
            return [RawExtraction(
                file_id=file_id,
                source_ref="video",
                text="[Video extraction requires ffmpeg and LLM configuration]",
            )]

        frame_files = sorted(frames_dir.glob("frame_*.jpg"))
        if not frame_files:
            return [RawExtraction(
                file_id=file_id,
                source_ref="video",
                text="[No frames extracted from video]",
            )]

        for i, frame_path in enumerate(frame_files):
            # Each frame ~= 30s * i offset
            timestamp_sec = i * 30
            h, m, s = timestamp_sec // 3600, (timestamp_sec % 3600) // 60, timestamp_sec % 60
            ts_str = f"{h:02d}:{m:02d}:{s:02d}"

            frame_bytes = frame_path.read_bytes()
            b64 = base64.b64encode(frame_bytes).decode()
            data_uri = f"data:image/jpeg;base64,{b64}"

            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"This is frame at timestamp {ts_str} from a forensic video. "
                                "Describe what you see: people, objects, text, locations, actions. "
                                "Return JSON: {\"text\": \"...\", \"entities_raw\": [...], \"timestamps\": [...]}. "
                                "Return ONLY the JSON."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ]
            try:
                data = await llm_client.chat_json(messages, temperature=0.1)
                extractions.append(RawExtraction(
                    file_id=file_id,
                    source_ref=f"frame {ts_str}",
                    text=str(data.get("text", "")),
                    entities_raw=list(data.get("entities_raw", [])),
                    timestamps=list(data.get("timestamps", [ts_str])),
                ))
            except llm_client.LLMNotConfiguredError:
                extractions.append(RawExtraction(
                    file_id=file_id,
                    source_ref=f"frame {ts_str}",
                    text="[Frame description requires LLM configuration]",
                ))
                break
            except Exception as exc:
                logger.warning("Frame %s extraction failed for %s: %s", ts_str, file_id, exc)
                extractions.append(RawExtraction(
                    file_id=file_id,
                    source_ref=f"frame {ts_str}",
                    text=f"[Frame extraction error: {exc}]",
                ))

    return extractions or [RawExtraction(
        file_id=file_id,
        source_ref="video",
        text="[No content extracted from video]",
    )]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def extract_file(
    file_id: str,
    file_type: str,
    content: bytes,
) -> list[RawExtraction]:
    """
    Dispatch extraction to the correct handler based on file_type.

    Args:
        file_id: Evidence file ID (e.g. "EV-0001").
        file_type: Normalized file type string (e.g. "csv", "pdf", "png").
        content: Raw decrypted file bytes.

    Returns:
        List of RawExtraction objects.
    """
    ft = file_type.lower().strip(".")

    if ft == "csv":
        return _extract_csv(content, file_id)
    elif ft in ("xlsx", "xls"):
        return _extract_xlsx(content, file_id)
    elif ft == "pdf":
        return await _extract_pdf(content, file_id)
    elif ft in ("png", "jpg", "jpeg", "webp", "bmp", "gif"):
        return await _extract_image(content, file_id, ft)
    elif ft in ("mp4", "avi", "mkv", "mov", "webm"):
        return await _extract_video(content, file_id)
    else:
        # Generic text fallback
        try:
            text = content.decode("utf-8", errors="replace")
            return [RawExtraction(
                file_id=file_id,
                source_ref="file",
                text=text[:8000],
            )]
        except Exception:
            return [RawExtraction(
                file_id=file_id,
                source_ref="file",
                text=f"[Unsupported file type: {ft}]",
            )]


async def run_extraction(
    case_id: str,
    file_id: str,
    user_id: str,
    run_id: str,
) -> list[dict]:
    """
    Full extraction pipeline for one file:
    1. Verify and load the evidence file.
    2. Dispatch to the correct extractor.
    3. Return list of raw extraction dicts (for DB storage).
    """
    conn = get_connection()
    try:
        # Load file metadata
        row = conn.execute(
            "SELECT file_type FROM evidence_files WHERE file_id = ? AND case_id = ?",
            (file_id, case_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Evidence file {file_id} not found in case {case_id}")

        file_type = row["file_type"]

        # Get verified plaintext
        content = get_verified_bytes(conn, file_id, user_id)
    finally:
        conn.close()

    extractions = await extract_file(file_id, file_type, content)

    return [
        {
            "run_id": run_id,
            "file_id": e.file_id,
            "source_ref": e.source_ref,
            "extracted_text": e.text,
            "entities_json": json.dumps(e.entities_raw),
            "timestamps_json": json.dumps(e.timestamps),
        }
        for e in extractions
    ]

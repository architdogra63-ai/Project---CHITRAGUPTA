"""Vault writer tests."""

import tempfile
from pathlib import Path

import pytest

from app.vault.writer import (
    get_writer,
    VaultWriter,
    VaultError,
    ClaimInput,
    FlagInput,
    EntityRef,
)
from app.vault.naming import validate_note_name


def _sample_row() -> dict:
    """Helper returning sample evidence row (from Part D conftest)."""
    return {
        "file_id": "EV-0001",
        "case_id": "CASE-001",
        "original_filename": "bank.csv",
        "file_type": "csv",
        "size_bytes": 120,
        "sha256": "a" * 64,
        "status": "verified",
        "ledger_entry_no": 1,
        "uploaded_by": "officer_17",
        "uploaded_at": "2026-10-30T10:00:00.000000Z",
        "tamper_reason": None,
    }


class TestSyncEvidenceNote:
    """Test sync_evidence_note (Tests 11-13)."""

    def test_sync_evidence_note_verified(self, tmp_path: Path) -> None:
        """Test 11: sync_evidence_note creates file with correct tags and body."""
        writer = VaultWriter(tmp_path)
        row = _sample_row()
        
        path = writer.sync_evidence_note(row)
        
        assert path.exists()
        assert path.name == "EV-0001.md"
        assert "evidence" in str(path)
        
        content = path.read_text(encoding="utf-8")
        assert "tags:" in content
        assert "evidence" in content
        assert "csv" in content
        assert "Status: verified" in content
        assert "SHA-256:" in content
        assert "Ledger entry: 1" in content

    def test_sync_evidence_note_twice_byte_identical(self, tmp_path: Path) -> None:
        """Test 12: calling sync twice produces byte-identical files."""
        writer = VaultWriter(tmp_path)
        row = _sample_row()
        
        path1 = writer.sync_evidence_note(row)
        content1 = path1.read_bytes()
        
        path2 = writer.sync_evidence_note(row)
        content2 = path2.read_bytes()
        
        assert content1 == content2

    def test_sync_evidence_note_preserves_summary_claims_entities_on_tampered(self, tmp_path: Path) -> None:
        """Test 13: sync with tampered status preserves summary, claims, entities."""
        writer = VaultWriter(tmp_path)
        row = _sample_row()
        
        # First sync with verified
        writer.sync_evidence_note(row)
        
        # Add summary, claim, entity via writer methods
        writer.set_evidence_summary("CASE-001", "EV-0001", "Test summary")
        writer.upsert_entity("CASE-001", "person", "John Doe", "EV-0001")
        
        # Now sync with tampered
        row_tampered = dict(row)
        row_tampered["status"] = "tampered"
        row_tampered["tamper_reason"] = "hash_mismatch"
        writer.sync_evidence_note(row_tampered)
        
        path = writer._path("CASE-001", "evidence", "EV-0001.md")
        content = path.read_text(encoding="utf-8")
        
        assert "Test summary" in content
        assert "PERSON-john-doe" in content
        assert "tampered" in content
        assert "hash_mismatch" in content
        assert "[!danger]" in content

    def test_sync_invalid_status_raises(self, tmp_path: Path) -> None:
        writer = VaultWriter(tmp_path)
        row = _sample_row()
        row["status"] = "pending"
        
        with pytest.raises(ValueError, match="Invalid status"):
            writer.sync_evidence_note(row)


class TestSetEvidenceSummary:
    """Test set_evidence_summary."""

    def test_set_summary_updates_note(self, tmp_path: Path) -> None:
        writer = VaultWriter(tmp_path)
        writer.sync_evidence_note(_sample_row())
        
        writer.set_evidence_summary("CASE-001", "EV-0001", "New summary")
        
        path = writer._path("CASE-001", "evidence", "EV-0001.md")
        content = path.read_text(encoding="utf-8")
        assert "New summary" in content

    def test_set_summary_missing_raises(self, tmp_path: Path) -> None:
        writer = VaultWriter(tmp_path)
        with pytest.raises(VaultError, match="not found"):
            writer.set_evidence_summary("CASE-001", "EV-9999", "summary")


class TestUpsertEntity:
    """Test upsert_entity (Test 14)."""

    def test_upsert_entity_merges_duplicates(self, tmp_path: Path) -> None:
        """Test 14: upsert_entity with "Rahul Sharma" and " rahul   sharma " -> same note name."""
        writer = VaultWriter(tmp_path)
        
        name1 = writer.upsert_entity("CASE-001", "person", "Rahul Sharma", "EV-0001")
        name2 = writer.upsert_entity("CASE-001", "person", " rahul   sharma ", "EV-0002")
        
        assert name1 == name2 == "PERSON-rahul-sharma"
        
        # Check both evidence IDs are in the note
        path = writer._path("CASE-001", "entities", "people", "PERSON-rahul-sharma.md")
        content = path.read_text(encoding="utf-8")
        assert "EV-0001" in content
        assert "EV-0002" in content

    def test_upsert_entity_different_types_different_notes(self, tmp_path: Path) -> None:
        writer = VaultWriter(tmp_path)
        
        name1 = writer.upsert_entity("CASE-001", "person", "12345")
        name2 = writer.upsert_entity("CASE-001", "account", "12345")
        
        assert name1 == "PERSON-12345"
        assert name2 == "ACC-12345"


class TestWriteClaim:
    """Test write_claim (Tests 15-16)."""

    def test_write_claim_creates_all_notes(self, tmp_path: Path) -> None:
        """Test 15: write_claim with 2 entities creates claim, 2 entities, updates evidence."""
        writer = VaultWriter(tmp_path)
        writer.sync_evidence_note(_sample_row())
        
        claim = ClaimInput(
            claim_id="CL-0001",
            source_file_id="EV-0001",
            source_ref="page 1",
            claim_type="statement",
            text="Test claim text",
            entities=(
                EntityRef("person", "John Doe"),
                EntityRef("place", "New York"),
            ),
            confidence=0.9,
        )
        
        writer.write_claim("CASE-001", claim)
        
        # Check claim note exists
        claim_path = writer._path("CASE-001", "claims", "CL-0001.md")
        assert claim_path.exists()
        
        # Check entity notes exist
        person_path = writer._path("CASE-001", "entities", "people", "PERSON-john-doe.md")
        place_path = writer._path("CASE-001", "entities", "places", "PLACE-new-york.md")
        assert person_path.exists()
        assert place_path.exists()
        
        # Check all wikilinks resolve to existing files
        claim_content = claim_path.read_text(encoding="utf-8")
        assert "[[PERSON-john-doe]]" in claim_content
        assert "[[PLACE-new-york]]" in claim_content
        assert person_path.exists()
        assert place_path.exists()

    def test_write_claim_missing_evidence_raises(self, tmp_path: Path) -> None:
        """Test 16: write_claim when evidence note missing -> VaultError, no entities created."""
        writer = VaultWriter(tmp_path)
        
        claim = ClaimInput(
            claim_id="CL-0001",
            source_file_id="EV-9999",  # Doesn't exist
            source_ref="page 1",
            claim_type="statement",
            text="Test claim",
            entities=(EntityRef("person", "John Doe"),),
        )
        
        with pytest.raises(VaultError, match="not found"):
            writer.write_claim("CASE-001", claim)
        
        # No entity files should have been created
        entities_dir = writer._path("CASE-001", "entities", "people")
        assert not list(entities_dir.glob("*.md"))


class TestMarkClaimsStale:
    """Test mark_claims_stale (Test 17)."""

    def test_mark_claims_stale_only_target(self, tmp_path: Path) -> None:
        """Test 17: mark_claims_stale after claims from two different files."""
        writer = VaultWriter(tmp_path)
        writer.sync_evidence_note(_sample_row())
        
        # Create second evidence
        row2 = dict(_sample_row())
        row2["file_id"] = "EV-0002"
        row2["original_filename"] = "transactions.csv"
        writer.sync_evidence_note(row2)
        
        # Create claims for both files
        claim1 = ClaimInput(
            claim_id="CL-0001",
            source_file_id="EV-0001",
            source_ref="page 1",
            claim_type="statement",
            text="Claim from EV-0001",
            entities=(),
        )
        claim2 = ClaimInput(
            claim_id="CL-0002",
            source_file_id="EV-0002",
            source_ref="row 1",
            claim_type="statement",
            text="Claim from EV-0002",
            entities=(),
        )
        writer.write_claim("CASE-001", claim1)
        writer.write_claim("CASE-001", claim2)
        
        # Mark EV-0001 stale
        changed = writer.mark_claims_stale("CASE-001", "EV-0001")
        assert changed == ["CL-0001"]
        
        # Check CL-0001 is stale
        claim1_path = writer._path("CASE-001", "claims", "CL-0001.md")
        content1 = claim1_path.read_text(encoding="utf-8")
        assert "stale" in content1
        assert "[!warning]" in content1
        assert "Stale" in content1
        
        # Check CL-0002 is NOT stale
        claim2_path = writer._path("CASE-001", "claims", "CL-0002.md")
        content2 = claim2_path.read_text(encoding="utf-8")
        assert "stale" not in content2
        assert "active" in content2 or "status: active" in content2
        
        # Second call returns empty
        changed2 = writer.mark_claims_stale("CASE-001", "EV-0001")
        assert changed2 == []


class TestWriteFlag:
    """Test write_flag (Test 18)."""

    def test_write_flag_both_claims_exist(self, tmp_path: Path) -> None:
        """Test 18: write_flag with both claims present -> tags [conflict]."""
        writer = VaultWriter(tmp_path)
        writer.sync_evidence_note(_sample_row())
        
        # Create two claims
        claim1 = ClaimInput("CL-0001", "EV-0001", "p1", "statement", "Claim 1")
        claim2 = ClaimInput("CL-0002", "EV-0001", "p2", "statement", "Claim 2")
        writer.write_claim("CASE-001", claim1)
        writer.write_claim("CASE-001", claim2)
        
        flag = FlagInput(
            flag_id="FLAG-0001",
            conflict_type="time",
            claim_a="CL-0001",
            claim_b="CL-0002",
            description="Time conflict",
        )
        
        writer.write_flag("CASE-001", flag)
        
        flag_path = writer._path("CASE-001", "flags", "FLAG-0001.md")
        assert flag_path.exists()
        
        content = flag_path.read_text(encoding="utf-8")
        assert "conflict" in content
        assert "FLAG-0001" in content

    def test_write_flag_missing_claim_raises(self, tmp_path: Path) -> None:
        """Test 18: write_flag with missing claim -> VaultError."""
        writer = VaultWriter(tmp_path)
        writer.sync_evidence_note(_sample_row())
        
        claim1 = ClaimInput("CL-0001", "EV-0001", "p1", "statement", "Claim 1")
        writer.write_claim("CASE-001", claim1)
        
        flag = FlagInput(
            flag_id="FLAG-0001",
            conflict_type="time",
            claim_a="CL-0001",
            claim_b="CL-9999",  # Missing
            description="Time conflict",
        )
        
        with pytest.raises(VaultError, match="not found"):
            writer.write_flag("CASE-001", flag)


class TestSetFlagReview:
    """Test set_flag_review (Test 19)."""

    def test_set_flag_review_dismissed_changes_tags(self, tmp_path: Path) -> None:
        """Test 19: set_flag_review(reviewer_decision="dismissed") -> tags [conflict-dismissed]."""
        writer = VaultWriter(tmp_path)
        writer.sync_evidence_note(_sample_row())
        
        claim1 = ClaimInput("CL-0001", "EV-0001", "p1", "statement", "Claim 1")
        claim2 = ClaimInput("CL-0002", "EV-0001", "p2", "statement", "Claim 2")
        writer.write_claim("CASE-001", claim1)
        writer.write_claim("CASE-001", claim2)
        
        flag = FlagInput("FLAG-0001", "time", "CL-0001", "CL-0002", "Conflict")
        writer.write_flag("CASE-001", flag)
        
        writer.set_flag_review("CASE-001", "FLAG-0001", reviewer_decision="dismissed")
        
        flag_path = writer._path("CASE-001", "flags", "FLAG-0001.md")
        content = flag_path.read_text(encoding="utf-8")
        assert "conflict-dismissed" in content


class TestCorruptionHandling:
    """Test corruption handling (Test 20)."""

    def test_overwrite_with_garbage_then_sync(self, tmp_path: Path) -> None:
        """Test 20: overwrite evidence note with garbage, then sync -> garbage in .corrupt/, new valid note."""
        writer = VaultWriter(tmp_path)
        writer.sync_evidence_note(_sample_row())
        
        # Overwrite with garbage
        note_path = writer._path("CASE-001", "evidence", "EV-0001.md")
        note_path.write_text("not valid frontmatter\n\nbody")
        
        # Sync again
        writer.sync_evidence_note(_sample_row())
        
        # Original should be in .corrupt/
        corrupt_dir = writer._path("CASE-001", ".corrupt")
        corrupt_files = list(corrupt_dir.glob("EV-0001.*.md"))
        assert len(corrupt_files) == 1
        
        # New note should be valid
        new_content = note_path.read_text(encoding="utf-8")
        assert new_content.startswith("---\n")
        assert "type: evidence" in new_content


class TestPathTraversal:
    """Test path traversal prevention (Test 21)."""

    def test_case_id_traversal_raises(self, tmp_path: Path) -> None:
        """Test 21: writer call with case_id="../x" -> ValueError, nothing written."""
        writer = VaultWriter(tmp_path)
        
        with pytest.raises(ValueError, match="invalid case_id"):
            writer.sync_evidence_note(dict(_sample_row(), case_id="../x"))
        
        # Nothing should be written outside root
        assert not (tmp_path.parent / "x").exists()


class TestNoTempFilesLeft:
    """Test no temp files left (Test 22)."""

    def test_no_temp_files_after_writes(self, tmp_path: Path) -> None:
        """Test 22: after series of writes, no .tmp-* files remain."""
        writer = VaultWriter(tmp_path)
        writer.sync_evidence_note(_sample_row())
        
        claim = ClaimInput("CL-0001", "EV-0001", "p1", "statement", "Claim")
        writer.write_claim("CASE-001", claim)
        
        writer.upsert_entity("CASE-001", "person", "John Doe", "EV-0001", "CL-0001")
        
        flag = FlagInput("FLAG-0001", "time", "CL-0001", "CL-0001", "Conflict")
        writer.write_flag("CASE-001", flag)
        
        temp_files = list(tmp_path.rglob(".tmp-*.part"))
        assert len(temp_files) == 0


class TestConcurrentUpsertEntity:
    """Test concurrent upsert_entity (Test 23)."""

    def test_concurrent_upsert_entity(self, tmp_path: Path) -> None:
        """Test 23: 10 threads call upsert_entity for one entity, each with different claim id."""
        import threading
        
        writer = VaultWriter(tmp_path)
        
        def upsert_claim(claim_num: int) -> None:
            claim_id = f"CL-{claim_num:04d}"
            writer.upsert_entity("CASE-001", "person", "John Doe", "EV-0001", claim_id)
        
        threads = [threading.Thread(target=upsert_claim, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        path = writer._path("CASE-001", "entities", "people", "PERSON-john-doe.md")
        content = path.read_text(encoding="utf-8")
        
        # Should have all 10 claim IDs
        for i in range(10):
            assert f"CL-{i:04d}" in content
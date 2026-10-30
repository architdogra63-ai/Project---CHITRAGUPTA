"""Vault notes parsing and rendering tests."""

import pytest

from app.vault.notes import (
    NoteParseError,
    clean_text,
    render_note,
    parse_note,
    wikilink,
)
from app.vault.naming import validate_note_name


class TestCleanText:
    """Test clean_text function (Tests 9-10)."""

    def test_clean_text_escapes_wikilinks(self) -> None:
        """Test 9: clean_text("a [[[b]]]\n#tag  c") -> no [[, ]] or #tag remains."""
        result = clean_text("a [[[b]]]\n#tag  c")
        assert "[[" not in result
        assert "]]" not in result
        assert "#tag" not in result
        assert "a [ [ [ b ] ]" in result
        assert "# tag" in result

    def test_clean_text_truncates(self) -> None:
        """Test 10: clean_text with 3000 characters, max_len=2000 -> length 2000, ends with ..."""
        long_text = "x" * 3000
        result = clean_text(long_text, max_len=2000)
        assert len(result) == 2000
        assert result.endswith("…")


class TestWikilink:
    """Test wikilink function."""

    def test_wikilink_valid(self) -> None:
        assert wikilink("EV-0001") == "[[EV-0001]]"
        assert wikilink("PERSON-rahul-sharma") == "[[PERSON-rahul-sharma]]"

    def test_wikilink_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            wikilink("../x")
        with pytest.raises(ValueError):
            wikilink("a b")


class TestRenderParseRoundtrip:
    """Test render_note then parse_note roundtrip (Test 7)."""

    def test_roundtrip_with_special_chars(self) -> None:
        """Test 7: roundtrip with values containing :, quotes and [[."""
        fm = {
            "type": "evidence",
            "id": "EV-0001",
            "case_id": "CASE-001",
            "file": "bank.csv",
            "file_type": "csv",
            "size_bytes": 120,
            "sha256": "a" * 64,
            "ledger_entry": 1,
            "status": "verified",
            "uploaded_by": "officer_17",
            "uploaded_at": "2026-10-30T10:00:00.000000Z",
            "summary": "Test summary with : colon and \"quotes\" and [[wikilink]]",
            "entities": ["ACC-1234567890"],
            "claims": ["CL-0001"],
            "tags": ["evidence", "csv"],
        }
        body = "# EV-0001 - bank.csv\n\n> Status: verified\n\n## Entities\n\n- [[ACC-1234567890]]\n"
        
        text = render_note(fm, body)
        parsed_fm, parsed_body = parse_note(text)
        
        assert parsed_fm == fm
        assert parsed_body == body

    def test_roundtrip_empty_lists(self) -> None:
        """Test roundtrip with empty lists."""
        fm = {
            "type": "entity",
            "entity_type": "person",
            "name": "John Doe",
            "key": "PERSON-john-doe",
            "case_id": "CASE-001",
            "aliases": [],
            "evidence": [],
            "claims": [],
            "tags": ["entity", "person"],
        }
        body = "# John Doe\n\nType: person | Key: `PERSON-john-doe`\n\n## Seen in\n\n_None yet._\n\n## Claims\n\n_None yet._\n"
        
        text = render_note(fm, body)
        parsed_fm, parsed_body = parse_note(text)
        
        assert parsed_fm == fm
        assert parsed_body == body


class TestParseNoteErrors:
    """Test parse_note error cases (Test 8)."""

    def test_no_frontmatter(self) -> None:
        """Test 8: parse_note on no frontmatter raises NoteParseError."""
        with pytest.raises(NoteParseError, match="start with"):
            parse_note("# Just a heading\n\nContent")

    def test_bad_yaml(self) -> None:
        """Test 8: parse_note on bad YAML raises NoteParseError."""
        with pytest.raises(NoteParseError, match="Invalid YAML"):
            parse_note("---\ninvalid: [yaml\n---\n\nBody")

    def test_yaml_not_dict(self) -> None:
        """Test 8: parse_note on YAML that is a list raises NoteParseError."""
        with pytest.raises(NoteParseError, match="must be a dictionary"):
            parse_note("---\n- item1\n- item2\n---\n\nBody")

    def test_missing_closing_delimiter(self) -> None:
        """Test missing closing --- raises NoteParseError."""
        with pytest.raises(NoteParseError, match="Missing closing"):
            parse_note("---\ntype: test\n\nBody")
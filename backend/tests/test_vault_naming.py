"""Vault naming tests."""

import pytest

from app.vault.naming import (
    slugify,
    normalize_account,
    normalize_phone,
    entity_key,
    entity_note_name,
    entity_folder,
    validate_case_id,
    validate_file_id,
    validate_claim_id,
    validate_flag_id,
    validate_note_name,
    ENTITY_TYPES,
)


class TestSlugify:
    """Test slugify function (Tests 1-3)."""

    def test_slugify_basic(self) -> None:
        """Test 1: slugify("Rahul Sharma"), slugify("  A   B ") -> rahul-sharma, a-b."""
        assert slugify("Rahul Sharma") == "rahul-sharma"
        assert slugify("  A   B ") == "a-b"

    def test_slugify_devanagari(self) -> None:
        """Test 2: slugify("राहुल शर्मा") -> राहुल-शर्मा (combining marks kept)."""
        assert slugify("राहुल शर्मा") == "राहुल-शर्मा"

    def test_slugify_empty_result(self) -> None:
        """Test 3: slugify("!!!") -> 8 hex characters."""
        result = slugify("!!!")
        assert len(result) == 8
        assert all(c in "0123456789abcdef" for c in result)


class TestNormalizeAccount:
    """Test normalize_account function (Test 4)."""

    def test_normalize_account(self) -> None:
        """Test 4: normalize_account("1234 5678-90ab") -> 1234567890AB."""
        assert normalize_account("1234 5678-90ab") == "1234567890AB"


class TestNormalizePhone:
    """Test normalize_phone function (Test 5)."""

    def test_normalize_phone(self) -> None:
        """Test 5: normalize_phone on various inputs."""
        assert normalize_phone("98765 43210") == "919876543210"
        assert normalize_phone("09876543210") == "919876543210"
        assert normalize_phone("+91 98765 43210") == "919876543210"
        assert normalize_phone("12345") == "12345"


class TestEntityFunctions:
    """Test entity key, note name, folder functions."""

    def test_entity_key_person(self) -> None:
        assert entity_key("person", "Rahul Sharma") == "PERSON-rahul-sharma"

    def test_entity_key_account(self) -> None:
        assert entity_key("account", "1234 5678") == "ACC-12345678"

    def test_entity_key_phone(self) -> None:
        assert entity_key("phone", "98765 43210") == "PHONE-919876543210"

    def test_entity_note_name(self) -> None:
        assert entity_note_name("person", "Rahul Sharma") == "PERSON-rahul-sharma"

    def test_entity_folder(self) -> None:
        assert entity_folder("person") == "people"
        assert entity_folder("account") == "accounts"
        assert entity_folder("phone") == "phones"
        assert entity_folder("place") == "places"
        assert entity_folder("device") == "devices"

    def test_unknown_entity_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown entity type"):
            entity_key("unknown", "value")
        with pytest.raises(ValueError, match="unknown entity type"):
            entity_note_name("unknown", "value")
        with pytest.raises(ValueError, match="unknown entity type"):
            entity_folder("unknown")


class TestValidation:
    """Test validation functions (Test 6)."""

    def test_validate_case_id(self) -> None:
        assert validate_case_id("CASE-001") == "CASE-001"
        assert validate_case_id("CASE-12345") == "CASE-12345"
        with pytest.raises(ValueError):
            validate_case_id("case-001")
        with pytest.raises(ValueError):
            validate_case_id("CASE-01")

    def test_validate_file_id(self) -> None:
        assert validate_file_id("EV-0001") == "EV-0001"
        assert validate_file_id("EV-12345") == "EV-12345"
        with pytest.raises(ValueError):
            validate_file_id("ev-0001")
        with pytest.raises(ValueError):
            validate_file_id("EV-001")

    def test_validate_claim_id(self) -> None:
        assert validate_claim_id("CL-0001") == "CL-0001"
        with pytest.raises(ValueError):
            validate_claim_id("cl-0001")
        with pytest.raises(ValueError):
            validate_claim_id("CL-001")

    def test_validate_flag_id(self) -> None:
        assert validate_flag_id("FLAG-0001") == "FLAG-0001"
        with pytest.raises(ValueError):
            validate_flag_id("flag-0001")
        with pytest.raises(ValueError):
            validate_flag_id("FLAG-001")

    def test_validate_note_name(self) -> None:
        """Test 6: validate_note_name on various invalid inputs."""
        assert validate_note_name("valid-name") == "valid-name"
        assert validate_note_name("valid_name") == "valid_name"
        assert validate_note_name("राहुल") == "राहुल"
        
        with pytest.raises(ValueError, match="invalid character"):
            validate_note_name("../x")
        with pytest.raises(ValueError, match="invalid character"):
            validate_note_name("a b")
        with pytest.raises(ValueError, match="invalid character"):
            validate_note_name("a.b")
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_note_name("")
        with pytest.raises(ValueError, match="too long"):
            validate_note_name("a" * 121)


class TestEntityTypes:
    """Test ENTITY_TYPES dict."""

    def test_entity_types(self) -> None:
        assert ENTITY_TYPES["person"] == ("PERSON", "people")
        assert ENTITY_TYPES["account"] == ("ACC", "accounts")
        assert ENTITY_TYPES["phone"] == ("PHONE", "phones")
        assert ENTITY_TYPES["place"] == ("PLACE", "places")
        assert ENTITY_TYPES["device"] == ("DEVICE", "devices")
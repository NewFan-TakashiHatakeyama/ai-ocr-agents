import pytest

from newfan_ingest.errors import IngestError
from newfan_ingest.validation import IngestLimits, detect_kind, validate_upload

PDF = b"%PDF-1.7\n..."
PNG = b"\x89PNG\r\n\x1a\n\x00\x00"
JPEG = b"\xff\xd8\xff\xe0\x00"
ZIP = b"PK\x03\x04\x14\x00"


def test_detect_kind_by_magic() -> None:
    assert detect_kind(PDF, "a.pdf") == "pdf"
    assert detect_kind(PNG, "a.png") == "png"
    assert detect_kind(JPEG, "a.jpg") == "jpeg"
    assert detect_kind(ZIP, "a.docx") == "office"


def test_zip_without_office_ext_rejected() -> None:
    # ZIP だが Office 拡張子でない → 非対応
    assert detect_kind(ZIP, "a.zip") is None


def test_validate_ok() -> None:
    assert validate_upload(PDF, "invoice.pdf") == "pdf"


def test_extension_mismatch_rejected() -> None:
    # PNG 実体を .pdf 名で送る（ポリグロット対策, §11）
    with pytest.raises(IngestError) as exc:
        validate_upload(PNG, "evil.pdf")
    assert exc.value.code == "E1001"


def test_empty_rejected() -> None:
    with pytest.raises(IngestError) as exc:
        validate_upload(b"", "a.pdf")
    assert exc.value.code == "E1001"


def test_oversize_rejected() -> None:
    limits = IngestLimits(max_bytes=4)
    with pytest.raises(IngestError) as exc:
        validate_upload(PDF, "a.pdf", limits)
    assert exc.value.code == "E1002"


def test_unknown_format_rejected() -> None:
    with pytest.raises(IngestError) as exc:
        validate_upload(b"garbage-bytes", "a.bin")
    assert exc.value.code == "E1001"

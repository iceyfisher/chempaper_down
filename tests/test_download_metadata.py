from pathlib import Path

from paper_tool.resources import (
    content_disposition_filename,
    obvious_error_payload,
    resolve_download_extension,
)
from paper_tool.storage import validate_file


def test_rfc5987_content_disposition_filename():
    value = "attachment; filename*=UTF-8''acscatal.6c02592.s001.pdf"
    assert content_disposition_filename(value) == "acscatal.6c02592.s001.pdf"


def test_pdf_magic_wins_for_extensionless_acs_url():
    extension, filename = resolve_download_extension(
        "https://pubs.acs.org/action/downloadSupplement?id=opaque",
        "Supporting Information",
        declared_mime_type="application/octet-stream",
        content_disposition="attachment; filename=acscatal.6c02592.s001.pdf",
        head=b"%PDF-1.7\n",
    )
    assert extension == ".pdf"
    assert filename == "acscatal.6c02592.s001.pdf"


def test_zip_magic_preserves_office_container_type():
    extension, _ = resolve_download_extension(
        "https://example.invalid/opaque",
        declared_mime_type="application/octet-stream",
        content_disposition="attachment; filename=data.xlsx",
        head=b"PK\x03\x04",
    )
    assert extension == ".xlsx"


def test_error_payloads_and_unknown_binary_are_rejected(tmp_path: Path):
    assert obvious_error_payload(b"  <!doctype html><html>", extension=".pdf") == "html_instead_of_file"
    assert obvious_error_payload(b'{"error":"denied"}', extension=".pdf") == "json_instead_of_file"

    unknown = tmp_path / "opaque.bin"
    unknown.write_bytes(b"not a recognized attachment")
    assert validate_file(unknown, ".bin") == (False, "unknown_binary_type")


def test_declared_json_attachment_remains_supported(tmp_path: Path):
    path = tmp_path / "data.json"
    path.write_bytes(b'{"measurements":[1,2,3]}')
    assert validate_file(path, ".json") == (True, "ok")

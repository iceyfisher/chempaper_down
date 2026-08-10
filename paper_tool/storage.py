from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote


def clean_path_component(name: str) -> str:
    name = unquote(name)
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip().strip(".") or "Unknown"


def doi_to_filename(doi: str) -> str:
    return clean_path_component(doi.replace("/", "_"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def valid_pdf(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(64)
            fh.seek(max(0, size - 4096))
            tail = fh.read()
        return (
            head.startswith(b"%PDF")
            and b"%%EOF" in tail
            and b"\xef\xbf\xbd" not in head
        )
    except OSError:
        return False


def validate_file(path: Path, extension: str | None = None) -> tuple[bool, str]:
    if not path.is_file() or path.stat().st_size <= 0:
        return False, "missing_or_empty"

    ext = (extension or path.suffix).lower()
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(512)
        fh.seek(max(0, size - 4096))
        tail = fh.read()

    from .resources import obvious_error_payload

    error = obvious_error_payload(head, extension=ext)
    if error:
        return False, error
    if ext == ".pdf":
        return (valid_pdf(path), "ok" if valid_pdf(path) else "invalid_pdf")
    if ext == ".png" and not head.startswith(b"\x89PNG\r\n\x1a\n"):
        return False, "invalid_png"
    if ext in {".jpg", ".jpeg"} and not head.startswith(b"\xff\xd8\xff"):
        return False, "invalid_jpeg"
    if ext in {".docx", ".xlsx", ".pptx", ".zip"} and not head.startswith(b"PK"):
        return False, "invalid_zip_container"
    if ext == ".7z" and not head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return False, "invalid_7z"
    if ext == ".gz" and not head.startswith(b"\x1f\x8b"):
        return False, "invalid_gzip"
    if ext in {".mp4", ".mov"} and b"ftyp" not in head:
        return False, "invalid_media"
    return True, "ok"


def article_manifest_path(download_root: Path, doi: str) -> Path:
    return download_root / "_manifests" / f"{doi.replace('/', '_')}.json"


def load_article_manifest(download_root: Path, doi: str) -> dict[str, Any] | None:
    path = article_manifest_path(download_root, doi)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def manifest_has_complete_si(manifest: dict[str, Any] | None) -> bool:
    if not manifest or manifest.get("status") != "success":
        return False
    diagnostics = manifest.get("diagnostics") or {}
    if diagnostics.get("si_scan_complete") is not True:
        return False
    for item in manifest.get("si") or []:
        path_value = item.get("path")
        if not path_value or item.get("valid") is not True:
            return False
        path = Path(path_value)
        valid, _ = validate_file(path, item.get("extension") or path.suffix)
        if not valid:
            return False
    return True


def find_existing_paper(download_root: Path, doi: str) -> Path | None:
    """Hard DOI duplicate check.

    Only a valid main PDF under **/paper/<doi>.pdf counts as a duplicate.  SI-only
    partial runs are not considered complete and are allowed to resume.
    """

    expected_name = f"{doi_to_filename(doi)}.pdf"
    if not download_root.exists():
        return None
    for paper_dir in download_root.rglob("paper"):
        candidate = paper_dir / expected_name
        if valid_pdf(candidate):
            return candidate
    return None


def make_article_dirs(download_root: Path, publisher: str, journal: str) -> tuple[Path, Path, Path]:
    base = download_root / clean_path_component(f"{publisher} - {journal}")
    paper_dir = base / "paper"
    si_dir = base / "si"
    paper_dir.mkdir(parents=True, exist_ok=True)
    si_dir.mkdir(parents=True, exist_ok=True)
    return base, paper_dir, si_dir


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

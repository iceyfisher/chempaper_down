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
        if not path.is_file():
            return False
        size = path.stat().st_size
        if size <= 0:
            return False
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


def pdf_page_count(path: Path) -> int | None:
    """Best-effort page count without a PDF library.

    Prefers pypdf when installed (handles compressed object streams); falls
    back to the largest /Count in the page trees, which covers plain and
    linearized PDFs. Returns None when the count cannot be trusted.
    """

    try:
        if not path.is_file():
            return None
        with path.open("rb") as fh:
            head = fh.read(64)
            if not head.startswith(b"%PDF"):
                return None
        try:
            from pypdf import PdfReader

            return len(PdfReader(str(path)).pages)
        except ImportError:
            pass
        data = path.read_bytes()
        counts = [int(m) for m in re.findall(rb"/Count\s+(\d+)", data)]
        return max(counts) if counts else None
    except Exception:
        return None


def page_span_from_metas(metas: list) -> int | None:
    """Expected page count from citation_firstpage/citation_lastpage metas."""

    def meta_value(name: str) -> str | None:
        for meta in metas or []:
            if isinstance(meta, dict) and meta.get("name", "").lower() == name:
                value = str(meta.get("value") or "").strip()
                if value:
                    return value
        return None

    first, last = meta_value("citation_firstpage"), meta_value("citation_lastpage")

    def to_int(value: str | None) -> int | None:
        if value is None:
            return None
        # Page numbers can be roman numerals or carry article prefixes (e.g. e12345).
        match = re.search(r"\d+", value)
        return int(match.group(0)) if match else None

    first_n, last_n = to_int(first), to_int(last)
    if first_n is None or last_n is None:
        return None
    span = last_n - first_n + 1
    return span if span >= 1 else None


def looks_like_truncated_paper(path: Path, expected_pages: int | None) -> str | None:
    """Detect a publisher-served first-page preview or a truncated download.

    Publishers without entitlement (Wiley is the classic case) still answer the
    PDF request with HTTP 200 and a structurally valid one-page file, so the
    normal magic-byte validation passes. Cross-checking the page count against
    the citation page range catches that case.
    """

    if not expected_pages or expected_pages < 2:
        return None
    actual = pdf_page_count(path)
    if actual is None or actual < 1:
        return None
    if actual < expected_pages:
        return (
            f"PDF has {actual} page(s) but the article spans {expected_pages}; "
            "the file is a first-page preview or truncated."
        )
    return None


def validate_file(path: Path, extension: str | None = None) -> tuple[bool, str]:
    try:
        if not path.is_file():
            return False, "missing_or_empty"
        size = path.stat().st_size
        if size <= 0:
            return False, "missing_or_empty"
        with path.open("rb") as fh:
            head = fh.read(512)
            fh.seek(max(0, size - 4096))
            tail = fh.read()
    except OSError:
        return False, "missing_or_empty"

    ext = (extension or path.suffix).lower()

    from .resources import obvious_error_payload

    error = obvious_error_payload(head, extension=ext)
    if error:
        return False, error
    if ext == ".pdf":
        valid = (
            head.startswith(b"%PDF")
            and b"%%EOF" in tail
            and b"\xef\xbf\xbd" not in head
        )
        return valid, "ok" if valid else "invalid_pdf"
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
    return download_root / "_manifests" / f"{doi_to_filename(doi)}.json"


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

    Only a valid main PDF under **/pdf/<doi>.pdf (or the legacy **/paper/<doi>.pdf
    layout) counts as a duplicate.  SI-only partial runs are not considered
    complete and are allowed to resume.

    The walk prunes every underscore-prefixed internal directory (_staging,
    _browser_profile, _worker_runs, ...): walking the browser profile alone
    means thousands of files per call.
    """

    import os

    expected_name = f"{doi_to_filename(doi)}.pdf"
    if not download_root.exists():
        return None
    for dirpath, dirnames, filenames in os.walk(download_root):
        dirnames[:] = [d for d in dirnames if not d.startswith("_")]
        if Path(dirpath).name not in {"pdf", "paper"}:
            continue
        if expected_name in filenames:
            candidate = Path(dirpath) / expected_name
            if valid_pdf(candidate):
                return candidate
    return None


def article_dir_name(doi: str, year: str | int | None) -> str:
    year_part = clean_path_component(str(year)) if year not in (None, "") else "unknown"
    return f"{doi_to_filename(doi)}_{year_part}"


def journal_dir_name(journal: str | None) -> str:
    return clean_path_component(journal) if journal else "Unknown Journal"


def make_article_dirs(
    download_root: Path,
    doi: str,
    year: str | int | None,
    journal: str | None,
) -> tuple[Path, Path, Path]:
    """downloads/<journal>/<doi>_<year>/ with a pdf/ and a si/ subfolder.

    Journals are the top level so a large archive stays browsable; the article
    folder drops the journal suffix it used to carry.
    """

    base = download_root / journal_dir_name(journal) / article_dir_name(doi, year)
    pdf_dir = base / "pdf"
    si_dir = base / "si"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    si_dir.mkdir(parents=True, exist_ok=True)
    return base, pdf_dir, si_dir


def migrate_legacy_article_dirs(download_root: Path) -> int:
    """Move flat legacy article dirs into the journal/<article> layout.

    Legacy layout: downloads/<doi>_<year>_<journal>/{pdf,si}. New layout:
    downloads/<journal>/<doi>_<year>/{pdf,si}. Also rewrites paper/si paths in
    the matching _manifests entry so resume/duplicate checks keep working.
    Returns the number of migrated articles.
    """

    if not download_root.exists():
        return 0
    migrated = 0
    for entry in sorted(download_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        is_legacy_article = (entry / "pdf").is_dir() or (entry / "si").is_dir()
        if not is_legacy_article:
            continue  # journal-level folder or unrelated content
        # From the right: <journal> then <year>; the doi keeps its underscores
        # (slashes were already replaced by doi_to_filename).
        head, sep, journal_part = entry.name.rpartition("_")
        doi_part, sep2, year_part = head.rpartition("_")
        if not (sep and sep2 and re.match(r"^(19|20)\d{2}$", year_part)
                and re.match(r"^10\.\d{4,}", doi_part)):
            continue
        new_base = download_root / journal_dir_name(journal_part) / f"{doi_part}_{year_part}"
        if new_base.exists():
            continue
        new_base.parent.mkdir(parents=True, exist_ok=True)
        entry.rename(new_base)
        migrated += 1
        manifest_path = download_root / "_manifests" / f"{doi_part}.json"
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                for item in [data.get("paper") or {}, *(data.get("si") or [])]:
                    old_path = str(item.get("path") or "")
                    if old_path and entry.name in old_path:
                        for sep_ch in ("\\", "/"):
                            old_path = old_path.replace(
                                f"{entry.name}{sep_ch}",
                                f"{journal_dir_name(journal_part)}{sep_ch}{new_base.name}{sep_ch}",
                            )
                        item["path"] = old_path
                write_json_atomic(manifest_path, data)
            except (OSError, ValueError):
                pass
    return migrated


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

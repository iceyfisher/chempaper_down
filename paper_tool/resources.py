from __future__ import annotations

import re
from email.message import Message
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


KNOWN_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".svg",
    ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".zip", ".7z", ".gz", ".tar",
    ".csv", ".txt", ".xml", ".json", ".cif", ".mp4", ".mov", ".avi", ".mpeg", ".mpg",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".svg"}
NATIVE_EXTENSIONS = KNOWN_EXTENSIONS - IMAGE_EXTENSIONS

MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/x-7z-compressed": ".7z",
    "application/gzip": ".gz",
    "application/x-gzip": ".gz",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/json": ".json",
    "text/json": ".json",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "text/xml": ".xml",
    "application/xml": ".xml",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/tiff": ".tif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/mpeg": ".mpeg",
}

ZIP_CONTAINER_EXTENSIONS = {".zip", ".docx", ".xlsx", ".pptx"}


def infer_extension(url: str, text: str = "") -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("file", "filename"):
        if query.get(key):
            suffix = Path(query[key][0]).suffix.lower()
            if suffix in KNOWN_EXTENSIONS:
                return suffix
    suffix = Path(unquote(parsed.path)).suffix.lower()
    if suffix in KNOWN_EXTENSIONS:
        return suffix
    lower = (text or "").lower()
    for ext in sorted(KNOWN_EXTENSIONS, key=len, reverse=True):
        if ext in lower:
            return ext
    match = re.search(r"\b(pdf|png|jpe?g|tiff?|xlsx?|docx?|zip|csv|txt|cif|mp4|mov)\b", lower)
    if match:
        ext = "." + match.group(1).lower()
        return ".jpg" if ext == ".jpeg" else ext
    return ".bin"


def content_disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    message = Message()
    message["content-disposition"] = value
    filename = message.get_filename()
    if not filename:
        return None
    return Path(unquote(filename)).name or None


def sniff_extension(head: bytes) -> str | None:
    clean = head.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if clean.startswith(b"%PDF"):
        return ".pdf"
    if clean.startswith(b"PK\x03\x04") or clean.startswith(b"PK\x05\x06"):
        return ".zip"
    if clean.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".7z"
    if clean.startswith(b"\x1f\x8b"):
        return ".gz"
    if clean.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if clean.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if clean.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if clean.startswith((b"II*\x00", b"MM\x00*")):
        return ".tif"
    if clean.startswith(b"RIFF") and clean[8:12] == b"WEBP":
        return ".webp"
    if len(clean) >= 12 and clean[4:8] == b"ftyp":
        return ".mp4"
    if clean.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return ".ole"
    return None


def resolve_download_extension(
    url: str,
    text: str = "",
    *,
    declared_mime_type: str | None = None,
    content_disposition: str | None = None,
    head: bytes = b"",
) -> tuple[str, str | None]:
    """Resolve the real attachment type without trusting a single hint."""

    original_filename = content_disposition_filename(content_disposition)
    disposition_ext = infer_extension(original_filename or "")
    mime = (declared_mime_type or "").split(";", 1)[0].strip().lower()
    mime_ext = MIME_EXTENSIONS.get(mime, ".bin")
    link_ext = infer_extension(url, text)
    magic_ext = sniff_extension(head)

    if magic_ext == ".zip":
        for candidate in (disposition_ext, mime_ext, link_ext):
            if candidate in ZIP_CONTAINER_EXTENSIONS:
                return candidate, original_filename
        return ".zip", original_filename
    if magic_ext == ".ole":
        for candidate in (disposition_ext, mime_ext, link_ext):
            if candidate in {".doc", ".xls", ".ppt"}:
                return candidate, original_filename
    elif magic_ext:
        return magic_ext, original_filename

    for candidate in (disposition_ext, mime_ext, link_ext):
        if candidate in KNOWN_EXTENSIONS:
            return candidate, original_filename
    return ".bin", original_filename


def obvious_error_payload(
    head: bytes,
    *,
    declared_mime_type: str | None = None,
    extension: str = ".bin",
) -> str | None:
    clean = head.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    low = clean[:512].lower()
    mime = (declared_mime_type or "").split(";", 1)[0].strip().lower()
    if mime in {"text/html", "application/xhtml+xml"} or low.startswith(
        (b"<!doctype html", b"<html")
    ):
        return "html_instead_of_file"
    if extension != ".json" and (
        mime in {"application/json", "text/json"}
        or low.startswith((b"{", b"["))
    ):
        return "json_instead_of_file"
    if extension == ".bin":
        return "unknown_binary_type"
    return None

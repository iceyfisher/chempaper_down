from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


KNOWN_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".svg",
    ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".zip", ".gz", ".tar",
    ".csv", ".txt", ".xml", ".json", ".cif", ".mp4", ".mov", ".avi", ".mpeg", ".mpg",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".svg"}
NATIVE_EXTENSIONS = KNOWN_EXTENSIONS - IMAGE_EXTENSIONS


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

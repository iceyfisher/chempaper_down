from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Iterable


DOI_PATTERN = re.compile(
    r"10\.\d{4,9}/[-._;()/:A-Z0-9]+",
    flags=re.IGNORECASE,
)


def normalize_doi(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = value.rstrip(".,;:]}>'\"")
    return value.lower()


def extract_dois(text: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for match in DOI_PATTERN.finditer(text or ""):
        doi = normalize_doi(match.group(0))
        if doi and doi not in seen:
            seen.add(doi)
            result.append(doi)
    return result


def _walk_json(value: Any, preferred_key: str = "doi") -> Iterable[str]:
    if isinstance(value, dict):
        # Prefer explicit DOI-like keys, but recursively search all values too.
        keys = [preferred_key, "DOI", "doi_url", "doiUrl"]
        for key in keys:
            if key in value and isinstance(value[key], str):
                yield value[key]
        for child in value.values():
            yield from _walk_json(child, preferred_key)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child, preferred_key)
    elif isinstance(value, str):
        yield value


def load_dois_from_file(path: str | Path, doi_field: str = "doi") -> list[str]:
    """Read DOI lists produced by a human or an upstream AI agent.

    Supported: txt/md, json, jsonl/ndjson, csv/tsv. Unknown extensions fall back
    to text extraction, so search-agent manifests with mixed prose still work.
    """

    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)

    suffix = path.suffix.lower()
    values: list[str] = []

    if suffix == ".json":
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        values.extend(_walk_json(obj, doi_field))

    elif suffix in {".jsonl", ".ndjson"}:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                values.append(line)
            else:
                values.extend(_walk_json(obj, doi_field))

    elif suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh, delimiter=delimiter)
            for row in reader:
                if doi_field in row and row[doi_field]:
                    values.append(row[doi_field])
                else:
                    values.extend(v for v in row.values() if isinstance(v, str))

    else:
        return extract_dois(path.read_text(encoding="utf-8-sig", errors="replace"))

    return extract_dois("\n".join(values))

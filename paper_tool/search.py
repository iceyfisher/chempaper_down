"""Free academic metadata search backed by the OpenAlex API.

Pure-HTTP module: it never touches Pydoll/Edge, so it is safe to call from the
API process. OpenAlex is free (no key); a mailto address opts into the polite
pool. Set OPENALEX_MAILTO in .env to receive better rate limits.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import unquote

import httpx


OPENALEX_API = "https://api.openalex.org/works"
SELECT_FIELDS = (
    "id,doi,title,display_name,publication_year,primary_location,"
    "cited_by_count,open_access,authorships"
)
DOI_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)
REQUEST_TIMEOUT = httpx.Timeout(connect=15, read=30, write=15, pool=15)


@dataclass(slots=True)
class WorkRow:
    title: str
    doi: str
    year: str | None
    journal: str | None
    authors: list[str]
    cited_by_count: int
    open_access: bool

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "doi": self.doi,
            "year": self.year,
            "journal": self.journal,
            "authors": self.authors,
            "cited_by_count": self.cited_by_count,
            "open_access": self.open_access,
            "source": "openalex",
        }


def _mailto() -> str | None:
    return os.getenv("OPENALEX_MAILTO") or None


def clean_doi(value: str | None) -> str:
    if not value:
        return ""
    doi = DOI_PREFIX_RE.sub("", value.strip())
    return unquote(doi).lower()


def _journal_of(work: dict) -> str | None:
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    return source.get("display_name") or None


def _authors_of(work: dict, limit: int = 6) -> list[str]:
    names: list[str] = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        name = author.get("display_name")
        if name:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def parse_work(work: dict) -> WorkRow | None:
    if not isinstance(work, dict):
        return None
    title = work.get("display_name") or work.get("title") or ""
    doi = clean_doi(work.get("doi"))
    if not title and not doi:
        return None
    return WorkRow(
        title=title,
        doi=doi,
        year=str(work["publication_year"]) if work.get("publication_year") else None,
        journal=_journal_of(work),
        authors=_authors_of(work),
        cited_by_count=int(work.get("cited_by_count") or 0),
        open_access=bool((work.get("open_access") or {}).get("is_oa")),
    )


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", (value or "").lower()).strip()


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


async def search_openalex(query: str, limit: int = 12) -> list[dict]:
    """Free-text search; rows are ordered by OpenAlex relevance."""

    query = (query or "").strip()
    if not query:
        return []
    params = {
        "search": query,
        "per-page": max(1, min(int(limit), 50)),
        "select": SELECT_FIELDS,
    }
    mailto = _mailto()
    if mailto:
        params["mailto"] = mailto
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        response = await client.get(OPENALEX_API, params=params)
        response.raise_for_status()
        payload = response.json()
    rows = [row.to_dict() for row in map(parse_work, payload.get("results") or []) if row]
    return rows


async def resolve_title(
    client: httpx.AsyncClient,
    title: str,
    *,
    candidates: int = 4,
    min_similarity: float = 0.72,
) -> dict:
    """Resolve one exact title to its best DOI match via title.search filter."""

    params = {
        "filter": f"title.search:{title.strip()}",
        "per-page": max(1, candidates),
        "select": SELECT_FIELDS,
    }
    mailto = _mailto()
    if mailto:
        params["mailto"] = mailto
    result = {
        "query": title,
        "matched_title": None,
        "doi": None,
        "year": None,
        "journal": None,
        "similarity": 0.0,
        "resolved": False,
    }
    try:
        response = await client.get(OPENALEX_API, params=params)
        response.raise_for_status()
        works = response.json().get("results") or []
    except Exception as exc:
        result["error"] = repr(exc)
        return result

    best, best_score = None, 0.0
    for work in works:
        row = parse_work(work)
        if not row:
            continue
        score = title_similarity(title, row.title)
        if score > best_score:
            best, best_score = row, score
    if best is not None and best_score >= min_similarity:
        result.update(
            matched_title=best.title,
            doi=best.doi,
            year=best.year,
            journal=best.journal,
            similarity=round(best_score, 3),
            resolved=True,
        )
    elif best is not None:
        result.update(
            matched_title=best.title,
            doi=best.doi,
            year=best.year,
            journal=best.journal,
            similarity=round(best_score, 3),
        )
    return result


async def resolve_titles(titles: list[str]) -> list[dict]:
    """Batch title → DOI resolution with polite per-request spacing.

    OpenAlex allows ~10 requests/second; 0.15s spacing stays well inside the
    polite pool even for a few hundred titles.
    """

    cleaned = [t.strip() for t in titles if t and t.strip()]
    results: list[dict] = []
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        for index, title in enumerate(cleaned):
            if index:
                await asyncio.sleep(0.15)
            results.append(await resolve_title(client, title))
    return results

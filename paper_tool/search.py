"""Free academic metadata search backed by the OpenAlex API.

Pure-HTTP module: it never touches Pydoll/Edge, so it is safe to call from the
API process. OpenAlex is free (no key); a mailto address opts into the polite
pool. Set OPENALEX_MAILTO in .env to receive better rate limits.

Connectivity strategy: campus/proxy setups differ wildly in how (or whether)
they can reach OpenAlex. Every request walks three modes and remembers the
first that worked:

1. "env"     - honor http_proxy/https_proxy environment variables;
2. "direct"  - normal DNS + direct TLS (works on most campus networks);
3. "doh"     - resolve the host through a domestic DoH resolver (223.5.5.5)
               and connect by IP with SNI/Host overrides. This survives broken
               foreign DNS without any local proxy software.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import unquote

import httpx


OPENALEX_API = "https://api.openalex.org/works"
OPENALEX_HOST = "api.openalex.org"
DOH_RESOLVERS = (
    "https://223.5.5.5/resolve",
    "https://223.6.6.6/resolve",
    "https://120.53.53.53/resolve",
)
DOH_CACHE_TTL_SECONDS = 600
SELECT_FIELDS = (
    "id,doi,title,display_name,publication_year,primary_location,"
    "cited_by_count,open_access,authorships"
)
DOI_PREFIX_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)
REQUEST_TIMEOUT = httpx.Timeout(connect=15, read=30, write=15, pool=15)
TRANSIENT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

# "env" honors http_proxy/https_proxy variables; "direct" bypasses them; "doh"
# resolves through a domestic DoH resolver and dials the IP directly.
_CLIENT_MODES = ("env", "direct", "doh")
_working_mode: str | None = None
_doh_cache: dict[str, tuple[str, float]] = {}


async def _resolve_via_doh(host: str) -> str | None:
    """Resolve a foreign host through domestic DoH resolvers (cached)."""

    cached = _doh_cache.get(host)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
        for resolver in DOH_RESOLVERS:
            try:
                response = await client.get(
                    resolver, params={"name": host, "type": "A"}
                )
                payload = response.json()
            except Exception:
                continue
            for answer in payload.get("Answer") or []:
                if answer.get("type") == 1 and answer.get("data"):
                    ip = str(answer["data"])
                    _doh_cache[host] = (ip, time.monotonic() + DOH_CACHE_TTL_SECONDS)
                    return ip
    return None


def _mailto() -> str | None:
    return os.getenv("OPENALEX_MAILTO") or None


def clean_doi(value: str | None) -> str:
    if not value:
        return ""
    doi = DOI_PREFIX_RE.sub("", value.strip())
    return unquote(doi).lower()


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


async def openalex_get(params: dict) -> dict:
    """GET an OpenAlex endpoint via env proxy, direct, or DoH+IP dialing.

    Each mode is attempted twice (transient failures retry once); the first
    mode that produces a response is cached for the process lifetime.
    """

    global _working_mode
    # Probe the cached mode first, but always keep the other modes as fallback
    # in case the cached one stopped working.
    modes = tuple(
        dict.fromkeys(((_working_mode,) if _working_mode else ()) + _CLIENT_MODES)
    )
    last_error: Exception | None = None

    for mode in modes:
        if mode == "doh":
            ip = await _resolve_via_doh(OPENALEX_HOST)
            if not ip:
                continue
            url = f"https://{ip}/works"
            headers = {"Host": OPENALEX_HOST}
            request_extensions = {"sni_hostname": OPENALEX_HOST}
        else:
            url = OPENALEX_API
            headers = {}
            request_extensions = None
        for attempt in range(2):
            client = httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=True,
                trust_env=(mode == "env"),
            )
            try:
                response = await client.get(
                    url,
                    params=params,
                    headers=headers,
                    extensions=request_extensions,
                )
                response.raise_for_status()
            except TRANSIENT_ERRORS as exc:
                last_error = exc
                if _working_mode == mode:
                    _working_mode = None
                await asyncio.sleep(0.4)
                continue
            except httpx.HTTPStatusError:
                raise
            finally:
                await client.aclose()
            _working_mode = mode
            return response.json()

    raise ConnectionError(
        f"OpenAlex unreachable via proxy, direct and DoH+IP connection: {last_error!r}"
    )


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
    payload = await openalex_get(params)
    rows = [row.to_dict() for row in map(parse_work, payload.get("results") or []) if row]
    return rows


async def resolve_title(
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
        payload = await openalex_get(params)
        works = payload.get("results") or []
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
    for index, title in enumerate(cleaned):
        if index:
            await asyncio.sleep(0.15)
        results.append(await resolve_title(title))
    return results

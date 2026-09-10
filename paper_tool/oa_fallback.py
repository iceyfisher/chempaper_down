"""Green-OA repository fallback for articles the campus cannot access.

When the publisher flow cannot obtain the main PDF (no entitlement, hard
captcha), OpenAlex often knows an open repository copy (green OA). This
fallback queries OpenAlex and downloads the first valid PDF it can find —
either the recorded ``pdf_url`` or a PDF link scraped off the repository
landing page.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import urljoin

import httpx

from .netutil import env_proxy_usable
from .resources import sniff_extension
from .storage import doi_to_filename, make_article_dirs, sha256_file

logger = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_PDF_LINK_RE = re.compile(r'href="([^"]+\.pdf[^"]*)"', re.IGNORECASE)


def _looks_like_pdf(head: bytes) -> bool:
    return head.lstrip(b"\xef\xbb\xbf\x00\t\r\n ").startswith(b"%PDF")


async def try_green_oa(
    result,
    settings,
    *,
    timeout: float = 90.0,
    worker=None,
) -> object | None:
    """Fill ``result.paper`` from an open-access repository copy if possible.

    Direct HTTP is tried first; repository file hosts frequently sit behind a
    Cloudflare challenge, so when the worker's browser is still alive the same
    candidates are retried through it. Returns the FileResult on success.
    """

    from .models import FileResult

    if result.paper and result.paper.valid:
        return None
    doi = result.doi
    try:
        async with httpx.AsyncClient(
            timeout=30, trust_env=env_proxy_usable(), follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            response = await client.get(
                f"https://api.openalex.org/works/https://doi.org/{doi}"
            )
            if response.status_code != 200:
                result.diagnostics["oa_fallback"] = f"openalex_{response.status_code}"
                return None
            locations = response.json().get("locations") or []
            landings: list[str] = []
            for location in locations:
                if not location.get("is_oa"):
                    continue
                pdf_url = location.get("pdf_url")
                landing = location.get("landing_page_url")
                if pdf_url:
                    landings.append(pdf_url)
                elif landing:
                    landings.append(landing)
            for url in landings[:5]:
                for candidate in await _expand_candidate(client, url, timeout):
                    artifact = await _download(client, candidate, result, settings, timeout)
                    if artifact:
                        result.diagnostics["oa_fallback"] = candidate[:200]
                        return artifact
            if worker is not None and worker.browser is not None:
                artifact = await _browser_download(
                    worker, landings, result, settings, timeout
                )
                if artifact:
                    return artifact
    except Exception as exc:  # network failures must not mask the adapter result
        logger.info("green OA fallback failed for %s: %r", doi, exc)
        result.diagnostics["oa_fallback_error"] = repr(exc)[:200]
    result.diagnostics.setdefault("oa_fallback", "no_repository_copy")
    return None


async def _browser_download(worker, landings, result, settings, timeout):
    """Retry the candidates in the live browser (Cloudflare-gated hosts)."""

    from .download import native_navigation_download
    from .models import FileResult
    from .storage import validate_file

    pdf_urls: list[str] = [u for u in landings if u.lower().endswith(".pdf")]
    try:
        tab = await worker.browser.new_tab()
        try:
            for landing in landings[:3]:
                if landing in pdf_urls:
                    continue
                try:
                    await asyncio.wait_for(tab.go_to(landing), timeout=45)
                except Exception:
                    continue
                links_raw = await asyncio.wait_for(
                    tab.execute_script(
                        "return Array.from(document.querySelectorAll('a[href]'))"
                        ".map(a => a.href).filter(h => h.toLowerCase().endsWith('.pdf')).slice(0, 5);",
                        return_by_value=True,
                    ),
                    timeout=10,
                )
                from .download import _unwrap_script_result
                links = _unwrap_script_result(links_raw)
                for href in links if isinstance(links, list) else []:
                    if isinstance(href, str) and href not in pdf_urls:
                        pdf_urls.append(href)
        finally:
            try:
                await tab.close()
            except Exception:
                pass
        journal = result.journal or "Unknown Journal"
        year = result.year or "Unknown"
        _, paper_dir, _ = make_article_dirs(
            settings.download_root, result.doi, year, journal
        )
        target = paper_dir / f"{doi_to_filename(result.doi)}.pdf"
        for url in pdf_urls[:5]:
            path = await native_navigation_download(
                worker, url, target, timeout=min(60.0, timeout)
            )
            if path and path.exists():
                valid, reason = validate_file(path, ".pdf")
                if valid:
                    result.diagnostics["oa_fallback"] = f"browser:{url[:180]}"
                    result.paper = FileResult(
                        kind="paper",
                        path=str(path),
                        source_url=url,
                        method="green_oa_repository_browser",
                        extension=".pdf",
                        size=path.stat().st_size,
                        sha256=sha256_file(path),
                        valid=True,
                    )
                    return result.paper
                logger.info("browser OA candidate %s invalid: %s", url, reason)
    except Exception as exc:
        logger.info("browser OA fallback failed for %s: %r", result.doi, exc)
    return None


async def _expand_candidate(client, url: str, timeout: float) -> list[str]:
    """Return direct PDF URLs for a candidate (itself or links on its page)."""

    if url.lower().endswith(".pdf"):
        return [url]
    try:
        page = await client.get(url)
    except httpx.HTTPError:
        return []
    if page.status_code != 200 or len(page.content) > 3_000_000:
        return []
    if _looks_like_pdf(page.content[:512]):
        return [str(page.url)]
    content_type = page.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        return []
    found = []
    for href in _PDF_LINK_RE.findall(page.text)[:10]:
        absolute = urljoin(str(page.url), href)
        if absolute not in found:
            found.append(absolute)
    return found


async def _download(client, url: str, result, settings, timeout: float):
    from .models import FileResult

    try:
        response = await client.get(url)
    except httpx.HTTPError:
        return None
    head = response.content[:512]
    if response.status_code != 200 or not _looks_like_pdf(head):
        return None
    if sniff_extension(head) != ".pdf":
        return None
    journal = result.journal or "Unknown Journal"
    year = result.year or "Unknown"
    _, paper_dir, _ = make_article_dirs(settings.download_root, result.doi, year, journal)
    target = paper_dir / f"{doi_to_filename(result.doi)}.pdf"
    target.write_bytes(response.content)
    result.paper = FileResult(
        kind="paper",
        path=str(target),
        source_url=url,
        method="green_oa_repository",
        extension=".pdf",
        size=target.stat().st_size,
        sha256=sha256_file(target),
        valid=True,
    )
    return result.paper

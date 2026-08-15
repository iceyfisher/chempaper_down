from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from .base import AdapterContext, PublisherAdapter
from ..download import DownloadArtifact
from ..models import ArticleResult
from ..resources import (
    infer_extension,
    obvious_error_payload,
    resolve_download_extension,
)
from ..storage import doi_to_filename, validate_file


ELSEVIER_API_BASE = "https://api.elsevier.com/content"
ELSEVIER_ALLOWED_HOSTS = {
    "api.elsevier.com",
    "ars.els-cdn.com",
    "sciencedirect.com",
    "www.sciencedirect.com",
}
ELSEVIER_MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024 * 1024
PII_RE = re.compile(r"\b(S[A-Z0-9]{10,})\b", re.IGNORECASE)
MMC_EXTENSIONS = (
    ".pdf",
    ".docx",
    ".zip",
    ".xlsx",
    ".doc",
    ".xls",
    ".mp4",
    ".mov",
    ".pptx",
    ".ppt",
    ".csv",
    ".txt",
    ".rtf",
    ".avi",
    ".mpeg",
    ".mp3",
    ".cif",
)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return next((_text(item) for item in value if _text(item)), "")
    if isinstance(value, dict):
        for key in ("$", "@href", "href", "value", "#text"):
            if key in value and _text(value[key]):
                return _text(value[key])
    return ""


def _record_value(record: dict, *keys: str) -> str:
    for key in keys:
        for candidate in (key, f"@{key}"):
            value = _text(record.get(candidate))
            if value:
                return value
    return ""


def _find_pii(*values: str) -> str | None:
    for value in values:
        match = PII_RE.search(value or "")
        if match:
            return match.group(1).upper()
    return None


def _find_pii_in_payload(value) -> str | None:
    if isinstance(value, str):
        return _find_pii(value)
    if isinstance(value, dict):
        for child in value.values():
            pii = _find_pii_in_payload(child)
            if pii:
                return pii
    elif isinstance(value, list):
        for child in value:
            pii = _find_pii_in_payload(child)
            if pii:
                return pii
    return None


def parse_article_metadata(payload: dict | None) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    response = payload.get("full-text-retrieval-response") or payload
    core = response.get("coredata") if isinstance(response, dict) else None
    if not isinstance(core, dict):
        search_results = payload.get("search-results")
        entries = search_results.get("entry") if isinstance(search_results, dict) else None
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            core = entries[0]
    if not isinstance(core, dict):
        core = {}

    response_links = response.get("link") if isinstance(response, dict) else None
    links = core.get("link") or response_links or []
    if isinstance(links, dict):
        links = [links]
    article_url = ""
    for link in links:
        if not isinstance(link, dict):
            continue
        if _record_value(link, "ref").lower() == "scidir":
            article_url = _record_value(link, "href")
            break
    article_url = article_url or _record_value(core, "prism:url", "url")

    identifier = _record_value(core, "dc:identifier", "identifier")
    pii = (
        _find_pii(_record_value(core, "pii"), identifier, article_url)
        or _find_pii_in_payload(response)
        or ""
    )
    return {
        "title": _record_value(core, "dc:title", "title"),
        "journal": _record_value(core, "prism:publicationName", "publicationName"),
        "pii": pii,
        "article_url": article_url,
    }


def _allowed_elsevier_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() in ELSEVIER_ALLOWED_HOSTS
    )


def build_mmc_url(pii: str, number: int, extension: str) -> str:
    normalized_extension = (
        extension if extension.startswith(".") else f".{extension}"
    )
    return (
        "https://ars.els-cdn.com/content/image/"
        f"1-s2.0-{pii.upper()}-mmc{number}{normalized_extension.lower()}"
    )


def build_article_pdf_url(doi: str) -> str:
    return f"{ELSEVIER_API_BASE}/article/doi/{doi}?httpAccept=application/pdf"


async def _http_error_status(response: httpx.Response) -> str:
    status = f"http_{response.status_code}"
    try:
        body = await response.aread()
    except Exception:
        return status
    detail = body[:2048].decode("utf-8", errors="replace")
    detail = re.sub(r"<[^>]+>", " ", detail)
    detail = re.sub(r"\s+", " ", detail).strip()
    return f"{status}:{detail[:240]}" if detail else status


class ElsevierAdapter(PublisherAdapter):
    """Download the article through the Article API and SI through PII/mmc URLs."""

    key = "ELSEVIER"
    publisher_name = "Elsevier"

    @classmethod
    def matches_doi(cls, doi: str) -> bool:
        return doi.startswith("10.1016/")

    @staticmethod
    def _api_headers(api_key: str, accept: str) -> dict[str, str]:
        return {
            "X-ELS-APIKey": api_key,
            "Accept": accept,
        }

    async def _api_json(
        self,
        client: httpx.AsyncClient,
        ctx: AdapterContext,
        url: str,
    ) -> tuple[dict | None, str]:
        key = ctx.settings.elsevier_api_key
        if not key:
            return None, "api_key_missing"
        try:
            response = await client.get(
                url,
                headers=self._api_headers(key, "application/json"),
                params={"view": "META"},
            )
        except Exception as exc:
            return None, f"request_error:{type(exc).__name__}"
        if response.status_code != 200:
            return None, await _http_error_status(response)
        try:
            payload = response.json()
        except ValueError:
            return None, "invalid_json"
        return payload, "ok"

    async def _metadata_search_json(
        self,
        client: httpx.AsyncClient,
        ctx: AdapterContext,
    ) -> tuple[dict | None, str]:
        key = ctx.settings.elsevier_api_key
        if not key:
            return None, "api_key_missing"
        try:
            response = await client.get(
                f"{ELSEVIER_API_BASE}/metadata/article",
                headers=self._api_headers(key, "application/json"),
                params={
                    "query": f'DOI("{ctx.doi}")',
                    "view": "COMPLETE",
                    "count": "1",
                },
            )
        except Exception as exc:
            return None, f"request_error:{type(exc).__name__}"
        if response.status_code != 200:
            return None, await _http_error_status(response)
        try:
            payload = response.json()
        except ValueError:
            return None, "invalid_json"
        return payload, "ok"

    async def _article_metadata_xml(
        self,
        client: httpx.AsyncClient,
        ctx: AdapterContext,
        url: str,
    ) -> tuple[str | None, str]:
        key = ctx.settings.elsevier_api_key
        if not key:
            return None, "api_key_missing"
        try:
            response = await client.get(
                url,
                headers=self._api_headers(key, "text/xml"),
                params={"view": "META"},
            )
        except Exception as exc:
            return None, f"request_error:{type(exc).__name__}"
        if response.status_code != 200:
            return None, await _http_error_status(response)
        return response.text, "ok"

    async def _download_binary(
        self,
        client: httpx.AsyncClient,
        url: str,
        target: Path,
        *,
        api_key: str | None,
        accept: str = "*/*",
        link_text: str = "",
        expected_extension: str | None = None,
        referer: str | None = None,
    ) -> tuple[DownloadArtifact | None, str]:
        current = url
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        temporary.unlink(missing_ok=True)

        for _ in range(6):
            if not _allowed_elsevier_url(current):
                return None, "redirect_outside_elsevier_allowlist"
            host = (urlparse(current).hostname or "").lower()
            headers = {"Accept": accept}
            if referer:
                headers["Referer"] = referer
            if api_key and host == "api.elsevier.com":
                headers["X-ELS-APIKey"] = api_key
            try:
                async with client.stream(
                    "GET",
                    current,
                    headers=headers,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            return None, f"redirect_{response.status_code}_without_location"
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        return None, await _http_error_status(response)

                    content_length = response.headers.get("content-length")
                    if content_length and content_length.isdigit():
                        if int(content_length) > ELSEVIER_MAX_ATTACHMENT_BYTES:
                            return None, "attachment_too_large"

                    size = 0
                    head = bytearray()
                    with temporary.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > ELSEVIER_MAX_ATTACHMENT_BYTES:
                                temporary.unlink(missing_ok=True)
                                return None, "attachment_too_large"
                            if len(head) < 512:
                                head.extend(chunk[: 512 - len(head)])
                            handle.write(chunk)

                    content_type = response.headers.get("content-type", "")
                    disposition = response.headers.get("content-disposition", "")
                    extension, original_filename = resolve_download_extension(
                        current,
                        link_text,
                        declared_mime_type=content_type,
                        content_disposition=disposition,
                        head=bytes(head),
                    )
                    payload_error = obvious_error_payload(
                        bytes(head),
                        declared_mime_type=content_type,
                        extension=extension,
                    )
                    if payload_error:
                        temporary.unlink(missing_ok=True)
                        return None, payload_error
                    if expected_extension and extension != expected_extension:
                        temporary.unlink(missing_ok=True)
                        return None, f"unexpected_extension:{extension}"

                    valid, reason = validate_file(temporary, extension)
                    if not valid:
                        temporary.unlink(missing_ok=True)
                        return None, reason

                    final_target = target.with_suffix(extension)
                    temporary.replace(final_target)
                    response_headers = {
                        key: value
                        for key, value in {
                            "content-type": content_type,
                            "content-disposition": disposition,
                            "content-length": content_length or "",
                        }.items()
                        if value
                    }
                    return (
                        DownloadArtifact(
                            path=final_target,
                            extension=extension,
                            final_url=current,
                            original_filename=original_filename,
                            declared_mime_type=content_type.split(";", 1)[0].strip() or None,
                            content_disposition=disposition or None,
                            response_headers=response_headers,
                        ),
                        "ok",
                    )
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                return None, f"request_error:{type(exc).__name__}"
        return None, "too_many_redirects"

    async def _pii_from_tab(self, tab) -> str | None:
        for selector in (
            'meta[name="citation_pii"]',
            'meta[name="pii"]',
            'meta[property="pii"]',
        ):
            try:
                element = await asyncio.wait_for(
                    tab.query(selector, timeout=2, raise_exc=False),
                    timeout=3,
                )
            except Exception as exc:
                if self.is_browser_disconnect(exc):
                    raise
                element = None
            if element:
                pii = _find_pii(element.get_attribute("content") or "")
                if pii:
                    return pii
        try:
            current_url = await asyncio.wait_for(tab.current_url, timeout=3)
            return _find_pii(current_url)
        except Exception as exc:
            if self.is_browser_disconnect(exc):
                raise
            return None

    async def _probe_public_candidate(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        referer: str,
    ) -> tuple[str, str, str]:
        current = url
        for _ in range(6):
            if not _allowed_elsevier_url(current):
                return "redirect_outside_elsevier_allowlist", "", current
            try:
                async with client.stream(
                    "GET",
                    current,
                    headers={
                        "Referer": referer,
                        "Accept": "*/*",
                        "Range": "bytes=0-511",
                    },
                    follow_redirects=False,
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            return (
                                f"redirect_{response.status_code}_without_location",
                                "",
                                current,
                            )
                        current = urljoin(current, location)
                        continue

                    content_type = response.headers.get("content-type", "").lower()
                    if response.status_code in {200, 206}:
                        if (
                            "text/html" in content_type
                            or "application/json" in content_type
                        ):
                            return "unexpected_probe_payload", content_type, current
                        return "found", content_type, current
                    if response.status_code in {404, 410}:
                        return "missing", content_type, current
                    return f"http_{response.status_code}", content_type, current
            except Exception as exc:
                return f"request_error:{type(exc).__name__}", "", current
        return "too_many_redirects", "", current

    async def _probe_public_mmc(
        self,
        client: httpx.AsyncClient,
        pii: str,
        *,
        referer: str,
    ) -> tuple[list[dict[str, str]], int, str | None]:
        candidates: list[dict[str, str]] = []
        probes = 0
        consecutive_missing = 0
        for number in range(1, 13):
            found = False
            for extension in MMC_EXTENSIONS:
                probes += 1
                url = build_mmc_url(pii, number, extension)
                outcome, content_type, final_url = await self._probe_public_candidate(
                    client,
                    url,
                    referer=referer,
                )
                if outcome == "found":
                    candidates.append(
                        {
                            "url": final_url,
                            "text": f"{pii}-mmc{number}{extension}",
                            "filename": f"{pii}-mmc{number}{extension}",
                            "mimetype": content_type,
                            "ref": f"mmc{number}",
                            "source": "public_cdn_pii_probe",
                        }
                    )
                    found = True
                    break
                if outcome != "missing":
                    return candidates, probes, outcome
                await asyncio.sleep(0.2)
            if found:
                consecutive_missing = 0
            else:
                consecutive_missing += 1
                if consecutive_missing >= 2:
                    break
        return candidates, probes, None

    async def run(self, ctx: AdapterContext) -> ArticleResult:
        key = ctx.settings.elsevier_api_key
        article_api_url = f"{ELSEVIER_API_BASE}/article/doi/{ctx.doi}"
        browser_navigated = False

        timeout = httpx.Timeout(
            connect=20,
            read=max(60, ctx.settings.blob_download_timeout_seconds),
            write=30,
            pool=20,
        )
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            article_payload, article_meta_status = await self._api_json(
                client, ctx, article_api_url
            )
            metadata = parse_article_metadata(article_payload)

            article_url = metadata.get("article_url") or f"https://doi.org/{ctx.doi}"
            journal = metadata.get("journal") or "Unknown Journal"
            title = metadata.get("title") or None
            pii = metadata.get("pii") or None
            pii_source = "article_retrieval_metadata" if pii else None

            metadata_xml_status = "not_needed"
            if not pii:
                metadata_xml, metadata_xml_status = await self._article_metadata_xml(
                    client,
                    ctx,
                    article_api_url,
                )
                pii = _find_pii(metadata_xml or "") or pii
                if pii:
                    pii_source = "article_retrieval_xml"

            metadata_search_status = "not_needed"
            if not pii:
                search_payload, metadata_search_status = (
                    await self._metadata_search_json(client, ctx)
                )
                search_metadata = parse_article_metadata(search_payload)
                article_url = search_metadata.get("article_url") or article_url
                journal = search_metadata.get("journal") or journal
                title = search_metadata.get("title") or title
                pii = search_metadata.get("pii") or pii
                if pii:
                    pii_source = "article_metadata_search"

            if journal == "Unknown Journal" or not pii:
                article_url = await self.navigate(ctx, cloudflare=True)
                browser_navigated = True
                journal = await self.journal_from_meta(ctx.tab, journal)
                try:
                    title = title or await asyncio.wait_for(ctx.tab.title, timeout=3)
                except Exception as exc:
                    if self.is_browser_disconnect(exc):
                        raise
                pii = pii or await self._pii_from_tab(ctx.tab)
                if pii and pii_source is None:
                    pii_source = "browser_metadata"

            if pii:
                article_url = (
                    "https://www.sciencedirect.com/science/article/pii/"
                    f"{pii}"
                )

            _, paper_dir, si_dir = self.dirs(ctx, journal)
            result = ArticleResult(
                doi=ctx.doi,
                publisher=self.publisher_name,
                journal=journal,
                article_url=article_url,
                title=title,
            )
            result.diagnostics.update(
                {
                    "elsevier_api_key_configured": bool(key),
                    "elsevier_article_metadata_api": article_meta_status,
                    "elsevier_article_metadata_xml_api": metadata_xml_status,
                    "elsevier_article_metadata_search_api": metadata_search_status,
                    "elsevier_main_pdf_strategy": "article_retrieval_api_only",
                    "elsevier_si_strategy": "public_cdn_pii_mmc_probe_only",
                    "elsevier_browser_used_for_metadata_only": browser_navigated,
                    "elsevier_pii": pii,
                    "elsevier_pii_source": pii_source,
                }
            )

            paper_target = paper_dir / f"{doi_to_filename(ctx.doi)}.pdf"
            result.paper = self.existing_paper_result(ctx)
            if result.paper is None:
                if key:
                    paper_api_url = build_article_pdf_url(ctx.doi)
                    paper_artifact, paper_status = await self._download_binary(
                        client,
                        paper_api_url,
                        paper_target,
                        api_key=key,
                        accept="application/pdf",
                        expected_extension=".pdf",
                    )
                else:
                    paper_artifact, paper_status = None, "api_key_missing"
                result.diagnostics["elsevier_article_pdf_api"] = paper_status
                if paper_artifact:
                    result.paper = self.file_result(
                        "paper",
                        paper_artifact,
                        paper_api_url,
                        "elsevier_article_retrieval_api",
                        extension=".pdf",
                    )
            else:
                result.diagnostics["elsevier_article_pdf_api"] = "existing_valid_pdf"

            probe_count = 0
            probe_error = "pii_unavailable"
            candidates: list[dict[str, str]] = []
            if pii:
                candidates, probe_count, probe_error = await self._probe_public_mmc(
                    client,
                    pii,
                    referer=result.article_url,
                )

            result.diagnostics["elsevier_public_cdn_probes"] = probe_count
            result.diagnostics["elsevier_public_cdn_candidates"] = len(candidates)
            result.diagnostics["elsevier_public_cdn_probe_error"] = probe_error

            for item in candidates:
                url = item["url"]
                hint = item["filename"]
                ext = infer_extension(url, hint)
                target_si = self.si_target(si_dir, ctx.doi, url, ext)
                existing = self.existing_file_result(ctx, "si", target_si, url, ext)
                if existing:
                    result.si.append(existing)
                    continue

                artifact, download_status = await self._download_binary(
                    client,
                    url,
                    target_si,
                    api_key=None,
                    accept=item.get("mimetype") or "*/*",
                    link_text=hint,
                    referer=result.article_url,
                )
                result.si.append(
                    self.file_result(
                        "si",
                        artifact,
                        url,
                        "public_cdn_pii_probe",
                        extension=ext,
                        error=download_status,
                    )
                )

            result.diagnostics["si_scan_complete"] = probe_error is None
            result.diagnostics["si_scan_strategy"] = "public_cdn_pii_mmc_probe"

            if result.paper is None:
                paper_status = result.diagnostics.get("elsevier_article_pdf_api")
                if str(paper_status).startswith(("http_401", "http_403")):
                    result.message = (
                        "Elsevier Article Retrieval API denied the main PDF for this "
                        "key/network entitlement; SI discovery was still attempted."
                    )
                elif paper_status == "api_key_missing":
                    result.message = (
                        "ELSEVIER_API_KEY is not configured in the server process; "
                        "the main PDF requires the Article Retrieval API."
                    )
                else:
                    result.message = (
                        "Elsevier main PDF download failed; see "
                        "elsevier_article_pdf_api for the exact API outcome."
                    )
            return result

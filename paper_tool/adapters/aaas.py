from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import unquote, urlparse

from .base import AdapterContext, PublisherAdapter
from ..download import DownloadArtifact, blob_download, native_navigation_download
from ..models import ArticleResult
from ..resources import infer_extension
from ..storage import doi_to_filename


def _unique_links(items: list[dict]) -> list[dict]:
    selected: list[dict] = []
    seen: set[str] = set()
    for item in items:
        url = str(item.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        selected.append(item)
    return selected


def select_aaas_reader(items: list[dict]) -> dict | None:
    for item in _unique_links(items):
        if "/doi/reader/" in str(item.get("url") or ""):
            return item
    return None


def select_aaas_pdf(items: list[dict]) -> dict | None:
    for item in _unique_links(items):
        if "/doi/pdf/" in str(item.get("url") or ""):
            return item
    return None


def select_aaas_si(items: list[dict]) -> list[dict]:
    return [
        item
        for item in _unique_links(items)
        if "/doi/suppl/" in str(item.get("url") or "")
        and "/suppl_file/" in str(item.get("url") or "")
    ]


def _native_artifact(path: Path, url: str, extension: str) -> DownloadArtifact:
    name = Path(unquote(urlparse(url).path.rstrip("/"))).name or None
    return DownloadArtifact(
        path=path,
        extension=extension,
        final_url=url,
        original_filename=name,
    )


async def _download(
    ctx: AdapterContext,
    tab,
    url: str,
    target: Path,
    extension: str,
    *,
    link_text: str = "",
) -> tuple[DownloadArtifact | Path | None, str]:
    artifact = await blob_download(
        tab,
        ctx.worker.staging_dir,
        url,
        target,
        min(ctx.settings.blob_download_timeout_seconds, 75),
        link_text=link_text,
    )
    if artifact is not None:
        return artifact, "fetch_blob"

    path = await native_navigation_download(
        ctx.worker,
        url,
        target,
        min(ctx.settings.article_timeout_seconds, 180),
    )
    if path is None:
        return None, "fetch_blob_then_native_navigation_failed"
    return _native_artifact(path, url, extension), "native_navigation_fallback"


class AAASAdapter(PublisherAdapter):
    key = "AAAS"
    publisher_name = "AAAS"

    @classmethod
    def matches_doi(cls, doi: str) -> bool:
        return doi.startswith("10.1126/")

    async def _restore_article(self, ctx: AdapterContext, article_url: str) -> None:
        tab = ctx.tab
        try:
            await asyncio.wait_for(
                tab.go_to(article_url),
                timeout=ctx.settings.navigation_timeout_seconds,
            )
        except Exception:
            pass
        await asyncio.sleep(ctx.settings.settle_seconds)
        marker = await tab.query(
            'meta[name="citation_title"], a[href*="/doi/reader/"]',
            timeout=8,
            raise_exc=False,
        )
        if marker:
            return
        await self.navigate(ctx, cloudflare=True)

    async def run(self, ctx: AdapterContext) -> ArticleResult:
        article_url = await self.navigate(ctx, cloudflare=True)
        tab = ctx.tab
        journal = await self.journal_from_meta(tab, "Science Advances")
        _, paper_dir, si_dir = self.dirs(ctx, journal)
        result = ArticleResult(
            doi=ctx.doi,
            publisher=self.publisher_name,
            journal=journal,
            article_url=article_url,
            title=await tab.title,
        )

        result.paper = self.existing_paper_result(ctx)
        if result.paper is None:
            reader_candidates = await self.collect_links(
                tab,
                'div.info-panel__formats a[href*="/doi/reader/"][aria-label="PDF"], '
                'a[href*="/doi/reader/"][data-original-title="PDF"]',
            )
            reader = select_aaas_reader(reader_candidates)
            if reader:
                try:
                    await asyncio.wait_for(
                        tab.go_to(reader["url"]),
                        timeout=ctx.settings.navigation_timeout_seconds,
                    )
                except Exception:
                    pass
                await tab.query(
                    'a.navbar-download[href*="/doi/pdf/"], '
                    'a[data-download-files-key="pdf"][href*="/doi/pdf/"]',
                    timeout=30,
                    raise_exc=False,
                )
                pdf_candidates = await self.collect_links(
                    tab,
                    'a.navbar-download[href*="/doi/pdf/"], '
                    'a[data-download-files-key="pdf"][href*="/doi/pdf/"]',
                )
                paper = select_aaas_pdf(pdf_candidates)
                if paper:
                    pdf_url = paper["url"]
                    target = paper_dir / f"{doi_to_filename(ctx.doi)}.pdf"
                    path, method = await _download(
                        ctx,
                        tab,
                        pdf_url,
                        target,
                        ".pdf",
                    )
                    result.paper = self.file_result(
                        "paper", path, pdf_url, method, extension=".pdf"
                    )

            await self._restore_article(ctx, article_url)

        heading = await tab.query(
            '//h2[contains(normalize-space(.), "Supplementary Materials")]',
            timeout=12,
            raise_exc=False,
        )
        candidates = await self.collect_links(
            tab,
            'a[href*="/doi/suppl/"][href*="/suppl_file/"]',
        )
        si_links = select_aaas_si(candidates)
        result.diagnostics["aaas_supplementary_heading_found"] = bool(heading)
        result.diagnostics["aaas_si_candidates"] = len(si_links)

        for item in si_links:
            url = item["url"]
            extension = infer_extension(url, item["text"])
            target = self.si_target(si_dir, ctx.doi, url, extension)
            existing = self.existing_file_result(ctx, "si", target, url, extension)
            if existing:
                result.si.append(existing)
                continue
            path, method = await _download(
                ctx,
                tab,
                url,
                target,
                extension,
                link_text=item["text"],
            )
            result.si.append(
                self.file_result("si", path, url, method, extension=extension)
            )

        result.diagnostics["si_scan_complete"] = True
        return result

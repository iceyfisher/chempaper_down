from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

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


def select_aip_pdf(items: list[dict]) -> dict | None:
    for item in _unique_links(items):
        url = str(item.get("url") or "")
        if "/article-pdf/" in url or url.split("?", 1)[0].lower().endswith(".pdf"):
            return item
    return None


def select_aip_si(items: list[dict]) -> list[dict]:
    return [
        item
        for item in _unique_links(items)
        if "/article-supplement/" in str(item.get("url") or "")
    ]


def infer_aip_extension(url: str, text: str = "") -> str:
    extension = infer_extension(url, text)
    if extension != ".bin":
        return extension
    path = urlparse(url).path.lower()
    for route, candidate in (("/zip/", ".zip"), ("/pdf/", ".pdf")):
        if route in path:
            return candidate
    return extension


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
    url: str,
    target: Path,
    extension: str,
    *,
    link_text: str = "",
) -> tuple[DownloadArtifact | Path | None, str]:
    artifact = await blob_download(
        ctx.tab,
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


class AIPAdapter(PublisherAdapter):
    key = "AIP"
    publisher_name = "AIP Publishing"

    @classmethod
    def matches_doi(cls, doi: str) -> bool:
        return doi.startswith("10.1063/")

    async def run(self, ctx: AdapterContext) -> ArticleResult:
        article_url = await self.navigate(ctx, cloudflare=True)
        tab = ctx.tab
        journal = await self.journal_from_meta(tab, "The Journal of Chemical Physics")
        _, paper_dir, si_dir = self.dirs(ctx, journal)
        result = ArticleResult(
            doi=ctx.doi,
            publisher=self.publisher_name,
            journal=journal,
            article_url=article_url,
            title=await tab.title,
        )

        result.paper = self.existing_paper_result(ctx)
        current_url = await tab.current_url
        abstract_only = "/article-abstract/" in current_url
        if abstract_only and result.paper is None:
            result.message = (
                "AIP redirected this session to the abstract-only page; "
                "authorized subscription access is required for the main PDF."
            )
            result.diagnostics["aip_access"] = "abstract_only"
        if result.paper is None:
            candidates = await self.collect_links(
                tab,
                'a[data-doctype="contentPdf"][href], '
                'a.article-pdfLink[href], '
                'a[href*="/article-pdf/"]',
            )
            citation_pdf = await tab.query(
                'meta[name="citation_pdf_url"]', timeout=2, raise_exc=False
            )
            if citation_pdf:
                content = citation_pdf.get_attribute("content") or ""
                if content:
                    candidates.append(
                        {"url": urljoin(article_url, content), "text": "citation PDF"}
                    )
            paper = select_aip_pdf(candidates)
            result.diagnostics["aip_pdf_candidates"] = len(candidates)
            if paper and not abstract_only:
                pdf_url = paper["url"]
                target = paper_dir / f"{doi_to_filename(ctx.doi)}.pdf"
                path, method = await _download(ctx, pdf_url, target, ".pdf")
                result.paper = self.file_result(
                    "paper", path, pdf_url, method, extension=".pdf"
                )

        typed = await self.collect_links(
            tab,
            'div.dataSuppLink a[data-doctype="dataSupplementDoc"][href], '
            'a[data-doctype="dataSupplementDoc"][href]',
        )
        routed = await self.collect_links(tab, 'a[href*="/article-supplement/"]')
        si_links = select_aip_si(typed + routed)
        result.diagnostics["aip_si_candidates"] = len(si_links)

        for item in si_links:
            url = item["url"]
            extension = infer_aip_extension(url, item["text"])
            target = self.si_target(si_dir, ctx.doi, url, extension)
            existing = self.existing_file_result(ctx, "si", target, url, extension)
            if existing:
                result.si.append(existing)
                continue
            path, method = await _download(
                ctx,
                url,
                target,
                extension,
                link_text=item["text"],
            )
            result.si.append(
                self.file_result("si", path, url, method, extension=extension)
            )

        result.diagnostics["si_scan_complete"] = True
        if result.paper is None and not result.message:
            try:
                raw_text = await tab.execute_script(
                    "return document.body ? document.body.innerText.slice(0, 12000) : '';",
                    return_by_value=True,
                )
            except Exception:
                raw_text = ""
            visible = str(raw_text).lower()
            if "available to purchase" in visible:
                result.message = (
                    "AIP article is available to purchase; the current browser "
                    "session has no authorized PDF access."
                )
        return result

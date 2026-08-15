from __future__ import annotations

import asyncio
from urllib.parse import urljoin, urlparse

from .base import AdapterContext, PublisherAdapter
from ..download import blob_download, click_element_and_wait, native_navigation_download
from ..models import ArticleResult
from ..resources import infer_extension
from ..storage import doi_to_filename


RSC_FALLBACK = {
    "qo": "Organic Chemistry Frontiers",
    "ra": "RSC Advances",
    "cc": "Chemical Communications",
    "dt": "Dalton Transactions",
    "cp": "Physical Chemistry Chemical Physics",
    "ta": "Journal of Materials Chemistry A",
    "tb": "Journal of Materials Chemistry B",
    "tc": "Journal of Materials Chemistry C",
}


class RSCAdapter(PublisherAdapter):
    key = "RSC"
    publisher_name = "Royal Society of Chemistry"

    @classmethod
    def matches_doi(cls, doi: str) -> bool:
        return doi.startswith("10.1039/")

    async def run(self, ctx: AdapterContext) -> ArticleResult:
        article_url = await self.navigate(ctx, cloudflare=True)
        tab = ctx.tab
        access_issue = await self.access_issue(tab)
        if access_issue:
            return ArticleResult(
                doi=ctx.doi,
                publisher=self.publisher_name,
                article_url=article_url,
                title=await asyncio.wait_for(tab.title, timeout=3),
                message=access_issue,
                diagnostics={"access_issue": "publisher_challenge"},
            )
        pdf_discovery_error = None
        try:
            pdf_element = await asyncio.wait_for(
                tab.query(
                    'a[data-doctype="contentPdf"][href*="/article-pdf/"]',
                    timeout=min(ctx.settings.article_timeout_seconds, 20),
                    raise_exc=False,
                ),
                timeout=min(ctx.settings.article_timeout_seconds, 22),
            )
        except Exception as exc:
            if self.is_browser_disconnect(exc):
                raise
            pdf_element = None
            pdf_discovery_error = f"{type(exc).__name__}: {exc}"
        fallback = "Unknown Journal"
        current_url = await asyncio.wait_for(tab.current_url, timeout=3)
        parts = [x.lower() for x in urlparse(current_url).path.split("/") if x]
        for part in parts:
            if part in RSC_FALLBACK:
                fallback = RSC_FALLBACK[part]
                break
        journal = await self.journal_from_meta(tab, fallback)
        _, paper_dir, si_dir = self.dirs(ctx, journal)
        result = ArticleResult(
            doi=ctx.doi,
            publisher=self.publisher_name,
            journal=journal,
            article_url=article_url,
            title=await asyncio.wait_for(tab.title, timeout=3),
        )
        if pdf_discovery_error:
            result.diagnostics["rsc_pdf_discovery_error"] = pdf_discovery_error

        result.paper = self.existing_paper_result(ctx)
        if result.paper is None and pdf_element:
            href = pdf_element.get_attribute("href")
            pdf_url = (
                "https://pubs.rsc.org" + href
                if href and href.startswith("/")
                else urljoin(current_url, href or "")
            )
            target = paper_dir / f"{doi_to_filename(ctx.doi)}.pdf"
            path = await click_element_and_wait(
                ctx.worker,
                pdf_element,
                target,
                timeout=min(ctx.settings.native_download_timeout_seconds, 60),
            )
            method = "real_click"
            if path is None:
                path = await blob_download(
                    tab,
                    ctx.worker.staging_dir,
                    pdf_url,
                    target,
                    min(ctx.settings.blob_download_timeout_seconds, 75),
                    link_text="Download PDF",
                )
                method = "real_click_then_fetch_blob"
            if path is None:
                path = await native_navigation_download(
                    ctx.worker,
                    pdf_url,
                    target,
                    timeout=min(ctx.settings.native_download_timeout_seconds, 60),
                )
                method = "real_click_then_blob_then_native_navigation"
            result.paper = self.file_result(
                "paper", path, pdf_url, method, extension=".pdf"
            )

        si_links = await self.collect_links(tab, 'a[href*="/article-supplement/"]')
        for item in si_links:
            url = item["url"]
            if urlparse(url).netloc and "rsc.org" not in urlparse(url).netloc:
                continue
            ext = infer_extension(url, item["text"])
            target = self.si_target(si_dir, ctx.doi, url, ext)
            existing = self.existing_file_result(ctx, "si", target, url, ext)
            if existing:
                result.si.append(existing)
                continue
            path = await blob_download(
                tab,
                ctx.worker.staging_dir,
                url,
                target,
                min(ctx.settings.blob_download_timeout_seconds, 75),
                link_text=item["text"],
            )
            result.si.append(self.file_result("si", path, url, "fetch_blob", extension=ext))

        result.diagnostics["si_scan_complete"] = True
        return result

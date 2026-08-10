from __future__ import annotations

from urllib.parse import urljoin, urlparse

from .base import AdapterContext, PublisherAdapter
from ..download import blob_download, click_element_and_wait
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
        article_url = await self.navigate(ctx)
        tab = ctx.tab
        pdf_element = await tab.query(
            'a[data-doctype="contentPdf"][href*="/article-pdf/"]',
            timeout=min(ctx.settings.article_timeout_seconds, 60),
            raise_exc=False,
        )
        fallback = "Unknown Journal"
        parts = [x.lower() for x in urlparse(await tab.current_url).path.split("/") if x]
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
            title=await tab.title,
        )

        if pdf_element:
            href = pdf_element.get_attribute("href")
            pdf_url = ("https://pubs.rsc.org" + href) if href and href.startswith("/") else urljoin(await tab.current_url, href or "")
            target = paper_dir / f"{doi_to_filename(ctx.doi)}.pdf"
            path = await click_element_and_wait(
                ctx.worker,
                pdf_element,
                target,
                timeout=min(ctx.settings.native_download_timeout_seconds, 60),
            )
            result.paper = self.file_result("paper", path, pdf_url, "real_click", extension=".pdf")

        si_links = await self.collect_links(tab, 'a[href*="/article-supplement/"]')
        for idx, item in enumerate(si_links, 1):
            url = item["url"]
            if urlparse(url).netloc and "rsc.org" not in urlparse(url).netloc:
                continue
            ext = infer_extension(url, item["text"])
            target = si_dir / f"{doi_to_filename(ctx.doi)}_si_{idx:03d}{ext}"
            existing = self.existing_file_result("si", target, url, ext)
            if existing:
                result.si.append(existing)
                continue
            path = await blob_download(
                tab,
                ctx.worker.staging_dir,
                url,
                target,
                min(ctx.settings.blob_download_timeout_seconds, 75),
            )
            result.si.append(self.file_result("si", path, url, "fetch_blob", extension=ext))

        return result

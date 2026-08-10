from __future__ import annotations

from urllib.parse import urljoin

from .base import AdapterContext, PublisherAdapter
from ..download import blob_download
from ..models import ArticleResult
from ..resources import infer_extension
from ..storage import doi_to_filename


ACS_FALLBACK = {
    "acscatal": "ACS Catalysis",
    "orglett": "Organic Letters",
    "joc": "The Journal of Organic Chemistry",
    "jacs": "Journal of the American Chemical Society",
}


class ACSAdapter(PublisherAdapter):
    key = "ACS"
    publisher_name = "American Chemical Society"

    @classmethod
    def matches_doi(cls, doi: str) -> bool:
        return doi.startswith("10.1021/")

    async def run(self, ctx: AdapterContext) -> ArticleResult:
        article_url = await self.navigate(ctx, cloudflare=True)
        tab = ctx.tab
        suffix = ctx.doi.split("/", 1)[-1]
        fallback = ACS_FALLBACK.get(suffix.split(".", 1)[0], "Unknown Journal")
        journal = await self.journal_from_meta(tab, fallback)
        _, paper_dir, si_dir = self.dirs(ctx, journal)
        title = await tab.title

        result = ArticleResult(
            doi=ctx.doi,
            publisher=self.publisher_name,
            journal=journal,
            article_url=article_url,
            title=title,
        )

        paper_element = None
        for selector in (
            '//a[contains(normalize-space(.),"Open PDF")]',
            'a[href*="/article-pdf/"]',
        ):
            paper_element = await tab.query(selector, timeout=12, raise_exc=False)
            if paper_element:
                break
        if paper_element:
            href = paper_element.get_attribute("href")
            pdf_url = urljoin(await tab.current_url, href) if href else None
            if pdf_url:
                target = paper_dir / f"{doi_to_filename(ctx.doi)}.pdf"
                path = await blob_download(
                    tab, ctx.worker.staging_dir, pdf_url, target,
                    min(ctx.settings.blob_download_timeout_seconds, 75),
                )
                result.paper = self.file_result("paper", path, pdf_url, "fetch_blob", extension=".pdf")

        si_links = await self.collect_links(tab, 'a[data-doctype="dataSupplementDoc"]')
        if not si_links:
            si_links = await self.collect_links(tab, 'a[href*="/article-supplement/"]')

        for idx, item in enumerate(si_links, 1):
            ext = infer_extension(item["url"], item["text"])
            target = si_dir / f"{doi_to_filename(ctx.doi)}_si_{idx:03d}{ext}"
            existing = self.existing_file_result("si", target, item["url"], ext)
            if existing:
                result.si.append(existing)
                continue
            path = await blob_download(
                tab, ctx.worker.staging_dir, item["url"], target,
                min(ctx.settings.blob_download_timeout_seconds, 75),
            )
            result.si.append(self.file_result("si", path, item["url"], "fetch_blob", extension=ext))

        return result

from __future__ import annotations

import re

import httpx

from .base import AdapterContext, PublisherAdapter
from ..download import OriginBridgeManager, blob_download, native_navigation_download
from ..models import ArticleResult
from ..resources import IMAGE_EXTENSIONS, infer_extension
from ..storage import doi_to_filename


class ElsevierAdapter(PublisherAdapter):
    """Expandable Elsevier/ScienceDirect adapter.

    Production-ready part:
      1) DOI classification for common Elsevier DOI prefix 10.1016.
      2) Official Article Retrieval API PDF attempt when ELSEVIER_API_KEY is set.
      3) Entitlement failures simply fall through to browser discovery.

    Experimental/browser part:
      - ScienceDirect DOM changes frequently.  Candidate selectors and `mmcN`
        supplementary URLs are isolated here so the rest of the project remains
        unchanged when a tested DOI is supplied later.
    """

    key = "ELSEVIER"
    publisher_name = "Elsevier"

    @classmethod
    def matches_doi(cls, doi: str) -> bool:
        return doi.startswith("10.1016/")

    async def _api_pdf(self, ctx: AdapterContext, target):
        key = ctx.settings.elsevier_api_key
        if not key:
            return None
        url = f"https://api.elsevier.com/content/article/doi/{ctx.doi}"
        headers = {"X-ELS-APIKey": key, "Accept": "application/pdf"}
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                response = await client.get(url, headers=headers)
            if response.status_code == 200 and response.content.startswith(b"%PDF"):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(response.content)
                return target
        except Exception:
            pass
        return None

    async def run(self, ctx: AdapterContext) -> ArticleResult:
        article_url = await self.navigate(ctx)
        tab = ctx.tab
        journal = await self.journal_from_meta(tab, "Unknown Journal")
        _, paper_dir, si_dir = self.dirs(ctx, journal)
        result = ArticleResult(
            doi=ctx.doi,
            publisher=self.publisher_name,
            journal=journal,
            article_url=article_url,
            title=await tab.title,
        )

        target = paper_dir / f"{doi_to_filename(ctx.doi)}.pdf"
        path = await self._api_pdf(ctx, target)
        if path:
            result.paper = self.file_result(
                "paper", path,
                f"https://api.elsevier.com/content/article/doi/{ctx.doi}",
                "elsevier_article_retrieval_api",
                extension=".pdf",
            )
        else:
            # Experimental browser fallback.  Common ScienceDirect PDF routes use
            # `pdfft`/download URLs; keep selectors isolated for easy update.
            pdf_link = None
            for selector in (
                'a[href*="/pdfft"]',
                'a[href*="pdf"][href*="download"]',
                'a[aria-label*="PDF"]',
            ):
                pdf_link = await tab.query(selector, timeout=6, raise_exc=False)
                if pdf_link:
                    break
            if pdf_link:
                href = pdf_link.get_attribute("href") or ""
                from urllib.parse import urljoin
                pdf_url = urljoin(await tab.current_url, href)
                path = await native_navigation_download(
                    ctx.worker, pdf_url, target,
                    timeout=min(ctx.settings.native_download_timeout_seconds, 40),
                )
                result.paper = self.file_result("paper", path, pdf_url, "experimental_native", extension=".pdf")

        # Experimental SI candidate discovery: Elsevier supplementary assets often
        # expose mmc1/mmc2... resources on ars.els-cdn.com.  We intentionally collect
        # first, dedupe, then download so DOM navigation cannot make us miss files.
        links = await self.collect_links(tab, 'a[href]')
        candidates, seen = [], set()
        for item in links:
            url = item["url"]
            text = item["text"] or ""
            lower = (url + " " + text).lower()
            if (
                ("ars.els-cdn.com" in lower and re.search(r"-mmc\d+", lower))
                or "supplementary material" in lower
                or "supplementary data" in lower
            ):
                if url not in seen:
                    seen.add(url)
                    candidates.append(item)
        result.diagnostics["elsevier_experimental_si_candidates"] = len(candidates)

        bridges = OriginBridgeManager(ctx.worker, tab)
        try:
            for idx, item in enumerate(candidates, 1):
                url = item["url"]
                ext = infer_extension(url, item["text"])
                target_si = si_dir / f"{doi_to_filename(ctx.doi)}_si_{idx:03d}{ext}"
                existing = self.existing_file_result("si", target_si, url, ext)
                if existing:
                    result.si.append(existing)
                    continue
                if ext in IMAGE_EXTENSIONS:
                    bridge = await bridges.get(url)
                    path = await blob_download(
                        bridge, ctx.worker.staging_dir, url, target_si,
                        min(ctx.settings.blob_download_timeout_seconds, 60),
                    )
                    method = "experimental_same_origin_blob"
                else:
                    path = await native_navigation_download(
                        ctx.worker, url, target_si,
                        timeout=min(ctx.settings.native_download_timeout_seconds, 30),
                    )
                    method = "experimental_native"
                    if not path:
                        bridge = await bridges.get(url)
                        path = await blob_download(
                            bridge, ctx.worker.staging_dir, url, target_si,
                            min(ctx.settings.blob_download_timeout_seconds, 60),
                        )
                        method = "experimental_same_origin_blob"
                result.si.append(self.file_result("si", path, url, method, extension=ext))
        finally:
            await bridges.close()

        if result.paper is None:
            result.message = (
                "Elsevier adapter is partially implemented. Configure ELSEVIER_API_KEY "
                "for the official DOI Article Retrieval API, or provide a test DOI to "
                "stabilize current ScienceDirect PDF/SI DOM selectors."
            )
        return result

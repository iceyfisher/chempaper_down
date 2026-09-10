from __future__ import annotations

import asyncio

from .base import AdapterContext, PublisherAdapter
from ..download import _unwrap_script_result, blob_download, native_navigation_download
from ..models import ArticleResult
from ..storage import doi_to_filename


FIND_PDF_JS = """
(() => {
  const anchors = Array.from(document.querySelectorAll('a[href]'));
  const hit = anchors.find(a => /\.pdf(\?|$)/i.test(a.href))
    || anchors.find(a => /download\s+pdf/i.test(a.textContent || ''));
  return hit ? {url: hit.href, text: (hit.textContent || '').trim().slice(0, 60)} : null;
})()
"""


class ConfitAdapter(PublisherAdapter):
    """SSDM conference proceedings hosted on Japan's Confit system.

    10.7567/SSDM.* DOIs resolve to pub.confit.atlas.jp presentation pages.
    The paper file (an abstract-style proceedings PDF) is served from
    pub-files.atlas.jp, which 403s requests without the browser session, so
    the download must ride on the page that linked it.
    """

    key = "CONFIT"
    publisher_name = "SSDM Conference (Confit)"
    article_dom_selector = "a[href]"

    @classmethod
    def matches_doi(cls, doi: str) -> bool:
        return doi.lower().startswith("10.7567/ssdm.")

    async def run(self, ctx: AdapterContext) -> ArticleResult:
        tab = ctx.tab
        article_url = await self.navigate(ctx)
        result = ArticleResult(
            doi=ctx.doi,
            publisher=self.publisher_name,
            article_url=article_url,
        )
        try:
            title = await asyncio.wait_for(tab.title, timeout=5)
            result.title = title
        except Exception:
            pass

        pdf = None
        deadline = asyncio.get_running_loop().time() + 30
        while pdf is None and asyncio.get_running_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(
                    tab.execute_script(FIND_PDF_JS, return_by_value=True), timeout=10,
                )
                value = _unwrap_script_result(raw)
                if isinstance(value, dict) and value.get("url"):
                    pdf = value
                    break
            except Exception as exc:
                if self.is_browser_disconnect(exc):
                    raise
            await asyncio.sleep(2.0)
        if not isinstance(pdf, dict) or not pdf.get("url"):
            result.message = (
                "Confit presentation page did not expose a PDF download."
            )
            result.diagnostics["si_scan_complete"] = True
            return result

        pdf_url = pdf["url"]
        result.diagnostics["confit_pdf_url"] = pdf_url[:200]
        _, paper_dir, _ = self.dirs(ctx, "SSDM Conference", result.year)
        target = paper_dir / f"{doi_to_filename(ctx.doi)}.pdf"
        artifact = await blob_download(
            tab, ctx.worker.staging_dir, pdf_url, target,
            ctx.settings.blob_download_timeout_seconds, link_text=pdf.get("text", ""),
        )
        method = "confit_blob"
        if artifact is None:
            path = await native_navigation_download(
                ctx.worker, pdf_url, target,
                timeout=min(ctx.settings.native_download_timeout_seconds, 60),
            )
            artifact, method = path, "confit_native"
        result.paper = self.file_result(
            "paper", artifact, pdf_url, method, extension=".pdf"
        )
        if not result.paper.valid:
            result.message = "Confit PDF download failed."
        # SSDDM proceedings abstracts carry no supplementary files.
        result.diagnostics["si_scan_complete"] = True
        return result

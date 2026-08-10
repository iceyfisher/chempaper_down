from __future__ import annotations

import asyncio
from urllib.parse import urljoin

from .base import AdapterContext, PublisherAdapter
from ..browser import tab_identity
from ..download import blob_download, click_element_and_wait
from ..models import ArticleResult
from ..resources import infer_extension
from ..storage import doi_to_filename


WILEY_FALLBACK = {
    "anie": "Angewandte Chemie International Edition",
}


class WileyAdapter(PublisherAdapter):
    key = "WILEY"
    publisher_name = "Wiley"

    @classmethod
    def matches_doi(cls, doi: str) -> bool:
        return doi.startswith("10.1002/")

    async def _wait_reader(self, ctx: AdapterContext, old_ids: set[str]):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 45
        while loop.time() < deadline:
            try:
                tabs = await ctx.browser.get_opened_tabs()
            except Exception:
                tabs = []
            candidates = [x for x in tabs if tab_identity(x) not in old_ids] + [ctx.tab]
            seen = set()
            for tab in candidates:
                ident = tab_identity(tab)
                if ident in seen:
                    continue
                seen.add(ident)
                button = await tab.query(
                    'button#new-download-btn[aria-label="Download"]', timeout=1, raise_exc=False
                )
                if button:
                    return tab, button
            await asyncio.sleep(0.4)
        return None, None

    async def _restore_article(self, ctx: AdapterContext, article_url: str) -> None:
        tab = ctx.tab
        marker = await tab.query('a[href*="/doi/epdf/"]', timeout=2, raise_exc=False)
        table = await tab.query('table.support-info__table', timeout=2, raise_exc=False)
        if marker or table:
            return
        try:
            await tab.execute_script("history.back();", user_gesture=True)
        except Exception:
            pass
        for _ in range(20):
            marker = await tab.query('a[href*="/doi/epdf/"]', timeout=1, raise_exc=False)
            table = await tab.query('table.support-info__table', timeout=1, raise_exc=False)
            if marker or table:
                return
            await asyncio.sleep(0.4)
        try:
            await asyncio.wait_for(tab.go_to(article_url), timeout=ctx.settings.navigation_timeout_seconds)
        except Exception:
            pass
        marker = await tab.query('a[href*="/doi/epdf/"]', timeout=20, raise_exc=False)
        if not marker:
            raise RuntimeError("Wiley article page could not be restored for SI")

    async def run(self, ctx: AdapterContext) -> ArticleResult:
        article_url = await self.navigate(ctx)
        tab = ctx.tab
        suffix = ctx.doi.split("/", 1)[-1]
        fallback = WILEY_FALLBACK.get(suffix.split(".", 1)[0].lower(), "Unknown Journal")
        journal = await self.journal_from_meta(tab, fallback)
        _, paper_dir, si_dir = self.dirs(ctx, journal)
        result = ArticleResult(
            doi=ctx.doi,
            publisher=self.publisher_name,
            journal=journal,
            article_url=article_url,
            title=await tab.title,
        )

        epdf = None
        for selector in (
            'a.pdf-download[href*="/doi/epdf/"]',
            'a[title="ePDF"][href*="/doi/epdf/"]',
            'a[href*="/doi/epdf/"]',
        ):
            epdf = await tab.query(selector, timeout=15, raise_exc=False)
            if epdf:
                break

        reader_tab = None
        if epdf:
            old_tabs = await ctx.browser.get_opened_tabs()
            old_ids = {tab_identity(x) for x in old_tabs}
            try:
                await epdf.click(humanize=True)
            except Exception:
                await epdf.execute_script("this.click()", user_gesture=True)

            reader_tab, button = await self._wait_reader(ctx, old_ids)
            if reader_tab and button:
                try:
                    await button.click(humanize=True)
                except Exception:
                    await button.execute_script("this.click()", user_gesture=True)
                await asyncio.sleep(0.8)
                pdf_link = await reader_tab.query(
                    '#download-popup a[data-download-files-key="pdf"]', timeout=12, raise_exc=False
                )
                if not pdf_link:
                    pdf_link = await reader_tab.query(
                        'a[href*="/doi/pdfdirect/"]', timeout=8, raise_exc=False
                    )
                if pdf_link:
                    href = pdf_link.get_attribute("href") or ""
                    pdf_url = urljoin(await reader_tab.current_url, href)
                    target = paper_dir / f"{doi_to_filename(ctx.doi)}.pdf"
                    # Verified Wiley behavior: PDF option can be visibility:hidden,
                    # 0x0 and therefore cannot be human-clicked, but DOM click works.
                    path = await click_element_and_wait(
                        ctx.worker,
                        pdf_link,
                        target,
                        timeout=min(ctx.settings.native_download_timeout_seconds, 60),
                        js_only=True,
                    )
                    result.paper = self.file_result(
                        "paper", path, pdf_url, "reader_webelement_js_click", extension=".pdf"
                    )

        if reader_tab is not None and tab_identity(reader_tab) != tab_identity(tab):
            try:
                await reader_tab.close()
            except Exception:
                pass

        # Critical: paper flow may leave the main tab in the ePDF reader. Always
        # restore the original article before discovering/downloading SI.
        await self._restore_article(ctx, article_url)

        si_links = await self.collect_links(tab, 'table.support-info__table a[href]')
        for idx, item in enumerate(si_links, 1):
            ext = infer_extension(item["url"], item["text"])
            target = si_dir / f"{doi_to_filename(ctx.doi)}_si_{idx:03d}{ext}"
            existing = self.existing_file_result("si", target, item["url"], ext)
            if existing:
                result.si.append(existing)
                continue
            # Verified stable path: extract all URLs on the article first and use
            # Blob downloads instead of clicking SI links and mutating page state.
            path = await blob_download(
                tab,
                ctx.worker.staging_dir,
                item["url"],
                target,
                min(ctx.settings.blob_download_timeout_seconds, 90),
            )
            result.si.append(self.file_result("si", path, item["url"], "fetch_blob", extension=ext))

        return result

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


WILEY_SI_DISCOVERY_JS = r"""
(() => {
  const records = [];
  const seen = new Set();
  const norm = value => (value || '').replace(/\s+/g, ' ').trim();
  function add(anchor, source) {
    if (!anchor || !anchor.href || seen.has(anchor.href)) return;
    seen.add(anchor.href);
    records.push({
      url: anchor.href,
      text: norm(anchor.textContent),
      download: norm(anchor.getAttribute('download')),
      context: norm(anchor.closest('tr, li, section, div')?.textContent),
      source
    });
  }
  const selectors = [
    ['table.support-info__table a[href]', 'support_table'],
    ['a[href*="action/downloadSupplement"]', 'download_supplement'],
    ['a[href*="/doi/supinfo/"]', 'doi_supinfo'],
    ['a[href*="-sup-"]', 'sup_filename'],
    ['a[download][href]', 'download_attribute']
  ];
  for (const [selector, source] of selectors) {
    document.querySelectorAll(selector).forEach(anchor => add(anchor, source));
  }
  document.querySelectorAll('a[href]').forEach(anchor => {
    const text = norm(anchor.textContent + ' ' + anchor.getAttribute('download'));
    if (/supporting information|supplementary (material|data)/i.test(text)) {
      add(anchor, 'supporting_text');
    }
  });
  return {records};
})()
"""


def _unwrap(value):
    if not isinstance(value, dict):
        return value
    if "type" in value and "value" in value:
        return value["value"]
    if "result" in value:
        return _unwrap(value["result"])
    if "value" in value:
        return value["value"]
    return value


def select_wiley_si(records: list[dict]) -> list[dict]:
    selected: list[dict] = []
    seen: set[str] = set()
    for item in records:
        url = str(item.get("url") or "")
        text = " ".join(
            str(item.get(key) or "") for key in ("text", "download", "context")
        )
        lower = (url + " " + text).lower()
        source = item.get("source")
        if any(route in lower for route in ("/doi/pdfdirect/", "/doi/epdf/", "/doi/pdf/")):
            continue
        is_file = source in {
            "support_table", "download_supplement", "doi_supinfo", "sup_filename"
        } or any(
            marker in lower
            for marker in (
                "action/downloadsupplement", "/doi/supinfo/", "-sup-",
                "suppmat", "misc_information",
            )
        )
        if not is_file and source in {"download_attribute", "supporting_text"}:
            is_file = infer_extension(url, text) != ".bin"
        if not url or not is_file or url in seen:
            continue
        seen.add(url)
        selected.append({"url": url, "text": text.strip()})
    return selected


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

        result.paper = self.existing_paper_result(ctx)
        reader_tab = None
        if result.paper is None:
            epdf = None
            for selector in (
                'a.pdf-download[href*="/doi/epdf/"]',
                'a[title="ePDF"][href*="/doi/epdf/"]',
                'a[href*="/doi/epdf/"]',
            ):
                epdf = await tab.query(selector, timeout=15, raise_exc=False)
                if epdf:
                    break

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

        await tab.query(
            'table.support-info__table, a[href*="action/downloadSupplement"], a[href*="-sup-"]',
            timeout=30,
            raise_exc=False,
        )
        raw = await tab.execute_script(WILEY_SI_DISCOVERY_JS, return_by_value=True)
        records = (_unwrap(raw) or {}).get("records", [])
        si_links = select_wiley_si(records)
        result.diagnostics["wiley_raw_si_candidates"] = len(records)
        result.diagnostics["wiley_selected_si"] = len(si_links)
        for item in si_links:
            ext = infer_extension(item["url"], item["text"])
            target = self.si_target(si_dir, ctx.doi, item["url"], ext)
            existing = self.existing_file_result(ctx, "si", target, item["url"], ext)
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
                min(ctx.settings.wiley_article_timeout_seconds, 300),
                link_text=item["text"],
            )
            result.si.append(self.file_result("si", path, item["url"], "fetch_blob", extension=ext))

        result.diagnostics["si_scan_complete"] = True
        return result

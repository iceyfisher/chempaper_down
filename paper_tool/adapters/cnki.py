from __future__ import annotations

import asyncio
import re
from urllib.parse import quote_plus, urljoin

from .base import AdapterContext, PublisherAdapter
from ..download import click_element_and_wait, native_navigation_download
from ..models import ArticleResult
from ..resources import infer_extension
from ..storage import doi_to_filename


CNKI_SEARCH_URL = "https://kns.cnki.net/kns8s/defaultresult/index?dbcode=CJFQ&korder=SU&kw={query}"
CNKI_FALLBACK_SEARCH_URL = "https://search.cnki.com.cn/Search/Result?content={query}"

# Anchors pointing at real article detail pages on any CNKI host.
ARTICLE_LINK_PATTERNS = (
    "/kcms2/article/",
    "kcms/detail/detail.aspx",
    "KXReader/Detail",
    "mall.cnki.net/magazine/Article/",
)

RESULT_DISCOVERY_JS = r"""
(() => {
  const patterns = [
    /\/kcms2\/article\//,
    /kcms\/detail\/detail\.aspx/,
    /KXReader\/Detail/,
    /mall\.cnki\.net\/magazine\/Article\//
  ];
  const records = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href || '';
    if (!patterns.some(p => p.test(href)) || seen.has(href)) continue;
    seen.add(href);
    const row = a.closest('tr, li, div');
    records.push({
      url: href,
      text: (a.textContent || '').replace(/\s+/g, ' ').trim(),
      rowText: row ? (row.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 500) : ''
    });
  }
  return { records: records };
})()
"""

ARTICLE_METADATA_JS = r"""
(() => {
  const body = document.body ? document.body.innerText.slice(0, 20000) : '';
  const doiMatch = body.match(/DOI\s*[:：]\s*(10\.\d{4,9}\/[^\s,;，；]+)/i);
  let doiFromLink = '';
  const doiAnchor = document.querySelector('a[href*="doi.org/"]');
  if (doiAnchor) {
    const href = doiAnchor.getAttribute('href') || '';
    const idx = href.indexOf('doi.org/');
    if (idx >= 0) doiFromLink = href.slice(idx + 8);
  }
  const h1 = document.querySelector('h1');
  const journalMeta = document.querySelector('meta[name="citation_journal_title"]');
  return {
    doi: (doiMatch && doiMatch[1]) || doiFromLink || '',
    title: h1 ? (h1.textContent || '').replace(/\s+/g, ' ').trim() : '',
    journal: journalMeta ? (journalMeta.getAttribute('content') || '') : ''
  };
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


def normalize_cnki_doi(value: str) -> str:
    return (value or "").strip().strip(".").lower()


class CnkiAdapter(PublisherAdapter):
    """CNKI (知网) articles.

    CNKI papers have no Supporting Information in this pipeline: the adapter
    only retrieves the main article PDF plus the DOI / Chinese title metadata.
    It also acts as the last-resort adapter for DOIs no other publisher claims:
    the DOI is searched on CNKI and the matched article is downloaded.
    """

    key = "CNKI"
    publisher_name = "CNKI 中国知网"

    @classmethod
    def matches_doi(cls, doi: str) -> bool:
        # Explicit cnki: keys and any DOI no other adapter claimed.
        return True

    async def run(self, ctx: AdapterContext) -> ArticleResult:
        tab = ctx.tab
        article_url = await self._open_article(ctx)
        result = ArticleResult(
            doi=ctx.doi,
            publisher=self.publisher_name,
            article_url=article_url,
        )

        access_issue = await self.access_issue(tab)
        if access_issue:
            result.message = access_issue
            result.diagnostics["access_issue"] = "cnki_challenge"
            result.diagnostics["si_scan_complete"] = True
            return result

        metadata = await self._article_metadata(tab)
        result.title = metadata.get("title") or None
        if metadata.get("journal"):
            result.journal = metadata["journal"]
        found_doi = normalize_cnki_doi(metadata.get("doi") or "")
        if found_doi:
            result.diagnostics["cnki_doi"] = found_doi

        year = await self.year_from_meta(tab) or self._year_from_text(
            metadata.get("title") or ""
        )
        result.year = year
        _, paper_dir, _si_dir = self.dirs(ctx, result.journal or "CNKI", year)

        if not ctx.doi.startswith("cnki:") and found_doi:
            if found_doi != normalize_cnki_doi(ctx.doi):
                result.message = (
                    f"CNKI returned a different article (DOI {found_doi or 'unknown'}) "
                    f"for requested {ctx.doi}."
                )
                result.diagnostics["si_scan_complete"] = True
                return result

        target = paper_dir / f"{doi_to_filename(ctx.doi)}.pdf"
        path = await self._download_pdf(ctx, target)
        if path is None:
            result.message = (
                "CNKI article page was reached but no PDF/CAJ download could be "
                "completed; campus network entitlement or CAPTCHA may be required."
            )
            result.diagnostics["cnki_download"] = "failed"
        else:
            ext = infer_extension(str(path), "")
            result.diagnostics["cnki_download"] = "ok"
            result.paper = self.file_result(
                "paper", path, article_url, "cnki_click_download", extension=ext or ".pdf"
            )

        # CNKI articles carry no supporting information by design.
        result.diagnostics["si_scan_complete"] = True
        result.diagnostics["si_scan_strategy"] = "cnki_no_si_by_design"
        return result

    async def _open_article(self, ctx: AdapterContext) -> str:
        tab = ctx.tab
        if ctx.article_url_hint:
            return await self._navigate(ctx, ctx.article_url_hint)

        query = ctx.title_query or ctx.doi
        for template in (CNKI_SEARCH_URL, CNKI_FALLBACK_SEARCH_URL):
            url = template.format(query=quote_plus(query))
            current = await self._navigate(ctx, url)
            await asyncio.sleep(1.5)
            try:
                raw = await asyncio.wait_for(
                    tab.execute_script(RESULT_DISCOVERY_JS, return_by_value=True),
                    timeout=10,
                )
            except Exception as exc:
                if self.is_browser_disconnect(exc):
                    raise
                raw = None
            records = (_unwrap(raw) or {}).get("records") or []
            if records:
                ctx.navigation_diagnostics["cnki_search_source"] = template.split("?")[0]
                ctx.navigation_diagnostics["cnki_search_hits"] = len(records)
                return await self._navigate(ctx, urljoin(current, records[0]["url"]))
        raise RuntimeError(
            f"CNKI search returned no article page for query: {query!r}"
        )

    async def _navigate(self, ctx: AdapterContext, url: str) -> str:
        tab = ctx.tab
        try:
            await asyncio.wait_for(
                tab.go_to(url), timeout=ctx.settings.navigation_timeout_seconds
            )
        except Exception as exc:
            if self.is_browser_disconnect(exc):
                raise
        await asyncio.sleep(ctx.settings.settle_seconds)
        try:
            return await asyncio.wait_for(tab.current_url, timeout=3)
        except Exception:
            return url

    async def _article_metadata(self, tab) -> dict:
        try:
            raw = await asyncio.wait_for(
                tab.execute_script(ARTICLE_METADATA_JS, return_by_value=True),
                timeout=8,
            )
        except Exception as exc:
            if self.is_browser_disconnect(exc):
                raise
            return {}
        data = _unwrap(raw)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _year_from_text(text: str) -> str | None:
        match = re.search(r"(19|20)\d{2}", text or "")
        return match.group(0) if match else None

    async def _download_pdf(self, ctx: AdapterContext, target):
        tab = ctx.tab
        selectors = (
            "a#pdfDown",
            "a.btn-dlpdf",
            'a[href*="/kcms2/article/download"]',
            'a[href*="/article/download"]',
            'a[title*="PDF"]',
            "a#cajDown",
        )
        for selector in selectors:
            element = await tab.query(selector, timeout=3, raise_exc=False)
            if not element:
                continue
            path = await click_element_and_wait(
                ctx.worker,
                element,
                target,
                timeout=min(ctx.settings.native_download_timeout_seconds, 60),
                js_only=True,
            )
            if path is not None:
                return path
            try:
                href = element.get_attribute("href") or ""
            except Exception:
                href = ""
            if href.startswith(("http", "/")):
                url = urljoin(await tab.current_url, href)
                path = await native_navigation_download(
                    ctx.worker,
                    url,
                    target,
                    timeout=min(ctx.settings.native_download_timeout_seconds, 60),
                )
                if path is not None:
                    return path
        return None

from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin

from .base import AdapterContext, PublisherAdapter
from ..cnki_search import search_on_tab
from ..download import click_element_and_wait, native_navigation_download
from ..models import ArticleResult
from ..resources import infer_extension
from ..storage import doi_to_filename


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

# Enumerates every visible download entry (栏目) on a CNKI article page and
# ranks them: explicit PDF buttons first, CAJ next, generic download links
# and HTML-reading entries last. The Python side clicks them in this order.
DOWNLOAD_SCAN_JS = r"""
(() => {
  const entries = [];
  const seen = new Set();
  const classify = (el) => {
    const id = (el.id || '').toLowerCase();
    const cls = (el.className || '').toString().toLowerCase();
    const title = (el.title || el.getAttribute('title') || '').toLowerCase();
    const text = (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 30).toLowerCase();
    const href = (el.getAttribute('href') || '').toLowerCase();
    const bag = id + ' ' + cls + ' ' + title + ' ' + text + ' ' + href;
    if (/pdfdown|btn-dlpdf|dl-pdf|download\?dflag\=pdf|pdfdown/.test(bag)) return 100;
    if (/^pdf$/.test(text) || /^pdf下载$/.test(text) || title === 'pdf下载') return 92;
    if (/cajdown|btn-dlcaj|dl-caj/.test(bag)) return 72;
    if (/^caj$/.test(text) || /^caj下载$/.test(text)) return 64;
    if (href.indexOf('download') >= 0) return 44;
    if (/下载/.test(text) && /全文|正文/.test(text)) return 40;
    if (/html阅读|^html$/.test(text)) return 22;
    return -1;
  };
  document.querySelectorAll('a, button, [role="button"]').forEach(el => {
    if (!el.getClientRects().length && el.offsetParent === null) return;
    const score = classify(el);
    if (score < 20) return;
    const href = el.getAttribute('href') || '';
    const key = (el.id || '') + '|' + (el.className || '') + '|' + href + '|' + score;
    if (seen.has(key)) return;
    seen.add(key);
    entries.push({
      score: score,
      id: el.id || null,
      cls: (el.className || '').toString().trim().slice(0, 60) || null,
      text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 30) || null,
      href: href.slice(0, 200) || null
    });
  });
  entries.sort((a, b) => b.score - a.score);
  return { entries: entries.slice(0, 10) };
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
            url = await self._navigate(ctx, ctx.article_url_hint)
            from ..cnki_search import _handle_captcha_if_present

            await _handle_captcha_if_present(tab)
            return url

        query = ctx.title_query or ctx.doi
        rows, attempts = await search_on_tab(
            tab, query, ctx.settings, ctx.settings.settle_seconds
        )
        ctx.navigation_diagnostics["cnki_search_attempts"] = attempts
        if rows:
            ctx.navigation_diagnostics["cnki_search_source"] = rows[0].get("url")
            url = await self._navigate(ctx, urljoin(await self._current_url(tab), rows[0]["url"]))
            from ..cnki_search import _handle_captcha_if_present

            await _handle_captcha_if_present(tab)
            return url

        detail = "; ".join(
            f"{a.get('url')}: {a.get('records', 0)} hits, page={a.get('page_title', '')!r}"
            for a in attempts
        )
        raise RuntimeError(
            f"CNKI search returned no article page for query {query!r} ({detail})"
        )

    async def _current_url(self, tab) -> str:
        try:
            return await asyncio.wait_for(tab.current_url, timeout=3)
        except Exception:
            return ""

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
        """Scan the article page DOM for every download entry (栏目) and try
        them in priority order.

        CNKI renders different download entries per content type (journal
        article / thesis / conference): PDF buttons (#pdfDown, .btn-dlpdf),
        CAJ buttons, generic /download links. Instead of a fixed selector
        list, we enumerate all visible anchors/buttons whose id, class,
        title, text or href matches download patterns, score them (explicit
        PDF entries first, CAJ next, generic download links last), then click
        each candidate — falling back to navigating its href when the click
        yields no file. The scan inventory and the winning entry are recorded
        in the navigation diagnostics.
        """

        tab = ctx.tab
        timeout = min(ctx.settings.native_download_timeout_seconds, 60)

        try:
            raw = await asyncio.wait_for(
                tab.execute_script(DOWNLOAD_SCAN_JS, return_by_value=True),
                timeout=10,
            )
        except Exception as exc:
            if self.is_browser_disconnect(exc):
                raise
            raw = None
        entries = (_unwrap(raw) or {}).get("entries") or []
        ctx.navigation_diagnostics["cnki_download_entries"] = entries

        for index, entry in enumerate(entries):
            element = await self._locate_entry(tab, entry)
            if element is None:
                continue
            path = await click_element_and_wait(
                ctx.worker, element, target, timeout=timeout, js_only=True
            )
            if path is not None:
                ctx.navigation_diagnostics["cnki_download_entry"] = entry
                return path
            href = entry.get("href") or ""
            if href.startswith(("http", "/")):
                url = urljoin(await tab.current_url, href)
                path = await native_navigation_download(
                    ctx.worker, url, target, timeout=timeout
                )
                if path is not None:
                    ctx.navigation_diagnostics["cnki_download_entry"] = entry
                    return path
            # keep the article page usable for the next candidate
            if index < len(entries) - 1:
                try:
                    await tab.execute_script("history.back();", user_gesture=True)
                    await asyncio.sleep(ctx.settings.settle_seconds)
                except Exception:
                    pass
        return None

    async def _locate_entry(self, tab, entry: dict):
        """Re-locate a scanned entry element by its most specific attribute."""

        probes = []
        if entry.get("id"):
            probes.append(f"#{entry['id']}")
        href = entry.get("href") or ""
        if href:
            probes.append(f'a[href="{href}"]')
        cls = (entry.get("cls") or "").strip()
        if cls:
            first_class = cls.split()[0]
            if re.match(r"^[A-Za-z_-][\w-]*$", first_class):
                probes.append(f".{first_class}")
        if entry.get("text"):
            probes.append(f'a[title*="{entry["text"][:10]}"]')
        for probe in probes:
            try:
                element = await tab.query(probe, timeout=2, raise_exc=False)
            except Exception:
                element = None
            if element:
                return element
        return None

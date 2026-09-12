from __future__ import annotations

import asyncio
import re

from .base import AdapterContext, PublisherAdapter
from ..download import blob_download, native_navigation_download
from ..models import ArticleResult
from ..resources import infer_extension
from ..storage import doi_to_filename, looks_like_truncated_paper


DOCUMENT_URL_RE = re.compile(r"/document/(\d+)")
STAMP_URL = "https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber={arn}"

METADATA_WAIT_JS = """
(() => {
  const g = window.xplGlobal && window.xplGlobal.document
    ? window.xplGlobal.document.metadata : null;
  const stamp = document.querySelector('a[href*="/stamp/stamp.jsp"]');
  return {
    ready: !!(g && stamp),
    stamp: stamp ? stamp.getAttribute('href') : null,
    doi: g ? g.doi : null,
    title: g ? g.title : null,
    journal: g ? g.publicationTitle : null,
    year: g ? String(g.publicationYear || g.publicationDate || '') : null,
    startPage: g ? String(g.startPage || '') : '',
    endPage: g ? String(g.endPage || '') : ''
  };
})()
"""


class IeeeAdapter(PublisherAdapter):
    """IEEE Xplore articles.

    Verified download chain on ieeexplore.ieee.org (an Angular SPA):

    1. The article page exposes the PDF through an anchor styled as a button:
       ``a.xpl-btn-pdf...[href="/stamp/stamp.jsp?tp=&arnumber=<arn>"]``. The
       SPA hydrates slowly, so the adapter polls until the stamp anchor and
       the ``xplGlobal.document.metadata`` state object are both present.
    2. Metadata (DOI/title/journal/year) lives in ``xplGlobal`` only — IEEE
       does not inject citation <meta> tags into the SPA DOM.
    3. Navigating the stamp URL redirects (302) to the actual file at
       ``https://ieeexplore.ieee.org/ielx<...>/<arn>.pdf?...`` which downloads
       directly because the worker sets ``open_pdf_externally = True``.
    4. After the download the article tab goes one step back in history.

    Xplore serves Error 418 ("Unusual Traffic") to headless sessions, so this
    adapter runs on the headful persistent-profile worker (same as CNKI).

    IEEE articles carry no supporting information in this pipeline; only the
    main PDF is downloaded.
    """

    key = "IEEE"
    publisher_name = "IEEE Xplore"

    @classmethod
    def matches_doi(cls, doi: str) -> bool:
        # 10.23919 is IEEE's newer conference prefix; both resolve into Xplore.
        return doi.startswith(("10.1109/", "10.23919/"))

    async def _wait_metadata_and_stamp(self, ctx: AdapterContext, timeout: float) -> dict:
        """Poll until the SPA exposes xplGlobal metadata and the stamp anchor."""

        deadline = asyncio.get_running_loop().time() + timeout
        data: dict = {}
        while True:
            try:
                raw = await asyncio.wait_for(
                    ctx.tab.execute_script(METADATA_WAIT_JS, return_by_value=True),
                    timeout=5,
                )
                data = _unwrap_local(raw) or {}
            except Exception as exc:
                if self.is_browser_disconnect(exc):
                    raise
                data = {}
            if data.get("ready"):
                return data
            if asyncio.get_running_loop().time() >= deadline:
                return data
            await asyncio.sleep(1.0)

    async def run(self, ctx: AdapterContext) -> ArticleResult:
        tab = ctx.tab
        article_url = await self.navigate(ctx)
        result = ArticleResult(
            doi=ctx.doi,
            publisher=self.publisher_name,
            article_url=article_url,
        )

        arn_match = DOCUMENT_URL_RE.search(article_url or "")
        if not arn_match:
            # doi.org could not resolve (e.g. a placeholder 10.1109/<digits>):
            # an all-numeric suffix is the Xplore article number itself.
            suffix = ctx.doi.split("/", 1)[-1]
            if suffix.isdigit():
                article_url = f"https://ieeexplore.ieee.org/document/{suffix}"
                try:
                    await asyncio.wait_for(
                        tab.go_to(article_url),
                        timeout=ctx.settings.navigation_timeout_seconds,
                    )
                except Exception as exc:
                    if self.is_browser_disconnect(exc):
                        raise
                arn_match = re.match(r"^(\d+)$", suffix)
        if not arn_match:
            result.message = (
                "IEEE article page did not expose a document number; the DOI "
                "redirect may have been blocked before reaching Xplore."
            )
            result.diagnostics["si_scan_complete"] = True
            return result
        arn = arn_match.group(1)
        result.diagnostics["ieee_arnumber"] = arn

        access_issue = await self.access_issue(tab)
        if access_issue:
            result.message = access_issue
            result.diagnostics["access_issue"] = "ieee_challenge"
            result.diagnostics["si_scan_complete"] = True
            return result

        meta = await self._wait_metadata_and_stamp(
            ctx, min(30.0, ctx.settings.navigation_timeout_seconds * 1.5)
        )

        # xplGlobal carries the authoritative metadata (the SPA has no
        # citation <meta> tags); it also corrects placeholder DOIs.
        result.title = meta.get("title") or None
        result.journal = meta.get("journal") or None
        result.year = meta.get("year") or (await self.year_from_meta(tab))
        real_doi = (meta.get("doi") or "").strip().lower()
        if real_doi and real_doi != ctx.doi:
            result.diagnostics["ieee_doi_from_page"] = real_doi
            if ctx.doi.split("/")[0] == "10.1109" and "/" not in ctx.doi.split("/", 1)[1]:
                # placeholder like 10.1109/9144185: archive under the real DOI
                result.doi = real_doi

        stamp_href = meta.get("stamp") or f"/stamp/stamp.jsp?tp=&arnumber={arn}"
        from urllib.parse import urljoin

        stamp_url = urljoin("https://ieeexplore.ieee.org/", stamp_href)

        # Supplementary files, when present, hang off the article page's
        # "Supplementary Files" section — anchors whose href or text say so.
        # Collect them before the stamp navigation replaces the page.
        si_links = await self._collect_supplementary_links(tab)

        doi_for_name = result.doi or ctx.doi
        target = paper_dir = None
        _, paper_dir, _si_dir = self.dirs(
            ctx, result.journal or "IEEE", result.year
        )
        target = paper_dir / f"{doi_to_filename(doi_for_name)}.pdf"
        # Conference PDFs are multi-megabyte; two parallel workers share the
        # link, so allow a full minute before declaring the stamp download dead.
        timeout = min(max(ctx.settings.native_download_timeout_seconds, 60), 90)

        # The stamp page embeds the actual file in an iframe. Xplore reshaped
        # this over time: older articles point at ielx*.pdf, current ones at
        # /stampPDF/getPDF.jsp?tp=&arnumber=<arn>, which itself redirects to
        # the PDF file. NOTE: query each pattern separately — pydoll's
        # iframe-crossing splitter misparses a comma list whose segments start
        # with the `iframe` tag.
        pdf_url = None
        try:
            await asyncio.wait_for(
                tab.go_to(stamp_url), timeout=ctx.settings.navigation_timeout_seconds
            )
        except Exception as exc:
            if self.is_browser_disconnect(exc):
                raise
        deadline = asyncio.get_running_loop().time() + 20
        while pdf_url is None and asyncio.get_running_loop().time() < deadline:
            iframe = None
            for selector in (
                'iframe[src*="ielx"]',
                'iframe[src*="getPDF.jsp"]',
                'iframe[src*="/stampPDF/"]',
            ):
                iframe = await tab.query(selector, timeout=3, raise_exc=False)
                if iframe:
                    break
            if iframe:
                src = iframe.get_attribute("src") or ""
                if src:
                    pdf_url = urljoin("https://ieeexplore.ieee.org/", src)
                    break
            await asyncio.sleep(1.0)
        if pdf_url:
            result.diagnostics["ieee_pdf_url"] = pdf_url[:200]
            path = await native_navigation_download(ctx.worker, pdf_url, target, timeout)
            method = "ieee_stamp_iframe"
        else:
            path = None
            method = "ieee_stamp_iframe"
        # Restore the article tab regardless of the outcome.
        try:
            await tab.execute_script("history.back();", user_gesture=True)
        except Exception:
            pass

        if path is None:
            result.message = (
                "IEEE Xplore was reached but the PDF could not be downloaded; "
                "campus-network entitlement or CAPTCHA verification may be "
                "required for this article."
            )
            result.diagnostics["ieee_download"] = "failed"
        else:
            result.diagnostics["ieee_download"] = "ok"
            result.paper = self.file_result(
                "paper", path, stamp_url, method, extension=".pdf"
            )
            # Old scanned articles are sometimes served as a one-page preview.
            first = re.search(r"\d+", str(meta.get("startPage") or ""))
            last = re.search(r"\d+", str(meta.get("endPage") or ""))
            if first and last:
                span = int(last.group(0)) - int(first.group(0)) + 1
                result.diagnostics["expected_page_span"] = span
                truncated = looks_like_truncated_paper(path, span)
                if truncated:
                    result.paper.valid = False
                    result.paper.error = "preview_or_truncated_pdf"
                    result.diagnostics["paper_page_check"] = truncated

        # history.back() above restored the article page; fetch any
        # supplementary files found there through the session that holds the
        # campus entitlement.
        for si_url, si_text in si_links:
            ext = infer_extension(si_url, si_text)
            _, _, si_dir = self.dirs(ctx, result.journal or "IEEE", result.year)
            target = self.si_target(si_dir, doi_for_name, si_url, ext)
            artifact = await blob_download(
                tab, ctx.worker.staging_dir, si_url, target,
                ctx.settings.blob_download_timeout_seconds, link_text=si_text,
            )
            result.si.append(
                self.file_result("si", artifact, si_url, "ieee_supplementary",
                                 extension=ext)
            )
        result.diagnostics["ieee_si_links"] = len(si_links)
        result.diagnostics["si_scan_complete"] = True
        result.diagnostics["si_scan_strategy"] = "ieee_supplementary_links"
        return result

    async def _collect_supplementary_links(self, tab) -> list[tuple[str, str]]:
        try:
            raw = await asyncio.wait_for(
                tab.execute_script(
                    """
                    (() => {
                      const hits = [];
                      for (const a of document.querySelectorAll('a[href]')) {
                        const text = (a.textContent || '').trim();
                        const href = a.href || '';
                        if (/supplement/i.test(text) || /supplement/i.test(href)) {
                          hits.push({url: href, text: text.slice(0, 80)});
                        }
                      }
                      return hits;
                    })()
                    """,
                    return_by_value=True,
                ),
                timeout=10,
            )
        except Exception as exc:
            if self.is_browser_disconnect(exc):
                raise
            return []
        data = _unwrap_local(raw) or []
        seen, result = set(), []
        for item in data if isinstance(data, list) else []:
            url = (item or {}).get("url") or ""
            host = url.split("/")[2] if url.count("/") >= 2 else ""
            if not host.endswith("ieee.org"):
                continue  # supplementary files are served from ieee.org hosts
            if url and url not in seen:
                seen.add(url)
                result.append((url, (item or {}).get("text") or ""))
        return result


def _unwrap_local(value):
    if not isinstance(value, dict):
        return value
    if "type" in value and "value" in value:
        return _unwrap_local(value["value"])
    if "result" in value:
        return _unwrap_local(value["result"])
    if "value" in value:
        return value["value"]
    return value

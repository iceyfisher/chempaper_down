from __future__ import annotations

import asyncio
import re

from .base import AdapterContext, PublisherAdapter
from ..download import native_navigation_download
from ..models import ArticleResult
from ..storage import doi_to_filename


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
    year: g ? String(g.publicationYear || g.publicationDate || '') : null
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
        return doi.startswith("10.1109/")

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

        doi_for_name = result.doi or ctx.doi
        target = paper_dir = None
        _, paper_dir, _si_dir = self.dirs(
            ctx, result.journal or "IEEE", result.year
        )
        target = paper_dir / f"{doi_to_filename(doi_for_name)}.pdf"
        timeout = min(ctx.settings.native_download_timeout_seconds, 60)

        # The stamp page embeds the actual file in an iframe whose src points
        # at ielx*.pdf (e.g. /ielx7/6287639/8948470/09144185.pdf?...). Extract
        # it, download via a fresh tab, then step back to the article page.
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
            iframe = await tab.query('iframe[src*="ielx"]', timeout=3, raise_exc=False)
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

        # IEEE articles carry no supporting information by design.
        result.diagnostics["si_scan_complete"] = True
        result.diagnostics["si_scan_strategy"] = "ieee_no_si_by_design"
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

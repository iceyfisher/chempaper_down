from __future__ import annotations

import asyncio
import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from pydoll.exceptions import BrowserException, ConnectionException
from pydoll.protocol.page.events import PageEvent

from ..browser import BrowserWorker
from ..config import Settings
from ..download import DownloadArtifact
from ..models import ArticleResult, FileResult
from ..storage import doi_to_filename, make_article_dirs, sha256_file, validate_file


@dataclass(slots=True)
class AdapterContext:
    worker: BrowserWorker
    settings: Settings
    doi: str
    existing_paper: Path | None = None
    previous_manifest: dict | None = None
    want_si: bool = True
    title_query: str | None = None
    article_url_hint: str | None = None
    navigation_diagnostics: dict[str, object] = field(default_factory=dict)

    @property
    def tab(self):
        return self.worker.main_tab

    @property
    def browser(self):
        return self.worker.browser


class PublisherAdapter(ABC):
    key = "BASE"
    publisher_name = "Unknown Publisher"
    article_dom_selector = (
        'meta[name="citation_title"], '
        'meta[name="citation_pdf_url"], '
        'a[data-doctype="contentPdf"], '
        'a[href*="/article-pdf/"], '
        'a[href*="/doi/epdf/"], '
        'a[href*="/doi/pdf/"]'
    )

    @classmethod
    @abstractmethod
    def matches_doi(cls, doi: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def run(self, ctx: AdapterContext) -> ArticleResult:
        raise NotImplementedError

    async def navigate(self, ctx: AdapterContext, *, cloudflare: bool = False) -> str:
        """Navigate with short soft limits; the parent subprocess timeout is hard.

        The Pydoll Cloudflare helper is opt-in per adapter. When enabled it is
        armed *before* navigation, exactly like Pydoll's public
        ``expect_and_bypass_cloudflare_captcha`` context manager: the handler runs
        on the challenge page's load event and clicks the Turnstile widget while
        Cloudflare is still rendering it. That context manager itself is not used
        because its Page.disable cleanup has blocked for 60s on affected RSC
        pages after Edge was already gone; the callback is removed here under a
        watchdog instead.
        """

        url = f"https://doi.org/{ctx.doi}"
        tab = ctx.tab
        helper = None
        if cloudflare:
            ctx.navigation_diagnostics["cloudflare_wait_seconds"] = (
                ctx.settings.cloudflare_timeout_seconds
            )
            if ctx.settings.enable_pydoll_cloudflare_helper:
                helper = await self.arm_cloudflare_helper(ctx)
                ctx.navigation_diagnostics["pydoll_cloudflare_helper"] = (
                    "armed" if helper else "unavailable"
                )
            else:
                ctx.navigation_diagnostics["pydoll_cloudflare_helper"] = "disabled"

        try:
            try:
                await asyncio.wait_for(
                    tab.go_to(url), timeout=ctx.settings.navigation_timeout_seconds
                )
            except Exception as exc:
                if self.is_browser_disconnect(exc):
                    raise

            await asyncio.sleep(ctx.settings.settle_seconds)

            if helper is not None:
                await self.run_cloudflare_helper(ctx, helper)

            try:
                return await asyncio.wait_for(tab.current_url, timeout=3)
            except Exception as exc:
                if self.is_browser_disconnect(exc):
                    raise
                return url
        finally:
            if helper is not None:
                await self.disarm_cloudflare_helper(tab, helper)

    async def arm_cloudflare_helper(
        self,
        ctx: AdapterContext,
        *,
        tab=None,
        diagnostics: dict | None = None,
    ) -> dict | None:
        """Subscribe Pydoll's Turnstile handler to the next page load event.

        The handler has to be registered before navigation: Cloudflare injects
        the widget after the load event and re-renders it during the proof of
        work, so a handler invoked only after the page settled (or after a
        refresh) can miss the challenge window entirely. Secondary tabs may pass
        their own *tab* and *diagnostics* so they do not overwrite the article
        page's navigation record.
        """

        tab = ctx.tab if tab is None else tab
        diag = ctx.navigation_diagnostics if diagnostics is None else diagnostics
        bypass = getattr(tab, "_bypass_cloudflare", None)
        if bypass is None:
            diag["pydoll_cloudflare_error"] = (
                "Pydoll Cloudflare handler is unavailable"
            )
            return None

        done = asyncio.Event()

        async def handler(event):
            try:
                await bypass(
                    event,
                    time_to_wait_captcha=ctx.settings.cloudflare_timeout_seconds,
                )
            finally:
                done.set()

        try:
            if not tab.page_events_enabled:
                await asyncio.wait_for(tab.enable_page_events(), timeout=5)
            callback_id = await asyncio.wait_for(
                tab.on(PageEvent.LOAD_EVENT_FIRED, handler), timeout=5
            )
        except Exception as exc:
            if self.is_browser_disconnect(exc):
                raise
            diag["pydoll_cloudflare_error"] = repr(exc)
            return None
        return {"callback_id": callback_id, "done": done}

    async def run_cloudflare_helper(
        self,
        ctx: AdapterContext,
        helper: dict,
        *,
        tab=None,
        diagnostics: dict | None = None,
    ) -> None:
        tab = ctx.tab if tab is None else tab
        diag = ctx.navigation_diagnostics if diagnostics is None else diagnostics
        article_ready = await self.wait_for_article_dom(
            tab,
            timeout=min(3.0, ctx.settings.cloudflare_timeout_seconds),
        )
        diag["article_dom_before_cloudflare_helper"] = article_ready
        if article_ready:
            diag["pydoll_cloudflare_helper"] = "not_needed"
        else:
            try:
                await asyncio.wait_for(
                    helper["done"].wait(),
                    timeout=ctx.settings.cloudflare_timeout_seconds + 1,
                )
            except Exception as exc:
                if self.is_browser_disconnect(exc):
                    raise
                diag["pydoll_cloudflare_error"] = repr(exc)
            # Pydoll returns after the challenge interaction, while Cloudflare may
            # still be redirecting and building the article DOM. Do not classify
            # that intermediate page as a hard failure.
            article_ready = await self.wait_for_article_dom(
                tab,
                timeout=ctx.settings.cloudflare_timeout_seconds,
            )
        diag["article_dom_after_cloudflare_helper"] = article_ready

    @staticmethod
    async def disarm_cloudflare_helper(tab, helper: dict) -> None:
        try:
            await asyncio.wait_for(
                tab.remove_callback(helper["callback_id"]), timeout=3
            )
        except Exception:
            pass

    MANUAL_CHALLENGE_MARKERS = (
        "captcha",
        "just a moment",
        "checking your browser",
        "bot manager",
        "attention required",
        "security verification",
        "请稍候",
        "正在验证",
        "验证您是真人",
    )

    async def page_challenge_state(self, tab) -> str | None:
        """Return the visible challenge marker on the page, if any."""

        try:
            title = (await asyncio.wait_for(tab.title, timeout=3) or "").lower()
        except Exception as exc:
            if self.is_browser_disconnect(exc):
                raise
            return "unknown"
        for marker in self.MANUAL_CHALLENGE_MARKERS:
            if marker in title:
                return marker
        return None

    async def wait_for_manual_challenge(
        self, ctx: AdapterContext, *, tab=None, timeout: float | None = None
    ) -> bool:
        """Wait for the operator to clear a challenge we cannot auto-solve.

        Some publishers gate content behind hCaptcha or custom text captchas
        (IOP's Radware Bot Manager, Optica). The download browser is visible,
        so a human can solve the challenge once; the persistent profile keeps
        that clearance for the rest of the batch. In a headless session there
        is nobody to solve it, so give up immediately.
        """

        tab = ctx.tab if tab is None else tab
        wait = ctx.settings.manual_captcha_wait_seconds if timeout is None else timeout
        if getattr(ctx.worker, "headless", True) or wait <= 0:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        ctx.navigation_diagnostics.setdefault("manual_challenge_waits", []).append(
            {"wait_seconds": wait}
        )
        while True:
            marker = await self.page_challenge_state(tab)
            if marker is None:
                cleared = await self.wait_for_article_dom(
                    tab, timeout=ctx.settings.cloudflare_timeout_seconds
                )
                if cleared:
                    ctx.navigation_diagnostics["manual_challenge_cleared"] = True
                    return True
            if loop.time() >= deadline:
                ctx.navigation_diagnostics["manual_challenge_cleared"] = False
                return False
            await asyncio.sleep(2.0)

    async def wait_for_article_dom(self, tab, *, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                marker = await asyncio.wait_for(
                    tab.query(
                        self.article_dom_selector,
                        timeout=1,
                        raise_exc=False,
                    ),
                    timeout=min(2.0, remaining),
                )
            except Exception as exc:
                if self.is_browser_disconnect(exc):
                    raise
                marker = None
            if marker:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.5)

    @staticmethod
    def is_browser_disconnect(exc: BaseException) -> bool:
        if isinstance(exc, (ConnectionError, ConnectionException, BrowserException)):
            return True
        return getattr(exc, "winerror", None) in {10053, 10054, 10061, 1225}

    async def journal_from_meta(self, tab, fallback: str = "Unknown Journal") -> str:
        selectors = [
            'meta[name="citation_journal_title"]',
            'meta[name="DC.Source"]',
            'meta[name="dc.Source"]',
        ]
        for selector in selectors:
            try:
                element = await asyncio.wait_for(
                    tab.query(selector, timeout=2, raise_exc=False),
                    timeout=3,
                )
            except Exception as exc:
                if self.is_browser_disconnect(exc):
                    raise
                element = None
            if element:
                value = element.get_attribute("content")
                if value:
                    return value.strip()
        return fallback

    async def title_from_meta(self, tab, fallback: str | None = None) -> str | None:
        for selector in ('meta[name="citation_title"]', 'meta[name="DC.Title"]'):
            try:
                element = await asyncio.wait_for(
                    tab.query(selector, timeout=2, raise_exc=False),
                    timeout=3,
                )
            except Exception as exc:
                if self.is_browser_disconnect(exc):
                    raise
                element = None
            if element:
                value = element.get_attribute("content")
                if value and value.strip():
                    return value.strip()
        return fallback

    async def year_from_meta(self, tab) -> str | None:
        """Best-effort publication year from citation meta tags."""

        selectors = [
            'meta[name="citation_publication_date"]',
            'meta[name="citation_date"]',
            'meta[name="DC.Date"]',
            'meta[name="dc.Date"]',
            'meta[name="prism.publicationDate"]',
            'meta[name="citation_online_date"]',
        ]
        for selector in selectors:
            try:
                element = await asyncio.wait_for(
                    tab.query(selector, timeout=1, raise_exc=False),
                    timeout=2,
                )
            except Exception as exc:
                if self.is_browser_disconnect(exc):
                    raise
                element = None
            if element:
                value = element.get_attribute("content") or ""
                match = re.search(r"(19|20)\d{2}", value)
                if match:
                    return match.group(0)
        return None

    def dirs(self, ctx: AdapterContext, journal: str, year: str | None = None) -> tuple[Path, Path, Path]:
        label = self.article_label(ctx)
        return make_article_dirs(ctx.settings.download_root, label, year, journal)

    def article_label(self, ctx: AdapterContext) -> str:
        return ctx.doi

    async def access_issue(self, tab) -> str | None:
        try:
            title = await asyncio.wait_for(tab.title, timeout=3)
        except Exception as exc:
            if self.is_browser_disconnect(exc):
                raise
            title = ""
        try:
            raw = await asyncio.wait_for(
                tab.execute_script(
                    "return document.body ? document.body.innerText.slice(0, 12000) : '';",
                    return_by_value=True,
                ),
                timeout=3,
            )
        except Exception as exc:
            if self.is_browser_disconnect(exc):
                raise
            raw = ""
        if isinstance(raw, dict):
            raw = raw.get("value") or raw.get("result") or raw
        visible = f"{title}\n{raw}".lower()
        challenge_markers = (
            "just a moment",
            "checking your browser",
            "verify you are human",
            "attention required",
            "security verification",
            "bot manager",
            "captcha",
            "请稍候",
            "正在验证",
            "验证您是真人",
        )
        if any(marker in visible for marker in challenge_markers):
            return (
                "Publisher access challenge is still present in the current Pydoll "
                "browser session; the article DOM is not available."
            )
        if "access denied" in visible or "访问被拒绝" in visible:
            return "Publisher denied access in the current network session."
        return None

    def si_target(self, si_dir: Path, doi: str, source_url: str, extension: str) -> Path:
        digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:10]
        return si_dir / f"{doi_to_filename(doi)}_si_{digest}{extension}"

    def existing_paper_result(self, ctx: AdapterContext) -> FileResult | None:
        path = ctx.existing_paper
        if path is None or not path.exists():
            return None
        valid, _ = validate_file(path, ".pdf")
        if not valid:
            return None
        return FileResult(
            kind="paper",
            path=str(path),
            method="existing_main_pdf_for_si_resume",
            extension=".pdf",
            size=path.stat().st_size,
            sha256=sha256_file(path),
            valid=True,
            existing=True,
        )

    async def collect_links(self, tab, selector: str, base_url: str | None = None) -> list[dict]:
        try:
            elements = await asyncio.wait_for(
                tab.query(selector, timeout=15, find_all=True, raise_exc=False),
                timeout=20,
            )
        except Exception as exc:
            if self.is_browser_disconnect(exc):
                raise
            elements = None
        if not elements:
            return []
        base = base_url or await asyncio.wait_for(tab.current_url, timeout=3)
        result, seen = [], set()
        for element in elements:
            href = element.get_attribute("href")
            if not href:
                continue
            absolute = urljoin(base, href)
            if absolute in seen:
                continue
            seen.add(absolute)
            try:
                text = await asyncio.wait_for(element.text, timeout=3)
            except Exception:
                text = ""
            result.append({"url": absolute, "text": text, "element": element})
        return result


    def existing_file_result(
        self,
        ctx: AdapterContext,
        kind: str,
        target: Path,
        source_url: str,
        extension: str,
    ) -> FileResult | None:
        previous = ctx.previous_manifest or {}
        for item in previous.get("si") or []:
            if item.get("source_url") != source_url or not item.get("path"):
                continue
            candidate = Path(item["path"])
            actual_extension = item.get("extension") or candidate.suffix
            valid, _ = validate_file(candidate, actual_extension)
            if valid:
                return FileResult(
                    kind=kind,
                    path=str(candidate),
                    source_url=source_url,
                    method="already_exists_manifest_url_match",
                    extension=actual_extension,
                    size=candidate.stat().st_size,
                    sha256=sha256_file(candidate),
                    valid=True,
                    existing=True,
                    final_url=item.get("final_url"),
                    original_filename=item.get("original_filename"),
                    declared_mime_type=item.get("declared_mime_type"),
                    content_disposition=item.get("content_disposition"),
                    response_headers=item.get("response_headers") or {},
                )

        candidates = [target]
        candidates.extend(
            path for path in target.parent.glob(target.stem + ".*") if path != target
        )
        for candidate in candidates:
            actual_extension = candidate.suffix or extension
            valid, _ = validate_file(candidate, actual_extension)
            if valid:
                return FileResult(
                    kind=kind,
                    path=str(candidate),
                    source_url=source_url,
                    method="already_exists_stable_url_target",
                    extension=actual_extension,
                    size=candidate.stat().st_size,
                    sha256=sha256_file(candidate),
                    valid=True,
                    existing=True,
                )
        return None

    def file_result(self, kind: str, path: Path | DownloadArtifact | None, source_url: str, method: str,
                    *, extension: str | None = None, error: str | None = None) -> FileResult:
        from ..download import result_metadata
        artifact = path if isinstance(path, DownloadArtifact) else None
        actual_path = artifact.path if artifact else path
        actual_extension = artifact.extension if artifact else extension
        if actual_path and actual_path.exists():
            meta = result_metadata(actual_path, actual_extension)
            return FileResult(
                kind=kind,
                path=str(actual_path),
                source_url=source_url,
                method=method,
                extension=actual_extension or actual_path.suffix,
                size=meta["size"],
                sha256=meta["sha256"],
                valid=meta["valid"],
                error=None if meta["valid"] else meta["reason"],
                final_url=artifact.final_url if artifact else None,
                original_filename=artifact.original_filename if artifact else None,
                declared_mime_type=artifact.declared_mime_type if artifact else None,
                content_disposition=artifact.content_disposition if artifact else None,
                response_headers=artifact.response_headers if artifact else {},
            )
        return FileResult(
            kind=kind,
            source_url=source_url,
            method=method,
            extension=extension,
            valid=False,
            error=error or "download_failed",
        )

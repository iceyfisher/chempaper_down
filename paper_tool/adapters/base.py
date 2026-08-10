from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from ..browser import BrowserWorker
from ..config import Settings
from ..models import ArticleResult, FileResult
from ..storage import make_article_dirs, sha256_file, validate_file


@dataclass(slots=True)
class AdapterContext:
    worker: BrowserWorker
    settings: Settings
    doi: str

    @property
    def tab(self):
        return self.worker.main_tab

    @property
    def browser(self):
        return self.worker.browser


class PublisherAdapter(ABC):
    key = "BASE"
    publisher_name = "Unknown Publisher"

    @classmethod
    @abstractmethod
    def matches_doi(cls, doi: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def run(self, ctx: AdapterContext) -> ArticleResult:
        raise NotImplementedError

    async def navigate(self, ctx: AdapterContext, *, cloudflare: bool = False) -> str:
        """Navigate with short soft limits; the parent subprocess timeout is hard.

        The Pydoll Cloudflare helper is intentionally opt-in. In real ACS runs an
        iframe resolution failure could leave a Runtime.evaluate command blocked
        inside Pydoll. Normal DOI navigation is attempted first and publisher DOM
        markers decide whether the page is usable.
        """

        url = f"https://doi.org/{ctx.doi}"
        tab = ctx.tab

        try:
            await asyncio.wait_for(
                tab.go_to(url), timeout=ctx.settings.navigation_timeout_seconds
            )
        except Exception:
            pass

        await asyncio.sleep(ctx.settings.settle_seconds)

        if cloudflare and ctx.settings.enable_pydoll_cloudflare_helper:
            # Only invoke the helper when the article still has no obvious content.
            marker = await tab.query(
                'meta[name="citation_title"], a[href*="/article-pdf/"], a[href*="/doi/epdf/"]',
                timeout=2,
                raise_exc=False,
            )
            if not marker:
                try:
                    async with asyncio.timeout(ctx.settings.cloudflare_timeout_seconds + 3):
                        async with tab.expect_and_bypass_cloudflare_captcha(
                            time_to_wait_captcha=ctx.settings.cloudflare_timeout_seconds
                        ):
                            # Refresh current page instead of repeating the DOI
                            # redirect chain. Any internal hang is still contained
                            # by the one-DOI subprocess boundary.
                            try:
                                await tab.refresh()
                            except Exception:
                                pass
                except Exception:
                    pass

        try:
            return await tab.current_url
        except Exception:
            return url

    async def journal_from_meta(self, tab, fallback: str = "Unknown Journal") -> str:
        selectors = [
            'meta[name="citation_journal_title"]',
            'meta[name="DC.Source"]',
            'meta[name="dc.Source"]',
        ]
        for selector in selectors:
            element = await tab.query(selector, timeout=2, raise_exc=False)
            if element:
                value = element.get_attribute("content")
                if value:
                    return value.strip()
        return fallback

    def dirs(self, ctx: AdapterContext, journal: str) -> tuple[Path, Path, Path]:
        return make_article_dirs(ctx.settings.download_root, self.publisher_name, journal)

    async def collect_links(self, tab, selector: str, base_url: str | None = None) -> list[dict]:
        elements = await tab.query(selector, timeout=15, find_all=True, raise_exc=False)
        if not elements:
            return []
        base = base_url or await tab.current_url
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
                text = await element.text
            except Exception:
                text = ""
            result.append({"url": absolute, "text": text, "element": element})
        return result


    def existing_file_result(self, kind: str, target: Path, source_url: str, extension: str) -> FileResult | None:
        if not target.exists():
            return None
        valid, _ = validate_file(target, extension)
        if not valid:
            return None
        return FileResult(
            kind=kind, path=str(target), source_url=source_url, method="already_exists",
            extension=extension, size=target.stat().st_size, sha256=sha256_file(target),
            valid=True, existing=True,
        )

    def file_result(self, kind: str, path: Path | None, source_url: str, method: str,
                    *, extension: str | None = None, error: str | None = None) -> FileResult:
        from ..download import result_metadata
        if path and path.exists():
            meta = result_metadata(path, extension)
            return FileResult(
                kind=kind,
                path=str(path),
                source_url=source_url,
                method=method,
                extension=extension or path.suffix,
                size=meta["size"],
                sha256=meta["sha256"],
                valid=meta["valid"],
                error=None if meta["valid"] else meta["reason"],
            )
        return FileResult(
            kind=kind,
            source_url=source_url,
            method=method,
            extension=extension,
            valid=False,
            error=error or "download_failed",
        )

"""Shared, bounded discovery for publishers exposing article and SI links."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urljoin, urlparse

from .base import PublisherAdapter
from ..download import (
    DownloadArtifact, _unwrap_script_result, blob_download,
    move_downloaded, native_navigation_download, click_element_and_wait,
)
from ..models import ArticleResult
from ..resources import infer_extension, resolve_download_extension
from ..storage import doi_to_filename, validate_file


SNAPSHOT = """return {
 ready_state: document.readyState,
 title: document.title,
 metas: Array.from(document.querySelectorAll('meta[name]')).map(e =>
   ({name:e.name.toLowerCase(), value:e.content})),
 links: Array.from(document.querySelectorAll('a[href],iframe[src]')).map(e =>
   ({url:e.href || e.src, text:e.textContent || '', type:e.getAttribute('data-doctype') || ''}))
};"""


def unique_links(items):
    seen = set()
    result = []
    for item in items:
        url = item.get('url', '')
        if url and url not in seen and urlparse(url).scheme in {'http', 'https'}:
            seen.add(url)
            result.append(item)
    return result


def select_files(snapshot, base_url, publisher):
    links = unique_links([
        {**item, 'url': urljoin(base_url, item['url'])}
        for item in snapshot.get('links', []) if item.get('url')
    ])
    pdfs = [{'url': urljoin(base_url, m['value']), 'text': 'PDF'}
            for m in snapshot.get('metas', [])
            if m['name'] == 'citation_pdf_url' and m.get('value')]
    si = []
    for item in links:
        path = urlparse(item['url']).path.lower()
        if ('/article-pdf/' in path or '/doi/pdf/' in path
                or '/articlepdf/' in path):
            pdfs.append(item)
        if (item.get('type') == 'dataSupplementDoc'
                or '/article-supplement/' in path
                or '/suppl_file/' in path
                or (publisher == 'RSC' and '/suppdata/' in path)):
            si.append(item)
    return unique_links(pdfs), unique_links(si)


async def snapshot_page(tab):
    raw = await asyncio.wait_for(
        tab.execute_script(SNAPSHOT, return_by_value=True), timeout=10,
    )
    value = _unwrap_script_result(raw)
    if not isinstance(value, dict) or not isinstance(value.get('links'), list):
        raise ValueError('Article link scan returned an invalid response')
    if value.get('ready_state', 'complete') != 'complete':
        raise ValueError('Page is still loading; link scan is not complete')
    return value


async def download_validated(ctx, url, target, text='', *, paper=False):
    artifact = await blob_download(
        ctx.tab, ctx.worker.staging_dir, url, target,
        ctx.settings.blob_download_timeout_seconds, link_text=text,
    )
    if artifact:
        if paper and artifact.extension != '.pdf':
            # Quarantine a valid attachment that was advertised as the article PDF.
            await move_downloaded(artifact.path, ctx.worker.staging_dir / artifact.path.name)
            return None, 'unexpected_article_type'
        return artifact, 'fetch_blob'
    # Native downloads must stay in staging until their contents pass validation.
    pending = ctx.worker.staging_dir / ('pending_' + target.name)
    path = None
    method = 'native_navigation'
    if paper and ctx.doi.startswith('10.1039/'):
        path_url = urlparse(url).path
        element = await asyncio.wait_for(ctx.tab.query(
            f'a[href={json.dumps(url)}], a[href={json.dumps(path_url)}]',
            timeout=2, raise_exc=False,
        ), timeout=3)
        if element:
            path = await click_element_and_wait(ctx.worker, element, pending,
                                                ctx.settings.native_download_timeout_seconds)
            method = 'real_click'
    if path is None:
        path = await native_navigation_download(
            ctx.worker, url, pending, ctx.settings.native_download_timeout_seconds,
        )
        method = 'native_navigation'
    if path:
        with path.open('rb') as handle:
            head = handle.read(512)
        extension, _ = resolve_download_extension(url, text, head=head)
        valid, _ = validate_file(path, extension)
        if valid and (not paper or extension == '.pdf'):
            final = await move_downloaded(path, target.with_suffix(extension))
            return DownloadArtifact(path=final, extension=extension), method
    return None, 'blob_then_native_failed'


class LinkedPublisherAdapter(PublisherAdapter):
    fallback_journals = {}

    async def prepare_article(self, ctx):
        # Reuse the established refresh/helper/post-challenge wait sequence.
        # Reserve at least a quarter of the DOI budget for actual downloads.
        budget = max(10, ctx.settings.article_timeout_seconds * 0.75)
        deadline = asyncio.get_running_loop().time() + budget
        attempts = []
        ctx.navigation_diagnostics['navigation_budget_seconds'] = budget
        ctx.navigation_diagnostics['navigation_attempts'] = attempts
        for attempt in range(2):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            started = asyncio.get_running_loop().time()
            record = {'attempt': attempt + 1}
            attempts.append(record)
            try:
                async with asyncio.timeout(remaining):
                    url = await self.navigate(ctx, cloudflare=True)
                    # Inspect access after the post-helper DOM wait has finished.
                    ready = ctx.navigation_diagnostics.get('article_dom_after_cloudflare_helper')
                    if ready is None:
                        ready = await self.wait_for_article_dom(
                            ctx.tab, timeout=ctx.settings.cloudflare_timeout_seconds,
                        )
                    issue = await self.access_issue(ctx.tab)
                    record.update(url=url, ready=ready, issue=issue,
                                  helper=ctx.navigation_diagnostics.get('pydoll_cloudflare_helper'))
                    if ready and not issue:
                        return url, None
            except Exception as exc:
                if self.is_browser_disconnect(exc):
                    raise
                record['error'] = type(exc).__name__
            finally:
                record['elapsed_seconds'] = round(asyncio.get_running_loop().time() - started, 3)
        ctx.navigation_diagnostics['navigation_attempts'] = attempts
        try:
            url = await asyncio.wait_for(ctx.tab.current_url, timeout=3)
        except TimeoutError:
            url = f'https://doi.org/{ctx.doi}'
        issue = await self.access_issue(ctx.tab)
        try:
            ctx.navigation_diagnostics['page_title'] = await asyncio.wait_for(ctx.tab.title, timeout=3)
        except Exception as exc:
            if self.is_browser_disconnect(exc):
                raise
        return url, issue or 'Article DOM did not become available within navigation budget.'

    async def discover_si(self, ctx, snapshot, article_url):
        return select_files(snapshot, article_url, self.key)[1], True, {}

    async def run(self, ctx):
        article_url, issue = await self.prepare_article(ctx)
        suffix = ctx.doi.split('/', 1)[-1].split('.', 1)[0]
        result = ArticleResult(
            doi=ctx.doi, publisher=self.publisher_name, article_url=article_url,
            journal=self.fallback_journals.get(suffix, 'Unknown Journal'),
            paper=self.existing_paper_result(ctx),
            diagnostics={'si_scan_complete': False},
        )
        if issue:
            result.title = ctx.navigation_diagnostics.get('page_title')
            result.message = issue
            result.diagnostics.update(failure_stage='article_navigation',
                                      access_issue='publisher_challenge' if 'challenge' in issue
                                      else 'article_unavailable')
            return result
        try:
            snapshot = await snapshot_page(ctx.tab)
            result.title = snapshot.get('title')
            for part in urlparse(article_url).path.lower().split('/'):
                if part in self.fallback_journals:
                    result.journal = self.fallback_journals[part]
                    break
            result.journal = await self.journal_from_meta(ctx.tab, result.journal)
            result.year = await self.year_from_meta(ctx.tab)
            _, paper_dir, si_dir = self.dirs(ctx, result.journal, result.year)
            pdfs, _ = select_files(snapshot, article_url, self.key)
            if not pdfs:
                for reader in unique_links([item for item in snapshot['links']
                                            if '/doi/epdf/' in item['url'] or '/doi/reader/' in item['url']]):
                    tab = await ctx.browser.new_tab()
                    try:
                        await asyncio.wait_for(tab.go_to(reader['url']), ctx.settings.navigation_timeout_seconds)
                        if await self.access_issue(tab):
                            continue
                        page = await snapshot_page(tab)
                        pdfs.extend(select_files(page, reader['url'], self.key)[0])
                    finally:
                        await asyncio.wait_for(tab.close(), timeout=3)
            result.diagnostics['paper_candidates'] = len(pdfs)
            # Collect SI before downloading: native PDF handling may change tabs.
            try:
                links, complete, diagnostics = (await self.discover_si(ctx, snapshot, article_url)
                                                 if ctx.want_si else ([], False, {'si_requested': False}))
            except Exception as exc:
                if self.is_browser_disconnect(exc):
                    raise
                links = select_files(snapshot, article_url, self.key)[1]
                complete = False
                diagnostics = {'si_discovery_error': type(exc).__name__}
            result.diagnostics.update(diagnostics)
            result.diagnostics['si_candidates'] = len(links)
            if result.paper is None:
                for candidate in pdfs:
                    artifact, method = await download_validated(
                        ctx, candidate['url'], paper_dir / f'{doi_to_filename(ctx.doi)}.pdf', 'PDF', paper=True,
                    )
                    result.paper = self.file_result('paper', artifact, candidate['url'], method,
                                                  extension='.pdf')
                    if result.paper.extension != '.pdf':
                        result.paper.valid = False
                        result.paper.error = 'expected_article_pdf'
                    if result.paper.valid:
                        break
            if not result.paper or not result.paper.valid:
                result.diagnostics['failure_stage'] = 'paper_download' if pdfs else 'paper_discovery'
            for item in links:
                url = item['url']
                ext = infer_extension(url, item.get('text', ''))
                target = self.si_target(si_dir, ctx.doi, url, ext)
                existing = self.existing_file_result(ctx, 'si', target, url, ext)
                if existing:
                    result.si.append(existing)
                    continue
                artifact, method = await download_validated(ctx, url, target, item.get('text', ''))
                result.si.append(self.file_result('si', artifact, url, method, extension=ext))
            result.diagnostics['si_scan_complete'] = complete
            if not complete and ctx.want_si:
                result.diagnostics['failure_stage'] = 'si_discovery'
            elif any(not item.valid for item in result.si):
                result.diagnostics['failure_stage'] = 'si_download'
            if not ctx.want_si:
                result.diagnostics.pop('si_scan_complete', None)
        except Exception as exc:
            if self.is_browser_disconnect(exc):
                raise
            result.message = f'Publisher discovery/download failed: {type(exc).__name__}: {exc}'
            result.diagnostics.setdefault('failure_stage', 'discovery')
        return result

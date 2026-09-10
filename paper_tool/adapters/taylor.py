from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin, urlparse

import httpx

from .linked import LinkedPublisherAdapter, snapshot_page, unique_links
from ..netutil import env_proxy_usable


def figshare_article_id(url):
    parsed = urlparse(url)
    if parsed.hostname not in {'tandf.figshare.com', 'figshare.com', 'www.figshare.com', 'widgets.figshare.com'}:
        return None
    match = re.search(r'/(?:articles/(?:[^/]+/)*|ndownloader/articles/)(\d+)(?:/(?:\d+|embed))?/?$', parsed.path)
    return match.group(1) if match else None


class TaylorFrancisAdapter(LinkedPublisherAdapter):
    key = 'TAYLOR'
    publisher_name = 'Taylor & Francis'

    @classmethod
    def matches_doi(cls, doi):
        return doi.lower().startswith('10.1080/')

    async def discover_si(self, ctx, snapshot, article_url):
        files, complete, _ = await super().discover_si(ctx, snapshot, article_url)
        unresolved = []
        pages = [snapshot]
        supplemental = unique_links([
            item for item in snapshot['links']
            if '/doi/suppl/' in item['url'] and '/suppl_file/' not in item['url']
        ])
        for item in supplemental:
            tab = await ctx.browser.new_tab()
            helper = None
            page_diagnostics = {}
            try:
                # Supplemental pages sit behind the same Cloudflare challenge as
                # the article page, so arm the helper on this tab as well.
                helper = await self.arm_cloudflare_helper(
                    ctx, tab=tab, diagnostics=page_diagnostics,
                )
                try:
                    await asyncio.wait_for(
                        tab.go_to(item['url']), ctx.settings.navigation_timeout_seconds,
                    )
                except Exception as exc:
                    if self.is_browser_disconnect(exc):
                        raise
                if helper is not None:
                    await self.run_cloudflare_helper(
                        ctx, helper, tab=tab, diagnostics=page_diagnostics,
                    )
                if await self.access_issue(tab):
                    raise ValueError('Supplemental page is blocked')
                if not await self.wait_for_article_dom(tab, timeout=5):
                    raise ValueError('Supplemental page could not be verified')
                page = await snapshot_page(
                    tab, timeout=ctx.settings.normal_element_timeout_seconds,
                )
                pages.append(page)
                from .linked import select_files
                files.extend(select_files(page, item['url'], self.key)[1])
            except Exception as exc:
                if self.is_browser_disconnect(exc):
                    raise
                unresolved.append({'url': item['url'], 'error': type(exc).__name__,
                                   'cloudflare': page_diagnostics.get('pydoll_cloudflare_helper')})
            finally:
                if helper is not None:
                    await self.disarm_cloudflare_helper(tab, helper)
                # Closing a tab whose CDP session already broke must not abort
                # the remaining supplemental pages.
                try:
                    await asyncio.wait_for(tab.close(), timeout=3)
                except Exception:
                    pass
        figshare_ids = set()
        for page in pages:
            for item in page['links']:
                url = urljoin(article_url, item['url'])
                parsed = urlparse(url)
                article_id = figshare_article_id(url)
                if article_id:
                    figshare_ids.add(article_id)
                elif parsed.hostname in {'tandf.figshare.com', 'figshare.com', 'www.figshare.com', 'widgets.figshare.com'}:
                    if '/ndownloader/files/' in parsed.path or '/file/download/' in parsed.path:
                        files.append({'url': url, 'text': item.get('text', '')})
                    else:
                        unresolved.append({'url': url, 'error': 'unresolved_figshare_reference'})
                elif ('supplement' in item.get('text', '').lower()
                      and parsed.hostname != urlparse(article_url).hostname):
                    unresolved.append({'url': url, 'error': 'external_supplement_reference'})
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, trust_env=env_proxy_usable(),
        ) as client:
            for article_id in sorted(figshare_ids):
                api_url = f'https://api.figshare.com/v2/articles/{article_id}'
                try:
                    response = await client.get(api_url)
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data.get('files'), list) or not data['files']:
                        raise ValueError('Associated Figshare item has no public file list')
                    for item in data['files']:
                        if not item.get('download_url'):
                            raise ValueError('Missing Figshare download URL')
                        files.append({'url': item['download_url'], 'text': item.get('name', '')})
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    unresolved.append({'url': api_url, 'error': type(exc).__name__,
                                       'http_status': exc.response.status_code
                                       if isinstance(exc, httpx.HTTPStatusError) else None})
        return unique_links(files), complete and not unresolved, {
            'si_scan_sources': [article_url] + [item['url'] for item in supplemental],
            'figshare_article_ids': sorted(figshare_ids),
            'unresolved_si_sources': unresolved,
        }

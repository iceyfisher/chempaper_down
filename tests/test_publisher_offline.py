"""Offline regressions. All browser and HTTP operations are mocked."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from paper_tool.adapters.acs import ACSAdapter
from paper_tool.adapters.base import AdapterContext
from paper_tool.adapters.linked import select_files, snapshot_page
from paper_tool.adapters.taylor import TaylorFrancisAdapter, figshare_article_id
from paper_tool.config import Settings
from paper_tool.models import ArticleResult, FileResult, ItemStatus
from paper_tool.registry import get_adapter
from paper_tool.worker_main import finalize_status


def test_route_and_omega_name():
    assert get_adapter('10.1080/07366298608917877').key == 'TAYLOR'
    assert ACSAdapter.fallback_journals['acsomega'] == 'ACS Omega'


def test_new_and_legacy_links():
    snapshot = {'metas': [{'name': 'citation_pdf_url', 'value': '/doi/pdf/x'}],
                'links': [{'url': url, 'text': ''} for url in (
                    '/article-pdf/a.pdf', '/doi/pdf/x', '/article-supplement/a.zip',
                    '/doi/suppl/x/suppl_file/b.pdf', '/suppdata/c.cif', '/suppdata/c.cif')]}
    pdfs, si = select_files(snapshot, 'https://pubs.rsc.org/article/x', 'RSC')
    assert len(pdfs) == 2
    assert len(si) == 3
    assert all(item['url'].startswith('https://pubs.rsc.org/') for item in si)


def test_challenge_preserves_paper_and_incomplete_si(tmp_path):
    paper = tmp_path / 'article.pdf'
    paper.write_bytes(b'%PDF-1.4\n%%EOF')
    ctx = AdapterContext(SimpleNamespace(main_tab=None), Settings(download_root=tmp_path),
                         '10.1021/acsomega.5c13377', existing_paper=paper)
    adapter = ACSAdapter()
    adapter.prepare_article = AsyncMock(return_value=('https://pubs.acs.org/', 'Publisher access challenge'))
    result = asyncio.run(adapter.run(ctx))
    assert result.paper.valid and result.paper.existing
    assert result.diagnostics['si_scan_complete'] is False
    assert finalize_status(result) == ItemStatus.PARTIAL


def test_invalid_scan_raises():
    tab = SimpleNamespace(execute_script=AsyncMock(return_value=None))
    import pytest
    with pytest.raises(ValueError):
        asyncio.run(snapshot_page(tab))


def test_figshare_ids():
    assert figshare_article_id('https://tandf.figshare.com/articles/journal_contribution/title/12345') == '12345'
    assert figshare_article_id('https://evil.example/articles/12345') is None
    assert figshare_article_id('https://widgets.figshare.com/articles/12345/embed') == '12345'


def test_complete_empty_si_is_success():
    result = ArticleResult(doi='10.1080/example', paper=FileResult(kind='paper', valid=True),
                           diagnostics={'si_scan_complete': True})
    assert finalize_status(result) == ItemStatus.SUCCESS
    result.diagnostics['si_scan_complete'] = False
    assert finalize_status(result) == ItemStatus.PARTIAL


def test_taylor_empty_scan_makes_no_http_requests(monkeypatch):
    client = SimpleNamespace(get=AsyncMock(side_effect=AssertionError('No network allowed')))
    from unittest.mock import MagicMock
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr('paper_tool.adapters.taylor.httpx.AsyncClient', lambda **kwargs: context)
    links, complete, _ = asyncio.run(TaylorFrancisAdapter().discover_si(
        None, {'metas': [], 'links': []}, 'https://www.tandfonline.com/doi/full/10.1080/example'))
    assert links == [] and complete
    client.get.assert_not_awaited()


def test_navigation_retries_exactly_once_without_browser():
    class Tab:
        go_to = AsyncMock()

        @property
        async def current_url(self):
            return 'https://pubs.acs.org/doi/example'

    tab = Tab()
    ctx = AdapterContext(SimpleNamespace(main_tab=tab),
                         Settings(enable_pydoll_cloudflare_helper=False), '10.1021/example')
    adapter = ACSAdapter()
    adapter.navigate = AsyncMock(return_value='https://pubs.acs.org/doi/example')
    adapter.wait_for_article_dom = AsyncMock(return_value=False)
    adapter.access_issue = AsyncMock(return_value='Publisher access challenge')
    _, issue = asyncio.run(adapter.prepare_article(ctx))
    assert issue == 'Publisher access challenge'
    assert adapter.navigate.await_count == 2
    assert len(ctx.navigation_diagnostics['navigation_attempts']) == 2


def test_restored_refresh_helper_and_full_post_wait():
    class Tab:
        go_to = AsyncMock()
        refresh = AsyncMock()
        _bypass_cloudflare = AsyncMock()

        @property
        async def current_url(self):
            return 'https://pubs.acs.org/article/example'

    tab = Tab()
    settings = Settings(article_timeout_seconds=360, cloudflare_timeout_seconds=60, settle_seconds=0)
    ctx = AdapterContext(SimpleNamespace(main_tab=tab), settings, '10.1021/example')
    adapter = ACSAdapter()
    adapter.wait_for_article_dom = AsyncMock(side_effect=[False, True])
    adapter.access_issue = AsyncMock(return_value=None)
    url, issue = asyncio.run(adapter.prepare_article(ctx))
    assert issue is None and '/article/example' in url
    tab.refresh.assert_awaited_once()
    tab._bypass_cloudflare.assert_awaited_once_with({}, time_to_wait_captcha=60)
    assert [call.kwargs['timeout'] for call in adapter.wait_for_article_dom.await_args_list] == [3, 60]
    assert len(ctx.navigation_diagnostics['navigation_attempts']) == 1
    assert ctx.navigation_diagnostics['navigation_budget_seconds'] == 270


def test_extended_cloudflare_setting_survives_worker_payload():
    settings = Settings(cloudflare_timeout_seconds=90).normalized()
    assert settings.cloudflare_timeout_seconds == 90
    assert Settings.from_worker_payload(settings.to_worker_payload()).cloudflare_timeout_seconds == 90


def test_native_html_stays_out_of_final_directory(tmp_path, monkeypatch):
    from paper_tool.adapters.linked import download_validated
    staging = tmp_path / 'staging'
    staging.mkdir()
    bad = staging / 'error.pdf'
    bad.write_bytes(b'<!doctype html><html>Access denied</html>')
    ctx = AdapterContext(SimpleNamespace(main_tab=None, staging_dir=staging), Settings(), '10.1021/example')
    monkeypatch.setattr('paper_tool.adapters.linked.blob_download', AsyncMock(return_value=None))
    monkeypatch.setattr('paper_tool.adapters.linked.native_navigation_download', AsyncMock(return_value=bad))
    target = tmp_path / 'si' / 'file.pdf'
    artifact, _ = asyncio.run(download_validated(ctx, 'https://example.org/file.pdf', target))
    assert artifact is None
    assert not target.exists()


def test_valid_native_zip_gets_real_extension(tmp_path, monkeypatch):
    from paper_tool.adapters.linked import download_validated
    staging = tmp_path / 'staging'
    staging.mkdir()
    source = staging / 'native'
    source.write_bytes(b'PK\x05\x06' + b'\x00' * 18)
    ctx = AdapterContext(SimpleNamespace(main_tab=None, staging_dir=staging), Settings(), '10.1021/example')
    monkeypatch.setattr('paper_tool.adapters.linked.blob_download', AsyncMock(return_value=None))
    monkeypatch.setattr('paper_tool.adapters.linked.native_navigation_download', AsyncMock(return_value=source))
    artifact, _ = asyncio.run(download_validated(ctx, 'https://example.org/attachment', tmp_path / 'si/file.bin'))
    assert artifact.extension == '.zip'
    assert artifact.path.name == 'file.zip'


def test_reuse_requires_url_match_or_stable_target(tmp_path):
    source = tmp_path / 'existing.pdf'
    source.write_bytes(b'%PDF-1.4\n%%EOF')
    ctx = AdapterContext(None, Settings(), '10.1021/example', previous_manifest={
        'si': [{'source_url': 'https://example.org/a', 'path': str(source), 'extension': '.pdf'}]})
    adapter = ACSAdapter()
    assert adapter.existing_file_result(ctx, 'si', tmp_path / 'missing.pdf', 'https://example.org/a', '.pdf')
    assert adapter.existing_file_result(ctx, 'si', tmp_path / 'missing.pdf', 'https://example.org/b', '.pdf') is None


def test_figshare_file_list_and_failure(monkeypatch):
    from unittest.mock import MagicMock
    import httpx
    page = {'metas': [], 'links': [{'url': 'https://tandf.figshare.com/articles/12345', 'text': 'SI'}]}
    response = MagicMock()
    response.json.return_value = {'files': [
        {'name': 'a.pdf', 'download_url': 'https://ndownloader.figshare.com/files/1'},
        {'name': 'b.zip', 'download_url': 'https://ndownloader.figshare.com/files/2'},
    ]}
    client = SimpleNamespace(get=AsyncMock(return_value=response))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr('paper_tool.adapters.taylor.httpx.AsyncClient', lambda **kwargs: context)
    adapter = TaylorFrancisAdapter()
    links, complete, _ = asyncio.run(adapter.discover_si(None, page, 'https://www.tandfonline.com/article'))
    assert complete and len(links) == 2
    client.get.side_effect = httpx.ConnectError('offline fixture')
    _, complete, diagnostic = asyncio.run(adapter.discover_si(None, page, 'https://www.tandfonline.com/article'))
    assert not complete and diagnostic['unresolved_si_sources']


def test_full_scan_downloads_all_si_then_reuses_them(tmp_path, monkeypatch):
    from paper_tool.download import DownloadArtifact
    paper = tmp_path / 'paper.pdf'
    paper.write_bytes(b'%PDF-1.4\n%%EOF')
    ctx = AdapterContext(SimpleNamespace(main_tab=None), Settings(download_root=tmp_path),
                         '10.1021/acsomega.example', existing_paper=paper)
    adapter = ACSAdapter()
    adapter.prepare_article = AsyncMock(return_value=('https://pubs.acs.org/article/example', None))
    adapter.journal_from_meta = AsyncMock(return_value='ACS Omega')
    adapter.year_from_meta = AsyncMock(return_value='2026')
    page = {'title': 'Fixture article', 'metas': [], 'links': [
        {'url': 'https://pubs.acs.org/article-supplement/a.pdf', 'text': 'SI PDF'},
        {'url': 'https://pubs.acs.org/article-supplement/b.zip', 'text': 'SI ZIP'},
    ]}
    monkeypatch.setattr('paper_tool.adapters.linked.snapshot_page', AsyncMock(return_value=page))

    async def fixture_download(ctx, url, target, text):
        target.write_bytes(b'%PDF-1.4\n%%EOF' if target.suffix == '.pdf' else b'PK\x05\x06' + b'\x00' * 18)
        return DownloadArtifact(path=target, extension=target.suffix), 'offline_fixture'

    download = AsyncMock(side_effect=fixture_download)
    monkeypatch.setattr('paper_tool.adapters.linked.download_validated', download)
    result = asyncio.run(adapter.run(ctx))
    assert finalize_status(result) == ItemStatus.SUCCESS
    assert result.paper.existing and len(result.si) == 2
    assert download.await_count == 2
    ctx.previous_manifest = result.to_dict()
    download.reset_mock()
    retried = asyncio.run(adapter.run(ctx))
    assert len(retried.si) == 2 and all(item.existing for item in retried.si)
    download.assert_not_awaited()

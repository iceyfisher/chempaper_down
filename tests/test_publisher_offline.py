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


def test_taylor_download_supplement_links():
    from paper_tool.resources import infer_extension
    snapshot = {'metas': [], 'links': [
        {'url': 'https://www.tandfonline.com/action/downloadSupplement?doi=10.1080%2Fx&file=a_sm1.docx',
         'text': 'Supplemental material'},
        {'url': 'https://www.tandfonline.com/doi/suppl/10.1080/x?scroll=top', 'text': ''},
    ]}
    _, si = select_files(snapshot, 'https://www.tandfonline.com/doi/full/10.1080/x', 'TAYLOR')
    assert len(si) == 1 and 'downloadSupplement' in si[0]['url']
    assert infer_extension(si[0]['url']) == '.docx'
    _, rsc_si = select_files(snapshot, 'https://pubs.rsc.org/article/x', 'RSC')
    assert rsc_si == []


def test_taylor_supplemental_page_uses_cloudflare_helper(monkeypatch):
    from unittest.mock import MagicMock
    page = {'metas': [], 'links': [
        {'url': 'https://www.tandfonline.com/doi/suppl/10.1080/x?scroll=top', 'text': ''}]}
    supplemental = {'title': 'Supplemental', 'metas': [], 'links': [
        {'url': 'https://www.tandfonline.com/action/downloadSupplement?doi=10.1080%2Fx&file=a.docx',
         'text': 'SI'}]}
    tab = SimpleNamespace(go_to=AsyncMock(), close=AsyncMock())
    ctx = SimpleNamespace(browser=SimpleNamespace(new_tab=AsyncMock(return_value=tab)),
                          settings=Settings())
    adapter = TaylorFrancisAdapter()
    armed, ran = [], []

    async def fake_arm(ctx, *, tab=None, diagnostics=None):
        armed.append(tab)
        return {'callback_id': 1, 'done': None}

    async def fake_run(ctx, helper, *, tab=None, diagnostics=None):
        ran.append(tab)

    adapter.arm_cloudflare_helper = fake_arm
    adapter.run_cloudflare_helper = fake_run
    adapter.disarm_cloudflare_helper = AsyncMock()
    adapter.access_issue = AsyncMock(return_value=None)
    adapter.wait_for_article_dom = AsyncMock(return_value=True)
    monkeypatch.setattr('paper_tool.adapters.taylor.snapshot_page',
                        AsyncMock(return_value=supplemental))
    client = SimpleNamespace(get=AsyncMock())
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr('paper_tool.adapters.taylor.httpx.AsyncClient', lambda **kwargs: context)
    links, complete, _ = asyncio.run(adapter.discover_si(
        ctx, page, 'https://www.tandfonline.com/doi/full/10.1080/x'))
    assert armed == [tab] and ran == [tab]
    adapter.disarm_cloudflare_helper.assert_awaited_once()
    assert complete and len(links) == 1
    assert 'downloadSupplement' in links[0]['url']


def test_snapshot_waits_for_full_load():
    class Tab:
        def __init__(self, states):
            self.states = list(states)
            self.calls = 0

        async def execute_script(self, script, return_by_value=True):
            self.calls += 1
            state = self.states[min(self.calls - 1, len(self.states) - 1)]
            return {'result': {'result': {'value': {'ready_state': state, 'links': [], 'metas': []}}}}

    late = Tab(['interactive', 'complete'])
    assert asyncio.run(snapshot_page(late, timeout=5))['ready_state'] == 'complete'
    assert late.calls == 2
    # A page that keeps long-polling subresources pending is still scannable.
    stuck = Tab(['interactive'])
    assert asyncio.run(snapshot_page(stuck, timeout=0.1))['ready_state'] == 'interactive'
    assert stuck.calls == 2
    early = Tab(['loading'])
    import pytest
    with pytest.raises(ValueError, match='still loading'):
        asyncio.run(snapshot_page(early, timeout=0.1))

    class MarkedTab(Tab):
        async def execute_script(self, script, return_by_value=True):
            self.calls += 1
            return {'result': {'result': {'value': {
                'ready_state': 'loading', 'links': [], 'metas': [
                    {'name': 'citation_pdf_url', 'value': 'https://example.org/x.pdf'}]}}}}

    marked = MarkedTab(['loading'])
    assert asyncio.run(snapshot_page(marked, timeout=0.1))['ready_state'] == 'loading'


def test_snapshot_retries_transient_command_timeout():
    from pydoll.exceptions import CommandExecutionTimeout

    class Tab:
        def __init__(self):
            self.calls = 0

        async def execute_script(self, script, return_by_value=True):
            self.calls += 1
            if self.calls == 1:
                raise CommandExecutionTimeout('page still executing challenge script')
            return {'result': {'result': {'value': {'ready_state': 'complete', 'links': [], 'metas': []}}}}

    tab = Tab()
    assert asyncio.run(snapshot_page(tab, timeout=5))['ready_state'] == 'complete'
    assert tab.calls == 2


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


def test_cloudflare_helper_armed_before_navigation():
    calls = []

    class Tab:
        page_events_enabled = False
        handler = None

        async def enable_page_events(self):
            self.page_events_enabled = True
            calls.append('enable_page_events')

        async def on(self, event, callback, temporary=False):
            calls.append(('on', event))
            self.handler = callback
            return 7

        async def remove_callback(self, callback_id):
            calls.append(('remove_callback', callback_id))

        async def go_to(self, url):
            calls.append('go_to')
            await self.handler({'method': 'Page.loadEventFired'})

        async def refresh(self):
            calls.append('refresh')

        @property
        async def current_url(self):
            return 'https://pubs.acs.org/article/example'

        async def _bypass_cloudflare(self, event, time_to_wait_captcha=5):
            calls.append(('bypass', event, time_to_wait_captcha))

    tab = Tab()
    settings = Settings(article_timeout_seconds=360, cloudflare_timeout_seconds=60, settle_seconds=0)
    ctx = AdapterContext(SimpleNamespace(main_tab=tab), settings, '10.1021/example')
    adapter = ACSAdapter()
    adapter.wait_for_article_dom = AsyncMock(side_effect=[False, True])
    adapter.access_issue = AsyncMock(return_value=None)
    url, issue = asyncio.run(adapter.prepare_article(ctx))
    assert issue is None and '/article/example' in url
    assert calls[:3] == ['enable_page_events', ('on', 'Page.loadEventFired'), 'go_to']
    assert ('bypass', {'method': 'Page.loadEventFired'}, 60) in calls
    assert 'refresh' not in calls
    assert ('remove_callback', 7) in calls
    assert [call.kwargs['timeout'] for call in adapter.wait_for_article_dom.await_args_list] == [3, 60]
    assert ctx.navigation_diagnostics['pydoll_cloudflare_helper'] == 'armed'
    assert ctx.navigation_diagnostics['article_dom_before_cloudflare_helper'] is False
    assert ctx.navigation_diagnostics['article_dom_after_cloudflare_helper'] is True
    assert len(ctx.navigation_diagnostics['navigation_attempts']) == 1
    assert ctx.navigation_diagnostics['navigation_budget_seconds'] == 270
    assert ctx.navigation_diagnostics['navigation_attempt_cap_seconds'] == 90


def test_downloads_are_headful_unless_opted_out(monkeypatch):
    from dataclasses import replace
    settings = Settings.from_env('downloads')
    assert settings.headless_publishers == frozenset()
    assert settings.to_worker_payload()['headless_publishers'] == []
    monkeypatch.setenv('PAPER_TOOL_HEADLESS_PUBLISHERS', 'springer, wiley')
    assert Settings.from_env('downloads').headless_publishers == frozenset({'SPRINGER', 'WILEY'})
    opted_out = replace(settings, headless_publishers=frozenset({'ACS'}))
    assert Settings.from_worker_payload(opted_out.to_worker_payload()).headless_publishers == frozenset({'ACS'})


def test_new_publisher_routing():
    assert get_adapter('10.1038/s41928-021-00599-5').key == 'SPRINGER'
    assert get_adapter('10.3390/mi14112044').key == 'MDPI'
    assert get_adapter('10.1088/1361-6463/aaaf9d').key == 'IOP'
    assert get_adapter('10.7567/1882-0786/ab1b19').key == 'IOP'
    assert get_adapter('10.1103/PhysRevApplied.22.024075').key == 'APS'
    assert get_adapter('10.1364/OE.15.015964').key == 'OPTICA'
    assert get_adapter('10.1049/el.2014.1131').key == 'WILEY'
    assert get_adapter('10.23919/ISPSD50666.2021.9452259').key == 'IEEE'
    assert get_adapter('10.1109/TED.2023.3346369').key == 'IEEE'


def test_new_publisher_si_patterns():
    snapshot = {'metas': [], 'links': [
        # MDPI SI
        {'url': 'https://www.mdpi.com/article/10.3390/mi14112044/s1', 'text': 'Supplementary'},
        # IOP supplementary
        {'url': 'https://iopscience.iop.org/article/10.1088/x/supplementary', 'text': 'Supplementary data'},
        # APS supplemental
        {'url': 'https://journals.aps.org/prapplied/supplemental/10.1103/x', 'text': 'Supplemental'},
    ]}
    from paper_tool.adapters.linked import select_files as sf
    assert len(sf(snapshot, 'https://www.mdpi.com/x', 'MDPI')[1]) == 1
    assert len(sf(snapshot, 'https://iopscience.iop.org/x', 'IOP')[1]) == 1
    assert len(sf(snapshot, 'https://journals.aps.org/x', 'APS')[1]) == 1
    # No cross-publisher leakage: the other publishers see no SI here.
    assert sf(snapshot, 'https://pubs.acs.org/x', 'ACS')[1] == []


def test_cloudflare_publishers_get_larger_budget():
    settings = Settings()
    for doi in ('10.1021/acsomega.5c13377', '10.1039/c6ra08946a',
                '10.1080/07366299.2015.1087209', '10.1063/5.0061354',
                '10.1126/science.abc1234'):
        assert settings.timeout_for_doi(doi) == 360
        assert settings.article_timeout_for_doi(doi) == 360
    assert settings.article_timeout_for_doi('10.1007/s10967-017-5317-8') == 120
    assert settings.timeout_for_doi('10.1002/anie.202000001') == 600
    payload = settings.to_worker_payload()
    assert Settings.from_worker_payload(payload).cloudflare_article_timeout_seconds == 360


def test_worker_budget_stays_below_parent_kill():
    from paper_tool.worker_main import worker_budget_seconds
    settings = Settings()
    assert worker_budget_seconds(settings, '10.1021/x') == 324
    assert worker_budget_seconds(settings, '10.1002/x') == 540
    for doi in ('10.1007/x', '10.1021/x', '10.1002/x'):
        assert worker_budget_seconds(settings, doi) < settings.timeout_for_doi(doi)


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

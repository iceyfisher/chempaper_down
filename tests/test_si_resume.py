import asyncio
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from paper_tool.adapters.acs import ACSAdapter, merge_si_links
from paper_tool.adapters.base import AdapterContext
from paper_tool.adapters import wiley
from paper_tool.adapters.wiley import download_wiley_attachment, select_wiley_si
from paper_tool.config import Settings
from paper_tool.service import _duplicate_result
from paper_tool.storage import manifest_has_complete_si


def _write_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.7\nfixture\n%%EOF\n")


class _WileyFixtureParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_support_table = False
        self.current: dict | None = None
        self.records: list[dict] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "table" and "support-info__table" in values.get("class", ""):
            self.in_support_table = True
        if tag == "a" and values.get("href"):
            self.current = {
                "url": urljoin("https://onlinelibrary.wiley.com/doi/full/10.1002/example", values["href"]),
                "text": "",
                "download": values.get("download", ""),
                "context": "",
                "source": "support_table" if self.in_support_table else "supporting_text",
            }

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.records.append(self.current)
            self.current = None
        if tag == "table":
            self.in_support_table = False


def test_acs_link_groups_are_merged_without_duplicates():
    first = [{"url": "https://example.test/si/1", "text": "SI 1"}]
    second = [
        {"url": "https://example.test/si/1", "text": "duplicate"},
        {"url": "https://example.test/si/2", "text": "SI 2"},
    ]
    assert [item["url"] for item in merge_si_links(first, second)] == [
        "https://example.test/si/1",
        "https://example.test/si/2",
    ]


def test_wiley_fixture_discovers_all_files_and_excludes_article_pdf():
    parser = _WileyFixtureParser()
    fixture = Path(__file__).parent / "fixtures" / "wiley_supporting_info.html"
    parser.feed(fixture.read_text(encoding="utf-8"))
    selected = select_wiley_si(parser.records)
    assert len(selected) == 3
    assert {Path(item["url"].split("file=", 1)[-1]).suffix for item in selected} == {
        ".pdf", ".zip", ".mp4"
    }


def test_manifest_requires_explicit_complete_scan_and_valid_files(tmp_path: Path):
    si_path = tmp_path / "si.pdf"
    _write_pdf(si_path)
    manifest = {
        "doi": "10.1002/example",
        "status": "success",
        "diagnostics": {"si_scan_complete": True},
        "si": [{"path": str(si_path), "extension": ".pdf", "valid": True}],
    }
    assert manifest_has_complete_si(manifest)
    assert not manifest_has_complete_si({**manifest, "diagnostics": {}})
    si_path.unlink()
    assert not manifest_has_complete_si(manifest)


def test_wiley_uses_extended_hard_budget():
    settings = Settings(article_timeout_seconds=120).normalized()
    assert settings.timeout_for_doi("10.1002/anie.202503908") == 600
    assert settings.timeout_for_doi("10.1021/example") == 120


def test_wiley_large_attachment_falls_back_to_native_download(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, float]] = []
    url = (
        "https://onlinelibrary.wiley.com/action/downloadSupplement"
        "?doi=10.1002%2Fanie.2761225&file=anie74111-sup-0001-SuppMat.docx"
    )
    target = tmp_path / "stable.docx"

    async def fake_blob(tab, staging_dir, source_url, output, timeout, *, link_text):
        calls.append(("blob", timeout))
        return None

    async def fake_native(worker, source_url, output, timeout):
        calls.append(("native", timeout))
        output.write_bytes(b"PK\x03\x04fixture")
        return output

    monkeypatch.setattr(wiley, "blob_download", fake_blob)
    monkeypatch.setattr(wiley, "native_navigation_download", fake_native)
    worker = type("Worker", (), {"staging_dir": tmp_path})()

    artifact, method = asyncio.run(
        download_wiley_attachment(
            object(),
            worker,
            url,
            target,
            300,
            link_text="Supporting File 1",
            extension=".docx",
        )
    )

    assert method == "native_navigation_fallback"
    assert calls == [("blob", 300), ("native", 300)]
    assert artifact is not None
    assert artifact.path == target
    assert artifact.extension == ".docx"
    assert artifact.original_filename == "anie74111-sup-0001-SuppMat.docx"


def test_complete_duplicate_result_preserves_si_summary(tmp_path: Path):
    paper = tmp_path / "paper.pdf"
    si = tmp_path / "si.pdf"
    _write_pdf(paper)
    _write_pdf(si)
    manifest = {
        "doi": "10.1002/example",
        "status": "success",
        "paper": {"kind": "paper", "path": str(paper), "extension": ".pdf", "valid": True},
        "si": [{"kind": "si", "path": str(si), "extension": ".pdf", "valid": True}],
        "diagnostics": {"si_scan_complete": True},
    }
    result = _duplicate_result("10.1002/example", paper, manifest)
    assert str(result.status) == "skipped_duplicate"
    assert result.si_successful == 1
    assert result.diagnostics["si_scan_complete"] is True


def test_stable_url_target_prevents_cross_source_reuse(tmp_path: Path):
    adapter = ACSAdapter()
    settings = Settings(download_root=tmp_path).normalized()
    ctx = AdapterContext(worker=object(), settings=settings, doi="10.1021/example")
    first = adapter.si_target(tmp_path, ctx.doi, "https://example.test/si/one", ".pdf")
    second = adapter.si_target(tmp_path, ctx.doi, "https://example.test/si/two", ".pdf")
    assert first != second

    _write_pdf(first)
    assert adapter.existing_file_result(ctx, "si", first, "https://example.test/si/one", ".pdf")
    assert adapter.existing_file_result(ctx, "si", second, "https://example.test/si/two", ".pdf") is None

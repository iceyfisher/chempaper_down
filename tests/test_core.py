from pathlib import Path

from paper_tool.config import Settings
from paper_tool.doi import extract_dois
from paper_tool.models import ArticleResult, FileResult, ItemStatus
from paper_tool.storage import doi_to_filename, valid_pdf


def test_extract_dois_dedupes():
    values = extract_dois(
        "https://doi.org/10.1002/ANIE.123 10.1002/anie.123\n10.1039/D6QO00001A"
    )
    assert values == ["10.1002/anie.123", "10.1039/d6qo00001a"]


def test_doi_filename():
    assert doi_to_filename("10.1002/anie.123") == "10.1002_anie.123"


def test_invalid_pdf(tmp_path: Path):
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"<html>not pdf</html>")
    assert not valid_pdf(path)


def test_settings_worker_roundtrip(tmp_path: Path):
    source = Settings(download_root=tmp_path, article_timeout_seconds=123).normalized()
    restored = Settings.from_worker_payload(source.to_worker_payload())
    assert restored.download_root == tmp_path.resolve()
    assert restored.article_timeout_seconds == 123


def test_article_result_roundtrip():
    source = ArticleResult(
        doi="10.1002/example",
        status=ItemStatus.SUCCESS,
        paper=FileResult(kind="paper", path="x.pdf", valid=True),
        si=[FileResult(kind="si", path="x.xlsx", valid=True)],
    )
    restored = ArticleResult.from_dict(source.to_dict())
    assert restored.status == ItemStatus.SUCCESS
    assert restored.paper and restored.paper.valid
    assert restored.si_successful == 1

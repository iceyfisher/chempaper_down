from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from paper_tool.adapters.aaas import (
    select_aaas_pdf,
    select_aaas_reader,
    select_aaas_si,
)
from paper_tool.adapters.aip import (
    infer_aip_extension,
    select_aip_pdf,
    select_aip_si,
)
from paper_tool.registry import get_adapter, supported_publishers


FIXTURES = Path(__file__).parent / "fixtures"


class LinkParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.current: dict | None = None
        self.records: list[dict] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.current = {
                "url": urljoin(self.base_url, values["href"]),
                "text": values.get("download", ""),
            }

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += " " + data.strip()

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.records.append(self.current)
            self.current = None


def parse_fixture(name: str, base_url: str) -> list[dict]:
    parser = LinkParser(base_url)
    parser.feed((FIXTURES / name).read_text(encoding="utf-8"))
    return parser.records


def test_aip_extracts_article_pdf_and_archive_si():
    records = parse_fixture(
        "aip_article.html",
        "https://pubs.aip.org/aip/jcp/article/160/4/042502/3222771/example",
    )

    paper = select_aip_pdf(records)
    si = select_aip_si(records + records)

    assert paper is not None
    assert paper["url"].endswith("/042502_1_5.0176000.pdf")
    assert len(si) == 1
    assert si[0]["url"] == (
        "https://pubs.aip.org/jcp/article-supplement/3222771/zip/"
        "042502_1_5.0176000.suppl_material/"
    )
    assert infer_aip_extension(si[0]["url"], si[0]["text"]) == ".zip"


def test_aaas_extracts_reader_pdf_and_all_supplementary_files():
    article = parse_fixture(
        "aaas_article.html",
        "https://www.science.org/doi/10.1126/sciadv.aec3536",
    )
    reader = parse_fixture(
        "aaas_reader.html",
        "https://www.science.org/doi/reader/10.1126/sciadv.aec3536",
    )

    reader_link = select_aaas_reader(article)
    paper = select_aaas_pdf(reader)
    si = select_aaas_si(article + article)

    assert reader_link is not None
    assert reader_link["url"] == (
        "https://www.science.org/doi/reader/10.1126/sciadv.aec3536"
    )
    assert paper is not None
    assert paper["url"] == (
        "https://www.science.org/doi/pdf/10.1126/sciadv.aec3536?download=true"
    )
    assert [Path(item["url"]).suffix for item in si] == [".pdf", ".zip"]


def test_new_doi_prefixes_are_registered():
    assert get_adapter("10.1063/5.0176000").key == "AIP"
    assert get_adapter("10.1126/sciadv.aec3536").key == "AAAS"
    keys = {item["key"] for item in supported_publishers()}
    assert {"AIP", "AAAS"}.issubset(keys)

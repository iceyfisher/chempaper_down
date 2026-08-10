from pathlib import Path
import re


INDEX = Path(__file__).parents[1] / "paper_tool" / "static" / "index.html"


def test_web_timeout_choices_are_the_requested_three_values():
    html = INDEX.read_text(encoding="utf-8")
    select = re.search(r'<select id="timeout">(.*?)</select>', html, re.DOTALL)

    assert select is not None
    assert re.findall(r'<option value="(\d+)"', select.group(1)) == ["180", "210", "240"]
    assert '<option value="180" selected>' in select.group(1)


def test_web_copy_is_concise_and_timeout_help_is_interactive():
    html = INDEX.read_text(encoding="utf-8")

    assert "每个 DOI 使用独立 Python 子进程 + 独立 Edge" not in html
    assert "批量下载论文正文和补充材料" in html
    assert 'id="timeoutHelp" aria-live="polite"' in html
    assert "addEventListener('change',updateTimeoutHelp)" in html
    assert "Wiley 会自动使用 600 秒预算" in html

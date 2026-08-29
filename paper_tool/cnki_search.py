"""CNKI (知网) keyword search in an isolated browser subprocess.

Runs as:

    python -m paper_tool.cnki_search --query "..." --output result.json

The API process never loads Pydoll: this subprocess owns the Edge instance and
writes a JSON result file, mirroring the one-DOI-one-subprocess isolation model
used for downloads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import quote_plus

from .browser import BrowserWorker
from .config import Settings
from .storage import write_json_atomic


SEARCH_URL_TEMPLATES = (
    "https://kns.cnki.net/kns8s/defaultresult/index?dbcode=CJFQ&korder=SU&kw={query}",
    "https://search.cnki.com.cn/Search/Result?content={query}",
)

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,;，；\"']+")
YEAR_RE = re.compile(r"(19|20)\d{2}")

RESULT_DISCOVERY_JS = r"""
(() => {
  const patterns = [
    /\/kcms2\/article\//,
    /kcms\/detail\/detail\.aspx/,
    /KXReader\/Detail/,
    /mall\.cnki\.net\/magazine\/Article\//
  ];
  const records = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href || '';
    if (!patterns.some(p => p.test(href)) || seen.has(href)) continue;
    seen.add(href);
    const row = a.closest('tr, li, div');
    records.push({
      url: href,
      text: (a.textContent || '').replace(/\s+/g, ' ').trim(),
      rowText: row ? (row.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 500) : ''
    });
  }
  return { records: records };
})()
"""


def _unwrap(value):
    if not isinstance(value, dict):
        return value
    if "type" in value and "value" in value:
        return value["value"]
    if "result" in value:
        return _unwrap(value["result"])
    if "value" in value:
        return value["value"]
    return value


def parse_result_rows(records: list[dict]) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for record in records:
        url = str(record.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        title = str(record.get("text") or "").strip()
        row_text = str(record.get("rowText") or "")
        doi_match = DOI_RE.search(row_text)
        year_match = YEAR_RE.search(row_text)
        rows.append(
            {
                "title": title or (row_text[:80] if row_text else url),
                "url": url,
                "doi": doi_match.group(0).rstrip(".,").lower() if doi_match else None,
                "year": year_match.group(0) if year_match else None,
                "snippet": row_text[:260],
                "source": "cnki",
            }
        )
    return rows[:20]


async def run_search(query: str, settings: Settings, timeout: float) -> dict:
    started = time.monotonic()
    worker = BrowserWorker(1, settings)
    await worker.start()
    try:
        tab = worker.main_tab
        for template in SEARCH_URL_TEMPLATES:
            url = template.format(query=quote_plus(query))
            try:
                await asyncio.wait_for(
                    tab.go_to(url), timeout=settings.navigation_timeout_seconds
                )
            except Exception:
                pass
            await asyncio.sleep(settings.settle_seconds + 1.0)
            try:
                raw = await asyncio.wait_for(
                    tab.execute_script(RESULT_DISCOVERY_JS, return_by_value=True),
                    timeout=10,
                )
            except Exception:
                raw = None
            records = (_unwrap(raw) or {}).get("records") or []
            rows = parse_result_rows(records)
            if rows:
                return {
                    "query": query,
                    "search_url": template.split("?")[0],
                    "count": len(rows),
                    "rows": rows,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                }
        return {
            "query": query,
            "search_url": None,
            "count": 0,
            "rows": [],
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
    finally:
        try:
            await worker.close()
        except Exception:
            try:
                await worker.force_cleanup()
            except Exception:
                pass


async def async_main() -> int:
    parser = argparse.ArgumentParser(description="CNKI keyword search subprocess")
    parser.add_argument("--query", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    settings = Settings.from_env()
    try:
        payload = await asyncio.wait_for(
            run_search(args.query, settings, args.timeout), timeout=args.timeout
        )
    except asyncio.TimeoutError:
        payload = {
            "query": args.query,
            "error": f"cnki search exceeded {args.timeout}s",
            "count": 0,
            "rows": [],
        }
    write_json_atomic(Path(args.output), payload)
    print(json.dumps({"count": payload.get("count", 0)}, ensure_ascii=False), flush=True)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()

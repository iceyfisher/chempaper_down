from __future__ import annotations

import json
import re
from urllib.parse import urljoin

from .base import AdapterContext, PublisherAdapter
from ..download import OriginBridgeManager, blob_download, native_navigation_download
from ..models import ArticleResult
from ..resources import IMAGE_EXTENSIONS, infer_extension
from ..storage import doi_to_filename


DISCOVERY_JS = r"""
(() => {
  const norm = v => (v || '').replace(/\s+/g, ' ').trim();
  const records = [];
  const seen = new Set();
  function add(a, source, contextText) {
    if (!a || !a.href) return;
    const key = source + '||' + a.href + '||' + norm(a.textContent);
    if (seen.has(key)) return;
    seen.add(key);
    records.push({
      href: a.href,
      text: norm(a.textContent),
      source,
      parentText: norm(contextText || a.parentElement?.textContent || '')
    });
  }
  const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')];
  for (const h of headings) {
    const text = norm(h.textContent);
    if (/^supplementary\s+(file|material)\s*\d+/i.test(text) || /^esm\s*\d+/i.test(text)) {
      for (const a of h.querySelectorAll('a[href]')) add(a, 'file_heading', text);
    }
  }
  const suppHeading = headings.find(h => {
    const t = norm(h.textContent).toLowerCase();
    return t.includes('supplementary information') || t.includes('electronic supplementary material');
  });
  const container = suppHeading ? (suppHeading.closest('section') || suppHeading.parentElement) : null;
  if (container) {
    for (const a of container.querySelectorAll('a[href]')) add(a, 'supplement_section', a.parentElement?.textContent);
  }
  for (const a of document.querySelectorAll('a[href]')) {
    const href = (a.href || '').toLowerCase();
    const text = (norm(a.textContent) + ' ' + norm(a.parentElement?.textContent)).toLowerCase();
    if (href.includes('/springer-static/esm/') || href.includes('/esm/')) {
      add(a, 'global_esm_url', a.parentElement?.textContent);
    } else if ((href.includes('media.springernature.com') || href.includes('static-content.springer.com')) &&
               (text.includes('supplementary file') || text.includes('supplementary material') || text.includes('electronic supplementary'))) {
      add(a, 'global_supp_text', a.parentElement?.textContent);
    }
  }
  return {records};
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


def _supp_number(text: str) -> int | None:
    match = re.search(r"supplementary\s+(?:file|material)\s*(\d+)", text, re.I)
    if not match:
        match = re.search(r"\besm\s*(\d+)\b", text, re.I)
    return int(match.group(1)) if match else None


def _score(item: dict) -> int:
    source = item.get("source", "")
    text = (item.get("text", "") + " " + item.get("parentText", "")).lower()
    url = item.get("href", "").lower()
    score = {"file_heading": 100, "supplement_section": 70, "global_esm_url": 45, "global_supp_text": 30}.get(source, 0)
    if "supplementary file" in text:
        score += 60
    if "supplementary material" in text:
        score += 40
    if "/springer-static/esm/" in url:
        score += 60
    elif "/esm/" in url:
        score += 50
    if item.get("text", "").strip().lower() == "image":
        score -= 100
    return score


def _select_si(records: list[dict]) -> list[dict]:
    enriched = []
    for index, item in enumerate(records):
        text = item.get("text", "") + " " + item.get("parentText", "")
        enriched.append({
            **item,
            "number": _supp_number(text),
            "score": _score(item),
            "extension": infer_extension(item.get("href", ""), text),
            "index": index,
        })
    # canonical URL dedupe
    by_url: dict[str, dict] = {}
    for item in enriched:
        key = item["href"].split("#", 1)[0]
        if key not in by_url or item["score"] > by_url[key]["score"]:
            by_url[key] = item
    # Supplementary file N dedupe; preserve high-confidence unnumbered ESM.
    numbered: dict[int, dict] = {}
    unnumbered = []
    for item in by_url.values():
        if item["number"] is None:
            if item["score"] >= 50:
                unnumbered.append(item)
        elif item["number"] not in numbered or item["score"] > numbered[item["number"]]["score"]:
            numbered[item["number"]] = item
    selected = list(numbered.values()) + unnumbered
    selected.sort(key=lambda x: (x["number"] is None, x["number"] or 999999, x["index"]))
    return selected


class SpringerAdapter(PublisherAdapter):
    key = "SPRINGER"
    publisher_name = "Springer Nature"

    @classmethod
    def matches_doi(cls, doi: str) -> bool:
        return doi.startswith("10.1007/")

    async def run(self, ctx: AdapterContext) -> ArticleResult:
        article_url = await self.navigate(ctx)
        tab = ctx.tab
        pdf_element = await tab.query('a[href*="/content/pdf/"]', timeout=25, raise_exc=False)
        journal = await self.journal_from_meta(tab, "Unknown Journal")
        _, paper_dir, si_dir = self.dirs(ctx, journal)
        result = ArticleResult(
            doi=ctx.doi,
            publisher=self.publisher_name,
            journal=journal,
            article_url=article_url,
            title=await tab.title,
        )

        if pdf_element:
            href = pdf_element.get_attribute("href") or ""
            pdf_url = urljoin(await tab.current_url, href)
            target = paper_dir / f"{doi_to_filename(ctx.doi)}.pdf"
            # Verified SpringerLink behavior: direct navigation is faster and
            # reliable; ERR_ABORTED is expected when Chromium hands off download.
            path = await native_navigation_download(
                ctx.worker, pdf_url, target,
                timeout=min(ctx.settings.native_download_timeout_seconds, 45),
            )
            result.paper = self.file_result("paper", path, pdf_url, "native_navigation", extension=".pdf")

        # Ensure article page remained/restored before SI discovery.
        marker = await tab.query('a[href*="/content/pdf/"]', timeout=2, raise_exc=False)
        if not marker:
            try:
                await tab.go_to(article_url)
            except Exception:
                pass
            await tab.query('a[href*="/content/pdf/"]', timeout=20, raise_exc=False)

        raw = await tab.execute_script(DISCOVERY_JS, return_by_value=True)
        data = _unwrap(raw) or {}
        selected = _select_si(data.get("records", []))
        result.diagnostics["springer_raw_si_candidates"] = len(data.get("records", []))
        result.diagnostics["springer_selected_si"] = len(selected)

        bridges = OriginBridgeManager(ctx.worker, tab)
        try:
            for idx, item in enumerate(selected, 1):
                url = item["href"]
                ext = item["extension"]
                target = si_dir / f"{doi_to_filename(ctx.doi)}_si_{idx:03d}{ext}"
                existing = self.existing_file_result("si", target, url, ext)
                if existing:
                    result.si.append(existing)
                    continue
                if ext in IMAGE_EXTENSIONS:
                    context_tab = await bridges.get(url)
                    path = await blob_download(
                        context_tab, ctx.worker.staging_dir, url, target,
                        min(ctx.settings.blob_download_timeout_seconds, 75),
                    )
                    method = "same_origin_blob"
                    if not path:
                        path = await native_navigation_download(
                            ctx.worker, url, target,
                            timeout=min(ctx.settings.native_download_timeout_seconds, 30),
                        )
                        method = "native_navigation"
                else:
                    path = await native_navigation_download(
                        ctx.worker, url, target,
                        timeout=min(ctx.settings.native_download_timeout_seconds, 35),
                    )
                    method = "native_navigation"
                    if not path:
                        context_tab = await bridges.get(url)
                        path = await blob_download(
                            context_tab, ctx.worker.staging_dir, url, target,
                            min(ctx.settings.blob_download_timeout_seconds, 75),
                        )
                        method = "same_origin_blob"
                result.si.append(self.file_result("si", path, url, method, extension=ext))
        finally:
            await bridges.close()

        return result

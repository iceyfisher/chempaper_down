from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .browser import BrowserWorker
from .resources import infer_extension, obvious_error_payload, resolve_download_extension
from .storage import sha256_file, validate_file


@dataclass(slots=True)
class DownloadArtifact:
    path: Path
    extension: str
    final_url: str | None = None
    original_filename: str | None = None
    declared_mime_type: str | None = None
    content_disposition: str | None = None
    response_headers: dict[str, str] = field(default_factory=dict)


def _unwrap_script_result(value):
    if not isinstance(value, dict):
        return value
    if "type" in value and "value" in value:
        return value["value"]
    if "result" in value:
        return _unwrap_script_result(value["result"])
    if "value" in value:
        return value["value"]
    return value


async def move_downloaded(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    shutil.move(str(source), str(target))
    return target


async def wait_for_staging_download(
    staging_dir: Path,
    before: set[Path],
    timeout: float,
) -> Path | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_path: Path | None = None
    last_size: int | None = None
    stable = 0

    while loop.time() < deadline:
        current = {p.resolve() for p in staging_dir.iterdir() if p.is_file()}
        new = current - before
        completed = [
            p for p in new
            if not p.name.lower().endswith((".crdownload", ".tmp"))
        ]
        if completed:
            completed.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            candidate = completed[0]
            size = candidate.stat().st_size
            if candidate == last_path and size == last_size:
                stable += 1
            else:
                stable = 0
            last_path, last_size = candidate, size
            if stable >= 2:
                return candidate
        await asyncio.sleep(0.5)
    return None


async def native_navigation_download(
    worker: BrowserWorker,
    url: str,
    target: Path,
    timeout: float,
) -> Path | None:
    worker.clear_staging()
    tab = await worker.browser.new_tab()
    try:
        try:
            async with tab.expect_download(keep_file_at=worker.staging_dir, timeout=timeout) as download:
                try:
                    await asyncio.wait_for(tab.go_to(url), timeout=timeout)
                except Exception:
                    # net::ERR_ABORTED is normal when Chromium hands an attachment
                    # to its Download Manager.
                    pass
            source = Path(download.file_path)
            if source.exists():
                return await move_downloaded(source, target)
        except Exception:
            return None
        return None
    finally:
        try:
            await tab.close()
        except Exception:
            pass


async def blob_download(
    tab,
    staging_dir: Path,
    url: str,
    target: Path,
    timeout: float,
    *,
    link_text: str = "",
) -> DownloadArtifact | None:
    target.parent.mkdir(parents=True, exist_ok=True)
    script = f"""
    (async () => {{
      const response = await fetch({json.dumps(url)}, {{
        method: 'GET', credentials: 'include', redirect: 'follow', cache: 'no-store'
      }});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const blob = await response.blob();
      if (!blob || blob.size === 0) throw new Error('EMPTY_BLOB');
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = objectUrl;
      a.download = {json.dumps(target.name)};
      a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
      return {{
        size: blob.size,
        type: blob.type,
        finalUrl: response.url,
        contentType: response.headers.get('content-type') || blob.type || '',
        contentDisposition: response.headers.get('content-disposition') || '',
        contentLength: response.headers.get('content-length') || ''
      }};
    }})()
    """
    try:
        async with tab.expect_download(keep_file_at=staging_dir, timeout=timeout) as download:
            raw = await tab.execute_script(
                script,
                await_promise=True,
                return_by_value=True,
                user_gesture=True,
                timeout=int(timeout * 1000),
            )
        source = Path(download.file_path)
        if source.exists():
            meta = _unwrap_script_result(raw)
            if not isinstance(meta, dict):
                meta = {}
            content_type = str(meta.get("contentType") or meta.get("type") or "")
            content_disposition = str(meta.get("contentDisposition") or "")
            with source.open("rb") as handle:
                head = handle.read(512)
            extension, original_filename = resolve_download_extension(
                str(meta.get("finalUrl") or url),
                link_text,
                declared_mime_type=content_type,
                content_disposition=content_disposition,
                head=head,
            )
            mime = content_type.split(";", 1)[0].strip().lower()
            looks_json = head.lstrip(b"\xef\xbb\xbf\x00\t\r\n ").startswith((b"{", b"["))
            json_was_declared = (
                infer_extension(str(meta.get("finalUrl") or url), link_text) == ".json"
                or infer_extension(original_filename or "") == ".json"
            )
            if (mime in {"application/json", "text/json"} or looks_json) and not json_was_declared:
                return None
            error = obvious_error_payload(
                head,
                declared_mime_type=content_type,
                extension=extension,
            )
            if error:
                return None
            valid, _ = validate_file(source, extension)
            if not valid:
                return None

            final_target = target.with_suffix(extension)
            final_path = await move_downloaded(source, final_target)
            response_headers = {
                key: value
                for key, value in {
                    "content-type": content_type,
                    "content-disposition": content_disposition,
                    "content-length": str(meta.get("contentLength") or ""),
                }.items()
                if value
            }
            return DownloadArtifact(
                path=final_path,
                extension=extension,
                final_url=str(meta.get("finalUrl") or url),
                original_filename=original_filename,
                declared_mime_type=content_type.split(";", 1)[0].strip() or None,
                content_disposition=content_disposition or None,
                response_headers=response_headers,
            )
    except Exception:
        return None
    return None


async def click_element_and_wait(
    worker: BrowserWorker,
    element,
    target: Path,
    timeout: float,
    *,
    js_only: bool = False,
) -> Path | None:
    worker.clear_staging()
    before = {p.resolve() for p in worker.staging_dir.iterdir() if p.is_file()}
    try:
        if js_only:
            await element.execute_script("this.click()", user_gesture=True)
        else:
            try:
                await element.scroll_into_view()
            except Exception:
                pass
            try:
                await element.click(humanize=True)
            except Exception:
                await element.execute_script("this.click()", user_gesture=True)
    except Exception:
        return None

    source = await wait_for_staging_download(worker.staging_dir, before, timeout)
    if source is None:
        return None
    return await move_downloaded(source, target)


class OriginBridgeManager:
    """Creates one tab per foreign origin for same-origin fetch->Blob downloads."""

    def __init__(self, worker: BrowserWorker, article_tab):
        self.worker = worker
        self.article_tab = article_tab
        self.tabs: dict[str, object] = {}

    async def get(self, url: str):
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        article_parsed = urlparse(await self.article_tab.current_url)
        article_origin = f"{article_parsed.scheme}://{article_parsed.netloc}"
        if origin == article_origin:
            return self.article_tab
        if origin in self.tabs:
            return self.tabs[origin]

        tab = await self.worker.browser.new_tab()
        try:
            await asyncio.wait_for(tab.go_to(origin + "/"), timeout=10)
        except Exception:
            pass
        self.tabs[origin] = tab
        return tab

    async def close(self):
        for tab in list(self.tabs.values()):
            try:
                await tab.close()
            except Exception:
                pass
        self.tabs.clear()


def result_metadata(path: Path, extension: str | None = None) -> dict:
    valid, reason = validate_file(path, extension)
    return {
        "valid": valid,
        "reason": reason,
        "size": path.stat().st_size if path.exists() else None,
        "sha256": sha256_file(path) if path.exists() and path.is_file() else None,
    }

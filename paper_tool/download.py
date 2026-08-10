from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

from .browser import BrowserWorker
from .storage import sha256_file, validate_file


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
) -> Path | None:
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
      return {{size: blob.size, type: blob.type, finalUrl: response.url}};
    }})()
    """
    try:
        async with tab.expect_download(keep_file_at=staging_dir, timeout=timeout) as download:
            await tab.execute_script(
                script,
                await_promise=True,
                return_by_value=True,
                user_gesture=True,
                timeout=int(timeout * 1000),
            )
        source = Path(download.file_path)
        if source.exists():
            return await move_downloaded(source, target)
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

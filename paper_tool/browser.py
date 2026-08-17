from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import psutil
from pydoll.browser.chromium import Edge
from pydoll.browser.managers.temp_dir_manager import TempDirectoryManager
from pydoll.browser.options import ChromiumOptions

from .config import Settings


# Pydoll/Chromium Windows temp cleanup race observed in real runs.
_ORIGINAL_CLEANUP_HANDLER = TempDirectoryManager.handle_cleanup_error


def _patched_cleanup_error_handler(self, func, path, exc_info):
    _, exc_value, _ = exc_info
    if isinstance(exc_value, FileNotFoundError):
        return
    return _ORIGINAL_CLEANUP_HANDLER(self, func, path, exc_info)


if os.name == "nt":
    TempDirectoryManager.handle_cleanup_error = _patched_cleanup_error_handler


def tab_identity(tab) -> str:
    return str(
        getattr(tab, "_target_id", None)
        or getattr(tab, "target_id", None)
        or id(tab)
    )


def _kill_descendants_sync() -> None:
    """Kill only descendants of the current DOI subprocess.

    This never enumerates/kills unrelated user Edge instances. The browser spawned
    by Pydoll is a descendant of the DOI worker process, so this is a safe final
    cleanup barrier when CDP/WebSocket shutdown is already broken.
    """

    try:
        process = psutil.Process(os.getpid())
        children = process.children(recursive=True)
    except Exception:
        return

    for child in children:
        try:
            child.terminate()
        except Exception:
            pass
    try:
        _, alive = psutil.wait_procs(children, timeout=2)
    except Exception:
        alive = children
    for child in alive:
        try:
            child.kill()
        except Exception:
            pass


class BrowserWorker:
    """One Edge instance inside one short-lived DOI subprocess."""

    def __init__(self, worker_id: int, settings: Settings):
        self.worker_id = worker_id
        self.settings = settings
        self.staging_dir = settings.download_root / "_staging" / f"doi_process_{os.getpid()}"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._edge_context = None
        self.browser = None
        self.main_tab = None

    def clear_staging(self) -> None:
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        for path in list(self.staging_dir.iterdir()):
            try:
                if path.is_file():
                    path.unlink()
                else:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass

    async def start(self) -> "BrowserWorker":
        self.clear_staging()
        options = ChromiumOptions()
        options.headless = True
        options.set_default_download_directory(str(self.staging_dir.resolve()))
        options.prompt_for_download = False
        options.allow_automatic_downloads = True
        options.open_pdf_externally = True
        self._edge_context = Edge(options=options)
        self.browser = await self._edge_context.__aenter__()
        self.main_tab = await self.browser.start()
        return self

    async def close_extra_tabs(self) -> None:
        if not self.browser or not self.main_tab:
            return
        main_id = tab_identity(self.main_tab)
        try:
            tabs = await asyncio.wait_for(self.browser.get_opened_tabs(), timeout=4)
        except Exception:
            return
        for tab in tabs:
            if tab_identity(tab) == main_id:
                continue
            try:
                await asyncio.wait_for(tab.close(), timeout=2)
            except Exception:
                pass
        try:
            await asyncio.wait_for(self.main_tab.bring_to_front(), timeout=2)
        except Exception:
            pass

    async def close(self) -> None:
        """Best-effort graceful close, then descendant-only process cleanup."""

        if self._edge_context is not None:
            try:
                await asyncio.wait_for(
                    self._edge_context.__aexit__(None, None, None),
                    timeout=5,
                )
            except Exception:
                # WebSocketConnectionClosed / Runtime command timeout / broken CDP
                # are cleanup conditions, not reasons to keep the child process alive.
                pass
            finally:
                self._edge_context = None
                self.browser = None
                self.main_tab = None

        await asyncio.to_thread(_kill_descendants_sync)
        try:
            shutil.rmtree(self.staging_dir, ignore_errors=True)
        except Exception:
            pass

    async def force_cleanup(self) -> None:
        await asyncio.to_thread(_kill_descendants_sync)
        try:
            shutil.rmtree(self.staging_dir, ignore_errors=True)
        except Exception:
            pass

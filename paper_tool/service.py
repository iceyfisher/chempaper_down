from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import psutil

from .config import Settings
from .models import ArticleResult, FileResult, ItemStatus
from .storage import find_existing_paper, sha256_file, write_json_atomic


ProgressCallback = Callable[[ArticleResult], Awaitable[None] | None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _emit(callback: ProgressCallback | None, result: ArticleResult) -> None:
    if callback is None:
        return
    value = callback(result)
    if asyncio.iscoroutine(value):
        await value


def _duplicate_result(doi: str, existing: Path) -> ArticleResult:
    return ArticleResult(
        doi=doi,
        status=ItemStatus.SKIPPED_DUPLICATE,
        paper=FileResult(
            kind="paper",
            path=str(existing),
            extension=".pdf",
            size=existing.stat().st_size,
            sha256=sha256_file(existing),
            valid=True,
            existing=True,
            method="duplicate_scan_parent",
        ),
        message=f"重复下载 跳过任务doi：{doi}",
        started_at=_now(),
        finished_at=_now(),
        elapsed_seconds=0.0,
    )


def _kill_process_tree_sync(pid: int) -> None:
    """Kill a DOI worker process and only its descendants (including Edge)."""

    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return

    try:
        children = parent.children(recursive=True)
    except psutil.Error:
        children = []

    # Children first so Edge cannot remain orphaned.
    for proc in reversed(children):
        try:
            proc.terminate()
        except psutil.Error:
            pass
    try:
        parent.terminate()
    except psutil.Error:
        pass

    try:
        _, alive = psutil.wait_procs(children + [parent], timeout=2)
    except Exception:
        alive = children + [parent]

    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass


class DownloadService:
    """Crash-isolated batch orchestrator.

    Architecture:

        FastAPI / CLI parent
             |
             +-- DOI subprocess A -- Pydoll -- Edge A
             +-- DOI subprocess B -- Pydoll -- Edge B

    No Pydoll object lives in the API process. If Runtime.evaluate, navigation,
    Cloudflare handling or browser shutdown blocks, the parent hard-kills only that
    DOI subprocess and its descendant Edge process after the configured budget.
    """

    def __init__(self, settings: Settings):
        self.settings = settings.normalized()
        self.settings.download_root.mkdir(parents=True, exist_ok=True)
        self.manifest_dir = self.settings.download_root / "_manifests"
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir = self.settings.download_root / "_worker_runs"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = self.settings.download_root / "_logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _persist_result(self, result: ArticleResult) -> None:
        safe = result.doi.replace("/", "_")
        write_json_atomic(self.manifest_dir / f"{safe}.json", result.to_dict())

    async def _run_subprocess(
        self,
        doi: str,
        slot: int,
        callback: ProgressCallback | None,
    ) -> ArticleResult:
        start = time.monotonic()
        start_iso = _now()
        run_id = uuid.uuid4().hex[:10]
        safe = doi.replace("/", "_")
        work_dir = self.run_dir / f"{safe}_{run_id}"
        work_dir.mkdir(parents=True, exist_ok=True)
        request_path = work_dir / "request.json"
        result_path = work_dir / "result.json"
        log_path = self.log_dir / f"{safe}_{run_id}.log"

        request_payload = {
            "doi": doi,
            "settings": self.settings.to_worker_payload(),
        }
        write_json_atomic(request_path, request_payload)

        running = ArticleResult(
            doi=doi,
            status=ItemStatus.RUNNING,
            started_at=start_iso,
            message=f"slot-{slot}: starting isolated DOI subprocess",
            diagnostics={"run_id": run_id, "log_path": str(log_path)},
        )
        await _emit(callback, running)

        cmd = [
            sys.executable,
            "-m",
            "paper_tool.worker_main",
            "--request",
            str(request_path),
            "--result",
            str(result_path),
        ]

        subprocess_kwargs: dict = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
            "cwd": str(Path.cwd()),
        }
        if os.name == "nt":
            # CREATE_NEW_PROCESS_GROUP. The authoritative cleanup still uses psutil
            # tree termination, but the group boundary makes manual debugging sane.
            subprocess_kwargs["creationflags"] = 0x00000200
        else:
            subprocess_kwargs["start_new_session"] = True

        process = await asyncio.create_subprocess_exec(*cmd, **subprocess_kwargs)
        last_event: dict | None = None

        async def consume_output() -> None:
            nonlocal last_event
            assert process.stdout is not None
            with log_path.open("w", encoding="utf-8", errors="replace") as log:
                while True:
                    line_b = await process.stdout.readline()
                    if not line_b:
                        break
                    line = line_b.decode("utf-8", errors="replace").rstrip()
                    log.write(line + "\n")
                    log.flush()
                    if line.startswith("PAPER_TOOL_EVENT "):
                        try:
                            last_event = json.loads(line[len("PAPER_TOOL_EVENT "):])
                        except Exception:
                            last_event = {"message": line}
                        update = ArticleResult(
                            doi=doi,
                            status=ItemStatus.RUNNING,
                            started_at=start_iso,
                            elapsed_seconds=round(time.monotonic() - start, 2),
                            message=last_event.get("message") or last_event.get("stage"),
                            diagnostics={
                                "run_id": run_id,
                                "log_path": str(log_path),
                                "last_event": last_event,
                                "process_pid": process.pid,
                            },
                        )
                        await _emit(callback, update)

        reader_task = asyncio.create_task(consume_output())
        timed_out = False
        try:
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self.settings.article_timeout_seconds,
                )
            except asyncio.TimeoutError:
                timed_out = True
                await asyncio.to_thread(_kill_process_tree_sync, process.pid)
                try:
                    await asyncio.wait_for(process.wait(), timeout=self.settings.subprocess_kill_grace_seconds)
                except Exception:
                    pass
        except asyncio.CancelledError:
            # Server/job cancellation must not orphan Edge. Kill only this DOI
            # process tree, wait briefly, then propagate cancellation.
            await asyncio.to_thread(_kill_process_tree_sync, process.pid)
            try:
                await asyncio.wait_for(process.wait(), timeout=self.settings.subprocess_kill_grace_seconds)
            except Exception:
                pass
            raise
        finally:
            try:
                await asyncio.wait_for(reader_task, timeout=3)
            except Exception:
                reader_task.cancel()

        elapsed = round(time.monotonic() - start, 3)

        if timed_out:
            item = ArticleResult(
                doi=doi,
                status=ItemStatus.TIMEOUT,
                started_at=start_iso,
                finished_at=_now(),
                elapsed_seconds=elapsed,
                message=(
                    f"DOI subprocess exceeded {self.settings.article_timeout_seconds}s; "
                    "process tree and its Edge descendants were terminated"
                ),
                diagnostics={
                    "run_id": run_id,
                    "log_path": str(log_path),
                    "last_event": last_event,
                    "process_pid": process.pid,
                },
            )
            self._persist_result(item)
            return item

        if result_path.exists():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
                item = ArticleResult.from_dict(data)
                # Parent owns authoritative timing.
                item.elapsed_seconds = elapsed
                item.diagnostics = {
                    **(item.diagnostics or {}),
                    "run_id": run_id,
                    "log_path": str(log_path),
                    "last_event": last_event,
                    "process_pid": process.pid,
                    "subprocess_returncode": process.returncode,
                }
                self._persist_result(item)
                return item
            except Exception as exc:
                parse_error = repr(exc)
        else:
            parse_error = "result.json missing"

        item = ArticleResult(
            doi=doi,
            status=ItemStatus.PROCESS_ERROR,
            started_at=start_iso,
            finished_at=_now(),
            elapsed_seconds=elapsed,
            message=f"DOI subprocess exited without a valid result: {parse_error}",
            diagnostics={
                "run_id": run_id,
                "log_path": str(log_path),
                "last_event": last_event,
                "process_pid": process.pid,
                "subprocess_returncode": process.returncode,
            },
        )
        self._persist_result(item)
        return item

    async def run_batch(
        self,
        dois: list[str],
        *,
        callback: ProgressCallback | None = None,
    ) -> list[ArticleResult]:
        # Preserve submitted order and remove duplicates inside the request itself.
        ordered: list[str] = []
        seen: set[str] = set()
        for doi in dois:
            if doi not in seen:
                seen.add(doi)
                ordered.append(doi)

        result_by_doi: dict[str, ArticleResult] = {}
        pending: list[str] = []

        # HARD GLOBAL DUPLICATE CHECK BEFORE ANY CHILD/EDGE PROCESS STARTS.
        for doi in ordered:
            existing = find_existing_paper(self.settings.download_root, doi)
            if existing:
                item = _duplicate_result(doi, existing)
                result_by_doi[doi] = item
                self._persist_result(item)
                await _emit(callback, item)
            else:
                pending.append(doi)

        if not pending:
            return [result_by_doi[x] for x in ordered]

        semaphore = asyncio.Semaphore(min(self.settings.max_concurrency, len(pending)))
        slot_counter = 0
        slot_lock = asyncio.Lock()

        async def execute(doi: str) -> None:
            nonlocal slot_counter
            async with semaphore:
                # Re-check immediately before subprocess creation in case another job
                # finished the same DOI while this task was waiting for a slot.
                existing = find_existing_paper(self.settings.download_root, doi)
                if existing:
                    item = _duplicate_result(doi, existing)
                    result_by_doi[doi] = item
                    self._persist_result(item)
                    await _emit(callback, item)
                    return

                async with slot_lock:
                    slot_counter += 1
                    slot = ((slot_counter - 1) % self.settings.max_concurrency) + 1

                item = await self._run_subprocess(doi, slot, callback)
                result_by_doi[doi] = item
                await _emit(callback, item)

        tasks = [asyncio.create_task(execute(doi)) for doi in pending]
        await asyncio.gather(*tasks)
        return [result_by_doi[x] for x in ordered]

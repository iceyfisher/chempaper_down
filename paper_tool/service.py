from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import psutil

from .config import Settings
from .models import ArticleResult, FileResult, ItemStatus
from .storage import (
    doi_to_filename,
    find_existing_paper,
    load_article_manifest,
    manifest_has_complete_si,
    sha256_file,
    write_json_atomic,
)


ProgressCallback = Callable[[ArticleResult], Awaitable[None] | None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _duplicate_scan(download_root, doi: str):
    """Disk-bound duplicate pre-scan, safe to run in a worker thread."""

    return (
        find_existing_paper(download_root, doi),
        load_article_manifest(download_root, doi),
    )


def _read_worker_result(result_path: Path) -> tuple[ArticleResult | None, str | None]:
    if not result_path.exists():
        return None, "result.json missing"
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
        return ArticleResult.from_dict(data), None
    except Exception as exc:
        return None, repr(exc)


async def _emit(callback: ProgressCallback | None, result: ArticleResult) -> None:
    if callback is None:
        return
    value = callback(result)
    if asyncio.iscoroutine(value):
        await value


def _duplicate_result(
    doi: str,
    existing: Path,
    manifest: dict | None = None,
) -> ArticleResult:
    try:
        result = ArticleResult.from_dict(manifest) if manifest else ArticleResult(doi=doi)
    except (KeyError, TypeError, ValueError):
        result = ArticleResult(doi=doi)
    result.status = ItemStatus.SKIPPED_DUPLICATE
    result.paper = FileResult(
        kind="paper",
        path=str(existing),
        extension=".pdf",
        size=existing.stat().st_size,
        sha256=sha256_file(existing),
        valid=True,
        existing=True,
        method="duplicate_scan_parent_complete_si",
    )
    result.message = f"重复下载 跳过任务doi：{doi}（正文和 SI 已完整校验）"
    result.started_at = _now()
    result.finished_at = _now()
    result.elapsed_seconds = 0.0
    diagnostics = {
        key: value
        for key, value in (result.diagnostics or {}).items()
        if key
        not in {
            "run_id",
            "log_path",
            "last_event",
            "cleanup_event",
            "process_pid",
            "subprocess_returncode",
            "cleanup_timeout_after_result",
        }
    }
    result.diagnostics = {
        **diagnostics,
        "duplicate_scan": "complete_si",
        "last_event": {"stage": "duplicate", "message": result.message},
    }
    return result


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
        safe = doi_to_filename(result.doi)
        write_json_atomic(self.manifest_dir / f"{safe}.json", result.to_dict())

    async def _run_subprocess(
        self,
        doi: str,
        slot: int,
        callback: ProgressCallback | None,
        *,
        download_si: bool = True,
        hint: dict | None = None,
    ) -> ArticleResult:
        start = time.monotonic()
        start_iso = _now()
        run_id = uuid.uuid4().hex[:10]
        safe = doi_to_filename(doi)
        work_dir = self.run_dir / f"{safe}_{run_id}"
        work_dir.mkdir(parents=True, exist_ok=True)
        request_path = work_dir / "request.json"
        result_path = work_dir / "result.json"
        log_path = self.log_dir / f"{safe}_{run_id}.log"

        existing_paper = find_existing_paper(self.settings.download_root, doi)
        previous_manifest = load_article_manifest(self.settings.download_root, doi)
        worker_settings = self.settings.to_worker_payload()
        # The child derives its navigation budget from this value, so hand it the
        # per-publisher budget instead of the generic default.
        worker_settings["article_timeout_seconds"] = self.settings.article_timeout_for_doi(doi)
        request_payload = {
            "doi": doi,
            "settings": worker_settings,
            "resume_si": existing_paper is not None,
            "existing_paper": str(existing_paper) if existing_paper else None,
            "previous_manifest": previous_manifest,
            "download_si": download_si,
            "article_hint": hint or {},
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
        last_work_event: dict | None = None
        cleanup_event: dict | None = None

        async def consume_output() -> None:
            nonlocal cleanup_event, last_event, last_work_event
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
                        if last_event.get("stage") == "browser_close":
                            cleanup_event = last_event
                            continue
                        else:
                            last_work_event = last_event
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

        async def emit_heartbeats() -> None:
            while process.returncode is None:
                await asyncio.sleep(5)
                if process.returncode is not None:
                    return
                work_event = last_work_event or {
                    "stage": "browser_start",
                    "message": "Waiting for isolated browser worker",
                }
                if work_event.get("stage") in {
                    "finished",
                    "failed",
                    "browser_crashed",
                    "duplicate",
                }:
                    return
                elapsed_now = round(time.monotonic() - start, 2)
                update = ArticleResult(
                    doi=doi,
                    status=ItemStatus.RUNNING,
                    started_at=start_iso,
                    elapsed_seconds=elapsed_now,
                    message=(
                        f"{work_event.get('message') or work_event.get('stage')} "
                        f"· still running ({elapsed_now}s)"
                    ),
                    diagnostics={
                        "run_id": run_id,
                        "log_path": str(log_path),
                        "last_event": work_event,
                        "heartbeat": {
                            "stage": "heartbeat",
                            "elapsed_seconds": elapsed_now,
                        },
                        "process_pid": process.pid,
                    },
                )
                await _emit(callback, update)

        heartbeat_task = asyncio.create_task(emit_heartbeats())
        timed_out = False
        effective_timeout = self.settings.timeout_for_doi(doi)
        try:
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=effective_timeout,
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
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            # Give the log reader a short grace period, but NEVER await its
            # cancellation: a readline blocked on a stdout pipe that orphaned
            # Edge children still hold open is uncancellable on the Windows
            # Proactor, and awaiting it hangs this coroutine forever — which
            # leaks the concurrency slot (browser windows decay 4→3→2→1) and
            # stalls the job. asyncio.wait() does not cancel on timeout.
            done, _ = await asyncio.wait({reader_task}, timeout=3)
            if reader_task not in done:
                reader_task.cancel()

        elapsed = round(time.monotonic() - start, 3)

        if timed_out:
            completed, _ = _read_worker_result(result_path)
            if completed is not None:
                completed.elapsed_seconds = elapsed
                completed.diagnostics = {
                    **(completed.diagnostics or {}),
                    "run_id": run_id,
                    "log_path": str(log_path),
                    "last_event": last_work_event or last_event,
                    "cleanup_event": cleanup_event,
                    "process_pid": process.pid,
                    "subprocess_returncode": process.returncode,
                    "cleanup_timeout_after_result": True,
                }
                self._persist_result(completed)
                return completed
            item = ArticleResult(
                doi=doi,
                status=ItemStatus.TIMEOUT,
                started_at=start_iso,
                finished_at=_now(),
                elapsed_seconds=elapsed,
                message=(
                    f"DOI subprocess exceeded {effective_timeout}s; "
                    "process tree and its Edge descendants were terminated"
                ),
                diagnostics={
                    "run_id": run_id,
                    "log_path": str(log_path),
                    "last_event": last_work_event or last_event,
                    "cleanup_event": cleanup_event,
                    "process_pid": process.pid,
                },
            )
            self._persist_result(item)
            return item

        item, parse_error = _read_worker_result(result_path)
        if item is not None:
            # Parent owns authoritative timing.
            item.elapsed_seconds = elapsed
            item.diagnostics = {
                **(item.diagnostics or {}),
                "run_id": run_id,
                "log_path": str(log_path),
                "last_event": last_work_event or last_event,
                "cleanup_event": cleanup_event,
                "process_pid": process.pid,
                "subprocess_returncode": process.returncode,
            }
            self._persist_result(item)
            return item

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
        download_si: bool = True,
        article_hints: dict[str, dict] | None = None,
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
        # Each scan is disk-bound; run it in a worker thread so a large
        # archive cannot block the API event loop and freeze the web UI.
        for doi in ordered:
            existing, manifest = await asyncio.to_thread(
                _duplicate_scan, self.settings.download_root, doi
            )
            if existing and manifest_has_complete_si(manifest):
                item = _duplicate_result(doi, existing, manifest)
                result_by_doi[doi] = item
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
                # The archive scan is disk-bound; keep it off the event loop so a
                # large archive cannot stall the API's request handling.
                existing, manifest = await asyncio.to_thread(
                    _duplicate_scan, self.settings.download_root, doi
                )
                if existing and manifest_has_complete_si(manifest):
                    item = _duplicate_result(doi, existing, manifest)
                    result_by_doi[doi] = item
                    await _emit(callback, item)
                    return

                async with slot_lock:
                    slot_counter += 1
                    slot = ((slot_counter - 1) % self.settings.max_concurrency) + 1

                # Belt-and-braces: nothing inside _run_subprocess may hold a
                # slot forever. The budget plus teardown margin bounds even an
                # unexpected hang; on breach a timeout result is synthesized so
                # the slot is released and the job keeps moving.
                hard_cap = self.settings.timeout_for_doi(doi) + max(
                    30, self.settings.subprocess_kill_grace_seconds * 4
                )
                try:
                    async with asyncio.timeout(hard_cap):
                        item = await self._run_subprocess(
                            doi,
                            slot,
                            callback,
                            download_si=download_si,
                            hint=(article_hints or {}).get(doi),
                        )
                except TimeoutError:
                    item = ArticleResult(
                        doi=doi,
                        status=ItemStatus.TIMEOUT,
                        message=(
                            f"DOI worker did not return within the {hard_cap}s "
                            "hard slot guard; the slot was reclaimed."
                        ),
                        diagnostics={"failure_stage": "slot_guard"},
                    )
                result_by_doi[doi] = item
                await _emit(callback, item)

        tasks = [asyncio.create_task(execute(doi)) for doi in pending]
        await asyncio.gather(*tasks)
        return [result_by_doi[x] for x in ordered]

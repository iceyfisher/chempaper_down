from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from pydoll.exceptions import WebSocketConnectionClosed

from .adapters.base import AdapterContext
from .browser import BrowserWorker
from .config import Settings
from .models import ArticleResult, ItemStatus
from .registry import get_adapter
from .storage import find_existing_paper, sha256_file, write_json_atomic
from .models import FileResult


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def event(stage: str, message: str, **extra) -> None:
    payload = {"stage": stage, "message": message, **extra}
    print("PAPER_TOOL_EVENT " + json.dumps(payload, ensure_ascii=False), flush=True)


def finalize_status(result: ArticleResult) -> ItemStatus:
    paper_ok = bool(result.paper and result.paper.valid)
    if not paper_ok:
        return ItemStatus.FAILED
    return ItemStatus.PARTIAL if any(not x.valid for x in result.si) else ItemStatus.SUCCESS


async def run_one(request_path: Path, result_path: Path) -> int:
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    doi = payload["doi"]
    settings = Settings.from_worker_payload(payload["settings"])
    resume_si = bool(payload.get("resume_si"))
    started = time.monotonic()
    start_iso = now_iso()

    # A second hard duplicate check occurs in the child immediately before any
    # browser startup. This protects against two independent jobs racing.
    existing = find_existing_paper(settings.download_root, doi)
    if existing and not resume_si:
        result = ArticleResult(
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
                method="duplicate_scan_child",
            ),
            message=f"重复下载 跳过任务doi：{doi}",
            started_at=start_iso,
            finished_at=now_iso(),
            elapsed_seconds=0.0,
        )
        write_json_atomic(result_path, result.to_dict())
        event("duplicate", result.message)
        return 0

    adapter = get_adapter(doi)
    if adapter is None:
        result = ArticleResult(
            doi=doi,
            status=ItemStatus.FAILED,
            message="Unsupported DOI/publisher. Add a PublisherAdapter.",
            started_at=start_iso,
            finished_at=now_iso(),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        write_json_atomic(result_path, result.to_dict())
        return 2

    worker = BrowserWorker(1, settings)
    try:
        event("browser_start", "Starting isolated Edge process", publisher=adapter.key)
        await worker.start()
        event("adapter", f"Running {adapter.key} adapter")
        result = await adapter.run(
            AdapterContext(
                worker=worker,
                settings=settings,
                doi=doi,
                existing_paper=existing if resume_si else None,
                previous_manifest=payload.get("previous_manifest"),
            )
        )
        result.status = finalize_status(result)
        result.started_at = start_iso
        result.finished_at = now_iso()
        result.elapsed_seconds = round(time.monotonic() - started, 3)
        event(
            "finished",
            str(result.status),
            publisher=result.publisher,
            si_successful=result.si_successful,
            si_detected=result.si_detected,
        )
        write_json_atomic(result_path, result.to_dict())
        return 0 if result.status in {ItemStatus.SUCCESS, ItemStatus.PARTIAL} else 1
    except WebSocketConnectionClosed as exc:
        result = ArticleResult(
            doi=doi,
            status=ItemStatus.BROWSER_CRASHED,
            message=f"Pydoll WebSocket closed: {exc!r}",
            started_at=start_iso,
            finished_at=now_iso(),
            elapsed_seconds=round(time.monotonic() - started, 3),
            diagnostics={"traceback": traceback.format_exc()},
        )
        write_json_atomic(result_path, result.to_dict())
        event("browser_crashed", result.message)
        return 3
    except Exception as exc:
        result = ArticleResult(
            doi=doi,
            status=ItemStatus.FAILED,
            message=repr(exc),
            started_at=start_iso,
            finished_at=now_iso(),
            elapsed_seconds=round(time.monotonic() - started, 3),
            diagnostics={"traceback": traceback.format_exc()},
        )
        write_json_atomic(result_path, result.to_dict())
        event("failed", result.message)
        return 1
    finally:
        event("browser_close", "Closing isolated Edge")
        try:
            await worker.close()
        except Exception:
            try:
                await worker.force_cleanup()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Internal one-DOI worker")
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    code = asyncio.run(run_one(Path(args.request), Path(args.result)))
    raise SystemExit(code)


if __name__ == "__main__":
    main()

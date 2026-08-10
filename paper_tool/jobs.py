from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .models import ArticleResult
from .service import DownloadService
from .storage import write_json_atomic


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobState:
    id: str
    dois: list[str]
    status: str = "queued"
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    total: int = 0
    completed: int = 0
    running: int = 0
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self):
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total": self.total,
            "completed": self.completed,
            "running": self.running,
            "results": list(self.results.values()),
            "error": self.error,
        }


TERMINAL_ITEM_STATUSES = {
    "success",
    "partial",
    "failed",
    "skipped_duplicate",
    "timeout",
    "browser_crashed",
    "process_error",
}


class JobManager:
    def __init__(self, base_settings: Settings):
        self.base_settings = base_settings.normalized()
        self.jobs: dict[str, JobState] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.state_dir = self.base_settings.download_root / "_jobs"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _save(self, job: JobState):
        write_json_atomic(self.state_dir / f"{job.id}.json", job.to_dict())

    def get(self, job_id: str) -> JobState | None:
        job = self.jobs.get(job_id)
        if job:
            return job
        path = self.state_dir / f"{job_id}.json"
        if path.exists():
            # Persisted jobs are readable after API restart even though they cannot
            # be resumed automatically.
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
            recovered = JobState(
                id=data["id"],
                dois=[],
                status=data.get("status", "unknown"),
                created_at=data.get("created_at") or now_iso(),
                started_at=data.get("started_at"),
                finished_at=data.get("finished_at"),
                total=int(data.get("total", 0)),
                completed=int(data.get("completed", 0)),
                running=int(data.get("running", 0)),
                results={x["doi"]: x for x in data.get("results", []) if x.get("doi")},
                error=data.get("error"),
            )
            return recovered
        return None

    async def submit(
        self,
        dois: list[str],
        *,
        max_concurrency: int | None = None,
        article_timeout_seconds: int | None = None,
    ) -> JobState:
        job_id = uuid.uuid4().hex[:12]
        job = JobState(id=job_id, dois=dois, total=len(dois))
        self.jobs[job_id] = job
        self._save(job)
        settings = self.base_settings.with_overrides(
            max_concurrency=max_concurrency,
            article_timeout_seconds=article_timeout_seconds,
        )
        self.tasks[job_id] = asyncio.create_task(self._run(job, settings), name=f"paper-job-{job_id}")
        return job

    async def cancel(self, job_id: str) -> bool:
        task = self.tasks.get(job_id)
        job = self.jobs.get(job_id)
        if not task or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if job:
            job.status = "cancelled"
            job.finished_at = now_iso()
            job.running = 0
            self._save(job)
        return True

    async def _run(self, job: JobState, settings: Settings):
        job.status = "running"
        job.started_at = now_iso()
        self._save(job)

        async def progress(item: ArticleResult):
            data = item.to_dict()
            job.results[item.doi] = data
            job.completed = sum(
                1 for x in job.results.values() if x.get("status") in TERMINAL_ITEM_STATUSES
            )
            job.running = sum(1 for x in job.results.values() if x.get("status") == "running")
            self._save(job)

        try:
            service = DownloadService(settings)
            await service.run_batch(job.dois, callback=progress)
            job.status = "completed"
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as exc:
            job.status = "failed"
            job.error = repr(exc)
        finally:
            job.finished_at = now_iso()
            job.running = 0
            self._save(job)

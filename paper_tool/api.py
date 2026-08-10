from __future__ import annotations

import argparse
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .config import Settings
from .doi import extract_dois, load_dois_from_file
from .jobs import JobManager
from .registry import supported_publishers


BASE_SETTINGS = Settings.from_env()
JOB_MANAGER = JobManager(BASE_SETTINGS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Graceful server shutdown: cancellation propagates to DownloadService,
    # which kills every active DOI subprocess tree and descendant Edge.
    for job_id in list(JOB_MANAGER.tasks):
        try:
            await JOB_MANAGER.cancel(job_id)
        except Exception:
            pass


app = FastAPI(title="Paper Download Tool", version="0.2.0", lifespan=lifespan)


class JobRequest(BaseModel):
    dois: list[str] = Field(default_factory=list)
    doi_text: str | None = None
    max_concurrency: int = Field(default=2, ge=1, le=4)
    article_timeout_seconds: int = Field(default=120, ge=30, le=600)


class PathJobRequest(BaseModel):
    input_path: str
    doi_field: str = "doi"
    max_concurrency: int = Field(default=2, ge=1, le=4)
    article_timeout_seconds: int = Field(default=120, ge=30, le=600)


class AgentPathRequest(BaseModel):
    input_path: str
    doi_field: str = "doi"
    max_concurrency: int = Field(default=2, ge=1, le=4)
    article_timeout_seconds: int = Field(default=120, ge=30, le=600)
    job_tag: str | None = None


class AgentContentRequest(BaseModel):
    content: str
    format: Literal["txt", "json", "jsonl", "csv"] = "txt"
    doi_field: str = "doi"
    max_concurrency: int = Field(default=2, ge=1, le=4)
    article_timeout_seconds: int = Field(default=120, ge=30, le=600)


def _normalize_submitted_dois(raw: list[str], text: str | None = None) -> list[str]:
    combined = "\n".join(raw) + "\n" + (text or "")
    return extract_dois(combined)


async def _submit(dois: list[str], concurrency: int, timeout: int):
    if not dois:
        raise HTTPException(400, "No DOI found")
    job = await JOB_MANAGER.submit(
        dois,
        max_concurrency=concurrency,
        article_timeout_seconds=timeout,
    )
    return {
        "job_id": job.id,
        "status": job.status,
        "doi_count": len(dois),
        "status_url": f"/api/jobs/{job.id}",
        "results_url": f"/api/jobs/{job.id}/results",
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "version": "0.2.0",
        "architecture": "one-doi-one-subprocess-one-edge",
        "download_root": str(BASE_SETTINGS.download_root),
        "publishers": supported_publishers(),
        "default_concurrency": BASE_SETTINGS.max_concurrency,
        "default_article_timeout_seconds": BASE_SETTINGS.article_timeout_seconds,
    }


@app.post("/api/jobs")
async def create_job(request: JobRequest):
    dois = _normalize_submitted_dois(request.dois, request.doi_text)
    return await _submit(dois, request.max_concurrency, request.article_timeout_seconds)


@app.post("/api/jobs/upload")
async def create_job_from_upload(
    file: UploadFile = File(...),
    max_concurrency: int = 2,
    article_timeout_seconds: int = 120,
    doi_field: str = "doi",
):
    data = await file.read()
    input_dir = BASE_SETTINGS.download_root / "_job_inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(file.filename or "input.txt").name
    target = input_dir / filename
    target.write_bytes(data)
    try:
        dois = load_dois_from_file(target, doi_field)
    except Exception as exc:
        raise HTTPException(400, f"Could not parse uploaded DOI file: {exc!r}") from exc
    return await _submit(dois, max_concurrency, article_timeout_seconds)


@app.post("/api/jobs/path")
async def create_job_from_path(request: PathJobRequest):
    try:
        dois = load_dois_from_file(request.input_path, request.doi_field)
    except Exception as exc:
        raise HTTPException(400, f"Could not parse DOI file: {exc!r}") from exc
    return await _submit(dois, request.max_concurrency, request.article_timeout_seconds)


@app.post("/api/agent/jobs")
async def create_agent_job(request: AgentPathRequest):
    try:
        dois = load_dois_from_file(request.input_path, request.doi_field)
    except Exception as exc:
        raise HTTPException(400, f"Could not parse agent manifest: {exc!r}") from exc
    response = await _submit(dois, request.max_concurrency, request.article_timeout_seconds)
    response["job_tag"] = request.job_tag
    return response


@app.post("/api/agent/content")
async def create_agent_job_from_content(request: AgentContentRequest):
    input_dir = BASE_SETTINGS.download_root / "_job_inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    suffix = {"txt": ".txt", "json": ".json", "jsonl": ".jsonl", "csv": ".csv"}[request.format]
    target = input_dir / f"agent_inline{suffix}"
    target.write_text(request.content, encoding="utf-8")
    try:
        dois = load_dois_from_file(target, request.doi_field)
    except Exception:
        # TXT-ish payload fallback: DOI regex across the raw body.
        dois = extract_dois(request.content)
    return await _submit(dois, request.max_concurrency, request.article_timeout_seconds)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = JOB_MANAGER.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.to_dict()


@app.get("/api/jobs/{job_id}/results")
async def get_job_results(job_id: str):
    job = JOB_MANAGER.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"job_id": job.id, "status": job.status, "results": list(job.results.values())}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = JOB_MANAGER.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    cancelled = await JOB_MANAGER.cancel(job_id)
    return {"job_id": job_id, "cancelled": cancelled, "status": JOB_MANAGER.get(job_id).status}


def main():
    parser = argparse.ArgumentParser(description="Paper Tool local API + UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--download-root", default=None)
    args = parser.parse_args()

    global BASE_SETTINGS, JOB_MANAGER
    if args.download_root:
        BASE_SETTINGS = Settings.from_env(args.download_root)
        JOB_MANAGER = JobManager(BASE_SETTINGS)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

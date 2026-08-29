from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
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
from .search import resolve_titles, search_openalex


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


app = FastAPI(title="Paper Download Tool", version="0.3.0", lifespan=lifespan)


class JobRequest(BaseModel):
    dois: list[str] = Field(default_factory=list)
    doi_text: str | None = None
    max_concurrency: int = Field(default=2, ge=1, le=4)
    article_timeout_seconds: int = Field(default=120, ge=30, le=600)
    download_si: bool = True


class PathJobRequest(BaseModel):
    input_path: str
    doi_field: str = "doi"
    max_concurrency: int = Field(default=2, ge=1, le=4)
    article_timeout_seconds: int = Field(default=120, ge=30, le=600)
    download_si: bool = True


class AgentPathRequest(BaseModel):
    input_path: str
    doi_field: str = "doi"
    max_concurrency: int = Field(default=2, ge=1, le=4)
    article_timeout_seconds: int = Field(default=120, ge=30, le=600)
    job_tag: str | None = None
    download_si: bool = True


class AgentContentRequest(BaseModel):
    content: str
    format: Literal["txt", "json", "jsonl", "csv"] = "txt"
    doi_field: str = "doi"
    max_concurrency: int = Field(default=2, ge=1, le=4)
    article_timeout_seconds: int = Field(default=120, ge=30, le=600)
    download_si: bool = True


class DownloadItem(BaseModel):
    doi: str
    title_query: str | None = None
    article_url: str | None = None
    download_si: bool = True


class ItemsJobRequest(BaseModel):
    items: list[DownloadItem] = Field(min_length=1)
    max_concurrency: int = Field(default=2, ge=1, le=4)
    article_timeout_seconds: int = Field(default=120, ge=30, le=600)


class TitleBatchRequest(BaseModel):
    titles: list[str] = Field(min_length=1)
    max_concurrency: int = Field(default=2, ge=1, le=4)
    article_timeout_seconds: int = Field(default=120, ge=30, le=600)
    download_si: bool = True
    min_similarity: float = Field(default=0.72, ge=0.0, le=1.0)
    only_resolved: bool = True


def _normalize_submitted_dois(raw: list[str], text: str | None = None) -> list[str]:
    combined = "\n".join(raw) + "\n" + (text or "")
    return extract_dois(combined)


async def _submit(
    dois: list[str],
    concurrency: int,
    timeout: int,
    download_si: bool = True,
    article_hints: dict[str, dict] | None = None,
):
    if not dois:
        raise HTTPException(400, "No DOI found")
    job = await JOB_MANAGER.submit(
        dois,
        max_concurrency=concurrency,
        article_timeout_seconds=timeout,
        download_si=download_si,
        article_hints=article_hints,
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
        "version": "0.3.0",
        "architecture": "one-doi-one-subprocess-one-edge",
        "download_root": str(BASE_SETTINGS.download_root),
        "publishers": supported_publishers(),
        "default_concurrency": BASE_SETTINGS.max_concurrency,
        "default_article_timeout_seconds": BASE_SETTINGS.article_timeout_seconds,
    }


@app.post("/api/jobs")
async def create_job(request: JobRequest):
    dois = _normalize_submitted_dois(request.dois, request.doi_text)
    return await _submit(dois, request.max_concurrency, request.article_timeout_seconds, request.download_si)


@app.post("/api/jobs/items")
async def create_job_from_items(request: ItemsJobRequest):
    """Submit download items coming from the search UI.

    Each item carries its own DOI (or a cnki: key) and may override the
    job-level SI preference; title/URL hints are forwarded to the worker.
    """

    dois: list[str] = []
    seen: set[str] = set()
    hints: dict[str, dict] = {}
    for item in request.items:
        doi = item.doi.strip()
        if not doi or doi in seen:
            continue
        seen.add(doi)
        dois.append(doi)
        hint: dict = {}
        if item.title_query:
            hint["title_query"] = item.title_query
        if item.article_url:
            hint["article_url"] = item.article_url
        if item.download_si is False:
            hint["download_si"] = False
        if hint:
            hints[doi] = hint
    return await _submit(
        dois,
        request.max_concurrency,
        request.article_timeout_seconds,
        download_si=True,
        article_hints=hints,
    )


@app.post("/api/search/academic/batch")
async def search_titles_batch(request: TitleBatchRequest):
    """High-throughput title → DOI resolution on OpenAlex."""

    titles = [t.strip() for t in request.titles if t and t.strip()][:500]
    if not titles:
        raise HTTPException(400, "No titles provided")
    resolved = await resolve_titles(titles)
    for row in resolved:
        row["min_similarity"] = request.min_similarity
        row["resolved"] = bool(row.get("resolved")) or row.get("similarity", 0) >= request.min_similarity
    if request.only_resolved:
        resolved = [row for row in resolved if row.get("doi")]
    return {
        "requested": len(titles),
        "resolved": sum(1 for row in resolved if row.get("doi")),
        "rows": resolved,
    }


@app.get("/api/search/academic")
async def search_academic(q: str, limit: int = 12):
    if not q.strip():
        raise HTTPException(400, "Empty query")
    limit = max(1, min(limit, 50))
    try:
        rows = await search_openalex(q, limit)
    except Exception as exc:
        raise HTTPException(502, f"OpenAlex request failed: {exc!r}") from exc
    return {"query": q, "count": len(rows), "rows": rows}


@app.get("/api/search/cnki")
async def search_cnki(q: str, timeout: int = 120):
    """CNKI keyword search in an isolated browser subprocess."""

    if not q.strip():
        raise HTTPException(400, "Empty query")
    timeout = max(60, min(timeout, 300))
    out_dir = BASE_SETTINGS.download_root / "_search_runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"cnki_{uuid.uuid4().hex[:10]}.json"

    cmd = [
        sys.executable,
        "-m",
        "paper_tool.cnki_search",
        "--query",
        q.strip(),
        "--output",
        str(out_file),
        "--timeout",
        str(timeout),
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    # Cover the manual slider-captcha window inside the subprocess.
    import os as _os

    manual_wait = int(_os.getenv("PAPER_TOOL_CNKI_MANUAL_WAIT", "120"))
    wait_budget = timeout + manual_wait + 60
    try:
        await asyncio.wait_for(process.wait(), timeout=wait_budget)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except Exception:
            pass
        raise HTTPException(504, "CNKI search subprocess timed out")
    if not out_file.exists():
        output = b""
        if process.stdout:
            try:
                output = await process.stdout.read()
            except Exception:
                pass
        raise HTTPException(500, f"CNKI search failed: {output[-400:].decode('utf-8', 'replace')}")
    try:
        payload = json.loads(out_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise HTTPException(500, f"CNKI search produced invalid JSON: {exc!r}") from exc
    if payload.get("error"):
        raise HTTPException(502, str(payload["error"]))
    return payload


@app.post("/api/jobs/upload")
async def create_job_from_upload(
    file: UploadFile = File(...),
    max_concurrency: int = 2,
    article_timeout_seconds: int = 120,
    doi_field: str = "doi",
    download_si: bool = True,
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
    return await _submit(dois, max_concurrency, article_timeout_seconds, download_si)


@app.post("/api/jobs/path")
async def create_job_from_path(request: PathJobRequest):
    try:
        dois = load_dois_from_file(request.input_path, request.doi_field)
    except Exception as exc:
        raise HTTPException(400, f"Could not parse DOI file: {exc!r}") from exc
    return await _submit(dois, request.max_concurrency, request.article_timeout_seconds, request.download_si)


@app.post("/api/agent/jobs")
async def create_agent_job(request: AgentPathRequest):
    try:
        dois = load_dois_from_file(request.input_path, request.doi_field)
    except Exception as exc:
        raise HTTPException(400, f"Could not parse agent manifest: {exc!r}") from exc
    response = await _submit(dois, request.max_concurrency, request.article_timeout_seconds, request.download_si)
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
    return await _submit(dois, request.max_concurrency, request.article_timeout_seconds, request.download_si)


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

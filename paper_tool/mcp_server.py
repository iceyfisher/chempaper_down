"""MCP (Model Context Protocol) server exposing Paper Downloader to AI agents.

Two transports:

* **stdio** — `paper-tool-mcp` (or `python -m paper_tool.mcp_server`). Configure
  it in Claude Desktop / Cursor / any MCP client:

  ```json
  {
    "mcpServers": {
      "paper-downloader": {
        "command": "D:/Anaconda_envs/envs/chem-paper-agent/python.exe",
        "args": ["-m", "paper_tool.mcp_server"]
      }
    }
  }
  ```

* **HTTP** — the FastAPI server (`paper_tool.api`) mounts the streamable app at
  `http://127.0.0.1:8765/mcp` when the `mcp` package is importable.

Tools exposed to agents:

| Tool | Purpose |
|---|---|
| `search_papers(query, limit)` | OpenAlex search with authors/journal/publisher/keywords/abstract |
| `get_paper_metadata(doi)` | Full metadata for one DOI |
| `resolve_titles(titles)` | Batch title → DOI resolution with similarity scores |
| `search_cnki(query, timeout)` | CNKI search (may pop a headful window for the slider CAPTCHA) |
| `download_papers(dois, download_si, ...)` | Submit a download job; with `wait=True` blocks until terminal and returns per-DOI status + file paths |
| `get_job_status(job_id)` | Poll a previously submitted job |
| `list_publishers()` | Adapters compiled into this build |

CNKI/IEEE downloads run headful browser subprocesses and may require a manual
slider slide; agents should prefer DOIs from publishers other than CNKI for
unattended workflows.
"""

from __future__ import annotations

import asyncio
import os

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .jobs import JobManager, TERMINAL_ITEM_STATUSES
from .registry import supported_publishers
from .search import fetch_metadata, resolve_titles as _resolve_titles, search_openalex

mcp = FastMCP("paper-downloader")

_SETTINGS = Settings.from_env()
_JOBS: JobManager | None = None


def _job_manager() -> JobManager:
    global _JOBS
    if _JOBS is None:
        _JOBS = JobManager(_SETTINGS)
    return _JOBS


@mcp.tool()
async def search_papers(query: str, limit: int = 10) -> dict:
    """Search OpenAlex for papers by title or keywords.

    Returns relevance-ranked rows with: title, doi, year, journal, publisher,
    authors, keywords, abstract (when available), cited_by_count and
    open_access flag.
    """

    rows = await search_openalex(query, limit)
    return {"query": query, "count": len(rows), "rows": rows}


@mcp.tool()
async def get_paper_metadata(doi: str) -> dict:
    """Fetch full metadata for one DOI from OpenAlex.

    Returns title, authors, journal, publisher, year, keywords, abstract,
    cited_by_count and open_access flag; `null` when the DOI is unknown.
    """

    metadata = await fetch_metadata(doi)
    if metadata is None:
        return {"doi": doi, "found": False}
    return {"found": True, **metadata}


@mcp.tool()
async def resolve_titles(titles: list[str]) -> dict:
    """Resolve a list of paper titles to their best-matching DOIs.

    Each row carries the matched title, doi, year, journal and a similarity
    score (`resolved` is true when similarity >= 0.72).
    """

    rows = await _resolve_titles(titles)
    return {
        "requested": len(rows),
        "resolved": sum(1 for r in rows if r.get("doi")),
        "rows": rows,
    }


@mcp.tool()
async def search_cnki(query: str, timeout: int = 120) -> dict:
    """Search CNKI (知网) for Chinese literature by title or keywords.

    Returns up to 20 rows with title, url, doi (when listed) and year. NOTE:
    this launches a visible Edge window; if CNKI shows its slider CAPTCHA the
    flow waits up to PAPER_TOOL_CNKI_MANUAL_WAIT seconds (default 120) for a
    manual slide, so the call may block for a few minutes.
    """

    from .cnki_search import run_search

    timeout = max(60, min(timeout, 300))
    manual_wait = int(os.getenv("PAPER_TOOL_CNKI_MANUAL_WAIT", "120"))
    budget = max(timeout, manual_wait + 90)
    try:
        payload = await asyncio.wait_for(
            run_search(query, Settings.from_env(), timeout), timeout=budget
        )
    except asyncio.TimeoutError:
        return {
            "query": query,
            "error": (
                f"cnki search exceeded {budget}s; a slider CAPTCHA may be "
                "waiting in the Edge window - slide it and retry"
            ),
            "rows": [],
        }
    return {
        "query": query,
        "count": payload.get("count", 0),
        "rows": payload.get("rows", []),
        "elapsed_seconds": payload.get("elapsed_seconds"),
    }


@mcp.tool()
async def download_papers(
    dois: list[str],
    download_si: bool = True,
    max_concurrency: int = 2,
    wait: bool = True,
    timeout_seconds: int = 900,
) -> dict:
    """Download article PDFs (and optionally SI) for a list of DOIs.

    Publishes a crash-isolated download job and, when `wait` is true, polls
    until every DOI reaches a terminal state, returning per-DOI status plus
    the archived file paths (downloads/<doi>_<year>_<journal>/pdf|si).
    Set `wait=false` to get a job_id immediately and poll `get_job_status`.
    """

    dois_clean = [d.strip() for d in dois if d and d.strip()]
    if not dois_clean:
        return {"error": "no DOIs provided"}
    job = await _job_manager().submit(
        dois_clean,
        max_concurrency=max_concurrency,
        download_si=download_si,
    )
    if not wait:
        return {"job_id": job.id, "status": job.status, "doi_count": len(dois_clean)}

    deadline = asyncio.get_running_loop().time() + max(60, timeout_seconds)
    while asyncio.get_running_loop().time() < deadline:
        state = _job_manager().get(job.id)
        if state is None or state.status in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(2.0)
    return _job_payload(job.id)


@mcp.tool()
async def get_job_status(job_id: str) -> dict:
    """Fetch the current status and per-DOI results of a download job."""

    return _job_payload(job_id)


@mcp.tool()
async def list_publishers() -> dict:
    """List the publishers this build can download from."""

    return {"publishers": supported_publishers()}


def _job_payload(job_id: str) -> dict:
    state = _job_manager().get(job_id)
    if state is None:
        return {"job_id": job_id, "error": "job not found"}
    results = []
    for doi, item in state.results.items():
        paper = item.get("paper") or {}
        si = item.get("si") or []
        results.append(
            {
                "doi": doi,
                "status": item.get("status"),
                "publisher": item.get("publisher"),
                "title": item.get("title"),
                "year": item.get("year"),
                "pdf_path": paper.get("path") if paper.get("valid") else None,
                "si_paths": [
                    s.get("path") for s in si if s.get("valid") and s.get("path")
                ],
                "message": item.get("message"),
            }
        )
    return {
        "job_id": state.id,
        "status": state.status,
        "total": state.total,
        "completed": state.completed,
        "running": state.running,
        "results": results,
        "pending_dois": [
            d for d in state.dois if d not in state.results
        ] if state.status in {"queued", "running"} else [],
        "terminal_statuses": sorted(TERMINAL_ITEM_STATUSES),
    }


def main() -> None:
    """stdio transport entry point (`paper-tool-mcp`)."""

    mcp.run()


if __name__ == "__main__":
    main()

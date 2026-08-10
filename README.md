# Paper Download Tool v0.2.0

A crash-isolated DOI → article PDF + Supporting Information downloader for Windows/Edge workflows.

## Why v0.2.0 exists

The first implementation kept long-lived Pydoll `Edge` objects inside the FastAPI process. A stuck CDP command (`Runtime.evaluate`), broken Cloudflare iframe handling, or `WebSocketConnectionClosed` during shutdown could leave the browser worker wedged and the API job unable to terminate cleanly.

v0.2.0 changes the execution boundary:

```text
FastAPI / CLI parent process
        |
        +-- DOI subprocess 1 -- Pydoll -- Edge process tree 1
        +-- DOI subprocess 2 -- Pydoll -- Edge process tree 2
        +-- ...
```

**One DOI = one Python subprocess = one isolated Edge process tree.**

The parent never holds a Pydoll WebSocket. If one DOI exceeds the configured hard budget (default 120 s), the parent terminates only that DOI subprocess and its descendant Edge processes with `psutil`, records `timeout`, and continues the batch.

## Verified publisher strategies

### ACS (`10.1021/*`)

- Navigate DOI/article page.
- Navigation is attempted normally first. If no article DOM appears, the Pydoll Cloudflare helper may run inside the isolated DOI subprocess. It can be disabled with `PAPER_TOOL_ENABLE_CLOUDFLARE_HELPER=0`; any helper/CDP hang remains bounded by the parent hard timeout.
- Main PDF: authenticated browser-context `fetch → Blob → Chromium download`.
- SI: merge `data-doctype="dataSupplementDoc"` and `/article-supplement/` links, deduplicate by source URL, then download each response.
- Final URL, response MIME, `Content-Disposition`, original filename, and response headers are retained. Magic-byte inspection selects the real extension; HTML/JSON error pages and unknown `.bin` payloads are rejected before the final SI directory.

### RSC (`10.1039/*`)

- Main PDF: real browser click on `data-doctype="contentPdf"` / `/article-pdf/`.
- SI: `/article-supplement/`, Blob download.

### Wiley (`10.1002/*`)

- Article page → ePDF reader.
- Download button is clicked.
- Verified reader PDF option may be `visibility:hidden` and `0x0`, so final PDF action uses:

```python
await pdf_link.execute_script("this.click()", user_gesture=True)
```

- After paper download, the adapter **always restores the original Article page** before SI discovery.
- Supporting Information tables, `action/downloadSupplement`, `/doi/supinfo/`, and current platform attachment links are scanned together.
- SI URLs are collected first, then downloaded with Blob so clicking one SI cannot destroy page state.
- Wiley uses a 600-second hard DOI budget and a 300-second per-attachment budget. Timeout cleanup still targets only the current DOI process tree.

### Springer Nature Link / SpringerLink (`10.1007/*`)

- Main PDF: `/content/pdf/<doi>.pdf` via native navigation / Chromium download manager.
- SI discovery scans Supplementary headings, sections, and `springer-static/esm` resources, then deduplicates by canonical URL and Supplementary file number.
- Image SI: `media.springernature.com` origin bridge + same-origin Blob first.
- PDF/Office/archive/media SI: native navigation first, Blob fallback.

### Elsevier / ScienceDirect (`10.1016/*`)

- Official API first when `ELSEVIER_API_KEY` is configured:
  `https://api.elsevier.com/content/article/doi/{doi}` with `Accept: application/pdf`.
- Full PDF availability depends on Elsevier entitlement/API access.
- The intended SI implementation is Elsevier Object Retrieval: enumerate DOI-associated objects, then download `STANDARD` attachments by `ref` while retaining MIME and original filename. See the [Article Retrieval API](https://dev.elsevier.com/documentation/ArticleRetrievalAPI.wadl) and [Object Retrieval API](https://dev.elsevier.com/documentation/ObjectRetrievalAPI.wadl).
- HTTP 401/403 means insufficient authorization and 429 means quota/rate limiting; neither means "no SI".
- `ars.els-cdn.com/*-mmcN*` browser discovery remains only an experimental fallback when the API path is unavailable. The Object Retrieval implementation is intentionally deferred until an entitled API key and test DOI are available.

## Resumable DOI and SI rule

Before **any DOI subprocess or Edge process starts**, the parent recursively checks:

```text
downloads/**/paper/<doi_with_slash_replaced_by_underscore>.pdf
```

A task is complete only when the paper passes PDF validation and its existing manifest records `si_scan_complete=true` with every recorded SI file still valid and source-matched. If complete:

```text
重复下载 跳过任务doi：10.xxxx/xxxx
```

The duplicate result is returned without overwriting the original manifest. A missing/unknown scan state, failed SI, invalid attachment, or source mismatch resumes SI discovery even when the paper is already valid. The valid paper is retained; an SI file is reused only when its manifest source URL matches and its content still validates.

## Install

```powershell
cd D:\code\paper_search_agent
D:\Anaconda_envs\envs\chem-paper-agent\python.exe -m pip install -e . --no-deps
```

Verify console scripts:

```powershell
Get-Command paper-tool
Get-Command paper-tool-server
```

If PowerShell PATH does not expose the scripts, use:

```powershell
D:\Anaconda_envs\envs\chem-paper-agent\Scripts\paper-tool-server.exe --host 127.0.0.1 --port 8765 --download-root D:\code\paper_search_agent\downloads
D:\Anaconda_envs\envs\chem-paper-agent\python.exe -m paper_tool.cli --help
D:\Anaconda_envs\envs\chem-paper-agent\python.exe -m paper_tool.api --help
```

## Web UI

```powershell
paper-tool-server --host 127.0.0.1 --port 8765 --download-root D:\code\paper_search_agent\downloads
```

Open:

```text
http://127.0.0.1:8765
```

The UI accepts:

- pasted DOI text / DOI URLs;
- TXT / JSON / JSONL / CSV upload;
- a local manifest path accessible to the server;
- concurrency 1–4;
- hard per-DOI budget 180, 210, or 240 s in the Web UI;
- cancellation of the active Job.

## CLI batch

```powershell
paper-tool `
  --input D:\code\paper_search_agent\search_batch.json `
  --concurrency 2 `
  --article-timeout 120 `
  --download-root D:\code\paper_search_agent\downloads
```

Or without relying on the console-script PATH:

```powershell
D:\Anaconda_envs\envs\chem-paper-agent\python.exe -m paper_tool.cli `
  --input D:\code\paper_search_agent\search_batch.json `
  --concurrency 2 `
  --article-timeout 120 `
  --download-root D:\code\paper_search_agent\downloads
```

## AI Agent API

An upstream search agent can leave a 50–100 paper TXT/JSON/JSONL/CSV manifest with DOI fields and call:

```http
POST /api/agent/jobs
Content-Type: application/json
```

```json
{
  "input_path": "D:\\code\\paper_search_agent\\search_batch.json",
  "doi_field": "doi",
  "max_concurrency": 2,
  "article_timeout_seconds": 120,
  "job_tag": "search-agent-batch-001"
}
```

Poll:

```text
GET /api/jobs/<job_id>
GET /api/jobs/<job_id>/results
```

Cancel:

```text
POST /api/jobs/<job_id>/cancel
```

Each result includes a `_logs` path in diagnostics for the isolated DOI subprocess.

## Result statuses

- `success`
- `partial`
- `failed`
- `timeout`
- `browser_crashed`
- `process_error`
- `skipped_duplicate`

## Output layout

```text
downloads/
├─ American Chemical Society - ACS Catalysis/
│  ├─ paper/
│  └─ si/
├─ Royal Society of Chemistry - .../
├─ Wiley - .../
├─ Springer Nature - .../
├─ Elsevier - .../
├─ _jobs/
├─ _logs/
├─ _manifests/
└─ _worker_runs/
```

## Reliability notes

- Never globally kills `msedge.exe`.
- Timeout/cancel cleanup targets only the DOI subprocess PID and descendant processes.
- API shutdown cancels active jobs; cancellation propagates to subprocess tree cleanup.
- A browser crash cannot poison the next DOI because the next DOI starts in a new process and Edge instance.
- `article_timeout_seconds` is the normal hard parent wall-clock budget. Wiley DOI tasks use `PAPER_TOOL_WILEY_TIMEOUT` (default and maximum 600 s); one Wiley attachment may use up to 300 s.

## Tests

Offline tests use static HTML, headers, and binary fixtures and do not access publisher domains:

```powershell
D:\Anaconda_envs\envs\chem-paper-agent\python.exe -m pytest -q
```

Live browser checks are explicit and isolated under [`manual_tests/publishers`](manual_tests/publishers/README.md). They use the same Windows `chem-paper-agent` environment, write only to the ignored `_runs/` tree, and are never part of normal pytest collection.

See [`DEPENDENCY_CLEANUP.md`](DEPENDENCY_CLEANUP.md) for the non-destructive package audit. In particular, the legacy `chem-paper-agent` entry point is currently broken because its editable target no longer contains the `chem_agent` source package; that failure is not evidence that its environment dependencies can be deleted.

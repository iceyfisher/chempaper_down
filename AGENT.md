# AI Agent Interface

The downloader is designed to sit after an upstream search/ranking agent.

## Recommended handoff

Upstream agent writes JSON:

```json
{
  "query": "photocatalytic nitrate reduction",
  "papers": [
    {"doi": "10.1021/example", "candidate_score": 0.95},
    {"doi": "10.1002/example", "candidate_score": 0.92}
  ]
}
```

The downloader recursively finds DOI strings; the surrounding metadata is ignored for downloading and may remain in the upstream manifest.

## Submit by local path

```http
POST http://127.0.0.1:8765/api/agent/jobs
Content-Type: application/json
```

```json
{
  "input_path": "D:\\code\\paper_search_agent\\search_batch.json",
  "doi_field": "doi",
  "max_concurrency": 2,
  "article_timeout_seconds": 120,
  "job_tag": "batch-2026-08-10"
}
```

Response:

```json
{
  "job_id": "83d02d6c2fb2",
  "status": "queued",
  "doi_count": 83,
  "status_url": "/api/jobs/83d02d6c2fb2",
  "results_url": "/api/jobs/83d02d6c2fb2/results"
}
```

## Poll

```http
GET /api/jobs/83d02d6c2fb2
```

Terminal job states: `completed`, `failed`, `cancelled`.

Terminal DOI states: `success`, `partial`, `failed`, `timeout`, `browser_crashed`, `process_error`, `skipped_duplicate`.

## Cancel

```http
POST /api/jobs/83d02d6c2fb2/cancel
```

Cancellation kills only active DOI subprocess trees and their descendant Edge processes.

## Python client

```python
import asyncio
from paper_tool.agent_client import submit_manifest

state = asyncio.run(
    submit_manifest(
        r"D:\code\paper_search_agent\search_batch.json",
        base_url="http://127.0.0.1:8765",
        max_concurrency=2,
        article_timeout_seconds=120,
    )
)
```

## Batch sizing

For 50–100 DOI batches start with `max_concurrency=2`. Increase to 3 or 4 only after confirming the publisher mix and local memory/network conditions. Each active slot is a separate Python + Edge process tree.

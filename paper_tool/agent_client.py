from __future__ import annotations

import argparse
import asyncio
import json

import httpx


async def submit_manifest(
    input_path: str,
    *,
    base_url: str = "http://127.0.0.1:8765",
    max_concurrency: int = 2,
    article_timeout_seconds: int = 120,
    doi_field: str = "doi",
    wait: bool = True,
) -> dict:
    """Client for an upstream search/AI agent that leaves a DOI manifest file."""

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            base_url.rstrip("/") + "/api/agent/jobs",
            json={
                "input_path": input_path,
                "doi_field": doi_field,
                "max_concurrency": max_concurrency,
                "article_timeout_seconds": article_timeout_seconds,
            },
        )
        response.raise_for_status()
        created = response.json()
        if not wait:
            return created

        job_id = created["job_id"]
        while True:
            status_response = await client.get(base_url.rstrip("/") + f"/api/jobs/{job_id}")
            status_response.raise_for_status()
            state = status_response.json()
            print(
                f"job={job_id} status={state['status']} "
                f"completed={state['completed']}/{state['total']} running={state['running']}"
            )
            if state["status"] in {"completed", "failed", "cancelled"}:
                return state
            await asyncio.sleep(3)


def main():
    p = argparse.ArgumentParser(description="Submit an upstream AI-agent DOI manifest")
    p.add_argument("--input", required=True)
    p.add_argument("--base-url", default="http://127.0.0.1:8765")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--article-timeout", type=int, default=120)
    p.add_argument("--doi-field", default="doi")
    p.add_argument("--no-wait", action="store_true")
    args = p.parse_args()
    result = asyncio.run(
        submit_manifest(
            args.input,
            base_url=args.base_url,
            max_concurrency=args.concurrency,
            article_timeout_seconds=args.article_timeout,
            doi_field=args.doi_field,
            wait=not args.no_wait,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

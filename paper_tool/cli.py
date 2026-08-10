from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .config import Settings
from .doi import extract_dois, load_dois_from_file
from .service import DownloadService


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Crash-isolated DOI paper + SI downloader")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="TXT/JSON/JSONL/CSV search-list file")
    source.add_argument("--dois", help="Comma/newline separated DOI text")
    p.add_argument("--doi-field", default="doi")
    p.add_argument("--download-root", default="downloads")
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--article-timeout", type=int, default=120)
    p.add_argument("--json-output")
    return p


async def run(args) -> int:
    if args.input:
        dois = load_dois_from_file(args.input, args.doi_field)
    else:
        dois = extract_dois(args.dois or "")
    if not dois:
        raise SystemExit("No DOI found")

    settings = Settings.from_env(args.download_root).with_overrides(
        max_concurrency=args.concurrency,
        article_timeout_seconds=args.article_timeout,
    )
    service = DownloadService(settings)

    async def progress(item):
        status = str(item.status)
        if status == "running":
            print(f"RUNNING  {item.doi}  {item.message or ''}")
        elif status == "skipped_duplicate":
            print(f"重复下载 跳过任务doi：{item.doi}")
        else:
            print(
                f"{status.upper():18s} {item.doi} publisher={item.publisher or '-'} "
                f"SI={item.si_successful}/{item.si_detected} elapsed={item.elapsed_seconds} "
                f"{item.message or ''}"
            )

    results = await service.run_batch(dois, callback=progress)
    payload = [x.to_dict() for x in results]
    if args.json_output:
        Path(args.json_output).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    successish = sum(
        1 for x in results if str(x.status) in {"success", "partial", "skipped_duplicate"}
    )
    print(f"\nFinished: {successish}/{len(results)} success/partial/existing")
    return 0 if successish else 1


def main():
    args = parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()

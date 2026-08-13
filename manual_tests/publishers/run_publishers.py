"""Explicit live publisher checks; normal pytest never imports this file."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paper_tool.config import Settings
from paper_tool.service import DownloadService


HERE = Path(__file__).resolve().parent
CASES_PATH = HERE / "cases.json"
RUNS_ROOT = HERE / "_runs"
DOI_PREFIXES = {"aip": "10.1063/", "aaas": "10.1126/"}


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Run isolated live publisher checks")
    selection = command.add_mutually_exclusive_group(required=True)
    selection.add_argument("--publisher", choices=sorted(DOI_PREFIXES))
    selection.add_argument("--case", dest="case_name")
    selection.add_argument("--all-enabled", action="store_true")
    command.add_argument("--doi", help="Override the configured DOI for --publisher")
    return command


def load_cases() -> list[dict[str, Any]]:
    return list(json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"])


def select_cases(args: argparse.Namespace) -> list[dict[str, Any]]:
    cases = load_cases()
    if args.case_name:
        selected = [case for case in cases if case["name"] == args.case_name]
    elif args.all_enabled:
        selected = [case for case in cases if case.get("enabled", False)]
    elif args.doi:
        selected = [{
            "name": f"custom_{args.publisher}",
            "publisher": args.publisher,
            "doi": args.doi.strip(),
            "enabled": True,
            "article_timeout_seconds": 240,
            "expected_min_si": 0,
            "expected_extensions": [],
            "forbidden_extensions": [".bin"],
        }]
    else:
        selected = [
            case for case in cases
            if case.get("enabled", False) and case["publisher"] == args.publisher
        ]
    if not selected:
        raise SystemExit("No enabled case matched the request")
    for case in selected:
        if not case["doi"].lower().startswith(DOI_PREFIXES[case["publisher"]]):
            raise SystemExit(
                f"DOI {case['doi']!r} does not match {case['publisher']!r}"
            )
    return selected


def assess(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    si = result.get("si") or []
    valid_si = [item for item in si if item.get("valid")]
    actual = {
        str(item.get("extension") or "").lower()
        for item in valid_si
        if item.get("extension")
    }
    expected = {value.lower() for value in case.get("expected_extensions", [])}
    forbidden = {value.lower() for value in case.get("forbidden_extensions", [])}
    minimum = int(case.get("expected_min_si", 0))
    checks = {
        "paper_valid": bool((result.get("paper") or {}).get("valid")),
        "si_scan_complete": result.get("diagnostics", {}).get("si_scan_complete") is True,
        "minimum_si_found": len(si) >= minimum,
        "minimum_si_valid": len(valid_si) >= minimum,
        "expected_extensions_present": expected.issubset(actual),
        "forbidden_extensions_absent": forbidden.isdisjoint(actual),
        "all_reported_si_valid": len(valid_si) == len(si),
    }
    return {
        "case": case["name"],
        "publisher": case["publisher"],
        "doi": case["doi"],
        "si_discovered": len(si),
        "si_successful": len(valid_si),
        "recognized_extensions": sorted(actual),
        "checks": checks,
        "passed": all(checks.values()),
        "result": result,
    }


async def run(args: argparse.Namespace) -> int:
    cases = select_cases(args)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = RUNS_ROOT / f"{stamp}_{uuid.uuid4().hex[:8]}"
    run_root.mkdir(parents=True, exist_ok=False)
    timeout = max(int(case.get("article_timeout_seconds", 240)) for case in cases)
    settings = Settings.from_env(run_root / "downloads").with_overrides(
        max_concurrency=1,
        article_timeout_seconds=timeout,
    )
    results = await DownloadService(settings).run_batch([case["doi"] for case in cases])
    by_doi = {result.doi: result.to_dict() for result in results}
    reports = [assess(case, by_doi[case["doi"]]) for case in cases]
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_root": str(run_root),
        "cases": reports,
        "passed": all(item["passed"] for item in reports),
    }
    report_path = run_root / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(report_path)
    return 0 if report["passed"] else 1


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()

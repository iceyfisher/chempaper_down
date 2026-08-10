"""Explicit live publisher verification entry point.

This module is intentionally outside tests/ so normal pytest collection cannot
contact publisher websites. Run it manually from the repository root.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paper_tool.config import Settings
from paper_tool.service import DownloadService


HERE = Path(__file__).resolve().parent
CASES_PATH = HERE / "cases.json"
RUNS_ROOT = HERE / "_runs"
DOI_PREFIXES = {
    "acs": "10.1021/",
    "wiley": "10.1002/",
    "elsevier": "10.1016/",
}


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Manually run isolated live publisher checks"
    )
    selection = command.add_mutually_exclusive_group(required=True)
    selection.add_argument("--publisher", choices=sorted(DOI_PREFIXES))
    selection.add_argument("--case", dest="case_name")
    selection.add_argument("--all-enabled", action="store_true")
    command.add_argument("--doi", help="Override/add a DOI for --publisher")
    return command


def load_cases() -> list[dict[str, Any]]:
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return list(payload["cases"])


def select_cases(args: argparse.Namespace) -> list[dict[str, Any]]:
    cases = load_cases()
    if args.case_name:
        selected = [case for case in cases if case["name"] == args.case_name]
        if not selected:
            raise SystemExit(f"Unknown case: {args.case_name}")
        if not selected[0].get("enabled", False):
            raise SystemExit(
                f"Case {args.case_name!r} is disabled; use --publisher and an explicit --doi"
            )
        return selected

    if args.all_enabled:
        return [case for case in cases if case.get("enabled", False)]

    assert args.publisher
    if args.doi:
        configured = [
            case
            for case in cases
            if case["publisher"] == args.publisher
            and case["doi"].lower() == args.doi.strip().lower()
        ]
        if configured:
            return configured
        selected = {
            "name": f"custom_{args.publisher}",
            "publisher": args.publisher,
            "doi": args.doi.strip(),
            "enabled": True,
            "expected_min_si": 0,
            "expected_extensions": [],
            "forbidden_extensions": [".bin"],
        }
        return [selected]
    return [
        case
        for case in cases
        if case.get("enabled", False) and case["publisher"] == args.publisher
    ]


def validate_selection(cases: list[dict[str, Any]]) -> None:
    if not cases:
        raise SystemExit("No enabled case matched the requested publisher")
    for case in cases:
        publisher = case["publisher"]
        doi = case["doi"].lower()
        if not doi.startswith(DOI_PREFIXES[publisher]):
            raise SystemExit(f"DOI {case['doi']!r} does not match publisher {publisher!r}")
        required_env = case.get("requires_env")
        if publisher == "elsevier":
            required_env = "ELSEVIER_API_KEY"
        if required_env and not os.getenv(required_env):
            raise SystemExit(f"{required_env} is required for {case['name']}")


def assess(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    si = result.get("si") or []
    valid_si = [item for item in si if item.get("valid")]
    extensions = sorted({item.get("extension") for item in valid_si if item.get("extension")})
    expected = {value.lower() for value in case.get("expected_extensions", [])}
    forbidden = {value.lower() for value in case.get("forbidden_extensions", [])}
    actual = {value.lower() for value in extensions}
    checks = {
        "si_scan_complete": result.get("diagnostics", {}).get("si_scan_complete") is True,
        "minimum_si_found": len(si) >= int(case.get("expected_min_si", 0)),
        "minimum_si_valid": len(valid_si) >= int(case.get("expected_min_si", 0)),
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
        "recognized_extensions": extensions,
        "checks": checks,
        "passed": all(checks.values()),
        "result": result,
    }


async def run(args: argparse.Namespace) -> int:
    cases = select_cases(args)
    validate_selection(cases)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = RUNS_ROOT / f"{stamp}_{uuid.uuid4().hex[:8]}"
    download_root = run_root / "downloads"
    run_root.mkdir(parents=True, exist_ok=False)

    settings = Settings.from_env(download_root).with_overrides(max_concurrency=1)
    service = DownloadService(settings)
    results = await service.run_batch([case["doi"] for case in cases])
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
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(report_path)
    return 0 if report["passed"] else 1


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()

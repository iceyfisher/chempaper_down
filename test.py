"""Manual campus-network download checks. Run explicitly; never imported by pytest."""
import argparse
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from paper_tool.config import Settings
from paper_tool.service import DownloadService


CASES = {
    'rsc': ['10.1039/' + suffix for suffix in (
        'b9nj00371a', 'c4dt01489h', 'c6dt04034a', 'c6ra08946a', 'c7dt01954h', 'c8nj01074a')],
    'acs': ['10.1021/acsomega.5c13377'],
    'taylor': ['10.1080/' + suffix for suffix in (
        '01496395.2012.697523', '01496395.2015.1085874',
        '01496395.2016.1165252', '01496395.2016.1274760',
        '07366290600646947', '07366290600761936', '07366290701416000',
        '07366290701634578', '07366291003685383', '07366298608917877',
        '07366299.2011.539129', '07366299.2012.757160', '07366299.2013.800431',
        '07366299.2013.836422', '07366299.2013.866857', '07366299.2015.1087209',
        '18811248.2007.9711301')],
}
PREFIXES = {'rsc': '10.1039/', 'acs': '10.1021/', 'taylor': '10.1080/'}


async def run(args):
    dois = ([args.doi.strip().lower()] if args.doi else
            [doi for cases in CASES.values() for doi in cases] if args.all else CASES[args.publisher])
    if args.publisher and any(not doi.startswith(PREFIXES[args.publisher]) for doi in dois):
        raise SystemExit('DOI does not match --publisher')
    if any(not doi.startswith(tuple(PREFIXES.values())) for doi in dois):
        raise SystemExit('Only RSC, ACS and Taylor & Francis are supported by this test')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    root = Path(__file__).resolve().parent / 'manual_tests/publishers/_runs' / f'{stamp}_{uuid4().hex[:8]}'
    root.mkdir(parents=True, exist_ok=False)
    settings = Settings.from_env(root / 'downloads').with_overrides(
        max_concurrency=1, article_timeout_seconds=args.article_timeout,
    )
    settings = replace(settings, cloudflare_timeout_seconds=args.cloudflare_timeout).normalized()
    reports = []
    report_path = root / 'report.json'

    def save():
        report_path.write_text(json.dumps({
            'created_at': datetime.now(timezone.utc).isoformat(),
            'run_root': str(root), 'requested_dois': dois, 'cases': reports,
            'finished_count': len(reports),
            'article_timeout_seconds': settings.article_timeout_seconds,
            'cloudflare_timeout_seconds': settings.cloudflare_timeout_seconds,
            'passed': len(reports) == len(dois) and all(r['passed'] for r in reports),
        }, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    print(f'Report: {report_path}', flush=True)
    service = DownloadService(settings)
    for doi in dois:
        result = (await service.run_batch([doi]))[0]
        data = result.to_dict()
        valid = bool(result.paper and result.paper.valid)
        complete = result.diagnostics.get('si_scan_complete') is True
        reports.append({
            'doi': doi, 'paper_valid': valid, 'si_discovered': len(result.si),
            'si_successful': sum(item.valid for item in result.si),
            'si_scan_complete': complete,
            'recognized_extensions': sorted({item.extension for item in result.si if item.extension}),
            'failure_stage': result.diagnostics.get('failure_stage'),
            'passed': valid and complete and all(item.valid for item in result.si),
            'result': data,
        })
        save()
        print(f'{doi}: {result.status}, SI {result.si_successful}/{result.si_detected}', flush=True)
    return 0 if all(r['passed'] for r in reports) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--publisher', choices=CASES)
    selection.add_argument('--all', action='store_true')
    parser.add_argument('--doi')
    parser.add_argument('--article-timeout', type=int, choices=range(120, 601), default=360, metavar='120..600')
    parser.add_argument('--cloudflare-timeout', type=int, choices=range(3, 121), default=60, metavar='3..120')
    args = parser.parse_args()
    if args.all and args.doi:
        parser.error('--all cannot be combined with --doi')
    if not (args.publisher or args.all or args.doi):
        parser.error('Choose --publisher, --doi or --all')
    raise SystemExit(asyncio.run(run(args)))

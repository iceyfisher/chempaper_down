# AIP and AAAS live checks

These scripts contact publisher websites. They are outside `tests/`, so normal
`pytest` does not collect them. Every run writes to a new ignored `_runs/`
directory and does not touch the main `downloads/` tree.

Activate the project environment and run from the repository root:

```powershell
conda activate chem-paper-agent
cd D:\code\paper_search_agent
```

AIP Publishing (`10.1063/5.0176000`):

```powershell
python manual_tests\publishers\run_publishers.py --case aip_jcp_pdf_and_zip_si
```

AAAS / Science (`10.1126/sciadv.aec3536`):

```powershell
python manual_tests\publishers\run_publishers.py --case aaas_science_advances_all_si
```

Both cases:

```powershell
python manual_tests\publishers\run_publishers.py --all-enabled
```

The report checks the article PDF, completed SI scan, minimum attachment count,
expected formats, rejected `.bin` files, and whether every discovered SI item
validated. It also retains source/final URLs, MIME, original filenames, hashes,
and the worker log path. Please share `report.json` and the matching `_logs/`
file if a case fails.

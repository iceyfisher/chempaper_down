# Manual publisher verification

These are explicit live checks. They are outside `tests/`, are not collected by normal `pytest`, and write only to a unique directory under `_runs/`. Existing project `downloads/` content is never cleaned or overwritten.

Use the same project environment; no second environment is required:

```powershell
conda activate chem-paper-agent
cd D:\code\paper_search_agent
```

Run the ACS regression DOI:

```powershell
D:\Anaconda_envs\envs\chem-paper-agent\python.exe manual_tests\publishers\run_publishers.py --publisher acs --doi 10.1021/acscatal.6c02592
```

Run the configured Wiley multi-attachment case:

```powershell
D:\Anaconda_envs\envs\chem-paper-agent\python.exe manual_tests\publishers\run_publishers.py --case wiley_multiple_si
```

Run all enabled cases:

```powershell
D:\Anaconda_envs\envs\chem-paper-agent\python.exe manual_tests\publishers\run_publishers.py --all-enabled
```

Elsevier is disabled in `cases.json`. Supply an entitled API key only through the process environment and an explicit DOI:

```powershell
$env:ELSEVIER_API_KEY = "your-key"
D:\Anaconda_envs\envs\chem-paper-agent\python.exe manual_tests\publishers\run_publishers.py --publisher elsevier --doi 10.1016/replace-with-an-entitled-doi
```

The key is read only from `ELSEVIER_API_KEY`; it is not copied into `cases.json`, result JSON, or logs.

Each run prints its `report.json` path. The report retains publisher results, source and final URLs, declared MIME, original filename, response headers, recognized extension, size, SHA-256, discovered/successful counts, and explicit acceptance checks. Please provide that report and the matching `_logs/` file when a live check fails.

Normal offline validation remains:

```powershell
D:\Anaconda_envs\envs\chem-paper-agent\python.exe -m pytest -q
```

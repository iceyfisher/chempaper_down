# Dependency cleanup candidates

This is an audit list only. No package has been removed.

## High-confidence removal candidates

- `DrissionPage`: no import was found in the current `paper_tool` source or tests.
- `playwright`: no import was found in the current `paper_tool` source or tests.

## Conditional candidates

- `huggingface_hub`: remove only if this environment is not also used by a model or search workflow.
- `PySocks`: remove only if no proxy configuration uses SOCKS.
- `py7zr`: the downloader recognizes 7z signatures without importing this package; retain it if archives must be opened or extracted in this environment.

## Keep

- Data/science: `pyarrow`, `rdkit`.
- Development: `pytest`, `ruff`.
- Server: `fastapi`, `uvicorn`, `httptools`, `watchfiles`.
- Browser/process: `pydoll-python`, `selenium`, `psutil`.
- Core HTTP/model packages used directly or transitively by the application, including `httpx`, `requests`, `urllib3`, and Pydantic packages.

## Broken legacy entry point is not a dependency signal

The environment's `chem-paper-agent` entry point currently fails because its editable installation points at this working tree while the legacy `chem_agent` source package is absent. A `ModuleNotFoundError: No module named 'chem_agent'` therefore does not prove that any package above is unnecessary.

The maintained v0.2.0 entry points in this repository are `paper-tool` and `paper-tool-server`.

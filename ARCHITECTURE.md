# Architecture

## Parent/child boundary

```text
                         ┌──────────────────────────────┐
GUI / Search Agent ────▶ │ FastAPI / JobManager        │
                         │ no Pydoll / no Edge objects  │
                         └──────────────┬───────────────┘
                                        │
                          asyncio semaphore (1–4)
                                        │
          ┌─────────────────────────────┼─────────────────────────────┐
          │                             │                             │
          ▼                             ▼                             ▼
┌──────────────────┐          ┌──────────────────┐          ┌──────────────────┐
│ DOI subprocess A │          │ DOI subprocess B │          │ DOI subprocess C │
│ worker_main.py   │          │ worker_main.py   │          │ worker_main.py   │
└────────┬─────────┘          └────────┬─────────┘          └────────┬─────────┘
         │                             │                             │
         ▼                             ▼                             ▼
   Pydoll + Edge A                Pydoll + Edge B                Pydoll + Edge C
```

The parent owns the wall-clock timeout. Cancellation or timeout uses `psutil` to terminate the DOI subprocess PID and recursively terminate only its descendants.

## Why asyncio.timeout alone is insufficient

`asyncio.timeout()` cancels Python coroutines, but an automation library can be blocked waiting for a CDP/WebSocket command or browser shutdown. Cancellation does not guarantee the external Edge process exits. A process boundary gives the parent an OS-level termination primitive.

## Publisher adapter contract

Every adapter implements:

```python
class PublisherAdapter:
    @classmethod
    def matches_doi(cls, doi: str) -> bool: ...
    async def run(self, ctx: AdapterContext) -> ArticleResult: ...
```

Adding a publisher only requires a new file under `paper_tool/adapters/` and registration in `adapters/__init__.py`.

## Failure containment

- DOM selector failure → DOI result `failed`.
- Pydoll WebSocket failure → `browser_crashed` when catchable.
- Worker exits without result → `process_error`.
- Worker/Edge hangs → parent `timeout`, process tree killed.
- API Job cancelled → all active child process trees killed.
- Next DOI always receives a fresh process and browser.

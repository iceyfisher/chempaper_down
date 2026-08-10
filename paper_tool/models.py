from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ItemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    TIMEOUT = "timeout"
    BROWSER_CRASHED = "browser_crashed"
    PROCESS_ERROR = "process_error"


@dataclass(slots=True)
class FileResult:
    kind: str
    path: str | None = None
    source_url: str | None = None
    method: str | None = None
    extension: str | None = None
    size: int | None = None
    sha256: str | None = None
    valid: bool = False
    existing: bool = False
    error: str | None = None
    final_url: str | None = None
    original_filename: str | None = None
    declared_mime_type: str | None = None
    content_disposition: str | None = None
    response_headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ArticleResult:
    doi: str
    status: ItemStatus = ItemStatus.PENDING
    publisher: str | None = None
    journal: str | None = None
    article_url: str | None = None
    title: str | None = None
    paper: FileResult | None = None
    si: list[FileResult] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float | None = None
    message: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def si_successful(self) -> int:
        return sum(1 for x in self.si if x.valid)

    @property
    def si_detected(self) -> int:
        return len(self.si)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = str(self.status)
        data["si_successful"] = self.si_successful
        data["si_detected"] = self.si_detected
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArticleResult":
        paper = FileResult(**data["paper"]) if data.get("paper") else None
        si = [FileResult(**x) for x in data.get("si", [])]
        return cls(
            doi=data["doi"],
            status=ItemStatus(data.get("status", "pending")),
            publisher=data.get("publisher"),
            journal=data.get("journal"),
            article_url=data.get("article_url"),
            title=data.get("title"),
            paper=paper,
            si=si,
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            elapsed_seconds=data.get("elapsed_seconds"),
            message=data.get("message"),
            diagnostics=data.get("diagnostics") or {},
        )

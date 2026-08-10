from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(slots=True)
class Settings:
    """Runtime settings shared by API, parent scheduler and DOI subprocesses."""

    download_root: Path = Path("downloads")
    max_concurrency: int = 2

    # Parent-process hard wall clock budget for one DOI subprocess.
    article_timeout_seconds: int = 120
    wiley_article_timeout_seconds: int = 600
    subprocess_kill_grace_seconds: int = 5

    # In-child soft operation limits. The parent timeout is authoritative.
    navigation_timeout_seconds: int = 30
    normal_element_timeout_seconds: int = 18
    native_download_timeout_seconds: int = 35
    blob_download_timeout_seconds: int = 75
    cloudflare_timeout_seconds: int = 12
    settle_seconds: float = 1.2

    max_concurrency_hard_limit: int = 4
    elsevier_api_key: str | None = None

    # Disable Pydoll's Cloudflare helper by default because a broken iframe/CDP
    # evaluation can block inside the library. The subprocess boundary still makes
    # it safe to enable for sites where it is useful.
    enable_pydoll_cloudflare_helper: bool = True

    @classmethod
    def from_env(cls, download_root: str | Path | None = None) -> "Settings":
        root = Path(download_root or os.getenv("PAPER_TOOL_DOWNLOAD_ROOT", "downloads"))
        return cls(
            download_root=root,
            max_concurrency=int(os.getenv("PAPER_TOOL_CONCURRENCY", "2")),
            article_timeout_seconds=int(os.getenv("PAPER_TOOL_ARTICLE_TIMEOUT", "120")),
            wiley_article_timeout_seconds=int(os.getenv("PAPER_TOOL_WILEY_TIMEOUT", "600")),
            subprocess_kill_grace_seconds=int(os.getenv("PAPER_TOOL_KILL_GRACE", "5")),
            navigation_timeout_seconds=int(os.getenv("PAPER_TOOL_NAV_TIMEOUT", "30")),
            normal_element_timeout_seconds=int(os.getenv("PAPER_TOOL_ELEMENT_TIMEOUT", "18")),
            native_download_timeout_seconds=int(os.getenv("PAPER_TOOL_NATIVE_TIMEOUT", "35")),
            blob_download_timeout_seconds=int(os.getenv("PAPER_TOOL_BLOB_TIMEOUT", "75")),
            cloudflare_timeout_seconds=int(os.getenv("PAPER_TOOL_CLOUDFLARE_TIMEOUT", "12")),
            elsevier_api_key=os.getenv("ELSEVIER_API_KEY") or None,
            enable_pydoll_cloudflare_helper=(
                os.getenv("PAPER_TOOL_ENABLE_CLOUDFLARE_HELPER", "1").strip().lower()
                in {"1", "true", "yes", "on"}
            ),
        ).normalized()

    def normalized(self) -> "Settings":
        concurrency = max(1, min(int(self.max_concurrency), self.max_concurrency_hard_limit))
        article_timeout = max(30, min(int(self.article_timeout_seconds), 600))
        return replace(
            self,
            download_root=Path(self.download_root).expanduser().resolve(),
            max_concurrency=concurrency,
            article_timeout_seconds=article_timeout,
            wiley_article_timeout_seconds=max(
                article_timeout,
                min(int(self.wiley_article_timeout_seconds), 600),
            ),
            subprocess_kill_grace_seconds=max(1, min(int(self.subprocess_kill_grace_seconds), 30)),
            navigation_timeout_seconds=max(5, min(int(self.navigation_timeout_seconds), article_timeout)),
            normal_element_timeout_seconds=max(2, min(int(self.normal_element_timeout_seconds), article_timeout)),
            native_download_timeout_seconds=max(5, min(int(self.native_download_timeout_seconds), article_timeout)),
            blob_download_timeout_seconds=max(10, min(int(self.blob_download_timeout_seconds), article_timeout)),
            cloudflare_timeout_seconds=max(3, min(int(self.cloudflare_timeout_seconds), 45)),
        )

    def with_overrides(
        self,
        *,
        max_concurrency: int | None = None,
        article_timeout_seconds: int | None = None,
    ) -> "Settings":
        return replace(
            self,
            max_concurrency=self.max_concurrency if max_concurrency is None else max_concurrency,
            article_timeout_seconds=(
                self.article_timeout_seconds
                if article_timeout_seconds is None
                else article_timeout_seconds
            ),
        ).normalized()

    def to_worker_payload(self) -> dict:
        return {
            "download_root": str(self.download_root),
            "article_timeout_seconds": self.article_timeout_seconds,
            "wiley_article_timeout_seconds": self.wiley_article_timeout_seconds,
            "navigation_timeout_seconds": self.navigation_timeout_seconds,
            "normal_element_timeout_seconds": self.normal_element_timeout_seconds,
            "native_download_timeout_seconds": self.native_download_timeout_seconds,
            "blob_download_timeout_seconds": self.blob_download_timeout_seconds,
            "cloudflare_timeout_seconds": self.cloudflare_timeout_seconds,
            "settle_seconds": self.settle_seconds,
            "elsevier_api_key": self.elsevier_api_key,
            "enable_pydoll_cloudflare_helper": self.enable_pydoll_cloudflare_helper,
        }

    @classmethod
    def from_worker_payload(cls, payload: dict) -> "Settings":
        return cls(
            download_root=Path(payload["download_root"]),
            max_concurrency=1,
            article_timeout_seconds=int(payload.get("article_timeout_seconds", 120)),
            wiley_article_timeout_seconds=int(payload.get("wiley_article_timeout_seconds", 600)),
            navigation_timeout_seconds=int(payload.get("navigation_timeout_seconds", 30)),
            normal_element_timeout_seconds=int(payload.get("normal_element_timeout_seconds", 18)),
            native_download_timeout_seconds=int(payload.get("native_download_timeout_seconds", 35)),
            blob_download_timeout_seconds=int(payload.get("blob_download_timeout_seconds", 75)),
            cloudflare_timeout_seconds=int(payload.get("cloudflare_timeout_seconds", 12)),
            settle_seconds=float(payload.get("settle_seconds", 1.2)),
            elsevier_api_key=payload.get("elsevier_api_key"),
            enable_pydoll_cloudflare_helper=bool(payload.get("enable_pydoll_cloudflare_helper", False)),
        ).normalized()

    def timeout_for_doi(self, doi: str) -> int:
        if doi.lower().startswith("10.1002/"):
            return max(self.article_timeout_seconds, self.wiley_article_timeout_seconds)
        return self.article_timeout_seconds

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv


def _load_runtime_env() -> None:
    load_dotenv(Path.cwd() / ".env", override=False)


# Publishers served through the Pydoll Cloudflare challenge flow.
CLOUDFLARE_DOI_PREFIXES = (
    "10.1021/",  # ACS
    "10.1039/",  # RSC
    "10.1080/",  # Taylor & Francis
    "10.1063/",  # AIP
    "10.1103/",  # APS
    "10.1126/",  # AAAS
    "10.3390/",  # MDPI (Cloudflare-gated even though content is open access)
)

# Publishers whose challenge may need a human in the visible browser window
# (hCaptcha on IOP, Optica's text captcha); their budget must cover the wait.
MANUAL_CHALLENGE_DOI_PREFIXES = (
    "10.1088/",  # IOP
    "10.7567/",  # JJAP (IOP)
    "10.1364/",  # Optica
)


@dataclass(slots=True)
class Settings:
    """Runtime settings shared by API, parent scheduler and DOI subprocesses."""

    download_root: Path = Path("downloads")
    max_concurrency: int = 2

    # Parent-process hard wall clock budget for one DOI subprocess.
    article_timeout_seconds: int = 120
    wiley_article_timeout_seconds: int = 600
    elsevier_article_timeout_seconds: int = 600
    # ACS/RSC/Taylor & Francis sit behind Cloudflare managed challenges, which
    # can take a minute or more to clear. The default 120s leaves too little
    # navigation budget for the challenge plus a retry.
    cloudflare_article_timeout_seconds: int = 360
    subprocess_kill_grace_seconds: int = 5

    # In-child soft operation limits. The parent timeout is authoritative.
    navigation_timeout_seconds: int = 30
    normal_element_timeout_seconds: int = 18
    native_download_timeout_seconds: int = 35
    blob_download_timeout_seconds: int = 75
    cloudflare_timeout_seconds: int = 60
    # Wait window for the operator to manually clear a captcha (hCaptcha on
    # IOP, Optica's text captcha) in the visible download browser.
    manual_captcha_wait_seconds: int = 180
    settle_seconds: float = 1.2

    max_concurrency_hard_limit: int = 4
    elsevier_api_key: str | None = None

    # Pydoll's helper runs only for adapters that opt into Cloudflare handling.
    # The per-DOI subprocess boundary contains any browser/CDP stall.
    enable_pydoll_cloudflare_helper: bool = True

    # Every download worker opens a visible Edge window by default: headless
    # sessions are blocked by Cloudflare managed challenges (ACS/RSC/Taylor),
    # IEEE's Error 418, and CNKI's slider CAPTCHA. List adapter keys here to run
    # those publishers windowless instead.
    headless_publishers: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, download_root: str | Path | None = None) -> "Settings":
        _load_runtime_env()
        root = Path(download_root or os.getenv("PAPER_TOOL_DOWNLOAD_ROOT", "downloads"))
        return cls(
            download_root=root,
            max_concurrency=int(os.getenv("PAPER_TOOL_CONCURRENCY", "2")),
            article_timeout_seconds=int(os.getenv("PAPER_TOOL_ARTICLE_TIMEOUT", "120")),
            wiley_article_timeout_seconds=int(os.getenv("PAPER_TOOL_WILEY_TIMEOUT", "600")),
            elsevier_article_timeout_seconds=int(
                os.getenv("PAPER_TOOL_ELSEVIER_TIMEOUT", "600")
            ),
            cloudflare_article_timeout_seconds=int(
                os.getenv("PAPER_TOOL_CLOUDFLARE_ARTICLE_TIMEOUT", "360")
            ),
            subprocess_kill_grace_seconds=int(os.getenv("PAPER_TOOL_KILL_GRACE", "5")),
            navigation_timeout_seconds=int(os.getenv("PAPER_TOOL_NAV_TIMEOUT", "30")),
            normal_element_timeout_seconds=int(os.getenv("PAPER_TOOL_ELEMENT_TIMEOUT", "18")),
            native_download_timeout_seconds=int(os.getenv("PAPER_TOOL_NATIVE_TIMEOUT", "35")),
            blob_download_timeout_seconds=int(os.getenv("PAPER_TOOL_BLOB_TIMEOUT", "75")),
            cloudflare_timeout_seconds=int(os.getenv("PAPER_TOOL_CLOUDFLARE_TIMEOUT", "60")),
            manual_captcha_wait_seconds=int(
                os.getenv("PAPER_TOOL_MANUAL_CAPTCHA_WAIT", "180")
            ),
            elsevier_api_key=os.getenv("ELSEVIER_API_KEY") or None,
            enable_pydoll_cloudflare_helper=(
                os.getenv("PAPER_TOOL_ENABLE_CLOUDFLARE_HELPER", "1").strip().lower()
                in {"1", "true", "yes", "on"}
            ),
            headless_publishers=frozenset(
                key.strip().upper()
                for key in os.getenv("PAPER_TOOL_HEADLESS_PUBLISHERS", "").split(",")
                if key.strip()
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
            elsevier_article_timeout_seconds=max(
                article_timeout,
                min(int(self.elsevier_article_timeout_seconds), 600),
            ),
            cloudflare_article_timeout_seconds=max(
                article_timeout,
                min(int(self.cloudflare_article_timeout_seconds), 600),
            ),
            subprocess_kill_grace_seconds=max(1, min(int(self.subprocess_kill_grace_seconds), 30)),
            manual_captcha_wait_seconds=max(
                0, min(int(self.manual_captcha_wait_seconds), 600)
            ),
            navigation_timeout_seconds=max(5, min(int(self.navigation_timeout_seconds), article_timeout)),
            normal_element_timeout_seconds=max(2, min(int(self.normal_element_timeout_seconds), article_timeout)),
            native_download_timeout_seconds=max(5, min(int(self.native_download_timeout_seconds), article_timeout)),
            blob_download_timeout_seconds=max(10, min(int(self.blob_download_timeout_seconds), article_timeout)),
            cloudflare_timeout_seconds=max(3, min(int(self.cloudflare_timeout_seconds), 120)),
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
            "elsevier_article_timeout_seconds": self.elsevier_article_timeout_seconds,
            "cloudflare_article_timeout_seconds": self.cloudflare_article_timeout_seconds,
            "navigation_timeout_seconds": self.navigation_timeout_seconds,
            "normal_element_timeout_seconds": self.normal_element_timeout_seconds,
            "native_download_timeout_seconds": self.native_download_timeout_seconds,
            "blob_download_timeout_seconds": self.blob_download_timeout_seconds,
            "cloudflare_timeout_seconds": self.cloudflare_timeout_seconds,
            "manual_captcha_wait_seconds": self.manual_captcha_wait_seconds,
            "settle_seconds": self.settle_seconds,
            "enable_pydoll_cloudflare_helper": self.enable_pydoll_cloudflare_helper,
            "headless_publishers": sorted(self.headless_publishers),
        }

    @classmethod
    def from_worker_payload(cls, payload: dict) -> "Settings":
        _load_runtime_env()
        return cls(
            download_root=Path(payload["download_root"]),
            max_concurrency=1,
            article_timeout_seconds=int(payload.get("article_timeout_seconds", 120)),
            wiley_article_timeout_seconds=int(payload.get("wiley_article_timeout_seconds", 600)),
            elsevier_article_timeout_seconds=int(
                payload.get("elsevier_article_timeout_seconds", 600)
            ),
            cloudflare_article_timeout_seconds=int(
                payload.get("cloudflare_article_timeout_seconds", 360)
            ),
            navigation_timeout_seconds=int(payload.get("navigation_timeout_seconds", 30)),
            normal_element_timeout_seconds=int(payload.get("normal_element_timeout_seconds", 18)),
            native_download_timeout_seconds=int(payload.get("native_download_timeout_seconds", 35)),
            blob_download_timeout_seconds=int(payload.get("blob_download_timeout_seconds", 75)),
            cloudflare_timeout_seconds=int(payload.get("cloudflare_timeout_seconds", 60)),
            manual_captcha_wait_seconds=int(
                payload.get("manual_captcha_wait_seconds", 180)
            ),
            settle_seconds=float(payload.get("settle_seconds", 1.2)),
            # The child inherits the server environment. Never serialize API keys
            # into downloads/_worker_runs/request.json.
            elsevier_api_key=os.getenv("ELSEVIER_API_KEY") or None,
            enable_pydoll_cloudflare_helper=bool(payload.get("enable_pydoll_cloudflare_helper", False)),
            headless_publishers=frozenset(
                str(key).upper() for key in payload.get("headless_publishers", ())
            ),
        ).normalized()

    def timeout_for_doi(self, doi: str) -> int:
        lowered = doi.lower()
        if lowered.startswith("10.1002/") or lowered.startswith("10.1049/"):
            return max(self.article_timeout_seconds, self.wiley_article_timeout_seconds)
        if lowered.startswith("10.1016/"):
            return max(self.article_timeout_seconds, self.elsevier_article_timeout_seconds)
        if lowered.startswith(CLOUDFLARE_DOI_PREFIXES):
            return max(self.article_timeout_seconds, self.cloudflare_article_timeout_seconds)
        if lowered.startswith(MANUAL_CHALLENGE_DOI_PREFIXES):
            # The budget must cover the operator solving a captcha by hand.
            return max(
                self.article_timeout_seconds,
                self.manual_captcha_wait_seconds + 240,
            )
        return self.article_timeout_seconds

    def article_timeout_for_doi(self, doi: str) -> int:
        """Budget handed to the DOI worker; challenge publishers need more room."""

        return self.timeout_for_doi(doi)

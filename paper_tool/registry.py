from __future__ import annotations

from .adapters import ALL_ADAPTERS
from .adapters.base import PublisherAdapter


def get_adapter(doi: str) -> PublisherAdapter | None:
    for adapter_cls in ALL_ADAPTERS:
        if adapter_cls.matches_doi(doi):
            return adapter_cls()
    return None


def supported_publishers() -> list[dict[str, str]]:
    return [
        {"key": cls.key, "name": cls.publisher_name}
        for cls in ALL_ADAPTERS
    ]

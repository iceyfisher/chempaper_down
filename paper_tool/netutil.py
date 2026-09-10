"""Network helpers shared by adapters that talk to publisher APIs directly."""
from __future__ import annotations

import os
import socket
from urllib.parse import urlparse


def env_proxy_usable() -> bool:
    """True when an env-configured proxy is actually reachable.

    Developer machines frequently export http_proxy/https_proxy for a local
    tunnel (e.g. 127.0.0.1:7890). When that tunnel is down, httpx's default
    trust_env=True turns every API call into an immediate ConnectError even
    though the campus network can reach the publisher directly. httpx has no
    built-in "fallback to direct", so callers pass trust_env=env_proxy_usable().
    """

    url = (
        os.getenv("https_proxy")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("http_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("all_proxy")
        or os.getenv("ALL_PROXY")
    )
    if not url:
        return False
    try:
        host = urlparse(url).hostname or "127.0.0.1"
        port = urlparse(url).port or (443 if url.lower().startswith("https") else 80)
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False

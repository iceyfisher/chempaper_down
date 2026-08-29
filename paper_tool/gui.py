"""Native desktop shell for the Paper Download Tool web UI.

Starts the FastAPI server on a free local port inside a background thread, then
opens the UI in a pywebview native window (Edge WebView2 on Windows). If
pywebview is not installed, the UI is opened in the default browser instead.

Entry points:
    paper-tool-app            (installed console script)
    python -m paper_tool.gui
"""

from __future__ import annotations

import socket
import threading
import time
import webbrowser
from urllib.parse import urlunparse

import httpx
import uvicorn


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return sock.getsockname()[1]


def _serve(port: int) -> None:
    from .api import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def _wait_healthy(port: int, timeout: float = 30.0) -> bool:
    url = urlunparse(("http", f"127.0.0.1:{port}", "/api/health", "", "", ""))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=2)
            if response.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def main() -> None:
    port = _free_port()
    thread = threading.Thread(target=_serve, args=(port,), daemon=True)
    thread.start()

    url = urlunparse(("http", f"127.0.0.1:{port}", "/", "", "", ""))
    if not _wait_healthy(port):
        raise SystemExit("Paper Tool server failed to start on port {port}".format(port=port))

    try:
        import webview

        print(f"Paper Downloader: {url} (close the window to quit)")
        webview.create_window(
            "Paper Downloader",
            url,
            width=1280,
            height=900,
            min_size=(960, 640),
        )
        webview.start()
    except ImportError:
        print(f"pywebview 未安装，改用默认浏览器打开：{url}")
        print("（可选）安装桌面窗口支持：python -m pip install pywebview")
        webbrowser.open(url)
        try:
            while thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

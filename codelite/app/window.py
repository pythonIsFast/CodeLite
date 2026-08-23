# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Opens Code Lite in a native window.

The window is drawn by the operating system's own webview (WebKitGTK on
Linux, WebView2 on Windows, WKWebView on macOS) via ``pywebview`` -- nothing
like Electron is bundled, which is what keeps the install in the low
single-digit megabytes. Flask runs in a daemon thread inside this same
process and only ever listens on localhost.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

from ..config import (
    WINDOW_HEIGHT,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
    WINDOW_TITLE,
    WINDOW_WIDTH,
    AppConfig,
)
from .server import create_app

logger = logging.getLogger(__name__)

STARTUP_TIMEOUT_SECONDS = 15.0


def serve_in_background(config: AppConfig) -> threading.Thread:
    """Start the Flask server in a daemon thread and wait until it accepts connections."""
    app = create_app(config)

    # Werkzeug's dev server is right for a single local user; silence its
    # request log so the terminal stays readable.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    thread = threading.Thread(
        target=lambda: app.run(
            host=config.host,
            port=config.port,
            threaded=True,
            debug=False,
            use_reloader=False,
        ),
        name="codelite-server",
        daemon=True,
    )
    thread.start()
    _wait_for_port(config.host, config.port)
    return thread


def _wait_for_port(host: str, port: int) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.4)
            if probe.connect_ex((host, port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(
        f"The Code Lite server did not start on {host}:{port} within "
        f"{STARTUP_TIMEOUT_SECONDS:.0f}s."
    )


def run(config: AppConfig | None = None, headless: bool = False) -> None:
    """Serve the app, and (unless ``headless``) open the native window."""
    config = config or AppConfig()
    serve_in_background(config)

    if headless:
        logger.info("Code Lite is serving on %s (no window requested)", config.base_url)
        _block_forever()
        return

    try:
        import webview  # noqa: PLC0415 - optional at import time on purpose
    except ImportError:
        logger.error(
            "pywebview is not installed, so the native window cannot open.\n"
            "  Install it with:  pip install pywebview\n"
            "  Or run without a window:  python3 -m codelite --headless\n"
            "Serving on %s in the meantime.",
            config.base_url,
        )
        _block_forever()
        return

    webview.create_window(
        WINDOW_TITLE,
        config.base_url,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT),
    )
    webview.start()


def _block_forever() -> None:
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass

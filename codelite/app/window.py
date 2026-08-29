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

import base64
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

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


class JsApi:
    """Exposed to the page as ``window.pywebview.api`` -- the one bit of native
    OS integration the page needs (a real folder picker instead of a typed
    path), kept to a single method on purpose."""

    def choose_directory(self, start: str | None = None) -> str | None:
        import webview  # noqa: PLC0415 - only reachable once pywebview is running

        initial = start if start and Path(start).is_dir() else str(Path.home())
        result = webview.windows[0].create_file_dialog(
            webview.FOLDER_DIALOG, directory=initial
        )
        return result[0] if result else None

    def open_external(self, url: str) -> bool:
        """Open OAuth in the real browser, outside the embedded app window."""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "auth.openai.com":
            raise ValueError("Only the ChatGPT login URL may be opened externally.")

        # On Linux, Python's ``webbrowser`` delegates to xdg-open and may
        # report success even when it cannot resolve a Flatpak desktop entry.
        # GIO uses the desktop's application registry directly.
        if sys.platform.startswith("linux"):
            try:
                import gi  # noqa: PLC0415 - optional native Linux integration

                gi.require_version("Gio", "2.0")
                from gi.repository import Gio  # noqa: PLC0415

                if Gio.AppInfo.launch_default_for_uri(url, None):
                    return True
            except Exception:  # noqa: BLE001 - availability is platform dependent
                logger.debug("The GIO browser opener failed", exc_info=True)

        try:
            if webbrowser.open(url, new=1):
                return True
        except OSError:
            logger.debug("The Python browser opener failed", exc_info=True)

        try:
            if sys.platform.startswith("linux"):
                subprocess.Popen(
                    ["xdg-open", url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            if sys.platform == "darwin":
                subprocess.Popen(
                    ["open", url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            if os.name == "nt":
                os.startfile(url)  # type: ignore[attr-defined]  # noqa: S606
                return True
        except OSError:
            logger.warning("Could not open the ChatGPT login URL in a browser", exc_info=True)
        return False

    def read_clipboard_image(self) -> dict[str, str] | None:
        """Read a screenshot from the native Linux clipboard as a PNG.

        WebKitGTK does not consistently expose image clipboard data to
        JavaScript's ``paste`` event. Its GTK host does, so this is a narrow
        native fallback used only after the browser-side clipboard paths fail.
        Other platforms simply return ``None`` and keep their normal webview
        clipboard handling.
        """
        try:
            import gi  # noqa: PLC0415 - optional native Linux integration

            gi.require_version("Gtk", "3.0")
            from gi.repository import Gdk, GLib, Gtk  # noqa: PLC0415

            captured: dict[str, str] | None = None

            def capture() -> None:
                nonlocal captured
                clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
                pixbuf = clipboard.wait_for_image()
                if pixbuf is None:
                    return
                _success, content = pixbuf.save_to_bufferv("png", [], [])
                captured = {
                    "name": "pasted-screenshot.png",
                    "type": "image/png",
                    "data": base64.b64encode(bytes(content)).decode("ascii"),
                }

            # pywebview invokes exposed Python functions off its GTK UI
            # thread. Marshal clipboard access back to that thread and bound
            # the wait so a broken clipboard provider cannot freeze the app.
            if GLib.MainContext.default().is_owner():
                capture()
            else:
                completed = threading.Event()

                def on_idle() -> bool:
                    try:
                        capture()
                    except Exception:  # noqa: BLE001 - report through the JS fallback
                        logger.debug("Could not read the GTK clipboard", exc_info=True)
                    finally:
                        completed.set()
                    return False

                GLib.idle_add(on_idle)
                completed.wait(timeout=3)
            return captured
        except Exception:  # noqa: BLE001 - clipboard availability is platform dependent
            logger.debug("Native clipboard image fallback was unavailable", exc_info=True)
            return None


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
        js_api=JsApi(),
    )
    webview.start()


def _block_forever() -> None:
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass

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

"""Owns the child process that runs the hidden browser window.

One process for the whole app, started lazily on the first call and reused
after that -- the point of a hidden window is that opening it is not free, so
nothing should pay that cost until something actually asks for the browser.
"""

from __future__ import annotations

import atexit
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .. import settings

#: How long a request may go unanswered before this is treated as a hang.
#: Falls back to this only if the setting cannot be read (e.g. during a
#: schema migration); the live value normally comes from settings.active().
DEFAULT_TIMEOUT_SECONDS = 30.0


class BrowserError(RuntimeError):
    """Anything that went wrong talking to the browser process."""


def _import_root() -> Path:
    """Whatever has to be on ``sys.path`` for ``import codelite`` to work.

    Mirrors the zipapp detection in :mod:`codelite.app.server`: inside a
    ``.pyz`` this module's ``__file__`` looks like
    ``/path/app.pyz/codelite/browser/client.py``, and the first parent that is
    an actual file *is* the archive -- a valid ``sys.path`` entry on its own
    via ``zipimport``. In a plain checkout there is no such parent, so this
    falls back to the project root three levels up.
    """
    module = Path(__file__).resolve()
    archive = next((parent for parent in module.parents if parent.is_file()), None)
    if archive is not None:
        return archive
    return module.parents[2]


class BrowserClient:
    """Talks JSON-lines to one ``codelite.browser.host`` child process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._next_id = 0

    def _child_command(self) -> list[str]:
        if getattr(sys, "frozen", False):
            # A frozen build's `sys.executable` is the app's own binary, not a
            # general Python interpreter -- there is nothing to hand `-c` to,
            # and no on-disk `codelite` package for a system Python to import
            # either. Failing clearly here beats a cryptic subprocess crash.
            raise BrowserError(
                "The browser tool needs a system Python interpreter to run its "
                "hidden window as a separate process, which this packaged build "
                "does not have. It works from a normal checkout or the Linux "
                "zipapp build."
            )
        root = _import_root()
        bootstrap = (
            f"import sys; sys.path.insert(0, {str(root)!r}); "
            "from codelite.browser.host import main; main()"
        )
        return [sys.executable, "-c", bootstrap]

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        command = self._child_command()
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            raise BrowserError(f"Could not start the browser process: {error}") from error
        self._queue = queue.Queue()
        threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True).start()

    def _read_loop(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self._queue.put(line)
        # The process ended; wake up whichever call() is still waiting rather
        # than leaving it blocked on a line that will never arrive.
        self._queue.put(None)

    def _timeout(self) -> float:
        try:
            return float(settings.active("browser_timeout_seconds"))
        except Exception:  # noqa: BLE001 - a settings hiccup must not hang a call
            return DEFAULT_TIMEOUT_SECONDS

    def call(self, action: str, **params: Any) -> Any:
        """Send one command, wait for its matching response, return `result`.

        Serialized by a single lock: the protocol is one request in flight at
        a time, matching how the host reads one line, replies, then reads the
        next -- there is no request id multiplexing on the wire.
        """
        with self._lock:
            self._ensure_started()
            assert self._proc is not None and self._proc.stdin is not None
            self._next_id += 1
            request_id = self._next_id
            payload = json.dumps({"id": request_id, "action": action, **params})
            try:
                self._proc.stdin.write(payload + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as error:
                raise BrowserError(f"The browser process is not available: {error}") from error

            deadline = time.monotonic() + self._timeout()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrowserError(
                        f"The browser did not respond to `{action}` within the "
                        "configured timeout."
                    )
                try:
                    line = self._queue.get(timeout=remaining)
                except queue.Empty:
                    raise BrowserError(
                        f"The browser did not respond to `{action}` within the "
                        "configured timeout."
                    ) from None

                if line is None:
                    stderr = ""
                    if self._proc is not None and self._proc.stderr is not None:
                        stderr = self._proc.stderr.read().strip()
                    detail = f" {stderr}" if stderr else ""
                    raise BrowserError(f"The browser process exited unexpectedly.{detail}")

                try:
                    response = json.loads(line)
                except ValueError as error:
                    raise BrowserError(
                        f"The browser sent an unreadable response: {error}"
                    ) from error

                if isinstance(response, dict) and response.get("id") == request_id:
                    break
                # A response to a call this client already gave up waiting on
                # (it timed out, but the host answered late) -- discard it and
                # keep waiting for the one that actually matches. Without this
                # every later call would desync forever off this one leftover
                # line.

        if not response.get("ok"):
            raise BrowserError(response.get("error") or f"`{action}` failed.")
        return response.get("result")

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.write(json.dumps({"id": 0, "action": "shutdown"}) + "\n")
                proc.stdin.flush()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001 - closing must not raise, only clean up
            proc.kill()


_lock = threading.Lock()
_client: BrowserClient | None = None


def get_client() -> BrowserClient:
    global _client
    with _lock:
        if _client is None:
            _client = BrowserClient()
        return _client


def close_client() -> None:
    global _client
    with _lock:
        client, _client = _client, None
    if client is not None:
        client.close()


atexit.register(close_client)

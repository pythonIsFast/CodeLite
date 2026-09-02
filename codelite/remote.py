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

"""Optional password-protected Cloudflare Quick Tunnel for the local UI."""

from __future__ import annotations

import atexit
import collections
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import secrets
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CLOUDFLARED_RELEASE_API = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
REMOTE_HOST_HEADER = "remote.codelite.invalid"
REMOTE_COOKIE = "codelite_remote"
_LOGIN_WINDOW_SECONDS = 60
_MAX_LOGIN_ATTEMPTS = 8
_SESSION_IDLE_SECONDS = 12 * 60 * 60
_TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class RemoteError(RuntimeError):
    pass


def _asset_name() -> str:
    machine = platform.machine().lower()
    if os.name == "nt":
        if machine not in {"amd64", "x86_64"}:
            raise RemoteError(f"Remote Control does not support Windows {machine} yet.")
        return "cloudflared-windows-amd64.exe"
    if sys_platform() == "linux":
        architectures = {
            "amd64": "amd64",
            "x86_64": "amd64",
            "aarch64": "arm64",
            "arm64": "arm64",
            "armv7l": "arm",
        }
        architecture = architectures.get(machine)
        if not architecture:
            raise RemoteError(f"Remote Control does not support Linux {machine} yet.")
        return f"cloudflared-linux-{architecture}"
    raise RemoteError("Remote Control is currently available on Windows and Linux.")


def sys_platform() -> str:
    # Isolated for small platform tests without replacing the sys module.
    import sys

    return sys.platform


class RemoteManager:
    def __init__(self, data_dir: Path, origin_url: str) -> None:
        self.data_dir = Path(data_dir)
        self.origin_url = origin_url
        suffix = ".exe" if os.name == "nt" else ""
        self.binary = self.data_dir / "bin" / f"cloudflared{suffix}"
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._url: str | None = None
        self._url_ready = threading.Event()
        self._salt: bytes | None = None
        self._password_hash: bytes | None = None
        self._sessions: dict[str, float] = {}
        self._attempts: collections.deque[float] = collections.deque()
        atexit.register(self.stop)

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is not None:
                self._clear_tunnel()
            return {
                "supported": self._supported(),
                "installed": self.binary.is_file(),
                "active": self._process is not None,
                "url": self._url,
            }

    def _supported(self) -> bool:
        try:
            _asset_name()
        except RemoteError:
            return False
        return True

    def download(self) -> dict[str, Any]:
        asset_name = _asset_name()
        request = urllib.request.Request(
            CLOUDFLARED_RELEASE_API,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "CodeLite-Remote"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                release = json.load(response)
        except (OSError, ValueError) as error:
            raise RemoteError(f"Could not find the cloudflared download: {error}") from error
        assets = release.get("assets") if isinstance(release, dict) else None
        asset = next(
            (
                item
                for item in assets or []
                if isinstance(item, dict) and item.get("name") == asset_name
            ),
            None,
        )
        if asset is None or not asset.get("browser_download_url"):
            raise RemoteError(f"The latest cloudflared release has no {asset_name} asset.")
        digest = str(asset.get("digest") or "")
        if not digest.startswith("sha256:"):
            raise RemoteError("The cloudflared release did not provide a SHA-256 digest.")

        self.binary.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.binary.with_suffix(self.binary.suffix + ".download")
        download = urllib.request.Request(
            str(asset["browser_download_url"]), headers={"User-Agent": "CodeLite-Remote"}
        )
        actual = hashlib.sha256()
        try:
            with urllib.request.urlopen(download, timeout=180) as response, temporary.open("wb") as out:
                while chunk := response.read(1024 * 1024):
                    actual.update(chunk)
                    out.write(chunk)
            if not hmac.compare_digest(actual.hexdigest(), digest.removeprefix("sha256:").lower()):
                raise RemoteError("The cloudflared download failed its SHA-256 check.")
            if os.name != "nt":
                temporary.chmod(0o700)
            temporary.replace(self.binary)
        except RemoteError:
            temporary.unlink(missing_ok=True)
            raise
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise RemoteError(f"Could not download cloudflared: {error}") from error
        return self.status()

    def remove(self) -> dict[str, Any]:
        self.stop()
        try:
            self.binary.unlink(missing_ok=True)
        except OSError as error:
            raise RemoteError(f"Could not remove cloudflared: {error}") from error
        return self.status()

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RemoteError("Remote Control is already active.")
            if not self.binary.is_file():
                raise RemoteError("Download cloudflared before starting Remote Control.")
            password = self._new_password()
            self._url = None
            self._url_ready.clear()
            try:
                process = subprocess.Popen(
                    [
                        str(self.binary),
                        "tunnel",
                        "--no-autoupdate",
                        "--http-host-header",
                        REMOTE_HOST_HEADER,
                        "--url",
                        self.origin_url,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError as error:
                raise RemoteError(f"Could not start cloudflared: {error}") from error
            self._process = process
            threading.Thread(
                target=self._read_tunnel_output,
                args=(process,),
                name="codelite-remote-tunnel",
                daemon=True,
            ).start()

        if not self._url_ready.wait(25):
            self.stop()
            raise RemoteError("cloudflared did not provide a public URL in time.")
        with self._lock:
            if not self._url:
                self.stop()
                raise RemoteError("cloudflared exited before Remote Control became available.")
            return {**self.status(), "password": password}

    def _read_tunnel_output(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            match = _TUNNEL_URL_RE.search(line)
            if match:
                with self._lock:
                    if process is self._process:
                        self._url = match.group(0)
                        self._url_ready.set()
                continue
            logger.debug("cloudflared: %s", line.rstrip())
        self._url_ready.set()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            self._clear_tunnel()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        return self.status()

    def _clear_tunnel(self) -> None:
        self._process = None
        self._url = None
        self._salt = None
        self._password_hash = None
        self._sessions.clear()

    def _new_password(self) -> str:
        alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        password = "-".join(
            "".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4)
        )
        self._salt = secrets.token_bytes(16)
        self._password_hash = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), self._salt, 250_000
        )
        self._sessions.clear()
        self._attempts.clear()
        return password

    def login(self, password: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            while self._attempts and self._attempts[0] < now - _LOGIN_WINDOW_SECONDS:
                self._attempts.popleft()
            if len(self._attempts) >= _MAX_LOGIN_ATTEMPTS:
                raise RemoteError("Too many login attempts. Try again in one minute.")
            self._attempts.append(now)
            if self._salt is None or self._password_hash is None:
                return None
            supplied = hashlib.pbkdf2_hmac("sha256", password.encode(), self._salt, 250_000)
            if not hmac.compare_digest(supplied, self._password_hash):
                return None
            token = secrets.token_urlsafe(32)
            self._sessions[token] = now
            return token

    def authenticated(self, token: str | None) -> bool:
        if not token:
            return False
        now = time.monotonic()
        with self._lock:
            seen = self._sessions.get(token)
            if seen is None or seen < now - _SESSION_IDLE_SECONDS:
                self._sessions.pop(token, None)
                return False
            self._sessions[token] = now
            return True

    def is_remote_host(self, host: str) -> bool:
        return host.partition(":")[0].lower() == REMOTE_HOST_HEADER

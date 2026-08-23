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
#
# Ported from openai-oauth (https://github.com/EvanZhouDev/openai-oauth),
# packages/core/src/models.ts, Apache-2.0.

"""Resolves the Codex client version and fetches Codex's model catalog.

Codex's `/models` endpoint wants a `client_version` query parameter that
mirrors the version of the official `@openai/codex` npm package -- the
backend uses it to gate which models a given client is allowed to see. We
fetch the latest published version from the npm registry once per hour and
fall back to a pinned default if that lookup fails (offline, npm down, etc).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .config import CODEX_REGISTRY_URL, DEFAULT_CODEX_CLIENT_VERSION

_VERSION_CACHE_TTL_SECONDS = 60 * 60
_VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+\b")

_cached_version: str | None = None
_cached_version_expires_at: float = 0.0


def _normalize_version(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.search(value.strip())
    return match.group(0) if match else None


def reset_codex_client_version_cache() -> None:
    """Mainly for tests: drop the cached npm-registry lookup."""
    global _cached_version, _cached_version_expires_at
    _cached_version = None
    _cached_version_expires_at = 0.0


def resolve_codex_client_version(explicit_version: str | None = None) -> str:
    """Return the Codex client version to advertise, fetching it if needed."""
    global _cached_version, _cached_version_expires_at

    normalized = _normalize_version(explicit_version)
    if normalized:
        return normalized

    now = time.monotonic()
    if _cached_version and now < _cached_version_expires_at:
        return _cached_version

    version = DEFAULT_CODEX_CLIENT_VERSION
    try:
        request = urllib.request.Request(
            CODEX_REGISTRY_URL, headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=10.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        fetched = _normalize_version(payload.get("version"))
        if fetched:
            version = fetched
    except (urllib.error.URLError, ValueError, TimeoutError):
        pass

    _cached_version = version
    _cached_version_expires_at = now + _VERSION_CACHE_TTL_SECONDS
    return version


@dataclass
class CodexModelInfo:
    slug: str
    visibility: str | None = None
    supported_in_api: bool | None = None
    use_responses_lite: bool | None = None
    support_verbosity: bool | None = None
    default_verbosity: str | None = None
    default_reasoning_level: str | None = None
    #: Effective *input* budget in tokens -- the figure to measure usage
    #: against. Codex reports 272000 for every model today, which is the 400000
    #: total minus 128000 held back for output. The official CLI compacts at
    #: 0.95 of it (~258400), which is why this app stops there too.
    context_window: int | None = None
    #: What the underlying model could take. Capability, not policy: requests
    #: are held to `context_window` regardless, so this must not be used as
    #: the denominator for a usage indicator.
    max_context_window: int | None = None
    display_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_int(value: Any) -> int | None:
    # Reject bools explicitly: `isinstance(True, int)` is True in Python.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None


def _to_model_info(value: Any) -> CodexModelInfo | None:
    if not isinstance(value, dict):
        return None
    slug = _optional_str(value.get("slug"))
    if not slug:
        return None
    return CodexModelInfo(
        slug=slug,
        visibility=_optional_str(value.get("visibility")),
        supported_in_api=_optional_bool(value.get("supported_in_api")),
        use_responses_lite=_optional_bool(value.get("use_responses_lite")),
        support_verbosity=_optional_bool(value.get("support_verbosity")),
        default_verbosity=_optional_str(value.get("default_verbosity")),
        default_reasoning_level=_optional_str(value.get("default_reasoning_level")),
        context_window=_optional_int(value.get("context_window")),
        max_context_window=_optional_int(value.get("max_context_window")),
        display_name=_optional_str(value.get("display_name")),
        raw=value,
    )


def is_public_codex_model(model: CodexModelInfo) -> bool:
    return model.supported_in_api is not False and model.visibility in (None, "list")


class CodexModelCatalogClient(Protocol):
    def request(self, path: str) -> tuple[int, bytes]: ...


def _upstream_error_message(body_text: str) -> str:
    if not body_text:
        return "Failed to load models from Codex."
    try:
        parsed = json.loads(body_text)
    except ValueError:
        return body_text
    if isinstance(parsed, dict):
        detail = parsed.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        error = parsed.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return body_text


def fetch_codex_model_catalog(
    request: Callable[[str], tuple[int, bytes]],
    *,
    codex_version: str | None = None,
) -> list[CodexModelInfo]:
    """Fetch and parse Codex's `/models` catalog through an authenticated `request` callback."""
    client_version = resolve_codex_client_version(codex_version)
    status, body = request(f"/models?client_version={urllib.parse.quote(client_version)}")
    body_text = body.decode("utf-8", errors="replace")

    if status >= 400:
        raise RuntimeError(_upstream_error_message(body_text))

    try:
        parsed = json.loads(body_text)
    except ValueError as error:
        raise RuntimeError("Codex returned an invalid models response.") from error

    if not isinstance(parsed, dict) or not isinstance(parsed.get("models"), list):
        raise RuntimeError("Codex returned a malformed models response.")

    models = [m for m in (_to_model_info(v) for v in parsed["models"]) if m is not None]
    if not models:
        raise RuntimeError("Codex returned an empty models list.")
    return models

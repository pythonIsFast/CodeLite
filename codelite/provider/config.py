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
# Default values below (endpoints, client id, model constants) are ported
# from openai-oauth (https://github.com/EvanZhouDev/openai-oauth),
# packages/core/src/runtime.ts and packages/core/src/models.ts, Apache-2.0.

"""Central, reusable configuration for the Code Lite provider layer.

Keeping every endpoint/model/path default in one place means the transport,
auth, and server modules never hardcode a URL themselves -- and a later
agent loop can build a :class:`ProviderConfig` itself instead of going
through the HTTP proxy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# -- OpenAI / ChatGPT OAuth -------------------------------------------------

DEFAULT_OAUTH_ISSUER = "https://auth.openai.com"
DEFAULT_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_OAUTH_SCOPE = "openid profile email offline_access"

# -- Codex backend ------------------------------------------------------------

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_CODEX_CLIENT_VERSION = "0.144.1"
CODEX_REGISTRY_URL = "https://registry.npmjs.org/@openai/codex/latest"

# -- Images -------------------------------------------------------------------

DEFAULT_IMAGE_MODEL = "gpt-image-2"
MAX_REFERENCE_IMAGES = 5
MAX_REFERENCE_IMAGE_BYTES = 50 * 1024 * 1024

# -- Chat completions default model -------------------------------------------

DEFAULT_CHAT_MODEL = "gpt-5.2"

# -- Local proxy server ---------------------------------------------------------

DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 10531

# -- Auth file ------------------------------------------------------------------

AUTH_FILENAME = "auth.json"
REFRESH_EXPIRY_MARGIN_SECONDS = 5 * 60
REFRESH_INTERVAL_SECONDS = 55 * 60


def default_auth_file_path() -> Path:
    """Resolve Code Lite's own auth file, separate from Codex credentials."""
    codelite_home = os.environ.get("CODELITE_HOME")
    if codelite_home:
        return Path(codelite_home) / AUTH_FILENAME
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "codelite" / AUTH_FILENAME


@dataclass
class ProviderConfig:
    """All knobs the provider layer needs, gathered in one place.

    Pass an instance to :func:`codelite.provider.load_session` or to the
    HTTP server; every field has a sensible default matching the reference
    implementation.
    """

    auth_file_path: Path = field(default_factory=default_auth_file_path)
    oauth_issuer: str = DEFAULT_OAUTH_ISSUER
    oauth_client_id: str = DEFAULT_OAUTH_CLIENT_ID
    oauth_token_url: str | None = None
    codex_base_url: str = DEFAULT_CODEX_BASE_URL
    codex_client_version: str | None = None
    chat_model: str = DEFAULT_CHAT_MODEL
    image_model: str = DEFAULT_IMAGE_MODEL
    instructions: str = ""
    ensure_fresh_tokens: bool = True

    def __post_init__(self) -> None:
        self.auth_file_path = Path(self.auth_file_path)

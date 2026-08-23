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

"""App-wide configuration for the Code Lite agent.

Deliberately separate from :mod:`codelite.provider.config`, which stays
concerned only with OAuth/Codex endpoints. This module holds what the *app*
needs: where the database lives, which port the local server binds, and the
defaults a new conversation starts with.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .permission.modes import Mode

DEFAULT_AGENT_MODEL = "gpt-5.6-sol"
DEFAULT_JUDGE_MODEL = "gpt-5.6-luna"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 10532

WINDOW_TITLE = "Code Lite"
WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 800
WINDOW_MIN_WIDTH = 720
WINDOW_MIN_HEIGHT = 520

SHELL_TIMEOUT_SECONDS = 120
MAX_TOOL_OUTPUT_CHARS = 30_000
MAX_AGENT_STEPS = 40


def default_data_dir() -> Path:
    """Resolve the app's data directory, honoring ``$XDG_DATA_HOME``."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "codelite"


@dataclass
class AppConfig:
    """Everything the agent app needs, in one place."""

    data_dir: Path = field(default_factory=default_data_dir)
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    agent_model: str = DEFAULT_AGENT_MODEL
    judge_model: str = DEFAULT_JUDGE_MODEL
    default_permission_mode: Mode = Mode.ASK
    shell_timeout_seconds: int = SHELL_TIMEOUT_SECONDS
    max_tool_output_chars: int = MAX_TOOL_OUTPUT_CHARS
    max_agent_steps: int = MAX_AGENT_STEPS

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "codelite.db"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

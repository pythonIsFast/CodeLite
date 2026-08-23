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

#: There is deliberately no step cap: a task takes as many turns as it takes,
#: and an arbitrary ceiling just abandons work halfway. The context window is
#: the real budget, so a run stops when it has nearly filled it.
#:
#: 0.95 is not arbitrary -- it is the same headroom the official Codex CLI
#: keeps before it compacts (0.95 of the 272000 input budget, so ~258400).
CONTEXT_STOP_FRACTION = 0.95

# Offline fallback only. Codex's /models catalog does report each model's real
# `context_window`, and that is what the app prefers -- see
# `Session.context_window`. These are the figures the catalog returned on
# 2026-08-23, kept so the indicator still works when the catalog is
# unreachable. Unlisted models fall back to DEFAULT_CONTEXT_WINDOW.
DEFAULT_CONTEXT_WINDOW = 272_000
CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-5.6-sol": 272_000,
    "gpt-5.6-terra": 272_000,
    "gpt-5.6-luna": 272_000,
    "gpt-5.5": 272_000,
    "gpt-5.4": 272_000,
    "gpt-5.4-mini": 272_000,
    "codex-auto-review": 272_000,
}


def context_window_for(model: str) -> int:
    """Fallback window size. Prefer ``Session.context_window`` for live data."""
    return CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)


def default_data_dir() -> Path:
    """Resolve the app's data directory, honoring Code Lite/XDG overrides."""
    codelite_home = os.environ.get("CODELITE_HOME")
    if codelite_home:
        return Path(codelite_home)
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
    context_stop_fraction: float = CONTEXT_STOP_FRACTION

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "codelite.db"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

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
from typing import Any, Sequence
from pathlib import Path

from .permission.modes import Mode

DEFAULT_AGENT_MODEL = "gpt-6-astra"
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
#: the real budget, so older history is compacted before it gets close to full.
#: The hard stop remains only as a safe fallback if compaction itself fails.
CONTEXT_COMPACT_FRACTION = 0.80
CONTEXT_STOP_FRACTION = 0.95
# Offline fallback only. Codex's /models catalog does report each model's real
# `context_window`, and that is what the app prefers -- see
# `Session.context_window`. These are the figures the catalog returned on
# 2026-08-23, kept so the indicator still works when the catalog is
# unreachable. Unlisted models fall back to DEFAULT_CONTEXT_WINDOW.
DEFAULT_CONTEXT_WINDOW = 272_000
CONTEXT_WINDOWS: dict[str, int] = {
    # Astra accepts 1.05M total tokens; 128k are reserved for output, leaving
    # the 922k effective input budget used by Code Lite's compaction meter.
    "gpt-6-astra": 922_000,
    "gpt-5.6-sol": 272_000,
    "gpt-5.6-terra": 272_000,
    "gpt-5.6-luna": 272_000,
    "gpt-5.5": 272_000,
    "gpt-5.4": 272_000,
    "gpt-5.4-mini": 272_000,
    "codex-auto-review": 272_000,
}


#: Every reasoning level Codex has been observed to accept, cheapest first.
#: This is a validation backstop, not the menu: which levels a given model
#: takes comes from the catalog (`supported_reasoning_levels`) and genuinely
#: differs -- Sol and Terra reach `ultra`, Luna stops at `max`, GPT-5.5 at
#: `xhigh`. Offering a level the chosen model rejects earns an HTTP 400.
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")

#: Requesting the Fast tier. Codex names it "Fast -- 1.5x speed, increased
#: usage" in its catalog and validates the value, but an account without the
#: entitlement is quietly served `default` instead of being refused. So this is
#: a request, never a promise: what actually happened is echoed back on the
#: response, and that echo is what the UI reports.
FAST_SERVICE_TIER = "priority"


def normalize_effort(value: Any, allowed: Sequence[str] | None = None) -> str:
    """Coerce a requested reasoning level, falling back to the model default.

    Returns "" for anything unusable, which means "send no reasoning field and
    let the model's own catalog default apply". Pass ``allowed`` (the model's
    own levels) whenever the model is known; without it the check can only be
    against everything Codex has ever accepted.
    """
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    permitted = tuple(allowed) if allowed else REASONING_EFFORTS
    return candidate if candidate in permitted else ""


#: Catalog entries that must never appear in the chat model picker. The image
#: model is a tool's business, not something to hold a conversation with, and
#: the review model is an internal Codex model. `codex-auto-review` is already
#: hidden by its catalog visibility today; it is named here so a change on
#: OpenAI's side cannot put it back in the list.
NON_CHAT_MODELS = frozenset({"codex-auto-review"})


def chat_models(available: list[str], image_model: str = "") -> list[str]:
    """The models worth offering as a conversation's model."""
    excluded = NON_CHAT_MODELS | ({image_model} if image_model else set())
    return [model for model in available if model not in excluded]


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
    compaction_model: str = DEFAULT_JUDGE_MODEL
    default_permission_mode: Mode = Mode.ASK
    #: Empty means each model's own default from Codex's catalog.
    default_reasoning_effort: str = ""
    shell_timeout_seconds: int = SHELL_TIMEOUT_SECONDS
    max_tool_output_chars: int = MAX_TOOL_OUTPUT_CHARS
    context_stop_fraction: float = CONTEXT_STOP_FRACTION
    context_compact_fraction: float = CONTEXT_COMPACT_FRACTION
    #: Which models the Auto picker may route to, and how it recovers.
    auto_router_model: str = DEFAULT_JUDGE_MODEL
    auto_fallback_model: str = "gpt-5.6-terra"
    auto_models: tuple[str, ...] = (
        "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"
    )
    #: How long to wait on a language server or MCP server, per request.
    lsp_timeout_seconds: float = 15.0
    lsp_diagnostic_wait_seconds: float = 1.0
    mcp_timeout_seconds: float = 20.0
    #: How much project text is put in front of the model, per run.
    max_memory_chars: int = 2_500
    max_instruction_chars: int = 12_000

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "codelite.db"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

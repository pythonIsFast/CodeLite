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

"""User-adjustable behaviour, described once.

Every setting is declared here with its type, bounds, group and help text, and
that one declaration serves three jobs: it validates what the API accepts, it
supplies the defaults, and it is handed to the UI to render the form. The point
is that a bound cannot drift -- an input box that allows a value the server
rejects, or the reverse, is not possible if both read the same table.

Bounds are not decoration. Several of these values are protective limits, so a
setting only moves them *within* what is safe rather than removing them: a
shell timeout of a week or a tool output of a gigabyte would break a run rather
than loosen it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

#: Groups in the order the UI shows them. The last one is deliberately last.
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("agent", "Agent", "Which models do the work."),
    ("auto", "Auto mode", "What the Auto picker is allowed to choose."),
    ("tools", "Tools", "Limits on what a single tool call may take or return."),
    (
        "integrations",
        "Integrations",
        "How long to wait for a language server or MCP server.",
    ),
    (
        "context",
        "Project context",
        "How much project memory and instruction text is put in front of the model. "
        "This is spent on every run, so it is a direct cost.",
    ),
    (
        "danger",
        "Danger zone",
        "These bound the context window. The defaults match what the official "
        "Codex CLI uses; raising them makes runs fail instead of finishing.",
    ),
)


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    #: "int", "float", "model" (one slug) or "models" (a set of slugs).
    kind: str
    default: Any
    group: str
    help: str
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    step: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "group": self.group,
            "help": self.help,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "unit": self.unit,
            "step": self.step,
        }


SETTINGS: tuple[Setting, ...] = (
    Setting(
        key="agent_model",
        label="Default model",
        kind="model",
        default="gpt-6-astra",
        group="agent",
        help="The model a new conversation starts with. Existing chats keep theirs.",
    ),
    Setting(
        key="judge_model",
        label="Safety check model",
        kind="model",
        default="gpt-5.6-luna",
        group="agent",
        help="Reviews shell commands in auto mode. A small model is the point here.",
    ),
    Setting(
        key="compaction_model",
        label="Compactor model",
        kind="model",
        default="gpt-5.6-luna",
        group="agent",
        help="Summarizes the complete visible conversation when Compact at is reached or Compact is pressed.",
    ),
    Setting(
        key="auto_router_model",
        label="Routing model",
        kind="model",
        default="gpt-5.6-luna",
        group="auto",
        help="Decides which model and effort a run gets. Called once per message, "
        "so it should be the cheapest one that can read a task.",
    ),
    Setting(
        key="auto_fallback_model",
        label="Fallback model",
        kind="model",
        default="gpt-5.6-terra",
        group="auto",
        help="Used when routing fails. Pick a balanced model, not the most "
        "capable one -- a failed routing decision should not become expensive.",
    ),
    Setting(
        key="auto_models",
        label="Models Auto may choose",
        kind="models",
        default=("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"),
        group="auto",
        help="Removing the largest model here is the simplest way to cap what a "
        "run can cost.",
    ),
    Setting(
        key="shell_timeout_seconds",
        label="Shell timeout",
        kind="int",
        default=120,
        group="tools",
        help="How long one command may run before it is killed. A build or a "
        "test suite may need considerably more than the default.",
        minimum=5,
        maximum=3600,
        unit="s",
    ),
    Setting(
        key="max_tool_output_chars",
        label="Tool output limit",
        kind="int",
        default=30_000,
        group="tools",
        help="Longer output is truncated. Every character of it is sent to the "
        "model on this and every later turn.",
        minimum=2_000,
        maximum=200_000,
        unit="chars",
        step=1_000,
    ),
    Setting(
        key="lsp_timeout_seconds",
        label="Language server timeout",
        kind="float",
        default=15.0,
        group="integrations",
        help="How long to wait for one request to a language server.",
        minimum=1,
        maximum=120,
        unit="s",
        step=0.5,
    ),
    Setting(
        key="lsp_diagnostic_wait_seconds",
        label="Diagnostics settle time",
        kind="float",
        default=1.0,
        group="integrations",
        help="How long to let a language server finish analysing before reading "
        "its diagnostics. Too short and a large file reports nothing.",
        minimum=0.1,
        maximum=30,
        unit="s",
        step=0.1,
    ),
    Setting(
        key="mcp_timeout_seconds",
        label="MCP server timeout",
        kind="float",
        default=20.0,
        group="integrations",
        help="How long to wait for one request to an MCP server.",
        minimum=1,
        maximum=300,
        unit="s",
        step=0.5,
    ),
    Setting(
        key="browser_timeout_seconds",
        label="Browser tool timeout",
        kind="float",
        default=30.0,
        group="integrations",
        help="How long to wait for the hidden browser to answer one action "
        "(navigate, click, a screenshot). A slow page may need more.",
        minimum=5,
        maximum=180,
        unit="s",
        step=1,
    ),
    Setting(
        key="max_memory_chars",
        label="Memory budget",
        kind="int",
        default=2_500,
        group="context",
        help="Per memory file -- global and project are counted separately.",
        minimum=200,
        maximum=20_000,
        unit="chars",
        step=100,
    ),
    Setting(
        key="max_instruction_chars",
        label="Repository instructions budget",
        kind="int",
        default=12_000,
        group="context",
        help="Total across all AGENTS.md and CLAUDE.md files that apply.",
        minimum=500,
        maximum=60_000,
        unit="chars",
        step=500,
    ),
    Setting(
        key="context_compact_fraction",
        label="Compact at",
        kind="float",
        default=0.80,
        group="danger",
        help="How full the context window may get before older history is "
        "replaced by a summary. Later means fewer summaries but a bigger, "
        "slower and more expensive request every turn.",
        minimum=0.30,
        maximum=0.95,
        unit="of the window",
        step=0.01,
    ),
    Setting(
        key="context_stop_fraction",
        label="Stop at",
        kind="float",
        default=0.95,
        group="danger",
        help="The hard stop if compaction cannot free enough room. 0.95 is what "
        "the official Codex CLI uses. Above it, the API rejects the request "
        "instead, which ends the run with an error rather than a message.",
        minimum=0.50,
        maximum=0.99,
        unit="of the window",
        step=0.01,
    ),
)

BY_KEY: dict[str, Setting] = {setting.key: setting for setting in SETTINGS}

#: Compaction has to happen before the stop, with room to actually do it. The
#: two are separately valid but nonsensical together if they cross, so the pair
#: is checked after both have been coerced.
MIN_FRACTION_GAP = 0.02


def defaults() -> dict[str, Any]:
    return {
        setting.key: list(setting.default)
        if setting.kind == "models"
        else setting.default
        for setting in SETTINGS
    }


def schema(models: Iterable[str] = ()) -> dict[str, Any]:
    """The form description handed to the UI."""
    return {
        "groups": [
            {"key": key, "label": label, "help": help_text}
            for key, label, help_text in GROUPS
        ],
        "settings": [setting.as_dict() for setting in SETTINGS],
        "models": list(models),
    }


def _clamp(value: float, setting: Setting) -> float:
    if setting.minimum is not None:
        value = max(setting.minimum, value)
    if setting.maximum is not None:
        value = min(setting.maximum, value)
    return value


def _coerce_one(setting: Setting, value: Any, known_models: set[str]) -> Any:
    if setting.kind == "int":
        # Reject bools explicitly: `isinstance(True, int)` is True in Python.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{setting.label} must be a number.")
        return int(_clamp(float(value), setting))
    if setting.kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{setting.label} must be a number.")
        return round(_clamp(float(value), setting), 4)
    if setting.kind == "model":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{setting.label} must be a model name.")
        slug = value.strip()
        # An unknown slug is kept rather than rejected: the catalog is fetched
        # over the network, and a lookup failure must not wipe a valid choice.
        return slug
    if setting.kind == "models":
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError(f"{setting.label} needs at least one model.")
        slugs = [str(item).strip() for item in value if str(item).strip()]
        if not slugs:
            raise ValueError(f"{setting.label} needs at least one model.")
        unknown = [slug for slug in slugs if known_models and slug not in known_models]
        if unknown and len(unknown) == len(slugs):
            raise ValueError(f"{setting.label}: none of those models exist.")
        return [slug for slug in slugs if slug not in unknown] or slugs
    raise ValueError(f"Unknown setting kind: {setting.kind}")


def coerce(raw: Mapping[str, Any], models: Iterable[str] = ()) -> dict[str, Any]:
    """Validate and clamp a partial settings payload onto the defaults.

    Out-of-range numbers are clamped rather than refused -- the bound is the
    real answer, and a form that silently corrects itself is friendlier than
    one that throws away everything else the user changed. Wrong *types* do
    raise, because there is no sensible correction for them.
    """
    known = {str(model) for model in models}
    values = defaults()
    for key, value in raw.items():
        setting = BY_KEY.get(key)
        if setting is None:
            continue  # An older or newer build's key; ignore rather than fail.
        values[key] = _coerce_one(setting, value, known)

    compact = float(values["context_compact_fraction"])
    stop = float(values["context_stop_fraction"])
    if compact > stop - MIN_FRACTION_GAP:
        # Compacting at or past the stop threshold means the run aborts before
        # compaction ever gets a chance, so the pair is pulled back apart.
        values["context_compact_fraction"] = round(
            max(
                BY_KEY["context_compact_fraction"].minimum or 0.0,
                stop - MIN_FRACTION_GAP,
            ),
            4,
        )
    return values


# -- process-wide limits ------------------------------------------------------
#
# The LSP and MCP clients are lazily created process-wide singletons that
# outlive any one conversation, and they are reached from deep inside tool
# calls that have no route to the app config. Rather than thread a config
# object through every one of those call sites, the two timeouts live here --
# one owner of mutable state instead of a copy in each integration.

_lock = threading.Lock()
_active: dict[str, Any] = defaults()


def apply(values: Mapping[str, Any]) -> None:
    with _lock:
        _active.update(values)


def active(key: str) -> Any:
    with _lock:
        return _active.get(key, BY_KEY[key].default)

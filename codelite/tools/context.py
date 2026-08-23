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

"""What a tool gets handed when it runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..permission.manager import PermissionManager


class PathOutsideWorkspace(Exception):
    """A tool tried to touch a path outside the conversation's workspace."""


@dataclass
class ToolContext:
    """Per-run context: where we are, what we may do, and what we were asked.

    ``task_prompt`` is the user message that started the current run. It is
    carried here because the judge model in ``auto`` mode is shown both the
    shell command *and* the task it is supposedly serving -- a command that
    looks fine in isolation may be obviously unrelated to the task.
    """

    workspace: Path
    permissions: PermissionManager
    task_prompt: str = ""
    shell_timeout_seconds: int = 120

    def resolve(self, raw_path: str) -> Path:
        """Resolve a model-supplied path, refusing anything outside the workspace.

        The model can and does produce paths like ``../../etc/passwd``; this
        is the single choke point that stops them, so every filesystem tool
        must route through it rather than using ``Path`` directly.
        """
        if not raw_path or not raw_path.strip():
            raise PathOutsideWorkspace("A path is required.")

        workspace = self.workspace.resolve()
        candidate = Path(raw_path).expanduser()
        resolved = (
            candidate if candidate.is_absolute() else workspace / candidate
        ).resolve()

        if resolved != workspace and workspace not in resolved.parents:
            raise PathOutsideWorkspace(
                f"`{raw_path}` is outside the workspace ({workspace}). "
                "Only paths inside the workspace can be accessed."
            )
        return resolved

    def relative(self, path: Path) -> str:
        """Render a path workspace-relative for display back to the model."""
        try:
            return str(path.relative_to(self.workspace.resolve()))
        except ValueError:
            return str(path)

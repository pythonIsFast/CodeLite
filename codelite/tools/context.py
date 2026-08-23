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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..permission.manager import PermissionManager
from ..provider.session import Session
from ..questions import QuestionManager


class PathOutsideWorkspace(Exception):
    """A tool tried to touch a path outside the conversation's workspace."""


@dataclass
class ToolContext:
    """Per-run context: where we are, what we may do, and what we were asked.

    ``task_prompt`` is the user message that started the current run. It is
    carried here because the judge model in ``auto`` mode is shown both the
    shell command *and* the task it is supposedly serving -- a command that
    looks fine in isolation may be obviously unrelated to the task.

    ``cwd`` is mutable: the shell tool writes back where the command left the
    working directory, so ``cd`` carries between calls the way it would in a
    real terminal.
    """

    workspace: Path
    permissions: PermissionManager
    session: Session
    questions: QuestionManager | None = None
    data_dir: Path = Path(".")
    #: The conversation's selected Codex model, used by tools that make a model call.
    model: str = ""
    task_prompt: str = ""
    shell_timeout_seconds: int = 120
    cwd: Path | None = None
    todos: list[dict[str, str]] = field(default_factory=list)
    #: One-turn multimodal inputs queued by tools such as ``view_image``.
    pending_model_inputs: list[dict[str, Any]] = field(default_factory=list)
    publish: Callable[[str, dict[str, Any]], None] | None = None

    def __post_init__(self) -> None:
        if self.cwd is None:
            self.cwd = self.workspace

    def emit(self, event: str, data: dict[str, Any]) -> None:
        """Send a UI event, if this run has somewhere to send it."""
        if self.publish is not None:
            self.publish(event, data)

    def add_model_image(self, image_url: str, label: str) -> None:
        """Give the next model turn a local image without persisting its bytes.

        The agent loop consumes these entries for exactly one request. Keeping
        a data URL in SQLite or resending it on every later step would be both
        wasteful and surprising for a user who only asked the agent to look.
        """
        self.pending_model_inputs.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"The image requested with view_image is attached: {label}",
                    },
                    {"type": "input_image", "image_url": image_url},
                ],
            }
        )

    def take_model_inputs(self) -> list[dict[str, Any]]:
        """Return and clear the one-turn inputs tools queued for the model."""
        inputs = self.pending_model_inputs
        self.pending_model_inputs = []
        return inputs

    def set_cwd(self, path: Path) -> bool:
        """Adopt a new working directory, ignoring anything outside the workspace."""
        try:
            resolved = path.resolve()
        except OSError:
            return False
        workspace = self.workspace.resolve()
        if resolved != workspace and workspace not in resolved.parents:
            return False
        if not resolved.is_dir():
            return False
        self.cwd = resolved
        return True

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

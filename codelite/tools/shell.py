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

"""The shell tool -- the most consequential thing the agent can do.

Every invocation goes through
:meth:`~codelite.permission.manager.PermissionManager.require_shell` first,
which is where the four permission modes (and the judge model in ``auto``
mode) actually take effect. Commands are one-shot: there is no persistent
shell session, so state like ``cd`` does not carry between calls.
"""

from __future__ import annotations

import subprocess
from typing import Any

from .base import Tool, ToolError, object_schema
from .context import ToolContext

MAX_OUTPUT_CHARS = 20_000


def _clip(text: str, label: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    kept = text[:MAX_OUTPUT_CHARS]
    return f"{kept}\n... [{label} truncated, {len(text) - MAX_OUTPUT_CHARS} more characters]"


def _run_shell(args: dict[str, Any], ctx: ToolContext) -> str:
    command = args.get("command")
    if not command or not str(command).strip():
        raise ToolError("`command` is required.")
    command = str(command)

    timeout = int(args.get("timeout") or ctx.shell_timeout_seconds)
    timeout = max(1, min(timeout, 600))

    # The gate. Raises PermissionDenied, which the loop reports to the model.
    ctx.permissions.require_shell(command, ctx.task_prompt)

    try:
        completed = subprocess.run(  # noqa: S602 - running shell commands is this tool's purpose
            command,
            shell=True,
            cwd=str(ctx.workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"`{command}` timed out after {timeout}s.") from None
    except OSError as error:
        raise ToolError(f"`{command}` could not be started: {error}") from error

    sections: list[str] = [f"exit code: {completed.returncode}"]
    if completed.stdout.strip():
        sections.append(f"stdout:\n{_clip(completed.stdout, 'stdout')}")
    if completed.stderr.strip():
        sections.append(f"stderr:\n{_clip(completed.stderr, 'stderr')}")
    if len(sections) == 1:
        sections.append("(no output)")
    return "\n\n".join(sections)


SHELL = Tool(
    name="shell",
    description=(
        "Run a shell command in the workspace directory and return its exit "
        "code, stdout and stderr. Each call is independent -- `cd` does not "
        "persist between calls, so chain with '&&' if you need it. May require "
        "user approval depending on the active permission mode."
    ),
    parameters=object_schema(
        {
            "command": {"type": "string", "description": "The command line to run."},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (max 600).",
            },
        },
        required=["command"],
    ),
    run=_run_shell,
)

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
mode) actually take effect.

Each call is still its own process, but the working directory carries over:
the command is wrapped so it reports where it finished, and that becomes the
starting directory for the next call. So ``cd build`` behaves the way it does
in a terminal instead of silently evaporating.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .base import Tool, ToolError, object_schema
from .context import ToolContext

#: Fallback only. The live limit comes from the conversation's context, which
#: carries the configured value -- truncating here at a second, lower number
#: made that setting dead for the tool that produces the most output.
DEFAULT_MAX_OUTPUT_CHARS = 20_000

# Unlikely to collide with real output, and stripped before the model sees it.
_CWD_MARKER = "__codelite_cwd_5f2b7a__:"


def _clip(text: str, label: str, limit: int = DEFAULT_MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    kept = text[:limit]
    return f"{kept}\n... [{label} truncated, {len(text) - limit} more characters]"


def _wrap(command: str) -> str:
    """Append a trailer that reports the final directory, preserving the exit code.

    If the command exits outright (``exit 1``) the trailer never runs -- we
    simply keep the previous directory, which is the safe fallback.
    """
    return (
        f"{command}\n"
        "__codelite_rc=$?\n"
        f"printf '\\n{_CWD_MARKER}%s' \"$(pwd)\"\n"
        "exit $__codelite_rc\n"
    )


def _split_cwd(stdout: str) -> tuple[str, str | None]:
    """Peel the reported directory off stdout. Returns (clean stdout, cwd or None)."""
    index = stdout.rfind(_CWD_MARKER)
    if index == -1:
        return stdout, None
    reported = stdout[index + len(_CWD_MARKER):].strip()
    # The trailer is printed after a newline we added, so drop that too.
    clean = stdout[:index]
    if clean.endswith("\n"):
        clean = clean[:-1]
    return clean, reported or None


def _run_shell(args: dict[str, Any], ctx: ToolContext) -> str:
    command = args.get("command")
    if not command or not str(command).strip():
        raise ToolError("`command` is required.")
    command = str(command)

    timeout = int(args.get("timeout") or ctx.shell_timeout_seconds)
    timeout = max(1, min(timeout, 600))

    started_in = ctx.cwd or ctx.workspace
    if not started_in.is_dir():  # the directory was removed since the last call
        started_in = ctx.workspace
        ctx.cwd = started_in

    # The gate. A session allowance is bound to this exact command and the
    # directory it starts in, rather than granting every future shell action.
    ctx.permissions.require_shell(command, ctx.task_prompt, ctx.relative(started_in) or ".")

    try:
        completed = subprocess.run(  # noqa: S602 - running shell commands is this tool's purpose
            _wrap(command),
            shell=True,
            cwd=str(started_in),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"`{command}` timed out after {timeout}s.") from None
    except OSError as error:
        raise ToolError(f"`{command}` could not be started: {error}") from error

    stdout, reported_cwd = _split_cwd(completed.stdout)
    moved_to = None
    refused_cwd = None
    if reported_cwd:
        previous = ctx.cwd
        if not ctx.set_cwd(Path(reported_cwd)):
            # The command cd'd out of the workspace. It may well have exited 0,
            # so say so explicitly rather than letting the model assume it stuck.
            refused_cwd = reported_cwd
        elif ctx.cwd != previous:
            moved_to = ctx.relative(ctx.cwd) or "."

    sections: list[str] = [f"exit code: {completed.returncode}"]
    if started_in != ctx.workspace:
        sections[0] += f"  (ran in ./{ctx.relative(started_in)})"
    if stdout.strip():
        sections.append(f"stdout:\n{_clip(stdout, 'stdout', ctx.max_tool_output_chars)}")
    if completed.stderr.strip():
        sections.append(f"stderr:\n{_clip(completed.stderr, 'stderr', ctx.max_tool_output_chars)}")
    if len(sections) == 1:
        sections.append("(no output)")
    if moved_to:
        sections.append(f"[working directory is now ./{moved_to} for later shell calls]")
    elif refused_cwd:
        sections.append(
            f"[the command ended up in {refused_cwd}, which is outside the "
            f"workspace -- the working directory stayed at "
            f"./{ctx.relative(started_in) or '.'}]"
        )
    return "\n\n".join(sections)


SHELL = Tool(
    name="shell",
    description=(
        "Run a shell command and return its exit code, stdout and stderr. "
        "Starts in the workspace root; `cd` persists to later calls, so you "
        "can move around like in a terminal. May require user approval "
        "depending on the active permission mode."
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

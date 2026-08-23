# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Human-editable global and project memory shared across conversations."""

from __future__ import annotations

import difflib
from typing import Any

from ..project.context import GLOBAL_MEMORY_PATH, MAX_MEMORY_CHARS, MEMORY_PATH
from .base import Tool, ToolError, object_schema
from .context import ToolContext


def _diff(before: str, after: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
            n=3,
        )
    )


def _run_project_memory(args: dict[str, Any], ctx: ToolContext) -> str:
    action = args.get("action")
    scope = str(args.get("scope") or "project")
    if scope == "global":
        data_root = ctx.data_dir.resolve()
        path = (data_root / GLOBAL_MEMORY_PATH).resolve()
        if data_root not in path.parents:
            raise ToolError("Global memory path leaves the Code Lite data directory.")
        display_name = "global memory"
        label = str(path)
    elif scope == "project":
        path = ctx.resolve(str(MEMORY_PATH))
        display_name = "project memory"
        label = ctx.relative(path)
    else:
        raise ToolError("`scope` must be global or project.")
    try:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
    except (OSError, UnicodeDecodeError) as error:
        raise ToolError(f"Could not read {display_name}: {error}") from error

    if action == "read":
        return current.strip() or f"{display_name.title()} is empty."
    content = args.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ToolError("`content` must be non-empty for append or replace.")
    if action == "append":
        addition = content.strip()
        if addition in current:
            return f"That {display_name} entry is already present."
        updated = current.rstrip() + ("\n" if current.strip() else "") + addition + "\n"
    elif action == "replace":
        updated = content.strip() + "\n"
    else:
        raise ToolError("`action` must be read, append, or replace.")
    if len(updated) > MAX_MEMORY_CHARS:
        raise ToolError(
            f"{display_name.title()} is limited to {MAX_MEMORY_CHARS:,} characters. "
            "Keep only stable, high-value facts."
        )
    ctx.permissions.require_write(label, _diff(current, updated, label))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return f"Updated {label} ({len(updated):,} characters)."


PROJECT_MEMORY = Tool(
    name="project_memory",
    description=(
        "Read or update concise, durable memory. Use global scope for user preferences "
        "and conventions that apply to every project; use project scope for commands "
        "and architecture specific to this workspace. Never store temporary progress or secrets."
    ),
    parameters=object_schema(
        {
            "action": {"type": "string", "enum": ["read", "append", "replace"]},
            "scope": {
                "type": "string",
                "enum": ["global", "project"],
                "description": "Memory scope (default: project).",
            },
            "content": {"type": "string", "description": "Memory text for append/replace."},
        },
        required=["action"],
    ),
    run=_run_project_memory,
)

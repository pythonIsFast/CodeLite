# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Human-editable project memory shared across conversations."""

from __future__ import annotations

import difflib
from typing import Any

from ..project.context import MAX_MEMORY_CHARS, MEMORY_PATH
from .base import Tool, ToolError, object_schema
from .context import ToolContext


def _diff(before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{MEMORY_PATH}",
            tofile=f"b/{MEMORY_PATH}",
            n=3,
        )
    )


def _run_project_memory(args: dict[str, Any], ctx: ToolContext) -> str:
    action = args.get("action")
    path = ctx.resolve(str(MEMORY_PATH))
    try:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
    except (OSError, UnicodeDecodeError) as error:
        raise ToolError(f"Could not read {MEMORY_PATH}: {error}") from error

    if action == "read":
        return current.strip() or "Project memory is empty."
    content = args.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ToolError("`content` must be non-empty for append or replace.")
    if action == "append":
        addition = content.strip()
        if addition in current:
            return "That project-memory entry is already present."
        updated = current.rstrip() + ("\n" if current.strip() else "") + addition + "\n"
    elif action == "replace":
        updated = content.strip() + "\n"
    else:
        raise ToolError("`action` must be read, append, or replace.")
    if len(updated) > MAX_MEMORY_CHARS:
        raise ToolError(
            f"Project memory is limited to {MAX_MEMORY_CHARS:,} characters. "
            "Keep only stable, high-value facts."
        )
    label = ctx.relative(path)
    ctx.permissions.require_write(label, _diff(current, updated))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return f"Updated {label} ({len(updated):,} characters)."


PROJECT_MEMORY = Tool(
    name="project_memory",
    description=(
        "Read or update concise, durable facts shared by every chat in this project. "
        "Store commands, conventions, architecture decisions, and user preferences; "
        "never store temporary task progress or secrets."
    ),
    parameters=object_schema(
        {
            "action": {"type": "string", "enum": ["read", "append", "replace"]},
            "content": {"type": "string", "description": "Memory text for append/replace."},
        },
        required=["action"],
    ),
    run=_run_project_memory,
)

# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tool that asks the chat UI to display a workspace file."""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolError, object_schema
from .context import ToolContext


def _run_showcase_file(args: dict[str, Any], ctx: ToolContext) -> str:
    path = ctx.resolve(args.get("path", ""))
    if not path.exists():
        raise ToolError(f"{ctx.relative(path)} does not exist.")
    if not path.is_file():
        raise ToolError(f"{ctx.relative(path)} is not a file.")
    return f"Showcased {ctx.relative(path)} in the chat."


SHOWCASE_FILE = Tool(
    name="showcase_file",
    description=(
        "Show an existing workspace file directly in the chat. Images, video, and audio "
        "get native previews; other files get an open link."
    ),
    parameters=object_schema(
        {"path": {"type": "string", "description": "Workspace-relative file path to show."}},
        required=["path"],
    ),
    run=_run_showcase_file,
)

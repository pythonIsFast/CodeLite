# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Vision tool that lets the agent inspect an image in the workspace."""

from __future__ import annotations

import base64
import mimetypes
from typing import Any

from .base import Tool, ToolError, object_schema
from .context import ToolContext

MAX_IMAGE_BYTES = 20 * 1024 * 1024
SUPPORTED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def _run_view_image(args: dict[str, Any], ctx: ToolContext) -> str:
    path = ctx.resolve_read(args.get("path", ""))
    if not path.is_file():
        raise ToolError(f"{ctx.relative(path)} is not an image file.")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ToolError(
            f"{ctx.relative(path)} exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MB image limit."
        )
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type not in SUPPORTED_IMAGE_TYPES:
        raise ToolError("`view_image` supports PNG, JPEG, GIF, and WebP files.")

    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    ctx.add_model_image(f"data:{mime_type};base64,{encoded}", ctx.relative(path))
    return "Image supplied directly to the model."


VIEW_IMAGE = Tool(
    name="view_image",
    description="Give the agent the actual pixels of a workspace image for its next model turn.",
    parameters=object_schema(
        {"path": {"type": "string", "description": "Workspace-relative PNG, JPEG, GIF, or WebP path."}},
        required=["path"],
    ),
    run=_run_view_image,
)

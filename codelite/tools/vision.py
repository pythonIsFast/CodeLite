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

from ..provider.chat import parse_responses_output
from .base import Tool, ToolError, object_schema
from .context import ToolContext

MAX_IMAGE_BYTES = 20 * 1024 * 1024
SUPPORTED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def _run_view_image(args: dict[str, Any], ctx: ToolContext) -> str:
    path = ctx.resolve(args.get("path", ""))
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
    response = ctx.session.send_responses(
        {
            # Match the conversation's selected Codex model. The provider's
            # `chat_model` is only a generic local-proxy default (gpt-5.2),
            # which ChatGPT OAuth's Codex endpoint rejects.
            "model": ctx.model or ctx.session.config.chat_model,
            "instructions": (
                "You are the visual-inspection step of a coding agent. Describe the image "
                "accurately and concisely, including visible text, layout, colours, and "
                "anything relevant to the requested inspection."
            ),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Inspect this image for the agent."},
                        {
                            "type": "input_image",
                            "image_url": f"data:{mime_type};base64,{encoded}",
                        },
                    ],
                }
            ],
        },
        stream=False,
    )
    if not isinstance(response, dict):  # Defensive: non-streaming calls return one object.
        raise ToolError("Image inspection returned an unexpected streaming response.")
    description = parse_responses_output(response).text.strip()
    if not description:
        raise ToolError("Image inspection returned no description.")
    return f"Visual inspection of {ctx.relative(path)}:\n{description}"


VIEW_IMAGE = Tool(
    name="view_image",
    description="Inspect a workspace image and return an image-aware description to the agent.",
    parameters=object_schema(
        {"path": {"type": "string", "description": "Workspace-relative PNG, JPEG, GIF, or WebP path."}},
        required=["path"],
    ),
    run=_run_view_image,
)

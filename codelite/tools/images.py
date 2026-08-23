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

"""Image-generation tool backed by the current ChatGPT OAuth session."""

from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .base import Tool, ToolError, object_schema
from .context import ToolContext


def _error_message(body: bytes, status: int) -> str:
    """Return an actionable message from an OpenAI-shaped error response."""
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"Image generation failed with HTTP {status}."
    error = decoded.get("error") if isinstance(decoded, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return f"Image generation failed: {error['message']}"
    return f"Image generation failed with HTTP {status}."


def _image_bytes(body: bytes) -> bytes:
    """Extract the first base64 image from an Images API response."""
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ToolError("Image generation returned an invalid JSON response.") from error

    data = decoded.get("data") if isinstance(decoded, dict) else None
    first = data[0] if isinstance(data, list) and data else None
    encoded = first.get("b64_json") if isinstance(first, dict) else None
    if not isinstance(encoded, str) or not encoded:
        raise ToolError("Image generation returned no image data.")
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ToolError("Image generation returned invalid image data.") from error


def _write_bytes_atomically(path: Path, content: bytes) -> None:
    """Write a completed image without exposing a partial file to the workspace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        temporary.replace(path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ToolError(f"Could not save generated image to {path}: {error}") from error


def _run_generate_image(args: dict[str, Any], ctx: ToolContext) -> str:
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ToolError("`prompt` is required and must not be empty.")

    path = ctx.resolve(args.get("path", ""))
    if path.exists() and path.is_dir():
        raise ToolError(f"{ctx.relative(path)} is a directory, not an image file.")

    # Ask before using the account's image-generation allowance or changing disk.
    label = ctx.relative(path)
    ctx.permissions.require_write(label)

    request = json.dumps(
        {"prompt": prompt.strip(), "n": 1, "response_format": "b64_json"}
    ).encode("utf-8")
    result = ctx.session.generate_image(request)
    if not 200 <= result.status < 300:
        raise ToolError(_error_message(result.body, result.status))

    content = _image_bytes(result.body)
    if not content:
        raise ToolError("Image generation returned an empty image.")
    _write_bytes_atomically(path, content)
    return f"Generated image saved to {label} ({len(content):,} bytes)."


GENERATE_IMAGE = Tool(
    name="generate_image",
    description=(
        "Generate a new image from a prompt using the signed-in ChatGPT account and "
        "save it to a workspace-relative path."
    ),
    parameters=object_schema(
        {
            "prompt": {
                "type": "string",
                "description": "Detailed description of the image to generate.",
            },
            "path": {
                "type": "string",
                "description": "Workspace-relative output path, for example assets/hero.png.",
            },
        },
        required=["prompt", "path"],
    ),
    run=_run_generate_image,
)

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
#
# Ported from openai-oauth (https://github.com/EvanZhouDev/openai-oauth),
# packages/core/src/images.ts, Apache-2.0.

"""Normalizes `/v1/images/generations` and `/v1/images/edits` requests.

ChatGPT OAuth's image backend is a stricter subset of the public OpenAI
Images API: only `b64_json` responses, no streaming, no masks, and a fixed
allow-list of fields. This module validates an incoming OpenAI-shaped
request and rewrites it into the JSON body Codex expects -- for edits, that
means turning uploaded files into `data:` URLs, since Codex takes
`images: [{"image_url": ...}]` rather than multipart uploads.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from .config import DEFAULT_IMAGE_MODEL, MAX_REFERENCE_IMAGE_BYTES, MAX_REFERENCE_IMAGES
from .multipart import MultipartForm, parse_multipart

_UNSUPPORTED_OPTIONS = (
    "input_fidelity",
    "moderation",
    "output_compression",
    "output_format",
    "partial_images",
)


class ImageRequestError(ValueError):
    """Raised for a client request that ChatGPT OAuth's image backend can't serve.

    Carries an OpenAI-shaped `error` body so the HTTP layer can return it
    as-is with a 400 status, matching the reference implementation.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def to_response_body(self) -> dict[str, Any]:
        return {"error": {"message": self.message, "type": "invalid_request_error"}}


@dataclass
class PreparedImageRequest:
    body: bytes


def _file_to_data_url(content_type: str | None, data: bytes) -> str:
    mime = content_type or "image/png"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def normalize_generation(body: dict[str, Any]) -> PreparedImageRequest:
    if body.get("stream") is True:
        raise ImageRequestError("Streaming image generation is not supported by ChatGPT OAuth.")

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise ImageRequestError("`prompt` must be a non-empty string.")

    for key in _UNSUPPORTED_OPTIONS:
        if body.get(key) is not None:
            raise ImageRequestError(
                f"`{key}` is not supported by ChatGPT OAuth image generation."
            )

    response_format = body.get("response_format")
    if response_format is not None and response_format != "b64_json":
        raise ImageRequestError("ChatGPT OAuth image generation only returns `b64_json`.")

    model = body.get("model")
    normalized: dict[str, Any] = {
        "model": model if isinstance(model, str) and model else DEFAULT_IMAGE_MODEL,
        "prompt": prompt,
    }
    for key in ("background", "n", "quality", "size"):
        if body.get(key) is not None:
            normalized[key] = body[key]

    return PreparedImageRequest(body=json.dumps(normalized).encode("utf-8"))


def normalize_edit(form: MultipartForm) -> PreparedImageRequest:
    if form.get_text("stream") == "true":
        raise ImageRequestError("Streaming image editing is not supported by ChatGPT OAuth.")
    if form.has("mask"):
        raise ImageRequestError("Image masks are not supported by ChatGPT OAuth.")

    prompt = form.get_text("prompt")
    if not prompt:
        raise ImageRequestError("`prompt` must be a non-empty string.")

    files = form.get_all_files("image", "image[]")
    if not files:
        raise ImageRequestError("At least one `image` is required.")
    if len(files) > MAX_REFERENCE_IMAGES:
        raise ImageRequestError("ChatGPT OAuth supports at most 5 reference images.")

    for file in files:
        if len(file.data) > MAX_REFERENCE_IMAGE_BYTES:
            raise ImageRequestError(
                f"Reference image `{file.filename}` exceeds the 50 MB limit."
            )

    for key in _UNSUPPORTED_OPTIONS:
        if form.has(key):
            raise ImageRequestError(f"`{key}` is not supported by ChatGPT OAuth image editing.")

    response_format = form.get_text("response_format")
    if response_format is not None and response_format != "b64_json":
        raise ImageRequestError("ChatGPT OAuth image editing only returns `b64_json`.")

    model = form.get_text("model")
    normalized: dict[str, Any] = {
        "images": [
            {"image_url": _file_to_data_url(file.content_type, file.data)} for file in files
        ],
        "model": model or DEFAULT_IMAGE_MODEL,
        "prompt": prompt,
    }

    n_text = form.get_text("n")
    if n_text:
        try:
            normalized["n"] = int(n_text)
        except ValueError:
            try:
                normalized["n"] = float(n_text)
            except ValueError:
                pass

    for key in ("background", "quality", "size"):
        value = form.get_text(key)
        if value:
            normalized[key] = value

    return PreparedImageRequest(body=json.dumps(normalized).encode("utf-8"))


def prepare_image_generation_request(body_bytes: bytes) -> PreparedImageRequest:
    try:
        parsed = json.loads(body_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise ImageRequestError("Image generation request is invalid JSON.") from error
    if not isinstance(parsed, dict):
        raise ImageRequestError("Image generation request body must be a JSON object.")
    return normalize_generation(parsed)


def prepare_image_edit_request(content_type: str, body_bytes: bytes) -> PreparedImageRequest:
    if "multipart/form-data" not in content_type:
        raise ImageRequestError("Image editing requires a multipart/form-data request body.")
    try:
        parts = parse_multipart(content_type, body_bytes)
    except ValueError as error:
        raise ImageRequestError("Image editing request contains invalid form data.") from error
    return normalize_edit(MultipartForm(parts))

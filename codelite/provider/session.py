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

"""The provider layer's single in-process entry point.

A :class:`Session` is the "send a chat request, get an answer" /
"generate an image, get bytes" interface described in the project brief: a
future agent loop can build one directly and call its methods, without
going through the local HTTP proxy at all. :mod:`codelite.provider.server`
is just another `Session` caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from .auth import EffectiveAuth, load_auth_tokens
from .chat import (
    ChatResult,
    build_chat_completion_response,
    chat_request_to_responses_body,
    parse_responses_output,
    stream_responses_as_chat_completion_chunks,
)
from .config import ProviderConfig
from .images import (
    ImageRequestError,
    prepare_image_edit_request,
    prepare_image_generation_request,
)
from .transport import CodexTransport, UpstreamResponse


@dataclass
class ImageResult:
    status: int
    body: bytes


class Session:
    """A configured connection to Codex, ready to serve chat/image/model requests."""

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self.config = config or ProviderConfig()
        self._transport = CodexTransport(self.config, self._resolve_auth)

    def _resolve_auth(self) -> EffectiveAuth:
        return load_auth_tokens(self.config)

    # -- models --------------------------------------------------------------

    def list_models(self) -> list[str]:
        return self._transport.list_model_ids()

    # -- responses -------------------------------------------------------------

    def send_responses(
        self, body: dict[str, Any], *, stream: bool = False
    ) -> dict[str, Any] | Iterator[bytes]:
        """Send a raw Responses-API-shaped request straight through to Codex."""
        return self._transport.send_responses_request(body, stream=stream)

    # -- chat completions --------------------------------------------------------

    def send_chat(self, request: dict[str, Any]) -> dict[str, Any]:
        """Non-streaming Chat Completions call, translated through `/responses`."""
        responses_body = chat_request_to_responses_body(request)
        response = self._transport.send_responses_request(responses_body, stream=False)
        assert isinstance(response, dict)
        result = parse_responses_output(response)
        return build_chat_completion_response(request.get("model", ""), result)

    def stream_chat(self, request: dict[str, Any]) -> Iterator[bytes]:
        """Streaming Chat Completions call: yields `data: ...\\n\\n` SSE chunks."""
        responses_body = chat_request_to_responses_body(request)
        raw_chunks = self._transport.send_responses_request(responses_body, stream=True)
        assert not isinstance(raw_chunks, dict)
        return stream_responses_as_chat_completion_chunks(request.get("model", ""), raw_chunks)

    # -- images --------------------------------------------------------------------

    def generate_image(self, request_body: bytes) -> ImageResult:
        prepared = prepare_image_generation_request(request_body)
        upstream = self._transport.send_image_request("/images/generations", prepared.body)
        return ImageResult(status=upstream.status, body=upstream.body)

    def edit_image(self, content_type: str, request_body: bytes) -> ImageResult:
        prepared = prepare_image_edit_request(content_type, request_body)
        upstream = self._transport.send_image_request("/images/edits", prepared.body)
        return ImageResult(status=upstream.status, body=upstream.body)


def load_session(config: ProviderConfig | None = None) -> Session:
    """Convenience factory mirroring `load_session()` style entry points."""
    return Session(config)

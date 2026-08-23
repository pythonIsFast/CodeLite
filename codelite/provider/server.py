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
# Route layout ported from openai-oauth (https://github.com/EvanZhouDev/openai-oauth),
# packages/openai-oauth/src/server.ts, Apache-2.0.

"""A local, OpenAI-compatible HTTP proxy in front of :class:`Session`.

This is the thinnest possible wrapper: every route just decodes an HTTP
request, calls the matching :class:`~codelite.provider.session.Session`
method, and encodes the result back. All the actual logic lives in
`session.py`/`chat.py`/`images.py`/`transport.py` so an in-process caller
(a future agent loop) never has to go through HTTP at all.

Built on `http.server` (stdlib only) rather than a framework. Not hardened
for exposure beyond localhost -- it binds to 127.0.0.1 by default, like the
reference implementation.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .auth import AuthError
from .config import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT, ProviderConfig
from .images import ImageRequestError
from .session import Session
from .transport import UpstreamError

logger = logging.getLogger("codelite.provider.server")

_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_SSE_HEADERS = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _error_body(message: str, error_type: str = "invalid_request_error") -> bytes:
    return json.dumps({"error": {"message": message, "type": error_type}}).encode("utf-8")


def _uses_server_replay_state(body: dict[str, Any]) -> bool:
    if isinstance(body.get("previous_response_id"), str):
        return True
    input_value = body.get("input")
    if not isinstance(input_value, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") == "item_reference" and isinstance(item.get("id"), str)
        for item in input_value
    )


def make_handler(session: Session) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CodeLiteProvider/0.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.info("%s - %s", self.address_string(), format % args)

        # -- helpers -----------------------------------------------------------

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", 0) or 0)
            return self.rfile.read(length) if length else b""

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", _JSON_CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_raw(self, body: bytes, status: int, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, message: str, status: int = 400, error_type: str = "invalid_request_error") -> None:
            self._send_raw(_error_body(message, error_type), status, _JSON_CONTENT_TYPE)

        def _send_sse(self, chunks) -> None:
            self.send_response(200)
            for key, value in _SSE_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                for chunk in chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _read_json_body(self) -> dict[str, Any] | None:
            raw = self._read_body()
            try:
                parsed = json.loads(raw or b"{}")
            except ValueError:
                self._send_error_json("Request body must be valid JSON.")
                return None
            if not isinstance(parsed, dict):
                self._send_error_json("Request body must be a JSON object.")
                return None
            return parsed

        # -- routing -------------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send_json({"ok": True})
                return
            if self.path == "/v1/models":
                try:
                    models = session.list_models()
                except (UpstreamError, RuntimeError, AuthError) as error:
                    self._send_error_json(str(error), 502, "upstream_error")
                    return
                self._send_json(
                    {
                        "object": "list",
                        "data": [
                            {"id": model_id, "object": "model", "created": 0, "owned_by": "codex-oauth"}
                            for model_id in models
                        ],
                    }
                )
                return
            self._send_error_json("Route not found.", 404, "not_found_error")

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._route_post()
            except AuthError as error:
                self._send_error_json(str(error), 401, "authentication_error")
            except (ImageRequestError,) as error:
                self._send_raw(
                    json.dumps(error.to_response_body()).encode("utf-8"), 400, _JSON_CONTENT_TYPE
                )
            except UpstreamError as error:
                self._send_error_json(str(error), 502, "upstream_error")
            except Exception as error:  # noqa: BLE001
                logger.exception("Unhandled error while serving %s", self.path)
                self._send_error_json(str(error), 500, "server_error")

        def _route_post(self) -> None:
            if self.path == "/v1/chat/completions":
                body = self._read_json_body()
                if body is None:
                    return
                if not isinstance(body.get("messages"), list):
                    self._send_error_json("`messages` must be an array.")
                    return
                if body.get("stream") is True:
                    self._send_sse(session.stream_chat(body))
                else:
                    self._send_json(session.send_chat(body))
                return

            if self.path == "/v1/responses":
                body = self._read_json_body()
                if body is None:
                    return
                if _uses_server_replay_state(body):
                    self._send_error_json(
                        "Stateless Codex responses endpoint does not support "
                        "`previous_response_id` or `item_reference`. Replay the full "
                        "conversation history in `input` on each request."
                    )
                    return
                wants_stream = body.get("stream") is True
                result = session.send_responses(body, stream=wants_stream)
                if wants_stream:
                    self._send_sse(result)
                else:
                    assert isinstance(result, dict)
                    self._send_json(result)
                return

            if self.path == "/v1/images/generations":
                raw_body = self._read_body()
                result = session.generate_image(raw_body)
                self._send_raw(result.body, result.status, _JSON_CONTENT_TYPE)
                return

            if self.path == "/v1/images/edits":
                content_type = self.headers.get("Content-Type", "")
                raw_body = self._read_body()
                result = session.edit_image(content_type, raw_body)
                self._send_raw(result.body, result.status, _JSON_CONTENT_TYPE)
                return

            self._send_error_json("Route not found.", 404, "not_found_error")

    return Handler


def run_server(
    config: ProviderConfig | None = None,
    *,
    host: str = DEFAULT_SERVER_HOST,
    port: int = DEFAULT_SERVER_PORT,
) -> None:
    """Start the proxy and block, serving requests until interrupted."""
    session = Session(config)
    handler_class = make_handler(session)
    httpd = ThreadingHTTPServer((host, port), handler_class)
    logger.info("Code Lite provider proxy listening on http://%s:%d/v1", host, port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()

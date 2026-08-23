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
# packages/core/src/sse.ts, Apache-2.0.

"""Server-Sent-Events parsing for the Codex `/responses` stream.

The Codex backend only ever answers `/responses` requests with a
`stream=true` body (see :mod:`codelite.provider.transport`). When a caller
asked for a non-streaming response, we still have to read that SSE stream
ourselves and fold it down into one final JSON object -- that's what
:func:`collect_completed_response_from_sse` does.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

_SSE_SEPARATOR = re.compile(r"\r?\n\r?\n")

_TERMINAL_EVENT_TYPES = {
    "error",
    "response.completed",
    "response.failed",
    "response.cancelled",
    "response.canceled",
    "response.incomplete",
}

_TERMINAL_RESPONSE_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "canceled",
    "incomplete",
}


@dataclass
class ServerSentEvent:
    event: str | None = None
    data: str | None = None


def _parse_event_block(block: str) -> ServerSentEvent:
    event_name: str | None = None
    data_lines: list[str] = []
    for line in re.split(r"\r?\n", block):
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    data = "\n".join(data_lines) if data_lines else None
    return ServerSentEvent(event=event_name, data=data)


def iterate_server_sent_events(chunks: Iterable[bytes]) -> Iterator[ServerSentEvent]:
    """Turn a stream of raw byte chunks (e.g. from an HTTP response body) into events."""
    buffer = ""
    for chunk in chunks:
        buffer += chunk.decode("utf-8", errors="ignore")
        blocks = _SSE_SEPARATOR.split(buffer)
        buffer = blocks.pop()
        for block in blocks:
            if block.strip():
                yield _parse_event_block(block)
    if buffer.strip():
        yield _parse_event_block(buffer)


def _is_terminal_payload(data: str) -> bool:
    if data == "[DONE]":
        return True
    try:
        parsed = json.loads(data)
    except ValueError:
        return False
    if not isinstance(parsed, dict):
        return False

    event_type = parsed.get("type")
    if isinstance(event_type, str) and event_type in _TERMINAL_EVENT_TYPES:
        return True

    response = parsed.get("response")
    if not isinstance(response, dict):
        return False

    response_type = response.get("type")
    status = response.get("status")
    return (isinstance(response_type, str) and response_type in _TERMINAL_EVENT_TYPES) or (
        isinstance(status, str) and status in _TERMINAL_RESPONSE_STATUSES
    )


def collect_completed_response_from_sse(chunks: Iterable[bytes]) -> dict[str, Any]:
    """Buffer an entire Responses-API SSE stream into one final response object.

    Tracks output items by id as they stream in (`response.output_item.*`)
    so that, even if the terminal `response.completed` event's `response.output`
    is (unusually) empty, we can still fall back to what we collected.
    """
    latest_response: dict[str, Any] | None = None
    latest_error: Any = None
    output_items: dict[str, dict[str, Any]] = {}

    def with_collected_output(response: dict[str, Any]) -> dict[str, Any]:
        output = response.get("output") if isinstance(response.get("output"), list) else []
        if output or not output_items:
            return response
        return {**response, "output": list(output_items.values())}

    for event in iterate_server_sent_events(chunks):
        if not event.data:
            continue

        terminal = bool(
            (event.event and event.event in _TERMINAL_EVENT_TYPES)
            or _is_terminal_payload(event.data)
        )

        try:
            parsed = json.loads(event.data)
        except ValueError:
            parsed = None

        if isinstance(parsed, dict):
            if event.event == "error":
                latest_error = parsed
            else:
                item = parsed.get("item")
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    output_items[item["id"]] = item

                response = parsed.get("response")
                if isinstance(response, dict):
                    latest_response = response

                if terminal and latest_response:
                    return with_collected_output(latest_response)

        if terminal and latest_response:
            return with_collected_output(latest_response)

    if latest_response:
        return with_collected_output(latest_response)

    suffix = f" Last error: {json.dumps(latest_error)}" if latest_error is not None else ""
    raise RuntimeError(f"No completed response found in SSE stream.{suffix}")

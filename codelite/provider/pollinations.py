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

"""Anonymous Pollinations chat transport behind Code Lite's Responses interface."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Iterator

POLLINATIONS_URL = "https://text.pollinations.ai/openai"
POLLINATIONS_MODEL = "openai-fast"
POLLINATIONS_REQUEST_INTERVAL_SECONDS = 15.0
USER_AGENT = "CodeLite/0.1 (+https://github.com/)"


class PollinationsError(RuntimeError):
    pass


def _content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("value")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def responses_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})

    pending_calls: list[dict[str, Any]] = []

    def flush_calls() -> None:
        if not pending_calls:
            return
        messages.append({"role": "assistant", "content": None, "tool_calls": list(pending_calls)})
        pending_calls.clear()

    input_items = body.get("input")
    if isinstance(input_items, str):
        input_items = [{"role": "user", "content": input_items}]
    for item in input_items or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            pending_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }
            )
            continue
        flush_calls()
        if item_type == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": str(item.get("output") or ""),
                }
            )
            continue
        role = item.get("role")
        if role in ("user", "assistant", "system", "developer"):
            messages.append({"role": role, "content": _content(item.get("content"))})
    flush_calls()

    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name") or "",
                    "description": tool.get("description") or "",
                    "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                },
            }
        )

    request: dict[str, Any] = {
        "model": POLLINATIONS_MODEL,
        "messages": messages,
        "stream": False,
    }
    if tools:
        request["tools"] = tools
        request["tool_choice"] = "auto"
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        request["reasoning_effort"] = reasoning["effort"]
    return request


def chat_to_responses(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices") or []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
    message = message if isinstance(message, dict) else {}
    output: list[dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        output.append(
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        if not isinstance(function, dict):
            continue
        output.append(
            {
                "id": f"fc_{uuid.uuid4().hex}",
                "type": "function_call",
                "call_id": call.get("id") or f"call_{uuid.uuid4().hex}",
                "name": function.get("name") or "",
                "arguments": function.get("arguments") or "{}",
            }
        )

    usage = payload.get("usage") or {}
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    return {
        "id": payload.get("id") or f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "status": "completed",
        "model": POLLINATIONS_MODEL,
        "output": output,
        "output_text": text if isinstance(text, str) else "",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": usage.get("total_tokens", input_tokens + output_tokens),
        },
    }


def response_as_sse(response: dict[str, Any]) -> Iterator[bytes]:
    for item in response.get("output") or []:
        if item.get("type") == "function_call":
            yield _event({"type": "response.output_item.added", "item": item})
        if item.get("type") == "message":
            for part in item.get("content") or []:
                text = part.get("text") if isinstance(part, dict) else None
                if isinstance(text, str) and text:
                    yield _event({"type": "response.output_text.delta", "delta": text})
        yield _event({"type": "response.output_item.done", "item": item})
    yield _event({"type": "response.completed", "response": response})
    yield b"data: [DONE]\n\n"


def _event(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")


class PollinationsTransport:
    def __init__(
        self,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._opener = opener
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_request: float | None = None

    def _wait_for_slot(self) -> None:
        with self._lock:
            now = self._monotonic()
            if self._last_request is not None:
                delay = POLLINATIONS_REQUEST_INTERVAL_SECONDS - (now - self._last_request)
                if delay > 0:
                    self._sleep(delay)
            self._last_request = self._monotonic()

    def send_responses_request(
        self, body: dict[str, Any], *, stream: bool
    ) -> dict[str, Any] | Iterator[bytes]:
        self._wait_for_slot()
        request = urllib.request.Request(
            POLLINATIONS_URL,
            data=json.dumps(responses_to_chat(body)).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with self._opener(request, timeout=180.0) as upstream:
                raw = upstream.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise PollinationsError(
                f"Pollinations request failed with HTTP {error.code}: {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise PollinationsError(f"Request to Pollinations failed: {error.reason}") from error
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise PollinationsError("Pollinations returned invalid JSON.") from error
        if not isinstance(payload, dict) or payload.get("error"):
            detail = payload.get("error") if isinstance(payload, dict) else "invalid response"
            raise PollinationsError(f"Pollinations request failed: {detail}")
        response = chat_to_responses(payload)
        if stream:
            return response_as_sse(response)
        return response

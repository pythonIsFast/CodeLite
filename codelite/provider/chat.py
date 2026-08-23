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
# The overall shape (translate Chat Completions <-> Responses API, including
# the streaming event handling) follows openai-oauth
# (https://github.com/EvanZhouDev/openai-oauth)
# packages/openai-oauth/src/{chat-completions,chat-messages,chat-stream}.ts,
# Apache-2.0. The reference implementation delegates the actual translation
# to the Vercel AI SDK (`ai` / `@ai-sdk/openai`), a third-party npm
# dependency; since Code Lite is stdlib-only, this module reimplements that
# translation directly against the public OpenAI Responses API request/event
# shapes instead of porting the AI SDK's internals.
#
# UNCERTAINTY: the Responses API streaming event names/fields below
# (`response.output_item.added`, `response.function_call_arguments.delta`,
# `response.output_text.delta`, `response.completed`, ...) are implemented
# from general knowledge of OpenAI's public Responses API docs, not from a
# fixture captured against a live Codex response. Verify against a real
# streaming call before relying on this in production; the non-streaming
# path only depends on the final `response.output` shape (confirmed against
# openai-oauth's own test fixtures) and is on firmer ground.

"""Translates between the OpenAI Chat Completions shape and the Responses API.

Code Lite talks to Codex exclusively through `/responses` (see
:mod:`codelite.provider.transport`). This module lets `/v1/chat/completions`
keep working for OpenAI-client tooling that doesn't speak Responses natively,
by converting requests in and responses (or SSE streams) back out.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

from .sse import iterate_server_sent_events

# -- Chat Completions request -> Responses API request ------------------------


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def _user_content_items(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "input_text", "text": ""}]

    items: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text" and isinstance(part.get("text"), str):
            items.append({"type": "input_text", "text": part["text"]})
        elif part_type == "image_url":
            image_url = part.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else None
            if isinstance(url, str):
                items.append({"type": "input_image", "image_url": url})
    return items or [{"type": "input_text", "text": ""}]


def messages_to_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Chat Completions `messages` into Responses API `input` items."""
    input_items: list[dict[str, Any]] = []
    tool_names_by_call_id: dict[str, str] = {}

    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            input_items.append(
                {
                    "role": "developer" if role == "developer" else "system",
                    "content": [{"type": "input_text", "text": _text_from_content(message.get("content"))}],
                }
            )
        elif role == "user":
            input_items.append({"role": "user", "content": _user_content_items(message.get("content"))})
        elif role == "assistant":
            text = _text_from_content(message.get("content"))
            if text:
                input_items.append(
                    {"role": "assistant", "content": [{"type": "output_text", "text": text}]}
                )
            for tool_call in message.get("tool_calls") or []:
                call_id = tool_call.get("id")
                function = tool_call.get("function") or {}
                name = function.get("name")
                if not isinstance(call_id, str) or not isinstance(name, str):
                    continue
                tool_names_by_call_id[call_id] = name
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": function.get("arguments") or "{}",
                    }
                )
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str):
                continue
            content = message.get("content")
            output = content if isinstance(content, str) else json.dumps(content)
            input_items.append({"type": "function_call_output", "call_id": call_id, "output": output})

    return input_items


def tools_to_responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    converted = []
    for definition in tools:
        if definition.get("type") != "function":
            continue
        function = definition.get("function") or {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": function.get("description"),
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted or None


def tool_choice_to_responses(tool_choice: Any) -> Any:
    if tool_choice in (None, "auto", "none", "required"):
        return tool_choice
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        function = tool_choice.get("function") or {}
        name = function.get("name")
        if isinstance(name, str):
            return {"type": "function", "name": name}
    return "auto"


def chat_request_to_responses_body(request: dict[str, Any]) -> dict[str, Any]:
    """Build a Responses API request body from a Chat Completions request body."""
    body: dict[str, Any] = {
        "model": request.get("model"),
        "input": messages_to_responses_input(request.get("messages") or []),
    }

    tools = tools_to_responses_tools(request.get("tools"))
    if tools:
        body["tools"] = tools
    tool_choice = tool_choice_to_responses(request.get("tool_choice"))
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if request.get("temperature") is not None:
        body["temperature"] = request["temperature"]
    if request.get("top_p") is not None:
        body["top_p"] = request["top_p"]
    if request.get("parallel_tool_calls") is not None:
        body["parallel_tool_calls"] = request["parallel_tool_calls"]
    if request.get("reasoning_effort") is not None:
        body["reasoning"] = {"effort": request["reasoning_effort"]}
    # `stop` (stop sequences) and `max_tokens` have no Responses API
    # equivalent that survives Codex's own body normalization (which drops
    # `max_output_tokens` unconditionally) -- silently not forwarded.
    return body


# -- Responses API response -> Chat Completions response ----------------------


@dataclass
class ChatResult:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)


def parse_responses_output(response: dict[str, Any]) -> ChatResult:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message" and item.get("role") == "assistant":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text_parts.append(part.get("text") or "")
        elif item_type == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id"),
                    "name": item.get("name"),
                    "arguments": item.get("arguments") or "{}",
                }
            )

    finish_reason = "tool_calls" if tool_calls else "stop"
    if not tool_calls and response.get("status") == "incomplete":
        finish_reason = "length"

    usage_raw = response.get("usage") or {}
    usage = {
        "prompt_tokens": usage_raw.get("input_tokens", 0),
        "completion_tokens": usage_raw.get("output_tokens", 0),
        "total_tokens": usage_raw.get(
            "total_tokens", usage_raw.get("input_tokens", 0) + usage_raw.get("output_tokens", 0)
        ),
    }
    cached = (usage_raw.get("input_tokens_details") or {}).get("cached_tokens")
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    reasoning_tokens = (usage_raw.get("output_tokens_details") or {}).get("reasoning_tokens")
    if reasoning_tokens is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}

    return ChatResult(
        text="".join(text_parts), tool_calls=tool_calls, finish_reason=finish_reason, usage=usage
    )


def build_chat_completion_response(model: str, result: ChatResult) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": result.text or None}
    if result.tool_calls:
        message["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
            for call in result.tool_calls
        ]

    return {
        "id": f"chatcmpl_{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": result.finish_reason}],
        "usage": result.usage,
    }


# -- Streaming: Responses SSE -> Chat Completions SSE chunks -------------------


def _sse_chunk(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


def stream_responses_as_chat_completion_chunks(
    model: str, response_chunks: Iterator[bytes]
) -> Iterator[bytes]:
    """Re-encode a raw Responses-API SSE byte stream as Chat Completions SSE chunks."""
    completion_id = f"chatcmpl_{uuid.uuid4().hex}"
    tool_index_by_item_id: dict[str, int] = {}
    next_tool_index = 0

    def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
        return _sse_chunk(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
        )

    yield chunk({"role": "assistant"})

    for event in iterate_server_sent_events(response_chunks):
        if not event.data or event.data == "[DONE]":
            continue
        try:
            payload = json.loads(event.data)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = payload.get("type")

        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                yield chunk({"content": delta})

        elif event_type == "response.output_item.added":
            item = payload.get("item") or {}
            if item.get("type") == "function_call":
                item_id = item.get("id")
                call_id = item.get("call_id") or item_id
                index = next_tool_index
                next_tool_index += 1
                if isinstance(item_id, str):
                    tool_index_by_item_id[item_id] = index
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": call_id,
                                "type": "function",
                                "function": {"name": item.get("name"), "arguments": ""},
                            }
                        ]
                    }
                )

        elif event_type == "response.function_call_arguments.delta":
            item_id = payload.get("item_id")
            index = tool_index_by_item_id.get(item_id) if isinstance(item_id, str) else None
            delta = payload.get("delta")
            if index is not None and isinstance(delta, str):
                yield chunk({"tool_calls": [{"index": index, "function": {"arguments": delta}}]})

        elif event_type in ("response.completed", "response.failed", "response.incomplete"):
            response = payload.get("response") or {}
            result = parse_responses_output(response)
            yield chunk({}, finish_reason=result.finish_reason)
            yield _sse_chunk(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": model,
                    "choices": [],
                    "usage": result.usage,
                }
            )
            break

    yield b"data: [DONE]\n\n"

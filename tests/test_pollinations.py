# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from __future__ import annotations

import json

from codelite.provider.pollinations import (
    POLLINATIONS_MODEL,
    PollinationsTransport,
    chat_to_responses,
    responses_to_chat,
)
from codelite.provider.sse import iterate_server_sent_events


def test_responses_request_becomes_an_openai_chat_request() -> None:
    request = responses_to_chat(
        {
            "model": "ignored",
            "instructions": "Be precise.",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "Read this."}]},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": '{"path":"README.md"}',
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "contents"},
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "read_file",
                    "description": "Read a file.",
                    "parameters": {"type": "object"},
                }
            ],
        }
    )

    assert request["model"] == POLLINATIONS_MODEL
    assert request["messages"][0] == {"role": "system", "content": "Be precise."}
    assert request["messages"][1] == {"role": "user", "content": "Read this."}
    assert request["messages"][2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert request["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "contents",
    }
    assert request["tools"][0]["function"]["name"] == "read_file"


def test_chat_response_becomes_responses_output() -> None:
    response = chat_to_responses(
        {
            "id": "chat_1",
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "I will read it.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                            }
                        ],
                    }
                }
            ],
        }
    )

    assert response["id"] == "chat_1"
    assert response["usage"] == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
    assert response["output"][0]["content"][0]["text"] == "I will read it."
    assert response["output"][1] == {
        "id": response["output"][1]["id"],
        "type": "function_call",
        "call_id": "call_1",
        "name": "read_file",
        "arguments": '{"path":"README.md"}',
    }


class _Response:
    def __init__(self, body: dict[str, object]) -> None:
        self._body = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_transport_spaces_requests_and_emits_responses_sse() -> None:
    now = [100.0]
    sleeps: list[float] = []

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    def opener(_request: object, timeout: float) -> _Response:
        assert timeout == 180.0
        return _Response(
            {
                "choices": [{"message": {"role": "assistant", "content": "OK", "tool_calls": []}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            }
        )

    transport = PollinationsTransport(opener=opener, monotonic=lambda: now[0], sleep=sleep)
    first = transport.send_responses_request({"input": "hello"}, stream=True)
    assert not isinstance(first, dict)
    events = list(iterate_server_sent_events(first))
    assert any(json.loads(event.data).get("type") == "response.completed" for event in events if event.data != "[DONE]")

    transport.send_responses_request({"input": "again"}, stream=False)
    assert sleeps == [15.0]
